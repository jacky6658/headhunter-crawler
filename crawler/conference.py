"""
台灣技術研討會講者搜尋 — COSCUP / MOPCON / PyCon TW / GDG

講者 = 驗證過的技術專家 + 公開 profile + 通常有社群連結
"""
import logging
import re
import time
from typing import Callable, List, Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

# 台灣主要技術研討會
CONFERENCES = [
    {
        'name': 'COSCUP',
        'speakers_url': 'https://pretalx.coscup.org/coscup-{year}/speaker/',
        'speaker_detail': 'https://pretalx.coscup.org/coscup-{year}/speaker/{code}/',
        'years': [2024, 2023],
    },
    {
        'name': 'MOPCON',
        'brave_query': 'site:mopcon.org speaker {skill} {year}',
        'years': [2024, 2023],
    },
    {
        'name': 'PyCon TW',
        'brave_query': 'site:tw.pycon.org speaker {skill} {year}',
        'years': [2024, 2023],
    },
]

GITHUB_PATTERN = re.compile(r'https?://github\.com/([\w\-.]+)/?')
LINKEDIN_PATTERN = re.compile(r'https?://(?:www\.)?linkedin\.com/in/([\w\-]+)/?')


class ConferenceSearcher:
    """技術研討會講者搜尋"""

    def __init__(self, config: dict, anti_detect, stop_event=None):
        self.config = config
        self.ad = anti_detect
        self.stop_event = stop_event
        self.on_progress: Optional[Callable] = None

    def _is_stopped(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def search(self, skills: list, location: str = 'Taiwan',
               brave_key: str = '') -> dict:
        """搜尋研討會講者"""
        candidates = []
        seen_names = set()

        # 1. COSCUP (pretalx API — 最大的台灣開源研討會)
        coscup_speakers = self._search_coscup(skills)
        for s in coscup_speakers:
            name_key = s.get('name', '').lower()
            if name_key not in seen_names:
                seen_names.add(name_key)
                candidates.append(s)

        # 2. Brave 搜尋其他研討會
        if brave_key:
            for conf in CONFERENCES:
                if self._is_stopped():
                    break
                if conf['name'] == 'COSCUP':
                    continue  # 已用 API 搜過

                brave_query = conf.get('brave_query')
                if not brave_query:
                    continue

                for skill in skills[:2]:
                    for year in conf.get('years', [2024])[:1]:
                        query = brave_query.format(skill=skill, year=year)
                        results = self._brave_search_speakers(query, brave_key, conf['name'])
                        for s in results:
                            name_key = s.get('name', '').lower()
                            if name_key and name_key not in seen_names:
                                seen_names.add(name_key)
                                candidates.append(s)

        logger.info(f"Conference 完成: {len(candidates)} 位講者")
        return {'success': True, 'data': candidates}

    def _search_coscup(self, skills: list) -> list:
        """從 COSCUP pretalx 搜尋講者"""
        candidates = []
        skill_set = set(s.lower() for s in skills)

        for year in [2024, 2023]:
            if self._is_stopped():
                break

            url = f"https://pretalx.coscup.org/coscup-{year}/speaker/"
            html, _status = self.ad.http_get(url, timeout=15)
            if not html:
                continue

            # 提取講者 code
            speaker_codes = list(set(re.findall(r'/speaker/(\w+)/', html)))
            logger.info(f"COSCUP {year}: {len(speaker_codes)} speakers found")

            # 只取前 30 位講者的詳情（避免太多請求）
            for i, code in enumerate(speaker_codes[:30]):
                if self._is_stopped():
                    break

                detail_url = f"https://pretalx.coscup.org/coscup-{year}/speaker/{code}/"
                detail_html, _s = self.ad.http_get(detail_url, timeout=10)
                if not detail_html:
                    continue

                speaker = self._parse_coscup_speaker(detail_html, code, year)
                if not speaker:
                    continue

                # 技能匹配：講者的 bio/talks 是否包含搜尋技能
                text = f"{speaker.get('bio', '')} {speaker.get('_talks', '')}".lower()
                if not any(s in text for s in skill_set):
                    continue

                candidates.append(speaker)
                time.sleep(0.3)

            time.sleep(1)

        return candidates

    def _parse_coscup_speaker(self, html: str, code: str, year: int) -> Optional[dict]:
        """解析 COSCUP 講者頁面"""
        # 名字
        name_match = re.search(r'<h2[^>]*>([^<]+)</h2>', html)
        if not name_match:
            return None
        name = name_match.group(1).strip()
        if not name or len(name) > 50:
            return None

        # Bio
        bio_match = re.search(r'<div class="biography[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
        bio = ''
        if bio_match:
            bio = re.sub(r'<[^>]+>', ' ', bio_match.group(1)).strip()[:300]

        # Talk titles
        talks = re.findall(r'<h3[^>]*>([^<]+)</h3>', html)
        talks_text = ' | '.join(t.strip() for t in talks if t.strip())

        # GitHub / LinkedIn from bio
        github_url = ''
        github_username = ''
        linkedin_url = ''
        all_text = f"{bio} {html}"

        gh_match = GITHUB_PATTERN.search(all_text)
        if gh_match:
            github_username = gh_match.group(1)
            github_url = f"https://github.com/{github_username}"

        li_match = LINKEDIN_PATTERN.search(all_text)
        if li_match:
            linkedin_url = f"https://www.linkedin.com/in/{li_match.group(1)}/"

        # 從 bio 提取技能關鍵字
        from crawler.cakeresume import CakeResumeSearcher
        skills = CakeResumeSearcher._extract_skills_from_text(f"{bio} {talks_text}")

        return {
            'source': 'conference',
            'name': name,
            'github_url': github_url,
            'github_username': github_username,
            'linkedin_url': linkedin_url,
            'linkedin_username': '',
            'cakeresume_url': '',
            'email': '',
            'location': 'Taiwan',  # COSCUP 是台灣研討會
            'bio': f"COSCUP {year} Speaker | {talks_text[:100]}" if talks_text else bio[:150],
            'company': '',
            'title': '',
            'skills': skills,
            'public_repos': 0,
            'followers': 0,
            'recent_push': '',
            'top_repos': [],
            'total_stars': 0,
            'score_factors': {'conference_speaker': True, 'conference': f'COSCUP {year}'},
            'tech_stack': skills,
            'top_repos_detail': [],
            'languages': {},
            '_talks': talks_text,
        }

    def _brave_search_speakers(self, query: str, brave_key: str, conf_name: str) -> list:
        """用 Brave 搜尋研討會講者"""
        endpoint = "https://api.search.brave.com/res/v1/web/search"
        headers = {
            'Accept': 'application/json',
            'X-Subscription-Token': brave_key,
        }
        params = urlencode({'q': query, 'count': 10})
        data, status = self.ad.http_get_json(f"{endpoint}?{params}", extra_headers=headers)

        if status != 200:
            return []

        candidates = []
        for r in data.get('web', {}).get('results', []):
            title = r.get('title', '')
            desc = re.sub(r'<[^>]+>', '', r.get('description', ''))
            url = r.get('url', '')

            # 嘗試從標題提取講者名
            name = ''
            # Common pattern: "Speaker Name - Talk Title"
            m = re.match(r'^([^-|]+?)(?:\s*[-|])', title)
            if m:
                name = m.group(1).strip()

            if not name or len(name) > 40:
                continue

            from crawler.cakeresume import CakeResumeSearcher
            skills = CakeResumeSearcher._extract_skills_from_text(f"{title} {desc}")

            candidates.append({
                'source': 'conference',
                'name': name,
                'github_url': '', 'github_username': '',
                'linkedin_url': '', 'linkedin_username': '',
                'cakeresume_url': '',
                'email': '',
                'location': 'Taiwan',
                'bio': f"{conf_name} Speaker | {title[:80]}",
                'company': '', 'title': '',
                'skills': skills,
                'public_repos': 0, 'followers': 0,
                'recent_push': '', 'top_repos': [],
                'total_stars': 0,
                'score_factors': {'conference_speaker': True, 'conference': conf_name},
                'tech_stack': skills,
                'top_repos_detail': [], 'languages': {},
            })

        return candidates
