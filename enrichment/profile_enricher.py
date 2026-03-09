"""
ProfileEnricher — 候選人 LinkedIn 深度分析統一入口

流程: LinkedIn API (免費優先) → Perplexity (付費備援) → Jina Reader (免費備援) → 返回原始資料 (都失敗)
輸出: 完整 Step1ne 候選人卡片欄位 (work_history, education_details, years_experience 等)
"""

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Callable

from .perplexity_client import PerplexityClient
from .jina_reader import JinaReader
from .prompts import PROFILE_ANALYSIS_PROMPT, JINA_TEXT_PARSE_PROMPT, NAME_SEARCH_PROMPT

logger = logging.getLogger(__name__)


class ProfileEnricher:
    """候選人 LinkedIn 深度分析 — LinkedIn API 優先、Perplexity 備援、Jina 降級"""

    def __init__(self, config: dict):
        """
        Args:
            config: enrichment 設定區塊 (from default.yaml)
        """
        self.config = config
        perplexity_key = config.get('perplexity', {}).get('api_key', '')
        perplexity_config = config.get('perplexity', {})

        # LinkedIn API client（Priority 1 — 免費、最完整）
        linkedin_config = config.get('linkedin', {})
        self.linkedin_api = None
        if linkedin_config.get('enabled', False):
            try:
                from .linkedin_client import LinkedInApiClient
                self.linkedin_api = LinkedInApiClient(linkedin_config)
                logger.info("LinkedIn API client 已初始化")
            except Exception as e:
                logger.warning(f"LinkedIn API client 初始化失敗: {e}")

        # Perplexity（Priority 2 — 付費、AI 分析）
        self.perplexity = PerplexityClient(perplexity_key, perplexity_config) if perplexity_key else None

        # Jina Reader（Priority 3 — 免費備援）
        self.jina = JinaReader(config.get('jina', {})) if config.get('jina', {}).get('enabled', True) else None

        self.batch_concurrency = config.get('batch', {}).get('concurrency', 3)
        self.batch_delay = config.get('batch', {}).get('delay_between', 1.0)

        # 可設定的 provider 順序
        self.provider_priority = config.get(
            'provider_priority', ['linkedin', 'perplexity', 'jina']
        )

        # v4 P2: Enrichment 快取（跨職缺不重複 enrich 同一人）
        cache_cfg = config.get('cache', {})
        self._cache_file = cache_cfg.get('file', 'data/enrichment_cache.json')
        self._cache_ttl_days = cache_cfg.get('ttl_days', 7)
        self._enrichment_cache = self._load_cache()
        self._cache_lock = threading.Lock()

        # 統計
        self._stats = {
            'total_calls': 0,
            'linkedin_calls': 0,
            'perplexity_calls': 0,
            'jina_calls': 0,
            'cache_hits': 0,
            'success': 0,
            'failed': 0,
            'start_time': datetime.now().isoformat(),
        }

    def enrich_candidate(self, candidate: dict, force: bool = False) -> dict:
        """
        分析一位候選人的專業背景

        支援兩種模式：
        1. 有 LinkedIn URL → 用 LinkedIn API / Perplexity / Jina 分析
        2. 無 LinkedIn URL 但有 name+company → 用 Perplexity 搜尋姓名

        Args:
            candidate: 候選人 dict (需含 linkedin_url 或 name)
            force: 強制重新分析（忽略快取）

        Returns:
            dict: 充實後的候選人資料
        """
        self._stats['total_calls'] += 1
        linkedin_url = candidate.get('linkedin_url', '')
        name = candidate.get('name', '').strip()

        # 決定 cache key：有 LinkedIn URL 用 URL，否則用 name
        cache_key = linkedin_url or (f"name:{name}" if name else '')

        if not linkedin_url and not name:
            logger.warning(f"候選人無 LinkedIn URL 也無姓名，跳過深度分析")
            return self._build_empty_result(candidate, '無 LinkedIn URL 且無姓名')

        # 檢查快取（force=True 時跳過）
        if not force and cache_key:
            cached = self._get_cached(cache_key)
            if cached:
                self._stats['cache_hits'] = self._stats.get('cache_hits', 0) + 1
                logger.info(f"enrichment cache hit: {name or '?'} ({cache_key[:50]}...)")
                return cached

        # ── 模式 1: 有 LinkedIn URL → 原始流程 ──
        linkedin_result_empty = False  # 追蹤 LinkedIn 是否回傳空資料
        if linkedin_url:
            for provider in self.provider_priority:
                result = None

                if provider == 'linkedin' and self.linkedin_api and self.linkedin_api.is_available():
                    result = self._enrich_via_linkedin_api(linkedin_url, candidate)
                    if result and result.get('success'):
                        if self._is_result_meaningful(result):
                            self._stats['linkedin_calls'] += 1
                            self._stats['success'] += 1
                            self._set_cached(cache_key, result)
                            return result
                        else:
                            linkedin_result_empty = True
                            logger.info(f"LinkedIn API 回傳空資料: {name}，繼續嘗試其他來源")

                elif provider == 'perplexity' and self.perplexity and self.perplexity.is_available():
                    result = self._enrich_via_perplexity(linkedin_url, candidate)
                    if result and result.get('success'):
                        if self._is_result_meaningful(result):
                            self._stats['perplexity_calls'] += 1
                            self._stats['success'] += 1
                            self._set_cached(cache_key, result)
                            return result
                        else:
                            linkedin_result_empty = True
                            logger.info(f"Perplexity LinkedIn 分析回傳空資料: {name}，繼續嘗試姓名搜尋")

                elif provider == 'jina' and self.jina and self.jina.is_available():
                    result = self._enrich_via_jina(linkedin_url, candidate)
                    if result and result.get('success'):
                        if self._is_result_meaningful(result):
                            self._stats['jina_calls'] += 1
                            self._stats['success'] += 1
                            self._set_cached(cache_key, result)
                            return result
                        else:
                            linkedin_result_empty = True

        # ── 模式 2: 用姓名搜尋（無 LinkedIn URL、或 LinkedIn 分析結果空白時的備援）──
        if name and self.perplexity and self.perplexity.is_available():
            reason = '無 LinkedIn URL' if not linkedin_url else 'LinkedIn 分析結果不足，可能是隱私設定'
            logger.info(f"候選人 {name} {reason}，嘗試用姓名搜尋")
            result = self._enrich_via_name_search(candidate)
            if result and result.get('success') and self._is_result_meaningful(result):
                # 如果是 LinkedIn 空資料後 fallback 成功，加上備註
                if linkedin_result_empty:
                    result['enrichment_notes'] = (
                        f"⚠️ LinkedIn 不公開，改用姓名搜尋補充資料\n"
                        + result.get('enrichment_notes', '')
                    )
                self._stats['perplexity_calls'] += 1
                self._stats['success'] += 1
                self._set_cached(cache_key, result)
                return result

        # 所有方法都失敗 — 標註原因
        self._stats['failed'] += 1
        if linkedin_result_empty:
            reason = '⚠️ LinkedIn 不公開 — 此候選人的 LinkedIn 資料受隱私設定保護，無法取得工作經歷與教育背景'
            logger.warning(f"候選人 {name or '?'} LinkedIn 不公開，所有搜尋都無法取得完整資料")
        else:
            reason = '所有分析來源都失敗'
            logger.warning(f"候選人 {name or '?'} 深度分析全部失敗")
        return self._build_empty_result(candidate, reason)

    def enrich_batch(self, candidates: list, on_progress: Callable = None) -> list:
        """
        批量分析候選人（並發）

        Args:
            candidates: 候選人列表
            on_progress: 進度回調 (completed, total, current_name)

        Returns:
            list: 充實後的候選人列表 (與輸入順序相同)
        """
        total = len(candidates)
        if total == 0:
            return []

        results = [None] * total
        completed = 0

        logger.info(f"開始批量深度分析: {total} 位候選人, 並發 {self.batch_concurrency}")

        with ThreadPoolExecutor(max_workers=self.batch_concurrency) as executor:
            # 提交所有任務
            future_to_idx = {}
            for idx, candidate in enumerate(candidates):
                future = executor.submit(self._enrich_with_delay, candidate, idx)
                future_to_idx[future] = idx

            # 收集結果
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception as e:
                    logger.error(f"批量分析第 {idx} 位失敗: {e}")
                    results[idx] = self._build_empty_result(
                        candidates[idx], f'批量分析錯誤: {e}'
                    )

                completed += 1
                if on_progress:
                    name = candidates[idx].get('name', '?')
                    on_progress(completed, total, name)

        # v4 P2: 批量完成後持久化快取
        self._save_cache()

        logger.info(f"批量深度分析完成: {total} 位, "
                     f"成功 {self._stats['success']}, 失敗 {self._stats['failed']}")
        return results

    def _enrich_with_delay(self, candidate: dict, idx: int) -> dict:
        """帶延遲的單人分析（用於批量處理）"""
        if idx > 0:
            time.sleep(self.batch_delay * idx * 0.3)  # 錯開請求時間
        return self.enrich_candidate(candidate)

    def _enrich_via_linkedin_api(self, linkedin_url: str, candidate: dict) -> Optional[dict]:
        """使用 linkedin-api 直接取得結構化 LinkedIn 資料（免費）"""
        try:
            raw = self.linkedin_api.fetch_profile(linkedin_url)

            if not raw or not raw.get('success'):
                logger.warning(f"LinkedIn API 分析失敗: {raw.get('error', '未知錯誤')}")
                return None

            return self._normalize_enrichment(raw, candidate, 'linkedin_api')

        except Exception as e:
            logger.error(f"LinkedIn API 分析異常: {e}", exc_info=True)
            return None

    def _enrich_via_perplexity(self, linkedin_url: str, candidate: dict) -> Optional[dict]:
        """用 Perplexity 分析 LinkedIn 頁面"""
        try:
            prompt = PROFILE_ANALYSIS_PROMPT.format(url=linkedin_url)
            raw = self.perplexity.analyze_profile(linkedin_url, prompt)

            if not raw or not raw.get('success'):
                logger.warning(f"Perplexity 分析失敗: {raw.get('error', '未知錯誤')}")
                return None

            return self._normalize_enrichment(raw, candidate, 'perplexity')

        except Exception as e:
            logger.error(f"Perplexity 分析異常: {e}", exc_info=True)
            return None

    def _enrich_via_name_search(self, candidate: dict) -> Optional[dict]:
        """用 Perplexity 搜尋候選人姓名+公司，取得背景資料（無 LinkedIn URL 時使用）"""
        try:
            name = candidate.get('name', '').strip()
            company = candidate.get('company', '') or candidate.get('organization', '') or ''
            title = candidate.get('title', '') or candidate.get('current_position', '') or ''
            location = candidate.get('location', '') or ''
            github_url = candidate.get('github_url', '') or candidate.get('github', '') or ''
            skills = candidate.get('skills', '')
            if isinstance(skills, list):
                skills = ', '.join(skills)

            prompt = NAME_SEARCH_PROMPT.format(
                name=name,
                company=company,
                title=title,
                location=location,
                github_url=github_url,
                skills=skills or '未知',
            )

            raw = self.perplexity.analyze_profile('', prompt)

            if not raw or not raw.get('success'):
                logger.warning(f"姓名搜尋分析失敗 ({name}): {raw.get('error', '未知錯誤')}")
                return None

            return self._normalize_enrichment(raw, candidate, 'name_search')

        except Exception as e:
            logger.error(f"姓名搜尋分析異常 ({candidate.get('name', '?')}): {e}", exc_info=True)
            return None

    def _enrich_via_jina(self, linkedin_url: str, candidate: dict) -> Optional[dict]:
        """用 Jina Reader 讀取頁面 + Perplexity 解析文字"""
        try:
            # Step 1: Jina 讀取頁面文字
            jina_result = self.jina.fetch_profile(linkedin_url)
            if not jina_result.get('success'):
                logger.warning(f"Jina Reader 失敗: {jina_result.get('error')}")
                return None

            raw_text = jina_result['content']

            # Step 2: 如果有 Perplexity，用它解析純文字
            if self.perplexity and self.perplexity.is_available():
                parse_prompt = JINA_TEXT_PARSE_PROMPT.format(raw_text=raw_text)
                raw = self.perplexity.analyze_profile('', parse_prompt)
                if raw and raw.get('success'):
                    return self._normalize_enrichment(raw, candidate, 'jina+perplexity')

            # Step 3: 沒有 Perplexity，用簡單解析
            return self._simple_parse(raw_text, candidate)

        except Exception as e:
            logger.error(f"Jina 分析異常: {e}", exc_info=True)
            return None

    def _normalize_enrichment(self, raw: dict, candidate: dict, source: str) -> dict:
        """
        將 Perplexity 回傳的分析結果正規化為 Step1ne 候選人欄位格式

        Args:
            raw: Perplexity API 回傳的 JSON
            candidate: 原始候選人資料
            source: 分析來源標記

        Returns:
            dict: 正規化後的候選人充實資料
        """
        # 提取穩定性指標
        stability = raw.get('stability_indicators', {})
        avg_tenure = stability.get('avg_tenure_months', 0)
        job_changes = stability.get('job_changes', 0)
        recent_gap = stability.get('recent_gap_months', 0)

        # 計算穩定性分數 (0-100)
        stability_score = self._calc_stability_score(avg_tenure, job_changes, recent_gap)

        # 提取技能
        skills = raw.get('skills', [])
        if isinstance(skills, list):
            skills_str = '、'.join(skills)
        else:
            skills_str = str(skills)

        # 提取學歷
        edu_details = raw.get('education_details', [])
        education_level = raw.get('education_level', '')
        if edu_details and not education_level:
            # 從 education_details 推導最高學歷
            for edu in edu_details:
                degree = edu.get('degree', '')
                if any(k in degree for k in ['博士', 'PhD', 'Ph.D']):
                    education_level = '博士'
                    break
                elif any(k in degree for k in ['碩士', 'Master', 'MS', 'MA', 'MBA']):
                    education_level = '碩士'
                elif any(k in degree for k in ['學士', 'Bachelor', 'BS', 'BA']):
                    if not education_level:
                        education_level = '大學'

        # 組合 enrichment 備註
        notes_parts = []
        if raw.get('summary'):
            notes_parts.append(f"AI 摘要: {raw['summary']}")
        if raw.get('languages'):
            langs = raw['languages'] if isinstance(raw['languages'], list) else [raw['languages']]
            notes_parts.append(f"語言: {', '.join(langs)}")
        if raw.get('certifications'):
            certs = raw['certifications'] if isinstance(raw['certifications'], list) else [raw['certifications']]
            if certs and certs[0]:  # 非空
                notes_parts.append(f"證照: {', '.join(certs)}")

        usage = raw.get('_usage', {})
        cost_str = f"${usage.get('cost', 0):.4f}" if usage.get('cost') else '免費'
        notes_parts.append(f"分析來源: {source} | 費用: {cost_str} | {datetime.now().strftime('%Y-%m-%d %H:%M')}")

        # 聯繫資訊（linkedin-api 額外取得）
        contact_info = raw.get('contact_info', {})

        return {
            'success': True,
            # 基本資料（覆蓋/補充）
            'name': raw.get('name') or candidate.get('name', ''),
            'current_position': raw.get('current_position') or candidate.get('title', ''),
            'company': raw.get('company', ''),
            'location': raw.get('location') or candidate.get('location', ''),
            'years_experience': str(raw.get('years_experience', '') or ''),
            'skills': skills_str or candidate.get('skills', ''),
            'education': education_level,

            # 深度資料 (JSONB)
            'work_history': raw.get('work_history', []),
            'education_details': edu_details,

            # 穩定性指標
            'stability_score': str(stability_score),
            'job_changes': str(job_changes),
            'avg_tenure_months': str(avg_tenure),
            'recent_gap_months': str(recent_gap),

            # 聯繫資訊（bonus — linkedin-api 提供）
            'contact_email': contact_info.get('email', ''),
            'contact_phone': ', '.join(contact_info.get('phone_numbers', [])) if contact_info.get('phone_numbers') else '',
            'contact_websites': contact_info.get('websites', []),

            # 備註
            'enrichment_notes': '\n'.join(notes_parts),

            # 元資料
            '_enrichment_source': source,
            '_enrichment_raw': raw,
            '_enrichment_time': datetime.now().isoformat(),
        }

    def _simple_parse(self, raw_text: str, candidate: dict) -> Optional[dict]:
        """
        簡單文字解析（Jina 純文字，無 Perplexity 時的降級處理）
        從 markdown 文字中提取基本資訊
        """
        if not raw_text or len(raw_text) < 30:
            return None

        # 基本提取
        lines = raw_text.strip().split('\n')
        name = ''
        headline = ''
        skills = []

        for line in lines[:50]:  # 只看前 50 行
            line = line.strip()
            if line.startswith('# ') and not name:
                name = line[2:].strip()
            elif line.startswith('## ') and not headline:
                headline = line[3:].strip()

        return {
            'success': True,
            'name': name or candidate.get('name', ''),
            'current_position': headline or candidate.get('title', ''),
            'company': '',
            'location': candidate.get('location', ''),
            'years_experience': '',
            'skills': candidate.get('skills', ''),
            'education': '',
            'work_history': [],
            'education_details': [],
            'stability_score': '',
            'job_changes': '',
            'avg_tenure_months': '',
            'recent_gap_months': '',
            'enrichment_notes': f'Jina 純文字解析（降級模式）| {datetime.now().strftime("%Y-%m-%d %H:%M")}',
            '_enrichment_source': 'jina_simple',
            '_enrichment_raw': {'raw_text': raw_text[:2000]},
            '_enrichment_time': datetime.now().isoformat(),
        }

    def _build_empty_result(self, candidate: dict, reason: str) -> dict:
        """建立空結果（分析失敗時使用）"""
        return {
            'success': False,
            'name': candidate.get('name', ''),
            'current_position': candidate.get('title', ''),
            'company': candidate.get('company', ''),
            'location': candidate.get('location', ''),
            'years_experience': '',
            'skills': candidate.get('skills', ''),
            'education': '',
            'work_history': [],
            'education_details': [],
            'stability_score': '',
            'job_changes': '',
            'avg_tenure_months': '',
            'recent_gap_months': '',
            'enrichment_notes': f'深度分析失敗: {reason}',
            '_enrichment_source': 'failed',
            '_enrichment_raw': {},
            '_enrichment_time': datetime.now().isoformat(),
        }

    @staticmethod
    def _calc_stability_score(avg_tenure: int, job_changes: int, recent_gap: int) -> int:
        """
        計算穩定性分數 (0-100)

        規則:
        - 平均任期 >= 36 月: 高穩定 (+40)
        - 平均任期 24-36 月: 中等 (+25)
        - 平均任期 < 24 月: 低 (+10)
        - 換工作 <= 3 次: +30
        - 換工作 4-6 次: +15
        - 換工作 > 6 次: +5
        - 最近無待業: +30
        - 待業 < 6 月: +15
        - 待業 >= 6 月: +5
        """
        score = 0

        # 平均任期
        if avg_tenure >= 36:
            score += 40
        elif avg_tenure >= 24:
            score += 25
        elif avg_tenure > 0:
            score += 10

        # 換工作次數
        if job_changes <= 3:
            score += 30
        elif job_changes <= 6:
            score += 15
        else:
            score += 5

        # 最近待業
        if recent_gap == 0:
            score += 30
        elif recent_gap < 6:
            score += 15
        else:
            score += 5

        return min(100, score)

    def clear_stale_cache(self) -> dict:
        """清除快取中的空結果 / 失敗結果，強制下次重新 enrich"""
        cleared = 0
        kept = 0
        with self._cache_lock:
            keys_to_remove = []
            for key, entry in self._enrichment_cache.items():
                result = entry.get('result', {})
                # 移除失敗的
                if not result.get('success'):
                    keys_to_remove.append(key)
                    continue
                # 移除空結果（無 work_history 且無 education_details 且無 skills）
                has_work = bool(result.get('work_history'))
                has_edu = bool(result.get('education_details'))
                has_skills = bool(result.get('skills'))
                if not has_work and not has_edu and not has_skills:
                    keys_to_remove.append(key)
                    continue
                kept += 1

            for key in keys_to_remove:
                self._enrichment_cache.pop(key, None)
                cleared += 1

        if cleared > 0:
            self._save_cache()

        logger.info(f"enrichment cache 清理: 移除 {cleared} 筆空/失敗結果, 保留 {kept} 筆有效")
        return {'cleared': cleared, 'kept': kept}

    def get_stats(self) -> dict:
        """回傳使用統計"""
        stats = {**self._stats}
        stats['enrichment_cache_size'] = len(self._enrichment_cache)
        if self.linkedin_api:
            stats['linkedin_api_usage'] = self.linkedin_api.get_stats()
        if self.perplexity:
            stats['perplexity_usage'] = self.perplexity.get_usage()
        if self.jina:
            stats['jina_usage'] = self.jina.get_stats()
        return stats

    # ── v4 P2: Enrichment 快取方法 ──────────────────────────────

    def _load_cache(self) -> dict:
        """從磁碟載入 enrichment 快取"""
        if not os.path.exists(self._cache_file):
            logger.info(f"enrichment cache 檔案不存在，建立空快取: {self._cache_file}")
            return {}
        try:
            with open(self._cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            logger.info(f"enrichment cache 載入: {len(data)} 筆快取")
            return data
        except Exception as e:
            logger.warning(f"enrichment cache 載入失敗: {e}")
            return {}

    def _save_cache(self):
        """持久化 enrichment 快取到磁碟"""
        with self._cache_lock:
            try:
                os.makedirs(os.path.dirname(self._cache_file) or '.', exist_ok=True)
                with open(self._cache_file, 'w', encoding='utf-8') as f:
                    json.dump(self._enrichment_cache, f, ensure_ascii=False, indent=None)
                logger.info(f"enrichment cache 已儲存: {len(self._enrichment_cache)} 筆")
            except Exception as e:
                logger.error(f"enrichment cache 儲存失敗: {e}")

    def _get_cached(self, cache_key: str) -> Optional[dict]:
        """查詢快取（含 TTL 檢查 + 空結果過濾）"""
        if not cache_key:
            return None
        entry = self._enrichment_cache.get(cache_key)
        if not entry:
            return None
        try:
            cached_at = datetime.fromisoformat(entry.get('cached_at', ''))
            age_days = (datetime.now() - cached_at).days
            if age_days > self._cache_ttl_days:
                logger.info(f"enrichment cache 過期: {cache_key[:50]}... ({age_days} days old)")
                with self._cache_lock:
                    self._enrichment_cache.pop(cache_key, None)
                return None
        except (ValueError, TypeError):
            with self._cache_lock:
                self._enrichment_cache.pop(cache_key, None)
            return None

        result = entry.get('result')

        # 過濾「假成功」的空結果：標記為成功但 work_history 和 education_details 都為空
        if result and result.get('success'):
            has_work = bool(result.get('work_history'))
            has_edu = bool(result.get('education_details'))
            has_skills = bool(result.get('skills'))
            if not has_work and not has_edu and not has_skills:
                logger.info(f"enrichment cache 空結果淘汰: {cache_key[:50]}... (無工作/學歷/技能)")
                with self._cache_lock:
                    self._enrichment_cache.pop(cache_key, None)
                return None

        return result

    @staticmethod
    def _is_result_meaningful(result: dict) -> bool:
        """
        檢查 enrichment 結果是否有實質資料（非空殼）

        判斷條件（至少滿足一項）：
        - work_history 有至少一筆記錄
        - education_details 有至少一筆記錄
        - skills 非空字串
        """
        if not result or not isinstance(result, dict):
            return False

        has_work = bool(result.get('work_history'))
        has_edu = bool(result.get('education_details'))

        # skills 可能是字串或 list
        skills = result.get('skills', '')
        if isinstance(skills, list):
            has_skills = len(skills) > 0
        else:
            has_skills = bool(str(skills).strip())

        return has_work or has_edu or has_skills

    def _set_cached(self, cache_key: str, result: dict):
        """寫入快取"""
        if not cache_key:
            return
        with self._cache_lock:
            cache_result = {k: v for k, v in result.items() if k != '_enrichment_raw'}
            self._enrichment_cache[cache_key] = {
                'cached_at': datetime.now().isoformat(),
                'result': cache_result,
            }
