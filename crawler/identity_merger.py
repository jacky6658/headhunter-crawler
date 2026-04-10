"""
跨平台身份合併引擎 — Union-Find 演算法

將來自 LinkedIn / GitHub / CakeResume / Dorking 等多源的候選人，
透過 email / GitHub username / LinkedIn URL / CakeResume 連結 / 姓名+公司 等合併鍵，
合併為同一人的完整 profile。
"""
import logging
import re
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from storage.models import Candidate
from crawler.dedup import DedupCache

logger = logging.getLogger(__name__)


class UnionFind:
    """Union-Find (Disjoint Set) 資料結構"""

    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, x: int, y: int) -> bool:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return False
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1
        return True


class IdentityMerger:
    """跨平台身份合併引擎"""

    # 合併鍵信心分數
    CONFIDENCE = {
        'email': 0.95,
        'github_username': 0.90,
        'linkedin_url': 0.90,
        'cakeresume_cross': 0.85,
        'name_company': 0.60,
    }

    def __init__(self, config: dict):
        self.config = config

    @staticmethod
    def _normalize_linkedin_url(url: str) -> str:
        if not url:
            return ''
        url = url.strip().lower()
        url = url.replace('://www.', '://')
        url = url.rstrip('/')
        if url.startswith('http://'):
            url = 'https://' + url[7:]
        return url

    @staticmethod
    def _normalize_email(email: str) -> str:
        if not email or '@' not in email:
            return ''
        return email.strip().lower()

    # ── 組織名稱過濾 ──

    _ORG_KEYWORDS = [
        ' co.', ' co,', ' ltd', ' inc.', ' inc,', ' llc', ' corp', ' gmbh',
        '有限公司', '股份有限公司',
    ]
    _ORG_BLOCKLIST = set()

    @classmethod
    def _is_org_name(cls, name: str) -> bool:
        if not name:
            return False
        nl = name.strip().lower()
        for kw in cls._ORG_KEYWORDS:
            if kw in nl:
                return True
        if '.' in nl and any(nl.endswith(ext) for ext in ['.io', '.com', '.org', '.net', '.ai']):
            return True
        return False

    # ── 主合併方法 ──

    def merge(self, source_data: Dict[str, list], dedup_cache: DedupCache,
              task=None) -> List[Candidate]:
        """
        合併所有來源的候選人資料

        Args:
            source_data: {'linkedin': [...], 'github': [...], 'cakeresume': [...], 'dorking': [...]}
            dedup_cache: DedupCache 實例
            task: SearchTask 實例（用於填入 client_name, job_title 等）

        Returns:
            List[Candidate] — 合併去重後的候選人列表
        """
        # 1. 收集所有候選人 raw dicts
        all_items = []
        for source, items in source_data.items():
            for item in items:
                item['_merge_source'] = source
                all_items.append(item)

        if not all_items:
            return []

        n = len(all_items)
        uf = UnionFind(n)
        merge_reasons = {}  # (i, j) → reason

        # 2. 建立索引
        email_idx = {}      # email → [indices]
        gh_idx = {}          # github_username → [indices]
        li_idx = {}          # normalized_linkedin_url → [indices]
        cake_idx = {}        # cakeresume_url → index
        name_company_idx = {}  # (name_lower, company_lower) → [indices]

        for i, item in enumerate(all_items):
            # Email index
            email = self._normalize_email(item.get('email', ''))
            if email and 'noreply' not in email:
                email_idx.setdefault(email, []).append(i)

            # GitHub username index
            gh = (item.get('github_username', '') or '').lower().strip()
            if gh:
                gh_idx.setdefault(gh, []).append(i)

            # LinkedIn URL index
            li = self._normalize_linkedin_url(item.get('linkedin_url', ''))
            if li and 'linkedin.com/in/' in li:
                li_idx.setdefault(li, []).append(i)

            # CakeResume URL index
            cake = (item.get('cakeresume_url', '') or '').strip().lower().rstrip('/')
            if cake:
                cake_idx[cake] = i

            # Name + Company index (weak signal)
            name = (item.get('name', '') or '').strip().lower()
            company = (item.get('company', '') or '').strip().lower()
            if name and company and len(name) > 1:
                name_company_idx.setdefault((name, company), []).append(i)

        # 3. Union by merge keys (strongest first)

        # Email merge (0.95)
        for email, indices in email_idx.items():
            for j in range(1, len(indices)):
                if uf.union(indices[0], indices[j]):
                    merge_reasons[(indices[0], indices[j])] = 'email'

        # GitHub username merge (0.90)
        for gh, indices in gh_idx.items():
            for j in range(1, len(indices)):
                if uf.union(indices[0], indices[j]):
                    merge_reasons[(indices[0], indices[j])] = 'github_username'

        # LinkedIn URL merge (0.90)
        for li, indices in li_idx.items():
            for j in range(1, len(indices)):
                if uf.union(indices[0], indices[j]):
                    merge_reasons[(indices[0], indices[j])] = 'linkedin_url'

        # CakeResume cross-reference (0.85)
        # If a CakeResume profile contains a GitHub URL or LinkedIn URL that matches another item
        for i, item in enumerate(all_items):
            if item.get('_merge_source') != 'cakeresume':
                continue
            # Check if this CakeResume item's GitHub URL matches a GitHub item
            gh = (item.get('github_username', '') or '').lower().strip()
            if gh and gh in gh_idx:
                for j in gh_idx[gh]:
                    if i != j and uf.union(i, j):
                        merge_reasons[(i, j)] = 'cakeresume_cross'
            # Check LinkedIn URL
            li = self._normalize_linkedin_url(item.get('linkedin_url', ''))
            if li and li in li_idx:
                for j in li_idx[li]:
                    if i != j and uf.union(i, j):
                        merge_reasons[(i, j)] = 'cakeresume_cross'

        # Name + Company merge (0.60, weak)
        for key, indices in name_company_idx.items():
            if len(indices) > 1:
                for j in range(1, len(indices)):
                    if uf.union(indices[0], indices[j]):
                        merge_reasons[(indices[0], indices[j])] = 'name_company'

        # 4. Group by connected components
        groups = {}  # root → [indices]
        for i in range(n):
            root = uf.find(i)
            groups.setdefault(root, []).append(i)

        # 5. Merge each group into a single Candidate
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        today = datetime.now().strftime('%Y-%m-%d')
        candidates = []
        org_filtered = 0

        for root, indices in groups.items():
            items = [all_items[i] for i in indices]

            # 組織過濾
            name = self._pick_best(items, 'name')
            if self._is_org_name(name):
                org_filtered += 1
                continue

            # 合併所有欄位
            merged = self._merge_items(items)

            # 計算信心分數
            confidence = 0.0
            sources = set()
            for i in indices:
                src = all_items[i].get('_merge_source', all_items[i].get('source', ''))
                sources.add(src)
            for (a, b), reason in merge_reasons.items():
                if uf.find(a) == root:
                    confidence = max(confidence, self.CONFIDENCE.get(reason, 0))

            # Dedup check
            li_url = self._normalize_linkedin_url(merged.get('linkedin_url', ''))
            gh_user = (merged.get('github_username', '') or '').lower()
            cake_url = (merged.get('cakeresume_url', '') or '').lower().rstrip('/')
            email = self._normalize_email(merged.get('email', ''))

            is_dup = dedup_cache.is_seen(
                linkedin_url=li_url,
                github_username=gh_user,
                cakeresume_url=cake_url,
                email=email,
            )

            # Mark as seen
            dedup_cache.mark_seen(
                linkedin_url=li_url or None,
                github_username=gh_user or None,
                cakeresume_url=cake_url or None,
                email=email or None,
            )

            # Build source string
            source_parts = []
            if any(s in ('linkedin',) for s in sources):
                source_parts.append('linkedin')
            if any(s in ('github',) for s in sources):
                source_parts.append('github')
            if any(s in ('cakeresume',) for s in sources):
                source_parts.append('cakeresume')
            if any(s in ('blog', 'conference', 'dorking') for s in sources):
                source_parts.append('dorking')
            source_str = '+'.join(source_parts) if source_parts else merged.get('source', 'unknown')

            # Build contact_methods
            contact_methods = []
            if email:
                email_source = 'github_patch' if any(s == 'github' for s in sources) else 'profile'
                contact_methods.append({'type': 'email', 'value': email, 'source': email_source})

            # Create Candidate
            skills = merged.get('skills', [])
            if isinstance(skills, str):
                skills = [s.strip() for s in skills.split(',') if s.strip()]

            candidate = Candidate(
                id=str(uuid.uuid4()),
                name=merged.get('name', ''),
                source=source_str,
                github_url=merged.get('github_url', ''),
                github_username=merged.get('github_username', ''),
                linkedin_url=merged.get('linkedin_url', ''),
                linkedin_username=merged.get('linkedin_username', ''),
                email=merged.get('email', ''),
                location=merged.get('location', ''),
                bio=merged.get('bio', ''),
                company=merged.get('company', ''),
                title=merged.get('title', ''),
                skills=skills,
                public_repos=merged.get('public_repos', 0),
                followers=merged.get('followers', 0),
                recent_push=merged.get('recent_push', ''),
                top_repos=merged.get('top_repos', []),
                total_stars=merged.get('total_stars', 0),
                score_factors=merged.get('score_factors', {}),
                tech_stack=merged.get('tech_stack', []),
                top_repos_detail=merged.get('top_repos_detail', []),
                languages=merged.get('languages', {}),
                client_name=task.client_name if task else '',
                job_title=task.job_title if task else '',
                task_id=task.id if task else '',
                search_date=today,
                status='new',
                created_at=now,
                is_duplicate=is_dup,
                cakeresume_url=merged.get('cakeresume_url', ''),
                contact_methods=contact_methods,
                identity_confidence=confidence,
                sources_found=list(sources),
            )
            candidates.append(candidate)

        merged_count = sum(1 for indices in groups.values() if len(indices) > 1)
        logger.info(f"身份合併完成: {n} 項 → {len(candidates)} 人 "
                    f"(合併 {merged_count} 組, 組織過濾 {org_filtered}, 重複 {sum(1 for c in candidates if c.is_duplicate)})")

        return candidates

    @staticmethod
    def _pick_best(items: list, field: str, prefer_non_empty: bool = True) -> str:
        """從多個候選中選最佳值：優先非空、最長"""
        values = [item.get(field, '') or '' for item in items]
        non_empty = [v for v in values if v.strip()]
        if not non_empty:
            return ''
        return max(non_empty, key=len)

    @staticmethod
    def _merge_items(items: list) -> dict:
        """合併多個 raw dict 為一個，每個欄位取最豐富版本"""
        merged = {}

        # 字串欄位：取最長非空值
        str_fields = [
            'name', 'github_url', 'github_username', 'linkedin_url', 'linkedin_username',
            'email', 'location', 'bio', 'company', 'title', 'cakeresume_url',
            'recent_push',
        ]
        for f in str_fields:
            values = [item.get(f, '') or '' for item in items]
            non_empty = [v for v in values if v.strip()]
            merged[f] = max(non_empty, key=len) if non_empty else ''

        # int 欄位：取最大值
        int_fields = ['public_repos', 'followers', 'total_stars']
        for f in int_fields:
            merged[f] = max((item.get(f, 0) or 0) for item in items)

        # list 欄位：取聯集（去重）
        for f in ['skills', 'tech_stack', 'top_repos']:
            all_vals = []
            seen = set()
            for item in items:
                vals = item.get(f, []) or []
                if isinstance(vals, str):
                    vals = [v.strip() for v in vals.split(',') if v.strip()]
                for v in vals:
                    vl = v.lower() if isinstance(v, str) else v
                    if vl not in seen:
                        seen.add(vl)
                        all_vals.append(v)
            merged[f] = all_vals

        # dict 欄位：合併（後者覆蓋前者）
        for f in ['score_factors', 'languages']:
            result = {}
            for item in items:
                val = item.get(f, {}) or {}
                if isinstance(val, dict):
                    result.update(val)
            merged[f] = result

        # list[dict] 欄位：取最豐富的版本
        for f in ['top_repos_detail', 'work_history', 'education_details']:
            best = []
            for item in items:
                val = item.get(f, []) or []
                if isinstance(val, list) and len(val) > len(best):
                    best = val
            merged[f] = best

        # source: 保留原始 source
        merged['source'] = items[0].get('source', '') if items else ''

        return merged
