"""
Perplexity Sonar API 封裝 — 讀取 URL 並產出結構化候選人分析

模型: sonar (最便宜 ~$0.006/candidate)
功能: 直接讀取 LinkedIn URL → 回傳結構化 JSON
"""

import json
import logging
import time
import ssl
from datetime import datetime
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

# Perplexity 定價 (per 1M tokens)
PRICING = {
    'sonar': {'input': 1.0, 'output': 1.0, 'request': 0.005},
    'sonar-pro': {'input': 3.0, 'output': 15.0, 'request': 0.006},
    'sonar-reasoning-pro': {'input': 2.0, 'output': 8.0, 'request': 0.006},
}


class PerplexityClient:
    """Perplexity Sonar API — 讀取 URL 並產出結構化候選人分析"""

    BASE_URL = "https://api.perplexity.ai/chat/completions"

    def __init__(self, api_key: str, config: dict = None):
        self.api_key = api_key
        self.config = config or {}
        self.model = self.config.get('model', 'sonar')
        self.timeout = self.config.get('timeout', 30)
        self.max_retries = self.config.get('max_retries', 2)

        # 使用量追蹤
        self._usage = {
            'calls': 0,
            'input_tokens': 0,
            'output_tokens': 0,
            'estimated_cost': 0.0,
            'session_start': datetime.now().isoformat(),
        }

    def analyze_profile(self, linkedin_url: str, prompt: str,
                        model_override: str = None,
                        system_prompt: str = None) -> dict:
        """
        呼叫 Perplexity Sonar 分析 LinkedIn URL

        Args:
            linkedin_url: LinkedIn 個人頁面 URL
            prompt: 分析指令 prompt（已填入 URL）

        Returns:
            dict: 解析後的 JSON 回應
            失敗時回傳 {'error': '錯誤訊息', 'success': False}
        """
        if not self.api_key:
            return {'error': 'Perplexity API key 未設定', 'success': False}

        # v4: 支援 model 覆蓋（P1: scoring 用 sonar 省成本）
        active_model = model_override or self.model

        # v4: 支援自訂 system prompt（P3: 拆分 job context 到 system）
        sys_content = system_prompt or '你是專業獵頭顧問 AI。請根據指令分析候選人資訊，以嚴格 JSON 格式回傳。不要包含任何 markdown 標記或額外文字。'

        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }

        payload = {
            'model': active_model,
            'messages': [
                {
                    'role': 'system',
                    'content': sys_content
                },
                {
                    'role': 'user',
                    'content': prompt,
                }
            ],
            'temperature': 0.1,  # 低溫度確保穩定輸出
            'max_tokens': 2000,
        }

        # 注意: Perplexity API 已不支援 response_format: json_object
        # 改為依賴 system prompt 指令 + _parse_json_response() 解析

        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                logger.info(f"Perplexity API 呼叫 (嘗試 {attempt + 1}/{self.max_retries + 1}, model={active_model}): {linkedin_url}")

                response = requests.post(
                    self.BASE_URL,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )

                if response.status_code == 429:
                    wait_time = min(2 ** attempt * 2, 30)
                    logger.warning(f"Perplexity 429 Rate Limit，等待 {wait_time}s")
                    time.sleep(wait_time)
                    continue

                if response.status_code != 200:
                    error_msg = f"Perplexity API 錯誤 {response.status_code}: {response.text[:200]}"
                    logger.error(error_msg)
                    last_error = error_msg
                    continue

                data = response.json()

                # 追蹤使用量
                usage = data.get('usage', {})
                self._track_usage(usage, model_used=active_model)

                # 提取回應文字
                content = data.get('choices', [{}])[0].get('message', {}).get('content', '')

                if not content:
                    last_error = 'Perplexity 回傳空內容'
                    continue

                # 解析 JSON
                result = self._parse_json_response(content)
                if result:
                    result['success'] = True
                    result['_usage'] = {
                        'input_tokens': usage.get('prompt_tokens', 0),
                        'output_tokens': usage.get('completion_tokens', 0),
                        'model': active_model,
                        'cost': self._estimate_cost(usage, model_used=active_model),
                    }
                    return result
                else:
                    last_error = f'JSON 解析失敗: {content[:200]}'
                    continue

            except requests.exceptions.Timeout:
                last_error = f'Perplexity API 超時 ({self.timeout}s)'
                logger.warning(last_error)
            except requests.exceptions.ConnectionError as e:
                last_error = f'Perplexity API 連線失敗: {e}'
                logger.warning(last_error)
            except Exception as e:
                last_error = f'Perplexity API 意外錯誤: {e}'
                logger.error(last_error, exc_info=True)

            if attempt < self.max_retries:
                time.sleep(1)

        return {'error': last_error or '所有重試都失敗', 'success': False}

    def score_candidate(self, candidate_profile: str, job_context: str, prompt: str) -> dict:
        """
        用 Perplexity 做綜合職缺匹配評分

        Args:
            candidate_profile: 已充實的候選人資料文字
            job_context: 職缺資訊文字
            prompt: 評分 prompt（已填入候選人和職缺資料）

        Returns:
            dict: ai_match_result 格式的結果
        """
        return self.analyze_profile('', prompt)

    def _parse_json_response(self, content: str) -> Optional[dict]:
        """
        嘗試從回應中提取 JSON

        支援:
        - 純 JSON
        - ```json ... ``` 包裹
        - 混合文字中的 JSON
        """
        content = content.strip()

        # 1. 直接嘗試解析
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # 2. 提取 ```json ... ``` 區塊
        if '```json' in content:
            start = content.find('```json') + 7
            end = content.find('```', start)
            if end > start:
                try:
                    return json.loads(content[start:end].strip())
                except json.JSONDecodeError:
                    pass

        # 3. 提取 ``` ... ``` 區塊
        if '```' in content:
            start = content.find('```') + 3
            # 跳過語言標記行
            newline = content.find('\n', start)
            if newline > 0:
                start = newline + 1
            end = content.find('```', start)
            if end > start:
                try:
                    return json.loads(content[start:end].strip())
                except json.JSONDecodeError:
                    pass

        # 4. 找最外層的 { ... }
        brace_start = content.find('{')
        brace_end = content.rfind('}')
        if brace_start >= 0 and brace_end > brace_start:
            try:
                return json.loads(content[brace_start:brace_end + 1])
            except json.JSONDecodeError:
                pass

        logger.warning(f"無法從 Perplexity 回應中提取 JSON: {content[:200]}")
        return None

    def _track_usage(self, usage: dict, model_used: str = None):
        """追蹤累計使用量"""
        self._usage['calls'] += 1
        self._usage['input_tokens'] += usage.get('prompt_tokens', 0)
        self._usage['output_tokens'] += usage.get('completion_tokens', 0)
        self._usage['estimated_cost'] += self._estimate_cost(usage, model_used)

    def _estimate_cost(self, usage: dict, model_used: str = None) -> float:
        """估算單次呼叫費用（v4: 支援指定模型計價）"""
        model = model_used or self.model
        pricing = PRICING.get(model, PRICING['sonar'])
        input_tokens = usage.get('prompt_tokens', 0)
        output_tokens = usage.get('completion_tokens', 0)

        cost = pricing['request']  # 基本請求費
        cost += (input_tokens / 1_000_000) * pricing['input']
        cost += (output_tokens / 1_000_000) * pricing['output']
        return round(cost, 6)

    def get_usage(self) -> dict:
        """回傳使用統計"""
        return {
            **self._usage,
            'model': self.model,
            'pricing': PRICING.get(self.model, {}),
        }

    def is_available(self) -> bool:
        """檢查 API key 是否已設定"""
        return bool(self.api_key and self.api_key.strip())
