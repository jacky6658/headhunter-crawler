"""
LinkedIn 搜尋模組 — 4 層備援（Playwright → Google → Bing → Brave）
來源: search-plan-executor.py L310-750
"""
import logging
import os
import random
import re
import time
from typing import Callable, Optional
from urllib.parse import quote, unquote, urlencode

import yaml

logger = logging.getLogger(__name__)

# Google 全域冷卻期（同一程序內共用）
_google_blocked_until = 0  # timestamp，在此之前不嘗試 Google
_GOOGLE_COOLDOWN_SECS = 600  # 被封後冷卻 10 分鐘


def _load_skill_synonyms() -> dict:
    """從 YAML 載入技能同義詞"""
    path = os.path.join(os.path.dirname(__file__), '..', 'config', 'skills_synonyms.yaml')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f"技能同義詞載入失敗: {e}")
        return {}


def _normalize_key(skill: str) -> str:
    return skill.lower().replace('.', '').replace(' ', '').replace('-', '').replace('_', '')


class LinkedInSearcher:
    """LinkedIn 人才搜尋，4 層備援"""

    def __init__(self, config: dict, anti_detect, ocr=None):
        self.config = config
        self.ad = anti_detect
        self.ocr = ocr
        self.skill_synonyms = _load_skill_synonyms()

        crawler_cfg = config.get('crawler', {})
        self.sample_per_page = crawler_cfg.get('sample_per_page', 5)

        li_cfg = crawler_cfg.get('linkedin', {})
        self.enable_playwright = li_cfg.get('enable_playwright', True)
        self.enable_google = li_cfg.get('enable_google', True)
        self.enable_bing = li_cfg.get('enable_bing', True)
        self.enable_brave = li_cfg.get('enable_brave', True)
        self.min_results = li_cfg.get('min_results_threshold', 3)

        self.on_progress: Optional[Callable] = None

    # ── 技能同義詞 ───────────────────────────────────────────

    def expand_skill_synonyms(self, skill: str) -> list:
        """回傳技能 + 所有已知別名"""
        key = _normalize_key(skill)
        synonyms = self.skill_synonyms.get(key)
        if synonyms:
            result = list(dict.fromkeys([skill] + synonyms))
            return result
        return [skill]

    # ── 查詢建構 ─────────────────────────────────────────────

    def build_query(self, skills: list, location: str) -> str:
        """
        主技能 (前 2 個) AND + 次技能 (3-7) OR + 同義詞展開
        用於 Google / Bing 等通用搜尋引擎
        """
        primary = skills[:2]
        secondary = skills[2:7]
        parts = []

        for skill in primary:
            synonyms = self.expand_skill_synonyms(skill)
            if len(synonyms) > 1:
                parts.append('(' + ' OR '.join(f'"{s}"' for s in synonyms) + ')')
            else:
                parts.append(f'"{skill}"')

        secondary_terms = []
        for skill in secondary:
            for s in self.expand_skill_synonyms(skill):
                term = f'"{s}"'
                if term not in secondary_terms:
                    secondary_terms.append(term)
        if secondary_terms:
            parts.append('(' + ' OR '.join(secondary_terms) + ')')

        query = f'site:linkedin.com/in/ ' + ' '.join(parts) + f' "{location}"'
        return query

    # 地區搜尋詞展開（LinkedIn profile 常見的寫法）
    LOCATION_VARIANTS = {
        'taiwan': ['"Taiwan"', '"Taipei"', '"台灣"', '"台北"', '"Hsinchu"', '"Taichung"'],
        'singapore': ['"Singapore"'],
        'hong kong': ['"Hong Kong"', '"香港"'],
        'japan': ['"Japan"', '"Tokyo"', '"Osaka"'],
        'united states': ['"United States"', '"USA"', '"San Francisco"', '"New York"'],
        'united kingdom': ['"United Kingdom"', '"UK"', '"London"'],
        'germany': ['"Germany"', '"Berlin"', '"Munich"'],
        'canada': ['"Canada"', '"Toronto"', '"Vancouver"'],
        'australia': ['"Australia"', '"Sydney"', '"Melbourne"'],
        'china': ['"China"', '"Shanghai"', '"Beijing"', '"Shenzhen"'],
        'korea': ['"Korea"', '"Seoul"'],
        'vietnam': ['"Vietnam"', '"Ho Chi Minh"', '"Hanoi"'],
        'india': ['"India"', '"Bangalore"', '"Mumbai"'],
    }

    def _location_query_part(self, location: str) -> str:
        """將 location 轉為 OR 搜尋詞，涵蓋常見寫法"""
        key = location.lower().strip()
        variants = self.LOCATION_VARIANTS.get(key)
        if variants:
            return '(' + ' OR '.join(variants) + ')'
        # 未知地區：用引號精確匹配
        return f'"{location}"'

    def build_brave_queries(self, primary_skills: list, secondary_skills: list,
                            location: str, job_title: str = '') -> list:
        """
        為 Brave API 建構多組搜尋查詢，由寬到窄：
        1. 職缺名稱 + 地區變體 (最精準匹配)
        2. 主技能 OR + 地區變體 (技能匹配)
        3. 主技能 AND + 地區變體 (嚴格)
        4. 主技能 + 次技能 + 地區變體 (補充)
        """
        queries = []
        loc_part = self._location_query_part(location)

        # 查詢 1: 職缺名稱 + 地區 — 最精準
        if job_title:
            import re as _re
            # 取括號外的部分作為主標題
            en_title = _re.sub(r'[（(][^）)]*[）)]', '', job_title).strip()
            if en_title:
                queries.append(f'site:linkedin.com/in/ "{en_title}" {loc_part}')

        # 查詢 2: 主技能 用 OR (寬鬆) — 限制同義詞數量避免查詢過長
        if primary_skills:
            all_terms = []
            for skill in primary_skills:
                for s in self.expand_skill_synonyms(skill)[:3]:  # 每個技能最多 3 個同義詞
                    t = f'"{s}"'
                    if t not in all_terms:
                        all_terms.append(t)
            all_terms = all_terms[:10]  # 總計最多 10 個 OR 項
            skill_part = ' OR '.join(all_terms)
            queries.append(f'site:linkedin.com/in/ ({skill_part}) {loc_part}')

        # 查詢 3: 主技能 AND (精準)
        if len(primary_skills) >= 2:
            terms = [f'"{s}"' for s in primary_skills[:2]]
            queries.append(f'site:linkedin.com/in/ {" ".join(terms)} {loc_part}')

        # 查詢 4: 主技能 + 次技能混合
        if secondary_skills:
            sec_terms = []
            for skill in secondary_skills[:3]:  # 最多取 3 個次技能
                for s in self.expand_skill_synonyms(skill)[:2]:  # 每個最多 2 個同義詞
                    t = f'"{s}"'
                    if t not in sec_terms:
                        sec_terms.append(t)
            if primary_skills:
                p = f'"{primary_skills[0]}"'
                s_part = ' OR '.join(sec_terms)
                queries.append(f'site:linkedin.com/in/ {p} ({s_part}) {loc_part}')

        # 去重
        seen = set()
        unique = []
        for q in queries:
            if q not in seen:
                seen.add(q)
                unique.append(q)

        return unique or [f'site:linkedin.com/in/ {loc_part}']

    # ── URL 工具 ─────────────────────────────────────────────

    @staticmethod
    def clean_url(href) -> Optional[str]:
        try:
            url = unquote(str(href))
            url = url.split('?')[0].split('%3F')[0]
            url = re.sub(r'^https?://[a-z]{2,3}\.linkedin\.com', 'https://www.linkedin.com', url)
            if not re.search(r'linkedin\.com/in/[\w\-]+', url):
                return None
            if not url.endswith('/'):
                url += '/'
            return url
        except Exception:
            return None

    @staticmethod
    def _make_item(url, name='', title='', company=''):
        username = url.rstrip('/').split('/')[-1]
        return {
            'source': 'linkedin',
            'name': name or username.replace('-', ' ').title(),
            'github_url': '', 'github_username': '',
            'linkedin_url': url, 'linkedin_username': username,
            'location': '', 'bio': title, 'company': company,
            'email': '', 'public_repos': 0, 'followers': 0,
            'skills': [], 'recent_push': '', 'top_repos': [],
        }

    @staticmethod
    def _parse_title_text(raw):
        text = re.sub(r'\s*\|\s*LinkedIn.*$', '', raw, flags=re.IGNORECASE).strip()
        m = re.match(r'^(.+?)\s*[-–]\s*(.+)$', text)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        return text, ''

    @staticmethod
    def extract_urls_from_html(html_text) -> list:
        """從搜尋結果 HTML 抽取 LinkedIn URL"""
        found = []
        seen = set()

        def add(url, name='', title='', company=''):
            if url in seen:
                return
            seen.add(url)
            found.append(LinkedInSearcher._make_item(url, name, title, company))

        # Google redirect
        for m in re.finditer(r'href="(/url\?q=https?://(?:www\.)?linkedin\.com/in/[^"&]+)', html_text):
            raw = unquote(m.group(1).replace('/url?q=', ''))
            url = LinkedInSearcher.clean_url(raw)
            if url:
                snippet = html_text[max(0, m.start()-300):m.end()+300]
                title_m = re.search(r'<(?:h3|h2)[^>]*>([^<]{5,80})</(?:h3|h2)>', snippet)
                name, title = LinkedInSearcher._parse_title_text(title_m.group(1)) if title_m else ('', '')
                add(url, name, title)

        # Direct href
        for m in re.finditer(r'href="(https?://(?:www\.)?linkedin\.com/in/[\w\-]+/?)(?:[^"]*)"', html_text):
            url = LinkedInSearcher.clean_url(m.group(1))
            if url:
                snippet = html_text[max(0, m.start()-200):m.end()+200]
                title_m = re.search(r'>([^<]{5,80})</(?:h[23]|a)>', snippet)
                name, title = LinkedInSearcher._parse_title_text(title_m.group(1)) if title_m else ('', '')
                add(url, name, title)

        # Plain text
        for m in re.finditer(r'linkedin\.com/in/([\w\-]+)', html_text):
            url = LinkedInSearcher.clean_url(f'https://www.linkedin.com/in/{m.group(1)}/')
            if url:
                add(url)

        return found

    def _sample(self, items: list) -> list:
        if len(items) > self.sample_per_page:
            return random.sample(items, self.sample_per_page)
        return items

    # ── Layer 1: Playwright ──────────────────────────────────

    def search_via_playwright(self, skills: list, location: str, pages: int,
                              browser=None) -> dict:
        global _google_blocked_until

        if not self.enable_playwright:
            return {'success': False, 'data': [], 'reason': 'disabled', 'google_blocked': False}

        # 檢查 Google 冷卻期
        if time.time() < _google_blocked_until:
            remaining = int(_google_blocked_until - time.time())
            logger.info(f"Playwright: Google 冷卻中，跳過 (剩 {remaining}s)")
            return {'success': False, 'data': [], 'reason': 'google_cooldown', 'google_blocked': True}

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return {'success': False, 'data': [], 'reason': 'playwright_not_installed', 'google_blocked': False}

        query = self.build_query(skills, location)
        logger.info(f"Playwright Google query: {query}")
        results = []
        seen_urls = set()
        google_blocked = False

        manage_browser = browser is None
        pw_instance = None

        try:
            if manage_browser:
                from crawler.browser_pool import BrowserPool
                pool = BrowserPool(headless=self.config.get('crawler', {}).get('headless', True))
                browser = pool.start()
                if not browser:
                    return {'success': False, 'data': [], 'reason': 'browser_start_failed', 'google_blocked': False}

            ctx = browser.new_context(
                user_agent=self.ad.get_random_ua(),
                locale='zh-TW',
                viewport={'width': 1280, 'height': 800},
                extra_http_headers={
                    'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
                },
            )
            self.ad.apply_stealth(ctx)
            page = ctx.new_page()
            self.ad.apply_page_stealth(page)

            try:
                for pg in range(pages):
                    start = pg * 10
                    url = (
                        f"https://www.google.com/search"
                        f"?q={quote(query)}&start={start}&num=10&hl=zh-TW"
                    )
                    page.goto(url, wait_until='domcontentloaded',
                              timeout=self.ad.page_load_timeout)
                    self.ad.request_delay()

                    html = page.content()

                    if self.ad.is_captcha_page(html):
                        logger.warning("Playwright: Google CAPTCHA，標記 Google 被封鎖")
                        google_blocked = True
                        _google_blocked_until = time.time() + _GOOGLE_COOLDOWN_SECS
                        break

                    page_items = self._sample(self.extract_urls_from_html(html))
                    for item in page_items:
                        li_url = item['linkedin_url']
                        if li_url not in seen_urls:
                            seen_urls.add(li_url)
                            results.append(item)

                    logger.info(f"Playwright page {pg+1}/{pages}: +{len(page_items)}, 累計 {len(results)}")
                    if self.on_progress:
                        self.on_progress(pg + 1, pages, len(results), 'linkedin_playwright')

                    if pg < pages - 1:
                        self.ad.page_delay()
            finally:
                ctx.close()
                if manage_browser:
                    pool.stop()

        except Exception as e:
            logger.error(f"Playwright 失敗: {e}")
            return {'success': False, 'data': results, 'reason': str(e), 'google_blocked': google_blocked}

        logger.info(f"Playwright LinkedIn: {len(results)} 筆")
        return {'success': True, 'data': results, 'google_blocked': google_blocked}

    # ── Layer 2: Google urllib ────────────────────────────────

    def search_via_google(self, skills: list, location: str, pages: int) -> dict:
        global _google_blocked_until

        if not self.enable_google:
            return {'success': True, 'data': [], 'captcha': False, 'google_blocked': False}

        # 檢查 Google 冷卻期
        if time.time() < _google_blocked_until:
            remaining = int(_google_blocked_until - time.time())
            logger.info(f"Google urllib: 冷卻中，跳過 (剩 {remaining}s)")
            return {'success': True, 'data': [], 'captcha': False, 'google_blocked': True}

        results = []
        seen_urls = set()
        captcha_detected = False
        google_blocked = False
        consecutive_429 = 0  # 追蹤連續 429 次數
        query = self.build_query(skills, location)
        logger.info(f"Google urllib query: {query}")

        for pg in range(pages):
            start = pg * 10
            search_url = f"https://www.google.com/search?q={quote(query)}&start={start}&num=10&hl=zh-TW"
            self.ad.request_delay()

            text, status = self.ad.http_get(search_url)
            if status == 429:
                consecutive_429 += 1
                logger.warning(f"Google 429 (連續第 {consecutive_429} 次)")
                if consecutive_429 >= 2:
                    logger.warning(f"Google 連續 {consecutive_429} 次 429，放棄並設定 {_GOOGLE_COOLDOWN_SECS}s 冷卻期")
                    google_blocked = True
                    _google_blocked_until = time.time() + _GOOGLE_COOLDOWN_SECS
                    break
                self.ad.exponential_backoff(consecutive_429)
                continue
            if status != 200:
                logger.warning(f"Google HTTP {status}")
                continue

            # 成功取得頁面，重設 429 計數
            consecutive_429 = 0

            if self.ad.is_captcha_page(text):
                logger.warning("Google CAPTCHA，設定冷卻期")
                captcha_detected = True
                google_blocked = True
                _google_blocked_until = time.time() + _GOOGLE_COOLDOWN_SECS
                break

            page_items = self._sample(self.extract_urls_from_html(text))
            for item in page_items:
                url = item['linkedin_url']
                if url not in seen_urls:
                    seen_urls.add(url)
                    results.append(item)

            if self.on_progress:
                self.on_progress(pg + 1, pages, len(results), 'linkedin_google')

        logger.info(f"Google LinkedIn: {len(results)} 筆")
        return {'success': True, 'data': results, 'captcha': captcha_detected, 'google_blocked': google_blocked}

    # ── Layer 3: Bing urllib ─────────────────────────────────

    def search_via_bing(self, skills: list, location: str, pages: int) -> dict:
        if not self.enable_bing:
            return {'success': True, 'data': []}

        results = []
        seen_urls = set()
        query = self.build_query(skills, location)
        logger.info(f"Bing query: {query}")

        for pg in range(pages):
            first = pg * 10 + 1
            search_url = f"https://www.bing.com/search?q={quote(query)}&first={first}&count=10"
            self.ad.request_delay()

            text, status = self.ad.http_get(search_url)
            if status != 200:
                logger.warning(f"Bing HTTP {status}")
                continue

            page_items = self._sample(self.extract_urls_from_html(text))
            for item in page_items:
                url = item['linkedin_url']
                if url not in seen_urls:
                    seen_urls.add(url)
                    results.append(item)

            if self.on_progress:
                self.on_progress(pg + 1, pages, len(results), 'linkedin_bing')

        logger.info(f"Bing LinkedIn: {len(results)} 筆")
        return {'success': True, 'data': results}

    # ── Layer 4: Brave API ───────────────────────────────────

    def search_via_brave(self, skills: list, brave_key: str, location: str,
                         pages: int, job_title: str = '',
                         primary_skills: list = None, secondary_skills: list = None) -> dict:
        if not self.enable_brave or not brave_key:
            return {'success': True, 'data': []}

        results = []
        seen_urls = set()
        endpoint = "https://api.search.brave.com/res/v1/web/search"
        brave_headers = {
            'Accept': 'application/json',
            'Accept-Encoding': 'gzip',
            'X-Subscription-Token': brave_key,
            'Cache-Control': 'no-cache',
        }

        # 用多組查詢策略取得更多結果
        p_skills = primary_skills or skills[:2]
        s_skills = secondary_skills or skills[2:]
        queries = self.build_brave_queries(p_skills, s_skills, location, job_title)
        logger.info(f"Brave: {len(queries)} 組查詢")

        total_pages_done = 0
        pages_per_query = max(2, pages)  # 每組至少查 2 頁

        for qi, query in enumerate(queries):
            # Brave API 查詢長度限制 ~400 字元，超過會 422
            if len(query) > 400:
                logger.info(f"Brave [{qi+1}/{len(queries)}]: 查詢過長 ({len(query)} chars)，截斷")
                query = query[:400].rsplit(' OR ', 1)[0] + ')'
            logger.info(f"Brave [{qi+1}/{len(queries)}]: {query}")
            for pg in range(pages_per_query):
                params = urlencode({'q': query, 'count': 20, 'offset': pg * 20})
                data, status = self.ad.http_get_json(
                    f"{endpoint}?{params}", extra_headers=brave_headers)

                if status == 401:
                    logger.error("Brave API: 金鑰無效 (401)")
                    return {'success': False, 'data': results}
                if status == 429:
                    logger.warning("Brave API: 速率限制 (429)")
                    break
                if status != 200:
                    logger.warning(f"Brave API: HTTP {status}")
                    continue

                self.ad.github_delay()
                page_raw = data.get('web', {}).get('results', [])

                new_count = 0
                for r in page_raw:
                    url = self.clean_url(r.get('url', ''))
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    name, title = self._parse_title_text(r.get('title', ''))
                    desc = re.sub(r'<[^>]+>', '', r.get('description', ''))
                    company = ''
                    loc = ''
                    mc = re.search(r'Experience:\s*([^·]+)', desc)
                    if mc:
                        company = mc.group(1).strip().rstrip('.')
                    ml = re.search(r'Location:\s*([^·]+)', desc)
                    if ml:
                        loc = ml.group(1).strip().rstrip('.')
                    headline = ''
                    mh = re.match(r'^([^·]{3,80})·', desc)
                    if mh:
                        h = mh.group(1).strip()
                        if not h.startswith('Experience') and not h.startswith('Location'):
                            headline = h
                    item = self._make_item(url, name, headline or title, company)
                    item['location'] = loc
                    results.append(item)
                    new_count += 1

                total_pages_done += 1
                if self.on_progress:
                    self.on_progress(total_pages_done,
                                     len(queries) * pages_per_query,
                                     len(results), 'linkedin_brave')

                # 這頁沒新結果，跳下一組查詢
                if new_count == 0:
                    break

        logger.info(f"Brave LinkedIn: {len(results)} 筆 (來自 {len(queries)} 組查詢)")
        return {'success': True, 'data': results}

    # ── 4 層備援主控 ─────────────────────────────────────────

    def search_with_fallback(self, skills: list, location_zh: str, location_en: str,
                             pages: int, brave_key: str = None,
                             browser=None, job_title: str = '',
                             primary_skills: list = None,
                             secondary_skills: list = None) -> dict:
        all_data = []
        seen = set()
        source_used = []
        google_blocked = False

        def add_results(items):
            for item in items:
                url = item['linkedin_url']
                if url not in seen:
                    seen.add(url)
                    all_data.append(item)

        # ── 第 1 優先: Brave API（付費 API，最穩定） ──
        if brave_key:
            logger.info(f"LinkedIn: [1] Brave API 搜尋 (job={job_title})...")
            brave_result = self.search_via_brave(
                skills, brave_key, location_en, pages,
                job_title=job_title,
                primary_skills=primary_skills,
                secondary_skills=secondary_skills,
            )
            add_results(brave_result.get('data', []))
            source_used.append('brave')
            logger.info(f"Brave: {len(all_data)} 筆")

        # ── 第 2 優先: Bing（免費、不易被封） ──
        if len(all_data) < self.min_results:
            logger.info(f"LinkedIn: [2] Bing 搜尋 (目前 {len(all_data)} 筆，需 {self.min_results})...")
            bing_result = self.search_via_bing(skills, location_en, pages)
            add_results(bing_result.get('data', []))
            source_used.append('bing')
            logger.info(f"Bing 補充後: {len(all_data)} 筆")

        # ── 第 3 優先: Playwright (Google) ──
        if len(all_data) < self.min_results:
            logger.info("LinkedIn: [3] 嘗試 Playwright...")
            pw_result = self.search_via_playwright(skills, location_zh, pages, browser)
            add_results(pw_result.get('data', []))
            google_blocked = pw_result.get('google_blocked', False)
            if pw_result.get('success'):
                source_used.append('playwright')

        # ── 第 4 優先: Google urllib — Google 沒被封才試 ──
        if not google_blocked and len(all_data) < self.min_results:
            logger.info("LinkedIn: [4] 嘗試 Google urllib...")
            google_result = self.search_via_google(skills, location_zh, pages)
            add_results(google_result.get('data', []))
            source_used.append('google')
            google_blocked = google_result.get('google_blocked', False)

        source_str = '+'.join(source_used)
        if google_blocked:
            logger.info(f"LinkedIn: Google 被封鎖中 (冷卻 {_GOOGLE_COOLDOWN_SECS}s)")
        logger.info(f"LinkedIn 最終 ({source_str}): {len(all_data)} 筆")
        return {'success': True, 'data': all_data, 'source': source_str}
