"""
持久化去重快取 — JSON 檔案儲存已見的 LinkedIn URL + GitHub username
啟動時自動從 Step1ne DB 同步既有候選人，避免重複抓取
"""
import json
import logging
import os
import threading

logger = logging.getLogger(__name__)


class DedupCache:
    """跨次執行不重複抓同一人"""

    def __init__(self, cache_file: str, db_url: str = None, api_base: str = None, api_key: str = None):
        self.cache_file = cache_file
        self.linkedin_urls: set = set()
        self.github_usernames: set = set()
        self._lock = threading.Lock()
        self.load()
        # 從系統 DB 同步既有候選人
        if api_base and api_key:
            self._sync_from_system(api_base, api_key)

    def load(self):
        if not os.path.exists(self.cache_file):
            return
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.linkedin_urls = set(data.get('linkedin_urls', []))
            self.github_usernames = set(data.get('github_usernames', []))
            logger.info(f"去重快取載入: LinkedIn={len(self.linkedin_urls)}, GitHub={len(self.github_usernames)}")
        except Exception as e:
            logger.warning(f"去重快取載入失敗: {e}")

    def save(self):
        with self._lock:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            try:
                with open(self.cache_file, 'w', encoding='utf-8') as f:
                    json.dump({
                        'linkedin_urls': list(self.linkedin_urls),
                        'github_usernames': list(self.github_usernames),
                    }, f, ensure_ascii=False)
            except Exception as e:
                logger.error(f"去重快取儲存失敗: {e}")

    def is_seen(self, linkedin_url: str = None, github_username: str = None) -> bool:
        if linkedin_url and linkedin_url in self.linkedin_urls:
            return True
        if github_username and github_username in self.github_usernames:
            return True
        return False

    def mark_seen(self, linkedin_url: str = None, github_username: str = None):
        with self._lock:
            if linkedin_url:
                self.linkedin_urls.add(linkedin_url)
            if github_username:
                self.github_usernames.add(github_username)

    def clear(self, source: str = None):
        """清除快取。source='linkedin'/'github'/None(全部)"""
        with self._lock:
            if source is None or source == 'linkedin':
                self.linkedin_urls.clear()
            if source is None or source == 'github':
                self.github_usernames.clear()
        self.save()
        logger.info(f"去重快取已清除: {source or '全部'}")

    def stats(self) -> dict:
        return {
            'linkedin': len(self.linkedin_urls),
            'github': len(self.github_usernames),
        }

    def _sync_from_system(self, api_base: str, api_key: str):
        """從 Step1ne 系統拉所有既有候選人的 LinkedIn URL + GitHub username，灌進去重快取"""
        from urllib.request import Request, urlopen
        from urllib.error import URLError
        try:
            req = Request(
                f"{api_base}/api/candidates?limit=9999",
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json'
                }
            )
            resp = urlopen(req, timeout=30)
            data = json.loads(resp.read().decode('utf-8'))

            candidates = data
            if isinstance(data, dict):
                candidates = data.get('data', data.get('candidates', []))
                if isinstance(candidates, dict):
                    candidates = candidates.get('candidates', [])

            synced_li = 0
            synced_gh = 0
            for c in candidates:
                li_url = c.get('linkedinUrl') or c.get('linkedin_url') or ''
                gh_url = c.get('githubUrl') or c.get('github_url') or ''
                contact = c.get('contact_link') or c.get('contactLink') or ''

                # LinkedIn URL
                if li_url and 'linkedin.com/in/' in li_url:
                    normalized = li_url.lower().rstrip('/').replace('www.', '')
                    self.linkedin_urls.add(normalized)
                    synced_li += 1
                elif contact and 'linkedin.com/in/' in contact:
                    normalized = contact.lower().rstrip('/').replace('www.', '')
                    self.linkedin_urls.add(normalized)
                    synced_li += 1

                # GitHub username
                if gh_url and 'github.com/' in gh_url:
                    username = gh_url.rstrip('/').split('/')[-1].lower()
                    if username and username not in ('', 'github.com'):
                        self.github_usernames.add(username)
                        synced_gh += 1

            self.save()
            logger.info(f"系統同步完成: 新增 LinkedIn={synced_li}, GitHub={synced_gh} "
                        f"→ 快取總計 LinkedIn={len(self.linkedin_urls)}, GitHub={len(self.github_usernames)}")

        except URLError as e:
            logger.warning(f"系統同步失敗（網路）: {e} — 使用本地快取繼續")
        except Exception as e:
            logger.warning(f"系統同步失敗: {e} — 使用本地快取繼續")
