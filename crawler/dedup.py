"""
持久化去重快取 — JSON 檔案儲存已見的 LinkedIn URL + GitHub username
"""
import json
import logging
import os
import threading

logger = logging.getLogger(__name__)


class DedupCache:
    """跨次執行不重複抓同一人"""

    def __init__(self, cache_file: str):
        self.cache_file = cache_file
        self.linkedin_urls: set = set()
        self.github_usernames: set = set()
        self._lock = threading.Lock()
        self.load()

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
