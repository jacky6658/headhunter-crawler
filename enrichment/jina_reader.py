"""
Jina Reader 免費 API 封裝 — URL 轉純文字

免費方案: 100 RPM, 無需 API key
用途: 作為 Perplexity 的備援，讀取 LinkedIn 頁面轉為 markdown
"""

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class JinaReader:
    """Jina Reader — 免費讀取 URL 轉為 markdown 純文字"""

    BASE_URL = "https://r.jina.ai/"

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.rate_limit_rpm = self.config.get('rate_limit_rpm', 60)
        self.timeout = self.config.get('timeout', 20)
        self._last_call_time = 0
        self._call_count = 0

    def fetch_profile(self, url: str) -> dict:
        """
        讀取 URL 並轉為 markdown 純文字

        Args:
            url: 要讀取的網頁 URL (例如 LinkedIn 個人頁面)

        Returns:
            dict: {
                'success': bool,
                'content': str (markdown 文字),
                'title': str,
                'error': str (如果失敗),
            }
        """
        if not url:
            return {'success': False, 'content': '', 'title': '', 'error': 'URL 為空'}

        # Rate limiting
        self._enforce_rate_limit()

        jina_url = f"{self.BASE_URL}{url}"

        headers = {
            'Accept': 'text/markdown',
            'X-No-Cache': 'true',
        }

        try:
            logger.info(f"Jina Reader 讀取: {url}")

            response = requests.get(
                jina_url,
                headers=headers,
                timeout=self.timeout,
            )

            self._call_count += 1
            self._last_call_time = time.time()

            if response.status_code == 429:
                logger.warning("Jina Reader 429 Rate Limit")
                return {
                    'success': False,
                    'content': '',
                    'title': '',
                    'error': 'Jina Reader rate limit (429)，請稍後重試',
                }

            if response.status_code != 200:
                error = f"Jina Reader 錯誤 {response.status_code}: {response.text[:200]}"
                logger.warning(error)
                return {
                    'success': False,
                    'content': '',
                    'title': '',
                    'error': error,
                }

            content = response.text
            if not content or len(content.strip()) < 20:
                return {
                    'success': False,
                    'content': '',
                    'title': '',
                    'error': 'Jina Reader 回傳內容太短或為空',
                }

            # 提取標題 (markdown 第一行通常是 # Title)
            title = ''
            lines = content.strip().split('\n')
            for line in lines:
                if line.startswith('# '):
                    title = line[2:].strip()
                    break

            return {
                'success': True,
                'content': content[:5000],  # 限制長度避免過多 token
                'title': title,
                'error': '',
            }

        except requests.exceptions.Timeout:
            error = f'Jina Reader 超時 ({self.timeout}s)'
            logger.warning(error)
            return {'success': False, 'content': '', 'title': '', 'error': error}

        except requests.exceptions.ConnectionError as e:
            error = f'Jina Reader 連線失敗: {e}'
            logger.warning(error)
            return {'success': False, 'content': '', 'title': '', 'error': error}

        except Exception as e:
            error = f'Jina Reader 意外錯誤: {e}'
            logger.error(error, exc_info=True)
            return {'success': False, 'content': '', 'title': '', 'error': error}

    def _enforce_rate_limit(self):
        """確保不超過 RPM 限制"""
        if self._last_call_time > 0:
            min_interval = 60.0 / self.rate_limit_rpm
            elapsed = time.time() - self._last_call_time
            if elapsed < min_interval:
                sleep_time = min_interval - elapsed
                logger.debug(f"Jina rate limit: 等待 {sleep_time:.1f}s")
                time.sleep(sleep_time)

    def is_available(self) -> bool:
        """Jina Reader 永遠可用（免費，無需 key）"""
        return True

    def get_stats(self) -> dict:
        """回傳使用統計"""
        return {
            'calls': self._call_count,
            'rate_limit_rpm': self.rate_limit_rpm,
            'cost': 0.0,  # 完全免費
        }
