"""
GitHub API 搜尋模組 — 支援多 token 輪換 + 全維度深度分析
來源: search-plan-executor.py L112-401
"""
import logging
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Callable, Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class GitHubSearcher:
    """GitHub 人才搜尋，支援多 token 輪換"""

    def __init__(self, config: dict, anti_detect):
        self.config = config
        self.ad = anti_detect

        crawler_cfg = config.get('crawler', {})
        gh_cfg = crawler_cfg.get('github', {})

        self.tokens = config.get('api_keys', {}).get('github_tokens', [])
        self._current_token_idx = 0
        self.max_workers = gh_cfg.get('max_workers', 4)
        self.sample_per_page = crawler_cfg.get('sample_per_page', 5)

        # GitHub 合法語言列表（從 config 載入）
        self.github_languages = set(gh_cfg.get('languages', []))

        self.on_progress: Optional[Callable] = None

    # ── Token 管理 ───────────────────────────────────────────

    @property
    def current_token(self) -> Optional[str]:
        if not self.tokens:
            return None
        return self.tokens[self._current_token_idx % len(self.tokens)]

    def rotate_token(self):
        """403 時切換下一個 token"""
        if len(self.tokens) <= 1:
            return
        old_idx = self._current_token_idx
        self._current_token_idx = (self._current_token_idx + 1) % len(self.tokens)
        logger.info(f"GitHub token 輪換: {old_idx} → {self._current_token_idx}")

    def get_headers(self, token: str = None) -> dict:
        h = {'Accept': 'application/vnd.github.v3+json'}
        t = token or self.current_token
        if t:
            h['Authorization'] = f'token {t}'
        return h

    def check_rate_limit(self, token: str = None) -> tuple:
        """回傳 (remaining, limit, reset_timestamp)"""
        headers = self.get_headers(token)
        data, status = self.ad.http_get_json(
            f"{GITHUB_API}/rate_limit",
            extra_headers=headers,
            timeout=10,
        )
        if status == 401 and 'Authorization' in headers:
            # Token 無效，降級到無 token 模式
            logger.warning("GitHub token 無效 (401)，降級為無 token 模式")
            self.tokens = []
            data, status = self.ad.http_get_json(
                f"{GITHUB_API}/rate_limit",
                extra_headers=self.get_headers(),
                timeout=10,
            )
        rate = data.get('rate', {})
        return rate.get('remaining', 0), rate.get('limit', 60), rate.get('reset', 0)

    # ── 查詢建構 ─────────────────────────────────────────────

    def _is_github_language(self, skill: str) -> bool:
        return skill.lower().strip() in self.github_languages

    def build_queries(self, skills: list, location: str) -> list:
        """建構 GitHub search queries — 多組策略"""
        queries = []
        seen = set()
        lang_skills = [s for s in skills if self._is_github_language(s)]
        kw_skills = [s for s in skills if not self._is_github_language(s)]

        def add(q):
            if q not in seen:
                seen.add(q)
                queries.append(q)

        # 策略 1: 語言 + 地區 (寬鬆)
        for lang in lang_skills[:2]:
            add(f'language:{lang} location:{location}')

        # 策略 2: 語言 + 關鍵字 (精準)
        if lang_skills and kw_skills:
            kw = ' '.join(f'"{k}"' for k in kw_skills[:2])
            add(f'{kw} language:{lang_skills[0]} location:{location}')

        # 策略 3: 關鍵字搜 bio (無語言職缺也能用)
        if kw_skills:
            for kw in kw_skills[:3]:
                add(f'"{kw}" in:bio location:{location}')

        # 策略 4: 所有關鍵字 OR (最寬鬆)
        if kw_skills and len(kw_skills) >= 2:
            kw_or = ' '.join(f'"{k}"' for k in kw_skills[:3])
            add(f'{kw_or} location:{location}')

        return queries or [f'location:{location}']

    # ── 搜尋 ────────────────────────────────────────────────

    def _search_page(self, query: str, page: int, gh_headers: dict) -> tuple:
        """搜尋單頁，回傳 (items, rate_limited)"""
        try:
            self.ad.github_delay()
            params = urlencode({
                'q': query, 'per_page': 30, 'page': page, 'sort': 'followers',
            })
            data, status = self.ad.http_get_json(
                f"{GITHUB_API}/search/users?{params}",
                extra_headers=gh_headers,
                timeout=15,
            )
            if status == 403:
                # 嘗試輪換 token
                self.rotate_token()
                return None, True
            if status != 200:
                logger.warning(f"GitHub search HTTP {status} page {page}")
                return [], False
            items = data.get('items', [])
            if len(items) > self.sample_per_page:
                items = random.sample(items, self.sample_per_page)
            return items, False
        except Exception as e:
            logger.error(f"GitHub search page {page} error: {e}")
            return [], False

    def fetch_user_detail(self, username: str, gh_headers: dict = None) -> Optional[dict]:
        """抓取 GitHub 用戶詳細資料"""
        if gh_headers is None:
            gh_headers = self.get_headers()
        try:
            user, s1 = self.ad.http_get_json(
                f"{GITHUB_API}/users/{username}",
                extra_headers=gh_headers, timeout=10,
            )
            if s1 == 403:
                self.rotate_token()
                gh_headers = self.get_headers()
                user, s1 = self.ad.http_get_json(
                    f"{GITHUB_API}/users/{username}",
                    extra_headers=gh_headers, timeout=10,
                )
            if s1 != 200:
                return None

            params = urlencode({'sort': 'updated', 'per_page': 10, 'type': 'owner'})
            repos, s2 = self.ad.http_get_json(
                f"{GITHUB_API}/users/{username}/repos?{params}",
                extra_headers=gh_headers, timeout=10,
            )
            if s2 != 200:
                repos = []

            languages = list({r.get('language') for r in repos if r.get('language')})
            recent_push = repos[0].get('pushed_at', '') if repos else ''
            top_repos = [r.get('name', '') for r in repos[:5]]

            return {
                'source': 'github',
                'name': user.get('name') or username,
                'github_url': user.get('html_url', f'https://github.com/{username}'),
                'github_username': username,
                'linkedin_url': '',
                'linkedin_username': '',
                'location': user.get('location', '') or '',
                'bio': user.get('bio', '') or '',
                'company': (user.get('company', '') or '').lstrip('@').strip(),
                'email': user.get('email', '') or '',
                'public_repos': user.get('public_repos', 0),
                'followers': user.get('followers', 0),
                'skills': languages,
                'recent_push': recent_push,
                'top_repos': top_repos,
            }
        except Exception as e:
            logger.error(f"GitHub detail error ({username}): {e}")
            return None

    def deep_analyze(self, username: str, gh_headers: dict = None) -> Optional[dict]:
        """
        GitHub 全維度深度分析 — 取得完整開發者畫像

        API 呼叫 (3-4 次):
        1. GET /users/{username}                           — 基本資料
        2. GET /users/{username}/repos?per_page=100&sort=stars — 完整 repo 列表
        3. GET /users/{username}/events?per_page=100        — 近期活動

        返回增強的用戶資料，包含:
        - 完整語言分佈（百分比）
        - Star 總數
        - 近 90 天活躍度
        - 技術棧推斷（從 repo 描述 + topic）
        - 評分因子
        """
        if gh_headers is None:
            gh_headers = self.get_headers()

        try:
            # 1. 基本用戶資料
            self.ad.github_delay()
            user, s1 = self.ad.http_get_json(
                f"{GITHUB_API}/users/{username}",
                extra_headers=gh_headers, timeout=10,
            )
            if s1 == 403:
                self.rotate_token()
                gh_headers = self.get_headers()
                user, s1 = self.ad.http_get_json(
                    f"{GITHUB_API}/users/{username}",
                    extra_headers=gh_headers, timeout=10,
                )
            if s1 != 200:
                logger.warning(f"GitHub deep_analyze 用戶 API 失敗 ({username}): {s1}")
                return None

            # 2. 完整 repo 列表（按 star 排序，最多 100 個）
            self.ad.github_delay()
            params = urlencode({
                'sort': 'stars', 'direction': 'desc',
                'per_page': 100, 'type': 'owner',
            })
            repos, s2 = self.ad.http_get_json(
                f"{GITHUB_API}/users/{username}/repos?{params}",
                extra_headers=gh_headers, timeout=15,
            )
            if s2 != 200:
                repos = []

            # 3. 近期活動（events）
            self.ad.github_delay()
            events, s3 = self.ad.http_get_json(
                f"{GITHUB_API}/users/{username}/events?per_page=100",
                extra_headers=gh_headers, timeout=15,
            )
            if s3 != 200:
                events = []

            # ── 分析語言分佈 ──
            lang_count = {}
            for repo in repos:
                lang = repo.get('language')
                if lang:
                    lang_count[lang] = lang_count.get(lang, 0) + 1
            total_lang_repos = sum(lang_count.values()) or 1
            languages = {
                lang: {
                    'repo_count': count,
                    'percentage': round(count / total_lang_repos * 100),
                }
                for lang, count in sorted(
                    lang_count.items(), key=lambda x: x[1], reverse=True
                )
            }
            primary_language = max(lang_count, key=lang_count.get) if lang_count else ''
            all_languages = list(lang_count.keys())

            # ── Star 總數 ──
            total_stars = sum(r.get('stargazers_count', 0) for r in repos)

            # ── Top repos（按 star 排序）──
            top_repos_detail = []
            for r in sorted(repos, key=lambda x: x.get('stargazers_count', 0),
                            reverse=True)[:5]:
                top_repos_detail.append({
                    'name': r.get('name', ''),
                    'stars': r.get('stargazers_count', 0),
                    'language': r.get('language', ''),
                    'description': (r.get('description', '') or '')[:100],
                    'topics': r.get('topics', []),
                })

            # ── 活躍度分析（近 90 天）──
            now = datetime.now()
            cutoff_90d = now - timedelta(days=90)
            cutoff_6m = now - timedelta(days=180)

            push_count_90d = 0
            active_repos_90d = set()
            last_push = ''

            for event in events:
                event_type = event.get('type', '')
                created = event.get('created_at', '')
                if not created:
                    continue
                try:
                    event_date = datetime.strptime(created[:19], '%Y-%m-%dT%H:%M:%S')
                except (ValueError, TypeError):
                    continue

                if not last_push or created > last_push:
                    last_push = created[:10]

                if event_date >= cutoff_90d:
                    if event_type in ('PushEvent', 'CreateEvent', 'PullRequestEvent'):
                        push_count_90d += 1
                        repo_name = event.get('repo', {}).get('name', '')
                        if repo_name:
                            active_repos_90d.add(repo_name)

            # 如果 events 沒有 push 記錄，從 repos 的 pushed_at 取
            if not last_push and repos:
                for r in repos:
                    pushed = r.get('pushed_at', '')
                    if pushed and (not last_push or pushed > last_push):
                        last_push = pushed[:10]

            is_active = False
            if last_push:
                try:
                    lp = datetime.strptime(last_push[:10], '%Y-%m-%d')
                    is_active = lp >= cutoff_6m
                except (ValueError, TypeError):
                    pass

            # ── 技術棧推斷（從 repo 描述 + topic + 名稱）──
            tech_stack_set = set()
            # 從語言
            tech_stack_set.update(all_languages)
            # 從 repo topics
            for r in repos[:20]:
                topics = r.get('topics', [])
                if isinstance(topics, list):
                    tech_stack_set.update(topics)
            # 從 repo 描述和名稱（尋找常見技術棧關鍵字）
            tech_keywords = [
                'fastapi', 'django', 'flask', 'spring', 'express',
                'react', 'vue', 'angular', 'next', 'nuxt',
                'docker', 'kubernetes', 'k8s', 'terraform', 'ansible',
                'postgresql', 'mysql', 'mongodb', 'redis', 'kafka',
                'graphql', 'rest', 'grpc', 'microservice',
                'aws', 'gcp', 'azure', 'ci/cd', 'jenkins',
                'prometheus', 'grafana', 'elasticsearch',
            ]
            for r in repos[:20]:
                desc = (r.get('description', '') or '').lower()
                name = (r.get('name', '') or '').lower()
                text = f"{desc} {name}"
                for kw in tech_keywords:
                    if kw in text:
                        tech_stack_set.add(kw)

            tech_stack = list(tech_stack_set)

            # ── 評分因子 ──
            has_quality = any(r.get('stargazers_count', 0) >= 10 for r in repos)
            score_factors = {
                'has_quality_repos': has_quality,
                'is_active_contributor': push_count_90d > 5,
                'language_diversity': len(lang_count),
                'community_influence': user.get('followers', 0) + total_stars,
                'repo_depth': user.get('public_repos', 0),
                'total_stars': total_stars,
            }

            # ── 組合結果 ──
            result = {
                'source': 'github',
                'name': user.get('name') or username,
                'github_url': user.get('html_url', f'https://github.com/{username}'),
                'github_username': username,
                'linkedin_url': '',
                'linkedin_username': '',
                'location': user.get('location', '') or '',
                'bio': user.get('bio', '') or '',
                'company': (user.get('company', '') or '').lstrip('@').strip(),
                'email': user.get('email', '') or '',
                'public_repos': user.get('public_repos', 0),
                'followers': user.get('followers', 0),
                'skills': all_languages,
                'recent_push': last_push,
                'top_repos': [r['name'] for r in top_repos_detail],

                # ── 深度分析額外欄位 ──
                'languages': languages,
                'primary_language': primary_language,
                'total_stars': total_stars,
                'top_repos_detail': top_repos_detail,
                'activity': {
                    'last_push': last_push,
                    'push_count_90d': push_count_90d,
                    'active_repos_90d': len(active_repos_90d),
                    'is_active': is_active,
                },
                'tech_stack': tech_stack,
                'score_factors': score_factors,
            }

            logger.info(f"GitHub deep_analyze 完成: {username} | "
                        f"語言={len(languages)} | Stars={total_stars} | "
                        f"90天push={push_count_90d} | 技術棧={len(tech_stack)}")
            return result

        except Exception as e:
            logger.error(f"GitHub deep_analyze error ({username}): {e}")
            return None

    def search_users(self, skills: list, location: str, pages: int = 10) -> dict:
        """搜尋 GitHub 用戶"""
        remaining, limit, _ = self.check_rate_limit()
        logger.info(f"GitHub rate limit: {remaining}/{limit}")
        if remaining < 10:
            return {
                'success': False,
                'rate_limit_warning': True,
                'data': [],
            }

        gh_headers = self.get_headers()
        seen_logins = set()
        all_logins = []

        queries = self.build_queries(skills, location)
        logger.info(f"GitHub queries: {queries}")

        for query in queries:
            for page in range(1, pages + 1):
                items, rate_limited = self._search_page(query, page, gh_headers)
                if rate_limited:
                    # 用新 token 重試
                    gh_headers = self.get_headers()
                    items, rate_limited = self._search_page(query, page, gh_headers)
                    if rate_limited:
                        return {
                            'success': False,
                            'rate_limit_warning': not bool(self.current_token),
                            'data': [],
                        }
                if not items:
                    break
                for user in items:
                    login = user.get('login', '')
                    if login and login not in seen_logins:
                        seen_logins.add(login)
                        all_logins.append(login)

                if self.on_progress:
                    self.on_progress(page, pages, len(all_logins), 'github')

        logger.info(f"GitHub: {len(all_logins)} 帳號，開始並行深度分析...")
        all_users = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_map = {
                executor.submit(self.deep_analyze, login, gh_headers): login
                for login in all_logins
            }
            for future in as_completed(future_map):
                detail = future.result()
                if detail:
                    # 基本驗證：名稱非空
                    if detail.get('name'):
                        all_users.append(detail)

        logger.info(f"GitHub 完成: {len(all_users)} 位")
        return {'success': True, 'data': all_users}
