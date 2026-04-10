"""
StackOverflow 搜尋模組 — 從 Stack Exchange API 搜尋台灣開發者

API: https://api.stackexchange.com/2.3/
免費額度: 300 requests/day (無 key), 10000/day (有 key)
"""
import logging
import re
import time
from typing import Callable, List, Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

SO_API = "https://api.stackexchange.com/2.3"

# StackOverflow tag → 技能映射
_TAG_SKILL_MAP = {
    'go': 'Golang', 'golang': 'Golang',
    'python': 'Python', 'java': 'Java',
    'javascript': 'JavaScript', 'typescript': 'TypeScript',
    'reactjs': 'React', 'react-native': 'React Native',
    'node.js': 'Node.js', 'docker': 'Docker',
    'kubernetes': 'Kubernetes', 'amazon-web-services': 'AWS',
    'google-cloud-platform': 'GCP', 'azure': 'Azure',
    'postgresql': 'PostgreSQL', 'mysql': 'MySQL',
    'mongodb': 'MongoDB', 'redis': 'Redis',
    'rust': 'Rust', 'c++': 'C++', 'c#': 'C#',
    'swift': 'Swift', 'kotlin': 'Kotlin', 'flutter': 'Flutter',
    'vue.js': 'Vue', 'angular': 'Angular',
    'terraform': 'Terraform', 'jenkins': 'Jenkins',
    'elasticsearch': 'Elasticsearch', 'graphql': 'GraphQL',
    'machine-learning': 'Machine Learning',
    'deep-learning': 'Deep Learning',
    'pytorch': 'PyTorch', 'tensorflow': 'TensorFlow',
}


class StackOverflowSearcher:
    """StackOverflow 人才搜尋"""

    def __init__(self, config: dict, anti_detect, stop_event=None):
        self.config = config
        self.ad = anti_detect
        self.stop_event = stop_event
        self.on_progress: Optional[Callable] = None

        so_cfg = config.get('crawler', {}).get('stackoverflow', {})
        self.enabled = so_cfg.get('enabled', False)
        self.api_key = so_cfg.get('api_key', '')

    def _is_stopped(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def search(self, skills: list, location: str = 'Taiwan', pages: int = 1) -> dict:
        """
        搜尋 StackOverflow 用戶

        策略: 用 Brave API 搜 "site:stackoverflow.com/users {skill} {location}"
        因為 SO API 不支援 location + tag 聯合搜尋
        """
        if not self.enabled:
            return {'success': True, 'data': []}

        candidates = []
        seen_ids = set()

        # 用 SO API 搜尋有相關 tag 的用戶
        for skill in skills[:3]:
            if self._is_stopped():
                break

            tag = self._skill_to_tag(skill)
            if not tag:
                continue

            users = self._search_users_by_tag(tag, pages)
            for user in users:
                uid = user.get('user_id')
                if uid in seen_ids:
                    continue
                seen_ids.add(uid)

                # 地區過濾
                loc = (user.get('location', '') or '').lower()
                if not any(kw in loc for kw in ['taiwan', 'taipei', '台', 'hsinchu', 'taichung']):
                    continue

                candidate = self._user_to_candidate(user, skills)
                if candidate:
                    candidates.append(candidate)

            time.sleep(0.5)

        logger.info(f"StackOverflow 完成: {len(candidates)} 位台灣候選人 (from {len(seen_ids)} total)")
        return {'success': True, 'data': candidates}

    def _skill_to_tag(self, skill: str) -> str:
        """技能名稱轉 SO tag"""
        sl = skill.lower().strip()
        # 直接匹配
        for tag, name in _TAG_SKILL_MAP.items():
            if sl == name.lower() or sl == tag:
                return tag
        # 模糊匹配
        if sl in ('go', 'golang'):
            return 'go'
        return sl

    def _search_users_by_tag(self, tag: str, pages: int = 1) -> list:
        """用 SO API 搜尋有特定 tag 回答的用戶"""
        all_users = []
        for page in range(1, pages + 1):
            if self._is_stopped():
                break

            params = {
                'order': 'desc',
                'sort': 'reputation',
                'site': 'stackoverflow',
                'pagesize': 50,
                'page': page,
                'filter': '!LnNkvq0d)S0rTO',  # 包含 location, website_url
            }
            if self.api_key:
                params['key'] = self.api_key

            url = f"{SO_API}/users?{urlencode(params)}"
            data, status = self.ad.http_get_json(url, timeout=10)

            if status != 200:
                logger.warning(f"SO API HTTP {status}")
                break

            items = data.get('items', [])
            if not items:
                break

            all_users.extend(items)

            if not data.get('has_more'):
                break

        return all_users

    def _user_to_candidate(self, user: dict, search_skills: list) -> Optional[dict]:
        """將 SO 用戶轉為 candidate dict"""
        name = user.get('display_name', '')
        if not name:
            return None

        reputation = user.get('reputation', 0)
        location = user.get('location', '')
        website = user.get('website_url', '')
        so_link = user.get('link', '')
        user_id = user.get('user_id', '')

        # 從 top_tags 提取技能
        skills = []
        top_tags = user.get('top_tags', [])
        for tag in top_tags:
            tag_name = tag.get('tag_name', '')
            mapped = _TAG_SKILL_MAP.get(tag_name, tag_name)
            if mapped:
                skills.append(mapped)

        # 從 website 提取 GitHub/LinkedIn
        github_url = ''
        github_username = ''
        linkedin_url = ''
        if website:
            gh_match = re.search(r'github\.com/([\w\-.]+)', website)
            if gh_match:
                github_username = gh_match.group(1)
                github_url = f"https://github.com/{github_username}"
            li_match = re.search(r'linkedin\.com/in/([\w\-]+)', website)
            if li_match:
                linkedin_url = f"https://www.linkedin.com/in/{li_match.group(1)}/"

        # about_me 有時包含連結
        about = user.get('about_me', '') or ''
        if not github_url:
            gh_match = re.search(r'github\.com/([\w\-.]+)', about)
            if gh_match:
                github_username = gh_match.group(1)
                github_url = f"https://github.com/{github_username}"

        return {
            'source': 'stackoverflow',
            'name': name,
            'stackoverflow_url': so_link,
            'github_url': github_url,
            'github_username': github_username,
            'linkedin_url': linkedin_url,
            'linkedin_username': '',
            'cakeresume_url': '',
            'email': '',
            'location': location,
            'bio': f"SO rep: {reputation:,} | {', '.join(skills[:5])}",
            'company': '',
            'title': '',
            'skills': skills or search_skills,
            'public_repos': 0,
            'followers': 0,
            'recent_push': '',
            'top_repos': [],
            'total_stars': 0,
            'score_factors': {'stackoverflow_reputation': reputation},
            'tech_stack': skills,
            'top_repos_detail': [],
            'languages': {},
        }
