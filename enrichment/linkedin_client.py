"""
LinkedIn API Client — 使用 linkedin-api 套件直接存取 LinkedIn Voyager API

優勢: 免費、結構化、完整資料 (experience, education, skills, contact info)
風險: LinkedIn 可能封鎖帳號，需謹慎使用

使用方式:
    client = LinkedInApiClient({'username': 'xxx', 'password': 'xxx', 'enabled': True})
    result = client.fetch_profile('https://www.linkedin.com/in/john-doe/')
"""

import logging
import re
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from urllib.parse import unquote

logger = logging.getLogger(__name__)


class LinkedInApiClient:
    """LinkedIn Voyager API 封裝 — 透過 linkedin-api 取得結構化候選人資料"""

    DEFAULT_MAX_REQUESTS_PER_HOUR = 80
    DEFAULT_COOLDOWN_SECONDS = 5

    def __init__(self, config: dict):
        """
        Args:
            config: enrichment.linkedin 設定區塊
                Keys: username, password, enabled, max_requests_per_hour,
                      request_cooldown_seconds, cookies_dir
        """
        self.config = config
        self.username = config.get('username', '')
        self.password = config.get('password', '')
        self.enabled = config.get('enabled', False)
        self.max_requests_per_hour = config.get(
            'max_requests_per_hour', self.DEFAULT_MAX_REQUESTS_PER_HOUR
        )
        self.request_cooldown = config.get(
            'request_cooldown_seconds', self.DEFAULT_COOLDOWN_SECONDS
        )
        self.cookies_dir = config.get('cookies_dir', '')

        # Lazy-init: 第一次呼叫時才認證
        self._api = None
        self._lock = threading.Lock()
        self._last_request_time = 0
        self._request_count_this_hour = 0
        self._hour_start = datetime.now()
        self._authenticated = False
        self._auth_error = None

        # 統計
        self._stats = {
            'calls': 0,
            'success': 0,
            'failed': 0,
            'auth_failures': 0,
            'rate_limited': 0,
            'session_start': datetime.now().isoformat(),
        }

    # ── 狀態查詢 ──────────────────────────────────────────────

    def is_available(self) -> bool:
        """檢查 LinkedIn API 是否已設定且啟用"""
        return bool(self.enabled and self.username and self.password)

    def is_authenticated(self) -> bool:
        """檢查是否已認證"""
        return self._authenticated and self._api is not None

    def get_auth_status(self) -> dict:
        """回傳認證狀態（供 UI 顯示）"""
        return {
            'enabled': self.enabled,
            'has_credentials': bool(self.username and self.password),
            'authenticated': self._authenticated,
            'auth_error': self._auth_error,
            'username_masked': self._mask_email(self.username) if self.username else '',
            'requests_this_hour': self._request_count_this_hour,
            'max_requests_per_hour': self.max_requests_per_hour,
        }

    def get_stats(self) -> dict:
        """回傳使用統計"""
        return {
            **self._stats,
            'authenticated': self._authenticated,
            'auth_error': self._auth_error,
            'requests_this_hour': self._request_count_this_hour,
            'max_per_hour': self.max_requests_per_hour,
            'cost': 0.0,  # 永遠免費
        }

    # ── 認證 ──────────────────────────────────────────────────

    @staticmethod
    def _mask_email(email: str) -> str:
        """遮蔽 email 顯示: j***e@gmail.com"""
        if not email or '@' not in email:
            return '***'
        local, domain = email.split('@', 1)
        if len(local) <= 2:
            return f'{local[0]}***@{domain}'
        return f'{local[0]}***{local[-1]}@{domain}'

    def _ensure_authenticated(self) -> bool:
        """Lazy 認證（含 cookie 快取），thread-safe"""
        if self._api is not None and self._authenticated:
            return True

        with self._lock:
            # Double-check inside lock
            if self._api is not None and self._authenticated:
                return True

            if not self.is_available():
                self._auth_error = 'LinkedIn 帳密未設定'
                return False

            try:
                from linkedin_api import Linkedin
                from linkedin_api.client import ChallengeException, UnauthorizedException

                logger.info(f"LinkedIn API: 嘗試認證 {self._mask_email(self.username)}")

                kwargs = {}
                if self.cookies_dir:
                    kwargs['cookies_dir'] = self.cookies_dir

                self._api = Linkedin(
                    self.username,
                    self.password,
                    **kwargs,
                )
                self._authenticated = True
                self._auth_error = None
                logger.info("LinkedIn API: 認證成功 ✓")
                return True

            except Exception as e:
                err_name = type(e).__name__
                if err_name == 'ChallengeException':
                    self._auth_error = (
                        f'LinkedIn 安全驗證 (Challenge): {e}. '
                        '請手動登入 LinkedIn 解除驗證後重試'
                    )
                elif err_name == 'UnauthorizedException' or '401' in str(e):
                    self._auth_error = 'LinkedIn 帳號密碼錯誤 (401 Unauthorized)'
                else:
                    self._auth_error = f'LinkedIn 認證失敗: {e}'

                self._stats['auth_failures'] += 1
                logger.error(f"LinkedIn API 認證失敗 [{err_name}]: {e}")
                return False

    # ── Rate Limiting ─────────────────────────────────────────

    def _enforce_rate_limit(self) -> bool:
        """
        檢查並執行速率限制
        Returns: True = 允許請求, False = 超過限制
        """
        now = datetime.now()

        # 重設每小時計數器
        if now - self._hour_start > timedelta(hours=1):
            self._request_count_this_hour = 0
            self._hour_start = now

        # 檢查每小時上限
        if self._request_count_this_hour >= self.max_requests_per_hour:
            self._stats['rate_limited'] += 1
            logger.warning(
                f"LinkedIn API 每小時配額已滿 "
                f"({self._request_count_this_hour}/{self.max_requests_per_hour})"
            )
            return False

        # 執行最小間隔
        elapsed = time.time() - self._last_request_time
        if elapsed < self.request_cooldown:
            sleep_time = self.request_cooldown - elapsed
            logger.debug(f"LinkedIn API cooldown: 等待 {sleep_time:.1f}s")
            time.sleep(sleep_time)

        self._request_count_this_hour += 1
        self._last_request_time = time.time()
        return True

    # ── URL 解析 ──────────────────────────────────────────────

    @staticmethod
    def extract_public_id(linkedin_url: str) -> Optional[str]:
        """
        從 LinkedIn URL 提取 public_id

        Examples:
            https://www.linkedin.com/in/john-doe/ → john-doe
            https://tw.linkedin.com/in/jane-wu?trk=abc → jane-wu
            https://linkedin.com/in/bob-chen → bob-chen
            https://www.linkedin.com/in/torri-huang-cma-366aa5147/ → torri-huang-cma-366aa5147
        """
        if not linkedin_url:
            return None

        match = re.search(r'linkedin\.com/in/([^/?#]+)', linkedin_url)
        if match:
            public_id = match.group(1).strip('/')
            return unquote(public_id)

        return None

    # ── 核心: 取得 Profile ────────────────────────────────────

    def fetch_profile(self, linkedin_url: str) -> dict:
        """
        從 LinkedIn API 取得完整 profile 資料

        Args:
            linkedin_url: LinkedIn 個人頁面 URL

        Returns:
            dict: 與 ProfileEnricher 格式相容的結構化資料
            {
                'success': bool,
                'name': str,
                'current_position': str,
                'company': str,
                'location': str,
                'years_experience': int,
                'skills': list[str],
                'work_history': list[dict],
                'education_details': list[dict],
                'education_level': str,
                'languages': list[str],
                'certifications': list[str],
                'summary': str,
                'stability_indicators': dict,
                'contact_info': dict,  # email, phone, websites
                'industry_tags': list[str],
                '_raw_profile': dict,
            }
        """
        self._stats['calls'] += 1

        # 1. 提取 public_id
        public_id = self.extract_public_id(linkedin_url)
        if not public_id:
            self._stats['failed'] += 1
            return {
                'success': False,
                'error': f'無法從 URL 提取 public_id: {linkedin_url}',
            }

        # 2. 確認已認證
        if not self._ensure_authenticated():
            self._stats['failed'] += 1
            return {
                'success': False,
                'error': self._auth_error or 'LinkedIn 認證失敗',
            }

        # 3. Rate limit 檢查
        if not self._enforce_rate_limit():
            self._stats['failed'] += 1
            return {
                'success': False,
                'error': f'LinkedIn API 本小時配額已滿 ({self.max_requests_per_hour})',
            }

        try:
            # 4. 取得主要 profile 資料
            logger.info(f"LinkedIn API: 取得 profile '{public_id}'")
            raw_profile = self._api.get_profile(public_id=public_id)

            if not raw_profile:
                self._stats['failed'] += 1
                return {'success': False, 'error': f'LinkedIn 回傳空 profile: {public_id}'}

            # 5. 取得聯絡資訊（獨立 API call）
            contact_info = {}
            try:
                if self._enforce_rate_limit():
                    contact_info = self._api.get_profile_contact_info(
                        public_id=public_id
                    ) or {}
            except Exception as e:
                logger.warning(f"LinkedIn API contact info 失敗（非致命）: {e}")

            # 6. 取得完整技能列表（獨立 API call，最多 100 筆）
            extended_skills = []
            try:
                if self._enforce_rate_limit():
                    extended_skills = self._api.get_profile_skills(
                        public_id=public_id
                    ) or []
            except Exception as e:
                logger.warning(f"LinkedIn API skills 失敗（非致命）: {e}")

            # 7. 映射為 enrichment 格式
            result = self._map_profile_to_enrichment(
                raw_profile, contact_info, extended_skills
            )
            result['_raw_profile'] = raw_profile
            self._stats['success'] += 1

            logger.info(
                f"LinkedIn API: '{public_id}' 分析完成 — "
                f"{len(result.get('work_history', []))} 段經歷, "
                f"{len(result.get('skills', []))} 項技能"
            )
            return result

        except Exception as e:
            self._stats['failed'] += 1

            # Session 過期處理
            err_str = str(e)
            if '401' in err_str or 'Unauthorized' in err_str or 'expired' in err_str.lower():
                self._authenticated = False
                self._api = None
                self._auth_error = 'LinkedIn session 已過期，下次會自動重新認證'
                logger.warning("LinkedIn API session expired — 下次自動重認證")

            logger.error(f"LinkedIn API fetch_profile 失敗: {e}", exc_info=True)
            return {'success': False, 'error': f'LinkedIn API 錯誤: {e}'}

    # ── 資料映射 ──────────────────────────────────────────────

    def _map_profile_to_enrichment(
        self,
        profile: dict,
        contact_info: dict,
        extended_skills: list,
    ) -> dict:
        """
        將 linkedin-api 原始 profile 映射為 ProfileEnricher 預期的格式

        linkedin-api get_profile() 回傳:
            firstName, lastName, headline, summary, industryName, locationName,
            experience[], education[], languages[], certifications[],
            skills[], publications[], volunteer[], honors[], projects[]
        """
        # ── Name ──
        first = profile.get('firstName', '')
        last = profile.get('lastName', '')
        name = f"{first} {last}".strip() or profile.get('public_id', '')

        # ── Current Position & Company ──
        headline = profile.get('headline', '')
        experience = profile.get('experience', [])
        current_position = ''
        current_company = ''
        if experience:
            current_exp = experience[0]  # 最新的在前面
            current_position = current_exp.get('title', '')
            current_company = current_exp.get('companyName', '')
            if not current_company:
                current_company = (
                    (current_exp.get('company') or {}).get('name', '')
                )
        if not current_position and headline:
            current_position = headline

        # ── Location ──
        location = (
            profile.get('locationName', '')
            or profile.get('geoLocationName', '')
        )

        # ── Work History ──
        work_history = []
        for exp in experience:
            tp = exp.get('timePeriod') or {}
            start_date = tp.get('startDate', {})
            end_date = tp.get('endDate', {})

            start_str = self._format_date(start_date)
            end_str = self._format_date(end_date) if end_date else '至今'
            duration = f"{start_str} - {end_str}" if start_str else ''

            company_name = exp.get('companyName', '')
            if not company_name:
                company_name = (exp.get('company') or {}).get('name', '')

            description = (exp.get('description') or '')[:200]

            work_history.append({
                'company': company_name,
                'title': exp.get('title', ''),
                'duration': duration,
                'description': description,
            })

        # ── Education ──
        education_list = profile.get('education', [])
        education_details = []
        education_level = ''

        for edu in education_list:
            school_name = edu.get('schoolName', '')
            if not school_name:
                school_name = (edu.get('school') or {}).get('schoolName', '')

            degree = edu.get('degreeName', '')
            field = edu.get('fieldOfStudy', '')
            tp = edu.get('timePeriod') or {}
            end_date = tp.get('endDate', {})
            year = str(end_date.get('year', '')) if end_date else ''

            education_details.append({
                'school': school_name,
                'degree': degree,
                'field': field,
                'year': year,
            })

            # 判斷最高學歷
            if any(k in degree for k in ['博士', 'PhD', 'Ph.D', 'Doctorate', 'Doctor']):
                education_level = '博士'
            elif any(k in degree for k in ['碩士', 'Master', 'MS', 'MA', 'MBA', 'M.S.', 'M.A.']):
                if education_level not in ('博士',):
                    education_level = '碩士'
            elif any(k in degree for k in ['學士', 'Bachelor', 'BS', 'BA', 'B.S.', 'B.A.']):
                if education_level not in ('博士', '碩士'):
                    education_level = '大學'

        # ── Skills（合併 profile.skills + extended_skills） ──
        skill_names = set()
        for s in profile.get('skills', []):
            name_val = s.get('name', '') if isinstance(s, dict) else str(s)
            if name_val:
                skill_names.add(name_val)
        for s in extended_skills:
            name_val = s.get('name', '') if isinstance(s, dict) else str(s)
            if name_val:
                skill_names.add(name_val)
        skills_list = sorted(skill_names)

        # ── Languages ──
        languages = []
        for lang in profile.get('languages', []):
            lang_name = lang.get('name', '') if isinstance(lang, dict) else str(lang)
            if lang_name:
                languages.append(lang_name)

        # ── Certifications ──
        certifications = []
        for cert in profile.get('certifications', []):
            cert_name = cert.get('name', '') if isinstance(cert, dict) else str(cert)
            if cert_name:
                certifications.append(cert_name)

        # ── Years of Experience & Stability ──
        years_experience, stability = self._calc_experience_and_stability(experience)

        # ── Summary ──
        summary = profile.get('summary', '') or ''

        # ── Contact Info (bonus) ──
        contact = {}
        if contact_info:
            phone_numbers = []
            for p in contact_info.get('phone_numbers', []):
                if isinstance(p, dict):
                    phone_numbers.append(p.get('number', ''))
                elif isinstance(p, str):
                    phone_numbers.append(p)

            websites = []
            for w in contact_info.get('websites', []):
                if isinstance(w, dict) and w.get('url'):
                    websites.append(w['url'])
                elif isinstance(w, str):
                    websites.append(w)

            contact = {
                'email': contact_info.get('email_address', ''),
                'phone_numbers': [p for p in phone_numbers if p],
                'websites': websites,
                'twitter': contact_info.get('twitter', ''),
            }

        # ── Industry Tags ──
        industry_tags = []
        if profile.get('industryName'):
            industry_tags.append(profile['industryName'])

        return {
            'success': True,
            'name': name,
            'current_position': current_position,
            'company': current_company,
            'location': location,
            'years_experience': years_experience,
            'skills': skills_list,
            'work_history': work_history,
            'education_details': education_details,
            'education_level': education_level,
            'languages': languages,
            'certifications': certifications,
            'summary': summary,
            'stability_indicators': stability,
            'contact_info': contact,
            'industry_tags': industry_tags,
        }

    def _calc_experience_and_stability(self, experience: list) -> tuple:
        """
        從工作經歷計算年資和穩定性指標

        Returns:
            tuple: (years_experience: int, stability_indicators: dict)
        """
        if not experience:
            return 0, {'avg_tenure_months': 0, 'job_changes': 0, 'recent_gap_months': 0}

        now_year = datetime.now().year
        now_month = datetime.now().month
        tenures = []
        recent_gap = 0

        for i, exp in enumerate(experience):
            tp = exp.get('timePeriod') or {}
            start = tp.get('startDate') or {}
            end = tp.get('endDate') or {}

            s_year = start.get('year', now_year) if start else now_year
            s_month = start.get('month', 1) if start else 1
            e_year = end.get('year', now_year) if end else now_year
            e_month = end.get('month', now_month) if end else now_month

            months = max(1, (e_year - s_year) * 12 + (e_month - s_month))
            tenures.append(months)

            # 計算最近待業時間（experience 是最新在前）
            if i == 0 and end:
                end_total = e_year * 12 + e_month
                now_total = now_year * 12 + now_month
                recent_gap = max(0, now_total - end_total)

        total_months = sum(tenures)
        years_experience = max(1, total_months // 12)
        avg_tenure = int(sum(tenures) / len(tenures)) if tenures else 0
        job_changes = max(0, len(experience) - 1)  # 換工作次數 = 工作數 - 1

        return years_experience, {
            'avg_tenure_months': avg_tenure,
            'job_changes': job_changes,
            'recent_gap_months': recent_gap,
        }

    @staticmethod
    def _format_date(date_dict: dict) -> str:
        """將 {year, month} dict 格式化為 'YYYY-MM' 字串"""
        if not date_dict:
            return ''
        year = date_dict.get('year', '')
        month = date_dict.get('month', '')
        if year and month:
            return f"{year}-{int(month):02d}"
        elif year:
            return str(year)
        return ''
