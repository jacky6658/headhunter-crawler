"""
CakeResume 搜尋模組 — 台灣工程師 portfolio 平台

策略:
  Layer 1: Brave API Dorking → site:cake.me "skill" "location"
  Layer 2: Profile 頁面解析 → __NEXT_DATA__ JSON 提取結構化資料
"""
import json
import logging
import re
import time
from typing import Callable, List, Optional
from urllib.parse import quote, urlencode

logger = logging.getLogger(__name__)

# 支援新舊域名: cake.me 和 cakeresume.com
CAKE_PROFILE_PATTERN = re.compile(r'https?://(?:www\.)?(?:cake\.me/me|cakeresume\.com)/([\w\-.]+)(?:\?|$)')
CAKE_PORTFOLIO_PATTERN = re.compile(r'https?://(?:www\.)?(?:cake\.me|cakeresume\.com)/portfolios/([\w\-.]+)')
CAKE_SEARCH_PATTERN = re.compile(r'https?://(?:www\.)?cakeresume\.com/search/')
GITHUB_URL_PATTERN = re.compile(r'https?://github\.com/([\w\-.]+)/?')
LINKEDIN_URL_PATTERN = re.compile(r'https?://(?:www\.)?linkedin\.com/in/([\w\-]+)/?')


class CakeResumeSearcher:
    """CakeResume 人才搜尋"""

    def __init__(self, config: dict, anti_detect, stop_event=None):
        self.config = config
        self.ad = anti_detect
        self.stop_event = stop_event
        self.on_progress: Optional[Callable] = None

        cake_cfg = config.get('crawler', {}).get('cakeresume', {})
        self.enabled = cake_cfg.get('enabled', True)
        self.max_results = cake_cfg.get('max_results_per_query', 20)
        self.profile_scrape = cake_cfg.get('profile_scrape', True)
        self.delay_min = cake_cfg.get('delay', {}).get('min', 1.0)
        self.delay_max = cake_cfg.get('delay', {}).get('max', 3.0)

    def _is_stopped(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def _delay(self):
        """隨機延遲"""
        import random
        time.sleep(random.uniform(self.delay_min, self.delay_max))

    # ── Algolia 搜尋（主力）──────────────────────────────────

    ALGOLIA_APP_ID = '966RG9M3EK'

    def _get_algolia_key(self) -> str:
        """從 CakeResume 頁面取得 Algolia 搜尋 key（每次有效期有限）"""
        import ssl
        from urllib.request import Request, urlopen
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = Request('https://www.cake.me/talent-search',
                          headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'})
            resp = urlopen(req, timeout=15, context=ctx)
            html = resp.read().decode()
            match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
                key = data.get('props', {}).get('pageProps', {}).get('initialState', {}).get(
                    'auth', {}).get('kickoff', {}).get('algolia', {}).get('key', '')
                if key:
                    logger.info(f"CakeResume Algolia key 取得成功 (len={len(key)})")
                    return key
        except Exception as e:
            logger.warning(f"CakeResume Algolia key 取得失敗: {e}")
        return ''

    def _algolia_search(self, skills: list, location: str = 'Taiwan',
                        max_results: int = 20) -> List[dict]:
        """用 Algolia API 直接搜尋 CakeResume 人才"""
        import ssl
        from urllib.request import Request, urlopen

        key = self._get_algolia_key()
        if not key:
            logger.warning("CakeResume: 無 Algolia key，跳過")
            return []

        url = f'https://{self.ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes/Item/query'
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        # 多組搜尋: 技能組合 + 地區
        all_hits = []
        seen_ids = set()
        queries = [
            f'{skills[0]} {location}' if skills else location,
            ' '.join(skills[:2]) if len(skills) >= 2 else '',
            f'{skills[0]} engineer {location}' if skills else '',
        ]
        queries = [q for q in queries if q]  # 移除空字串

        for q in queries:
            if self._is_stopped() or len(all_hits) >= max_results:
                break

            body = json.dumps({
                'query': q,
                'hitsPerPage': min(20, max_results - len(all_hits)),
            }).encode()

            req = Request(url, method='POST', data=body, headers={
                'X-Algolia-Application-Id': self.ALGOLIA_APP_ID,
                'X-Algolia-API-Key': key,
                'Content-Type': 'application/json',
            })

            try:
                resp = urlopen(req, timeout=10, context=ctx)
                data = json.loads(resp.read())
                hits = data.get('hits', [])
                logger.info(f"CakeResume Algolia [{q[:40]}]: {len(hits)} hits (total: {data.get('nbHits', 0)})")

                for h in hits:
                    uid = h.get('user_id') or h.get('user', {}).get('id')
                    if uid and uid in seen_ids:
                        continue
                    if uid:
                        seen_ids.add(uid)
                    all_hits.append(h)

            except Exception as e:
                logger.warning(f"CakeResume Algolia error: {e}")
                break

            time.sleep(0.5)

        # 轉為候選人格式
        candidates = []
        for h in all_hits:
            user = h.get('user', {})
            username = h.get('path', '') or user.get('username', '')
            name = user.get('name', '') or username
            if not name:
                continue

            cake_url = f"https://www.cake.me/me/{username}" if username else ''
            locations = user.get('desired_locations', [])
            loc_str = locations[0] if locations else ''

            candidates.append({
                'source': 'cakeresume',
                'name': name,
                'cakeresume_url': cake_url,
                'github_url': '', 'github_username': '',
                'linkedin_url': '', 'linkedin_username': '',
                'email': '',
                'location': loc_str,
                'bio': (h.get('body_plain_text_truncated', '') or user.get('description', '') or '')[:200],
                'company': '',
                'title': h.get('title', ''),
                'skills': h.get('tag_list', []) or self._extract_skills_from_text(
                    h.get('body_plain_text_truncated', '')),
                'public_repos': 0, 'followers': 0,
                'recent_push': '', 'top_repos': [],
                'total_stars': 0, 'score_factors': {},
                'tech_stack': h.get('tag_list', []),
                'top_repos_detail': [], 'languages': {},
                'is_profile': True,
            })

        logger.info(f"CakeResume Algolia 完成: {len(candidates)} 位候選人")
        return candidates

    # ── GitHub 交叉搜尋（用 CakeResume username 找 GitHub 帳號 → email）──

    def _cross_search_github(self, candidates: list):
        """
        對每個 CakeResume 候選人，用 username 去 GitHub 搜尋同名帳號
        找到就提取 email（.patch 方法）和 GitHub URL
        """
        import os
        tokens = os.environ.get('GITHUB_TOKENS', '').split(',')
        gh_headers = {'Accept': 'application/vnd.github.v3+json'}
        if tokens and tokens[0].strip():
            gh_headers['Authorization'] = f'token {tokens[0].strip()}'

        found = 0
        for c in candidates[:15]:  # 最多查前 15 個
            if self._is_stopped():
                break
            if c.get('github_url'):  # 已有 GitHub 連結的跳過
                continue

            cake_username = c.get('cakeresume_url', '').rstrip('/').split('/')[-1]
            if not cake_username:
                continue

            # 1. 直接用 CakeResume username 查 GitHub（很多人兩邊用同一個 username）
            try:
                data, status = self.ad.http_get_json(
                    f'https://api.github.com/users/{cake_username}',
                    extra_headers=gh_headers, timeout=8,
                )
                if status == 200 and data.get('type') != 'Organization':
                    c['github_url'] = data.get('html_url', '')
                    c['github_username'] = data.get('login', '')
                    c['email'] = data.get('email', '') or ''

                    # 用 .patch 提取 email（如果 API 沒給）
                    if not c['email']:
                        try:
                            from crawler.github import GitHubSearcher
                            email = GitHubSearcher._extract_email_static(
                                cake_username, self.ad)
                            if email:
                                c['email'] = email
                        except Exception:
                            pass

                    if c['github_url']:
                        found += 1
                        logger.info(f"CakeResume→GitHub: {c.get('name')} → {c['github_url']} "
                                   f"email={c.get('email', '')}")
                    time.sleep(0.3)
                    continue
            except Exception:
                pass

            # 2. 搜尋 GitHub（用名字的英文部分）
            name = c.get('name', '')
            # 提取英文名字部分（如果有）
            import re as _re
            eng_parts = _re.findall(r'[A-Za-z]+', name)
            if eng_parts and len(eng_parts) >= 2:
                search_name = ' '.join(eng_parts[:2])
                try:
                    data2, s2 = self.ad.http_get_json(
                        f'https://api.github.com/search/users?q={search_name}+location:Taiwan&per_page=1',
                        extra_headers=gh_headers, timeout=8,
                    )
                    if s2 == 200 and data2.get('total_count', 0) > 0:
                        user = data2['items'][0]
                        # 名字匹配驗證（避免誤配）
                        gh_name = (user.get('name') or user.get('login', '')).lower()
                        if any(p.lower() in gh_name for p in eng_parts[:2]):
                            c['github_url'] = user.get('html_url', '')
                            c['github_username'] = user.get('login', '')
                            found += 1
                            logger.info(f"CakeResume→GitHub (name): {name} → {c['github_url']}")
                    time.sleep(0.3)
                except Exception:
                    pass

        if found:
            logger.info(f"CakeResume→GitHub 交叉搜尋: {found}/{len(candidates)} 找到 GitHub 帳號")

    # ── Brave API 搜尋 CakeResume（備援）──────────────────────

    def _build_queries(self, skills: list, location: str, job_title: str = '',
                       company_queries: list = None) -> List[str]:
        """
        建構 Brave 搜尋 query（搜 CakeResume 站內）
        使用 cakeresume.com（舊域名，Brave 索引較完整）
        """
        queries = []

        # Query 1: 搜整站 + 技能（不限 /me/ 路徑，因為 Brave 索引不到）
        if skills:
            skill_str = ' '.join(skills[:3])
            queries.append(f'site:cakeresume.com {skill_str} {location} engineer')

        # Query 2: 直接搜人才（不用 site: 限制）
        if skills:
            queries.append(f'cakeresume {" ".join(skills[:2])} {location} portfolio')

        # Query 3: 職缺名稱
        if job_title:
            en_title = re.sub(r'[（(][^）)]*[）)]', '', job_title).strip()
            if en_title:
                queries.append(f'site:cakeresume.com {en_title} {location}')

        # Query 4: 公司策略注入
        if company_queries:
            for cq in company_queries[:3]:
                queries.append(f'site:cakeresume.com {cq}')

        # 去重 + 長度限制
        seen = set()
        unique = []
        for q in queries:
            if q not in seen and len(q) <= 380:
                seen.add(q)
                unique.append(q)

        return unique

    def _brave_search(self, queries: list, brave_key: str) -> List[dict]:
        """用 Brave API 搜尋 CakeResume profile URLs"""
        if not brave_key:
            return []

        endpoint = "https://api.search.brave.com/res/v1/web/search"
        headers = {
            'Accept': 'application/json',
            'Accept-Encoding': 'gzip',
            'X-Subscription-Token': brave_key,
        }

        results = []
        seen_urls = set()

        for qi, query in enumerate(queries):
            if self._is_stopped():
                break

            logger.info(f"CakeResume Brave [{qi+1}/{len(queries)}]: {query}")
            params = urlencode({'q': query, 'count': self.max_results})
            data, status = self.ad.http_get_json(
                f"{endpoint}?{params}", extra_headers=headers)

            if status == 429:
                logger.warning("CakeResume Brave: rate limited (429)")
                break
            if status != 200:
                logger.warning(f"CakeResume Brave: HTTP {status}")
                continue

            self.ad.github_delay()  # 重用現有延遲

            for r in data.get('web', {}).get('results', []):
                url = r.get('url', '')
                clean_url = url.split('?')[0].rstrip('/')

                # 匹配: cake.me/me/{user}, cakeresume.com/{user}, */portfolios/{slug}
                profile_match = CAKE_PROFILE_PATTERN.search(clean_url)
                portfolio_match = CAKE_PORTFOLIO_PATTERN.search(clean_url)

                # 也匹配 cakeresume.com/{username} (無 /jobs, /companies, /search 前綴)
                direct_match = None
                if not profile_match and not portfolio_match:
                    dm = re.match(r'https?://(?:www\.)?cakeresume\.com/([\w\-]+)$', clean_url)
                    if dm and dm.group(1) not in ('jobs', 'companies', 'search', 'portfolios',
                                                    'about', 'pricing', 'employers', 'resources'):
                        direct_match = dm

                if not profile_match and not portfolio_match and not direct_match:
                    continue

                if clean_url in seen_urls:
                    continue
                seen_urls.add(clean_url)

                username = (profile_match or portfolio_match or direct_match).group(1)
                name, title = self._parse_brave_title(r.get('title', ''))
                desc = re.sub(r'<[^>]+>', '', r.get('description', ''))

                # 轉換為可抓取的 URL（新域名）
                scrape_url = clean_url
                if 'cakeresume.com' in scrape_url:
                    scrape_url = scrape_url.replace('cakeresume.com', 'cake.me')
                    if '/me/' not in scrape_url and '/portfolios/' not in scrape_url:
                        scrape_url = f"https://www.cake.me/me/{username}"

                is_profile = bool(profile_match or direct_match)

                results.append({
                    'cake_url': scrape_url,
                    'cake_username': username,
                    'name': name,
                    'title': title,
                    'description': desc,
                    'is_profile': is_profile,
                })

        logger.info(f"CakeResume Brave: {len(results)} profiles found")
        return results

    @staticmethod
    def _parse_brave_title(raw: str) -> tuple:
        """解析 Brave 搜尋結果的標題"""
        # 格式通常: "名字的作品集 | CakeResume" 或 "名字 - 職稱"
        text = re.sub(r'\s*[\|｜]\s*CakeResume.*$', '', raw, flags=re.IGNORECASE).strip()
        text = re.sub(r'\s*的作品集\s*$', '', text).strip()
        text = re.sub(r'\s*的履歷\s*$', '', text).strip()

        m = re.match(r'^(.+?)\s*[-–]\s*(.+)$', text)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        return text, ''

    # ── Profile 頁面解析 ──────────────────────────────────

    def scrape_profile(self, cake_url: str) -> Optional[dict]:
        """
        GET CakeResume profile 頁面，解析 __NEXT_DATA__ 取得結構化資料
        回傳標準 candidate dict 或 None
        """
        try:
            html, _status = self.ad.http_get(cake_url, timeout=15)
            if not html:
                return None

            # 快照存檔（即使後續解析失敗也有 raw HTML）
            try:
                from crawler.snapshot import save_snapshot
                username = cake_url.rstrip('/').split('/')[-1]
                save_snapshot(html, username, source='cakeresume', url=cake_url)
            except Exception:
                pass

            # 解析 __NEXT_DATA__
            match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if not match:
                logger.debug(f"CakeResume: 無 __NEXT_DATA__: {cake_url}")
                return self._fallback_parse(html, cake_url)

            data = json.loads(match.group(1))
            ssr = data.get('props', {}).get('pageProps', {}).get('ssr', {})
            profile = ssr.get('profile', {})

            if not profile:
                return None

            name = profile.get('name', '')
            description = profile.get('description', '')
            meta = profile.get('meta_tags', {})
            meta_desc = meta.get('description', '')

            # 從 description 和 meta 提取資訊
            full_text = f"{description} {meta_desc}"

            # 提取 GitHub URL
            github_url = ''
            github_username = ''
            gh_match = GITHUB_URL_PATTERN.search(full_text)
            if not gh_match:
                gh_match = GITHUB_URL_PATTERN.search(html)
            if gh_match:
                github_username = gh_match.group(1)
                github_url = f"https://github.com/{github_username}"

            # 提取 LinkedIn URL
            linkedin_url = ''
            linkedin_username = ''
            li_match = LINKEDIN_URL_PATTERN.search(full_text)
            if not li_match:
                li_match = LINKEDIN_URL_PATTERN.search(html)
            if li_match:
                linkedin_username = li_match.group(1)
                linkedin_url = f"https://www.linkedin.com/in/{linkedin_username}/"

            # 提取技能關鍵字（從 description）
            skills = self._extract_skills_from_text(full_text)

            # 提取 email
            email = ''
            email_match = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', full_text)
            if email_match:
                email = email_match.group(0)

            # 提取 location
            location = ''
            for loc_kw in ['Taipei', 'Taiwan', '台北', '台灣', 'Hsinchu', 'Taichung', '新竹', '台中']:
                if loc_kw.lower() in full_text.lower():
                    location = loc_kw
                    break

            return {
                'source': 'cakeresume',
                'name': name,
                'cakeresume_url': cake_url,
                'github_url': github_url,
                'github_username': github_username,
                'linkedin_url': linkedin_url,
                'linkedin_username': linkedin_username,
                'email': email,
                'location': location,
                'bio': (meta_desc or description or '')[:200],
                'company': '',
                'title': '',
                'skills': skills,
                'public_repos': 0,
                'followers': 0,
                'recent_push': '',
                'top_repos': [],
                'total_stars': 0,
                'score_factors': {},
                'tech_stack': skills,  # CakeResume 的 skills 同時作為 tech_stack
                'top_repos_detail': [],
                'languages': {},
            }

        except json.JSONDecodeError:
            logger.warning(f"CakeResume: __NEXT_DATA__ JSON 解析失敗: {cake_url}")
            return None
        except Exception as e:
            logger.error(f"CakeResume scrape error ({cake_url}): {e}")
            return None

    def _fallback_parse(self, html: str, cake_url: str) -> Optional[dict]:
        """__NEXT_DATA__ 不存在時，從 meta tags 和 HTML 提取基礎資料"""
        name = ''
        desc = ''

        # og:title / twitter:title
        title_match = re.search(r'<meta[^>]*property="og:title"[^>]*content="([^"]*)"', html)
        if title_match:
            raw = title_match.group(1)
            name = re.sub(r'\s*[\|｜].*$', '', raw).strip()
            name = re.sub(r'\s*的作品集\s*$', '', name).strip()

        desc_match = re.search(r'<meta[^>]*(?:name|property)="(?:og:)?description"[^>]*content="([^"]*)"', html)
        if desc_match:
            desc = desc_match.group(1)[:200]

        if not name:
            return None

        return {
            'source': 'cakeresume',
            'name': name,
            'cakeresume_url': cake_url,
            'github_url': '', 'github_username': '',
            'linkedin_url': '', 'linkedin_username': '',
            'email': '',
            'location': '',
            'bio': desc,
            'company': '', 'title': '',
            'skills': self._extract_skills_from_text(desc),
            'public_repos': 0, 'followers': 0,
            'recent_push': '', 'top_repos': [],
            'total_stars': 0, 'score_factors': {},
            'tech_stack': [], 'top_repos_detail': [], 'languages': {},
        }

    # ── 技能提取 ──────────────────────────────────────────

    # 常見技術關鍵字（用於從非結構化文字中提取）
    _TECH_KEYWORDS = {
        'python', 'java', 'javascript', 'typescript', 'golang', 'go',
        'rust', 'c++', 'c#', 'ruby', 'php', 'swift', 'kotlin', 'scala',
        'react', 'vue', 'angular', 'next.js', 'nuxt', 'svelte',
        'node.js', 'express', 'fastapi', 'django', 'flask', 'spring',
        'docker', 'kubernetes', 'k8s', 'aws', 'gcp', 'azure',
        'postgresql', 'mysql', 'mongodb', 'redis', 'elasticsearch',
        'graphql', 'grpc', 'rest', 'microservices',
        'terraform', 'ansible', 'jenkins', 'ci/cd',
        'machine learning', 'deep learning', 'nlp', 'pytorch', 'tensorflow',
        'linux', 'git', 'agile', 'scrum',
        'html', 'css', 'sass', 'tailwind',
        'ios', 'android', 'flutter', 'react native',
    }

    @classmethod
    def _extract_skills_from_text(cls, text: str) -> List[str]:
        """從非結構化文字中提取技術關鍵字"""
        if not text:
            return []
        text_lower = text.lower()
        found = []
        for kw in cls._TECH_KEYWORDS:
            if kw in text_lower:
                found.append(kw)
        return found

    # ── 主搜尋方法 ────────────────────────────────────────

    def search(self, skills: list, location: str, pages: int = 1,
               brave_key: str = '', job_title: str = '',
               company_queries: list = None) -> dict:
        """
        CakeResume 搜尋主入口

        Returns: {'success': bool, 'data': [candidate_dict, ...]}
        """
        if not self.enabled:
            return {'success': True, 'data': []}

        # Layer 0 (主力): Algolia 直接搜尋
        algolia_candidates = self._algolia_search(skills, location, max_results=self.max_results)
        if algolia_candidates:
            # 用 CakeResume username 交叉搜尋 GitHub，補充聯繫方式
            self._cross_search_github(algolia_candidates)

            logger.info(f"CakeResume 完成 (Algolia): {len(algolia_candidates)} 位候選人")
            return {'success': True, 'data': algolia_candidates}

        # Layer 1 (備援): Brave API Dorking
        queries = self._build_queries(skills, location, job_title, company_queries)
        brave_results = self._brave_search(queries, brave_key)

        # Layer 2: Profile 頁面解析（如果啟用）
        candidates = []
        for i, br in enumerate(brave_results):
            if self._is_stopped():
                break

            cake_url = br['cake_url']

            if self.profile_scrape and br.get('is_profile'):
                # 完整解析 profile 頁面
                profile = self.scrape_profile(cake_url)
                if profile:
                    # 用 Brave 的名字作為 fallback
                    if not profile['name'] and br.get('name'):
                        profile['name'] = br['name']
                    candidates.append(profile)
                    self._delay()
                else:
                    # 解析失敗，用 Brave 的基礎資料
                    candidates.append(self._make_stub(br))
            else:
                # 不做 profile scrape，直接用 Brave 資料
                candidates.append(self._make_stub(br))

            if self.on_progress:
                self.on_progress(i + 1, len(brave_results), len(candidates), 'cakeresume')

        logger.info(f"CakeResume 完成: {len(candidates)} 位候選人 "
                    f"(Brave {len(brave_results)}, scraped {sum(1 for c in candidates if c.get('skills'))})")

        return {'success': True, 'data': candidates}

    @staticmethod
    def _make_stub(brave_result: dict) -> dict:
        """從 Brave 搜尋結果建立簡單的 candidate stub"""
        return {
            'source': 'cakeresume',
            'name': brave_result.get('name', ''),
            'cakeresume_url': brave_result.get('cake_url', ''),
            'github_url': '', 'github_username': '',
            'linkedin_url': '', 'linkedin_username': '',
            'email': '',
            'location': '',
            'bio': brave_result.get('description', '')[:200],
            'company': '',
            'title': brave_result.get('title', ''),
            'skills': [],
            'public_repos': 0, 'followers': 0,
            'recent_push': '', 'top_repos': [],
            'total_stars': 0, 'score_factors': {},
            'tech_stack': [], 'top_repos_detail': [], 'languages': {},
        }
