"""
多站 Google Dorking 搜尋 — 用 Brave API 搜尋 Medium / Dev.to / SpeakerDeck 等站點
根據結果 URL domain 分流處理，提取候選人 stub
"""
import logging
import re
import time
from typing import Callable, List, Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)


class DorkingSearcher:
    """多站 Google Dorking 搜尋"""

    def __init__(self, config: dict, anti_detect, stop_event=None):
        self.config = config
        self.ad = anti_detect
        self.stop_event = stop_event
        self.on_progress: Optional[Callable] = None

        dork_cfg = config.get('crawler', {}).get('dorking', {})
        self.enabled = dork_cfg.get('enabled', True)
        self.sites = dork_cfg.get('sites', [
            {'site': 'medium.com', 'source': 'blog'},
            {'site': 'dev.to', 'source': 'blog'},
            {'site': 'speakerdeck.com', 'source': 'conference'},
            {'site': 'hackmd.io', 'source': 'blog'},
        ])

    def _is_stopped(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def search(self, skills: list, location: str, brave_key: str = '',
               company_queries: list = None) -> dict:
        """
        多站搜尋主入口
        Returns: {'success': bool, 'data': [candidate_dict, ...]}
        """
        if not self.enabled or not brave_key:
            return {'success': True, 'data': []}

        endpoint = "https://api.search.brave.com/res/v1/web/search"
        headers = {
            'Accept': 'application/json',
            'Accept-Encoding': 'gzip',
            'X-Subscription-Token': brave_key,
        }

        all_candidates = []
        seen_urls = set()

        for site_cfg in self.sites:
            if self._is_stopped():
                break

            site = site_cfg['site']
            source = site_cfg.get('source', 'blog')

            # 建構 query
            skill_part = ' OR '.join(f'"{s}"' for s in skills[:3])
            query = f'site:{site} ({skill_part}) "{location}"'

            if len(query) > 380:
                query = f'site:{site} "{skills[0]}" "{location}"'

            logger.info(f"Dorking [{source}]: {query}")

            params = urlencode({'q': query, 'count': 10})
            data, status = self.ad.http_get_json(
                f"{endpoint}?{params}", extra_headers=headers)

            if status == 429:
                logger.warning("Dorking: Brave rate limited")
                break
            if status != 200:
                continue

            self.ad.github_delay()

            for r in data.get('web', {}).get('results', []):
                url = r.get('url', '')
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                title = r.get('title', '')
                desc = re.sub(r'<[^>]+>', '', r.get('description', ''))

                # 從標題和描述提取作者名
                name = self._extract_author(title, desc, site)
                if not name:
                    continue

                all_candidates.append({
                    'source': source,
                    'name': name,
                    'cakeresume_url': '',
                    'github_url': '', 'github_username': '',
                    'linkedin_url': '', 'linkedin_username': '',
                    'email': '',
                    'location': location,
                    'bio': f"{title[:100]} — {desc[:100]}",
                    'company': '',
                    'title': '',
                    'skills': self._extract_skills(f"{title} {desc}"),
                    'public_repos': 0, 'followers': 0,
                    'recent_push': '', 'top_repos': [],
                    'total_stars': 0, 'score_factors': {},
                    'tech_stack': [], 'top_repos_detail': [], 'languages': {},
                    '_dorking_url': url,
                    '_dorking_source': f'{source}:{site}',
                })

        logger.info(f"Dorking 完成: {len(all_candidates)} 候選人 from {len(self.sites)} sites")
        return {'success': True, 'data': all_candidates}

    @staticmethod
    def _extract_author(title: str, desc: str, site: str) -> str:
        """從標題/描述提取作者名"""
        # Medium: "Title | by Author Name | Publication"
        m = re.search(r'\|\s*by\s+([^|]+)', title)
        if m:
            return m.group(1).strip()

        # Dev.to: "Title - DEV Community" → author in desc
        m = re.search(r'(?:^|\b)by\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})', desc)
        if m:
            return m.group(1).strip()

        # SpeakerDeck: "Title by Author" or "Author - Title"
        m = re.search(r'by\s+([^-|]+)', title, re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            if len(name) < 40:
                return name

        return ''

    _TECH_KEYWORDS = {
        'python', 'java', 'javascript', 'typescript', 'golang', 'go',
        'rust', 'react', 'vue', 'angular', 'node.js', 'docker',
        'kubernetes', 'k8s', 'aws', 'gcp', 'terraform',
        'postgresql', 'mongodb', 'redis', 'graphql', 'grpc',
        'machine learning', 'deep learning', 'pytorch', 'tensorflow',
    }

    @classmethod
    def _extract_skills(cls, text: str) -> list:
        if not text:
            return []
        text_lower = text.lower()
        return [kw for kw in cls._TECH_KEYWORDS if kw in text_lower]
