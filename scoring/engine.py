"""
評分引擎 — 三層評分模型

1. Hard Skills Match — 技能比對（must_have + core + nice_to_have）
2. Domain Match — 產業/場景加分（context）
3. Constraints Check — 限制條件檢查

輸出: 總分(0-100) + 等級(A/B/C/D) + 評分細項
"""
import json
import logging
from typing import Dict, List, Optional

from scoring.normalizer import SkillNormalizer

logger = logging.getLogger(__name__)

# 等級閾值
GRADE_THRESHOLDS = {
    'A': 75,  # 強推薦
    'B': 55,  # 推薦
    'C': 35,  # 待評估（資料不足時，搜尋來源提供基線信心）
    # D: < 35  不推薦 / 資料嚴重不足
}

# must_have 缺失懲罰因子
MUST_HAVE_PENALTY = 0.7  # 每缺 1 個 must_have → 總分 × 0.7
GITHUB_MUST_HAVE_PENALTY = 0.8  # GitHub-only 候選人使用較寬鬆的懲罰

# GitHub 活躍度加分上限
GITHUB_BONUS_MAX = 20


class ScoringEngine:
    """技能評分引擎"""

    def __init__(self, normalizer: SkillNormalizer):
        self.normalizer = normalizer

    def score_candidate(self, candidate: dict, job_profile: dict) -> Dict:
        """
        評分單一候選人

        Args:
            candidate: {
                name, skills[], bio, company, location, title,
                source, public_repos, followers, top_repos[], recent_push,
                github_username, ...
            }
            job_profile: {
                job_profile: {
                    must_have: [{skill, weight}],
                    core: [{skill, weight}],
                    nice_to_have: [{skill, weight}],
                    context: [{tag, weight}],
                    constraints: {location: [], seniority_min_years: int}
                }
            }

        Returns: {
            total_score, grade, breakdown, constraints_pass,
            constraint_flags, skill_match_rate, matched_skills, missing_critical
        }
        """
        profile = job_profile.get('job_profile', job_profile)

        # 如果 profile 為空（沒有任何技能要求），不評分
        if not profile.get('must_have') and not profile.get('core') \
                and not profile.get('nice_to_have'):
            return {
                'total_score': 0,
                'grade': '',
                'breakdown': {},
                'constraints_pass': True,
                'constraint_flags': [],
                'skill_match_rate': 0,
                'matched_skills': [],
                'missing_critical': [],
            }

        # 1. 收集候選人的所有技能（從多個來源）
        candidate_skills = self._collect_skills(candidate)

        # 2. 技能比對
        must_have_result = self._match_skills(
            candidate_skills, profile.get('must_have', [])
        )
        core_result = self._match_skills(
            candidate_skills, profile.get('core', [])
        )
        nice_to_have_result = self._match_skills(
            candidate_skills, profile.get('nice_to_have', [])
        )

        # 3. Context 比對（產業/場景）
        context_result = self._check_context(
            candidate, profile.get('context', [])
        )

        # 4. 計算原始分數
        total_weight = (must_have_result['max'] + core_result['max']
                        + nice_to_have_result['max'] + context_result['max'])

        if total_weight == 0:
            raw_score = 0
        else:
            earned = (must_have_result['score'] + core_result['score']
                      + nice_to_have_result['score'] + context_result['score'])
            raw_score = (earned / total_weight) * 100

        # 5. must_have 缺失懲罰
        missing_must_have = must_have_result.get('missing', [])

        # 判斷候選人資料稀疏度
        is_github_only = (
            candidate.get('source') == 'github'
            and not candidate.get('linkedin_url')
            and not candidate.get('work_history')
        )
        has_sparse_data = (
            len(candidate_skills) <= 2
            and not candidate.get('work_history')
        )

        # 資料稀疏的候選人使用較寬鬆的懲罰
        if has_sparse_data:
            penalty_factor = 0.9  # 資料不足時不應過度懲罰
        elif is_github_only:
            penalty_factor = GITHUB_MUST_HAVE_PENALTY  # 0.8
        else:
            penalty_factor = MUST_HAVE_PENALTY  # 0.7
        penalty = penalty_factor ** len(missing_must_have)
        penalized_score = raw_score * penalty

        # 6. 搜尋相關性基線分
        # 候選人是通過搜尋引擎找到的，有基本相關性保底
        search_relevance_bonus = self._calc_search_relevance(
            candidate, profile
        )
        # 搜尋相關性作為「下限」— 即使技能完全沒匹配，也不低於此分數
        penalized_score = max(penalized_score, search_relevance_bonus)

        # 7. GitHub 活躍度加分
        github_bonus = 0
        if candidate.get('source') == 'github' or candidate.get('github_username'):
            github_bonus = self._calc_github_bonus(candidate)
            penalized_score = min(100, penalized_score + github_bonus)

        # 8. 四捨五入
        total_score = round(penalized_score)
        total_score = max(0, min(100, total_score))

        # 8. 評定等級
        grade = self._calc_grade(total_score)

        # 9. 限制條件檢查
        constraints_result = self._check_constraints(
            candidate, profile.get('constraints', {})
        )

        # 10. 所有匹配到的技能
        all_matched = list(set(
            must_have_result['matched'] + core_result['matched']
            + nice_to_have_result['matched']
        ))
        all_required = list(set(
            [s['skill'] for s in profile.get('must_have', [])]
            + [s['skill'] for s in profile.get('core', [])]
            + [s['skill'] for s in profile.get('nice_to_have', [])]
        ))
        match_rate = len(all_matched) / len(all_required) if all_required else 0

        return {
            'total_score': total_score,
            'grade': grade,
            'breakdown': {
                'must_have': must_have_result,
                'core': core_result,
                'nice_to_have': nice_to_have_result,
                'context': context_result,
                'github_bonus': github_bonus,
                'search_relevance': search_relevance_bonus,
            },
            'constraints_pass': constraints_result['pass'],
            'constraint_flags': constraints_result['flags'],
            'skill_match_rate': round(match_rate, 2),
            'matched_skills': all_matched,
            'missing_critical': missing_must_have,
        }

    def score_batch(self, candidates: list, job_profile: dict) -> List[Dict]:
        """
        批次評分 + 排序（依總分降序）

        Returns: list of { candidate: {...}, score_result: {...} }
        """
        results = []
        for candidate in candidates:
            c_dict = candidate if isinstance(candidate, dict) else candidate.to_dict()
            score_result = self.score_candidate(c_dict, job_profile)
            results.append({
                'candidate': c_dict,
                'score_result': score_result,
            })

        # 按分數降序排列
        results.sort(key=lambda x: x['score_result']['total_score'], reverse=True)
        return results

    def _collect_skills(self, candidate: dict) -> List[str]:
        """
        從候選人的多個資料來源收集技能

        來源:
        1. skills 欄位（直接列表）
        2. bio / title 文字提取
        3. top_repos 名稱推斷
        """
        raw_skills = []

        # 1. skills 欄位
        skills = candidate.get('skills', [])
        if isinstance(skills, str):
            skills = [s.strip() for s in skills.split(',') if s.strip()]
        raw_skills.extend(skills)

        # 2. bio 文字提取
        bio = candidate.get('bio', '')
        if bio:
            extracted = self.normalizer.extract_skills_from_text(bio)
            raw_skills.extend(extracted)

        # 3. title 文字提取
        title = candidate.get('title', '')
        if title and title != bio:  # 避免重複
            extracted = self.normalizer.extract_skills_from_text(title)
            raw_skills.extend(extracted)

        # 4. top_repos 名稱推斷
        top_repos = candidate.get('top_repos', [])
        if isinstance(top_repos, list):
            for repo_name in top_repos:
                if isinstance(repo_name, str):
                    extracted = self.normalizer.extract_skills_from_text(repo_name)
                    raw_skills.extend(extracted)

        # 5. tech_stack（深度分析結果）
        tech_stack = candidate.get('tech_stack', [])
        if isinstance(tech_stack, list):
            raw_skills.extend(tech_stack)

        # 5b. top_repos_detail — 從 repo topics、description、language 提取
        top_repos_detail = candidate.get('top_repos_detail', [])
        if isinstance(top_repos_detail, list):
            for repo in top_repos_detail:
                if isinstance(repo, dict):
                    topics = repo.get('topics', [])
                    if isinstance(topics, list):
                        raw_skills.extend(topics)
                    desc = repo.get('description', '')
                    if desc:
                        extracted = self.normalizer.extract_skills_from_text(desc)
                        raw_skills.extend(extracted)
                    lang = repo.get('language', '')
                    if lang:
                        raw_skills.append(lang)

        # 5c. languages distribution — 提取所有程式語言
        languages = candidate.get('languages', {})
        if isinstance(languages, dict):
            raw_skills.extend(languages.keys())

        # 6. work_history 提取（Phase 2 enrichment 結果）
        work_history = candidate.get('work_history', [])
        # work_history 可能是 JSON 字串（從 LocalStore 讀取時）
        if isinstance(work_history, str) and work_history:
            try:
                import json as _json
                work_history = _json.loads(work_history)
            except (ValueError, TypeError):
                work_history = []
        if isinstance(work_history, list):
            # 只取最近 3 段工作避免噪音
            for wh in work_history[:3]:
                if isinstance(wh, dict):
                    # 從職稱提取
                    wh_title = wh.get('title', '')
                    if wh_title:
                        extracted = self.normalizer.extract_skills_from_text(wh_title)
                        raw_skills.extend(extracted)
                    # 從描述提取
                    wh_desc = wh.get('description', '')
                    if wh_desc:
                        extracted = self.normalizer.extract_skills_from_text(wh_desc)
                        raw_skills.extend(extracted)

        # 7. current_position（enrichment 補充的職稱）
        current_pos = candidate.get('current_position', '')
        if current_pos and current_pos != title:
            extracted = self.normalizer.extract_skills_from_text(current_pos)
            raw_skills.extend(extracted)

        # 正規化 + 去重
        return self.normalizer.normalize_list(raw_skills)

    def _match_skills(self, candidate_skills: list,
                      profile_skills: list) -> Dict:
        """
        比對正規化後的技能

        Args:
            candidate_skills: ['java', 'spring', 'mysql', 'redis']
            profile_skills: [{'skill': 'java', 'weight': 8}, ...]

        Returns: {
            score: 實得分,
            max: 滿分,
            matched: ['java', 'spring'],
            missing: ['docker']
        }
        """
        if not profile_skills:
            return {'score': 0, 'max': 0, 'matched': [], 'missing': []}

        matched = []
        missing = []
        earned = 0
        total = 0

        candidate_set = set(s.lower() for s in candidate_skills)

        for item in profile_skills:
            skill = item.get('skill', '').lower()
            weight = item.get('weight', 1)
            total += weight

            # 正規化 profile 中的技能名稱
            normalized_skill = self.normalizer.normalize(skill)

            if normalized_skill in candidate_set or skill in candidate_set:
                matched.append(skill)
                earned += weight
            else:
                missing.append(skill)

        return {
            'score': earned,
            'max': total,
            'matched': matched,
            'missing': missing,
        }

    def _check_context(self, candidate: dict, context: list) -> Dict:
        """
        從 bio/company/title 推斷產業/場景加分

        Args:
            candidate: { bio, company, title, ... }
            context: [{'tag': 'fintech', 'weight': 3}, ...]

        Returns: { score, max, matched, missing }
        """
        if not context:
            return {'score': 0, 'max': 0, 'matched': [], 'missing': []}

        # 建立可搜尋的文字
        searchable = ' '.join([
            candidate.get('bio', ''),
            candidate.get('company', ''),
            candidate.get('title', ''),
        ]).lower()

        # 產業/場景關鍵字映射
        CONTEXT_KEYWORDS = {
            'fintech': ['fintech', 'banking', 'payment', 'financial', 'bank',
                        '金融', '銀行', '支付'],
            'ecommerce': ['ecommerce', 'e-commerce', 'shopping', 'retail',
                          '電商', '零售', 'shopee', 'momo', 'pchome'],
            'gaming': ['gaming', 'game', 'unity', 'unreal', '遊戲'],
            'healthcare': ['healthcare', 'medical', 'health', '醫療', '健康'],
            'high-availability': ['high-availability', 'ha', '99.9%', 'uptime',
                                   'scalable', 'distributed', '高可用'],
            'on-call': ['on-call', 'oncall', 'incident', 'pager', '值班'],
            'startup': ['startup', '新創'],
            'enterprise': ['enterprise', '企業'],
            'saas': ['saas', 'b2b', 'platform'],
            'ai': ['ai', 'machine learning', 'ml', 'deep learning', '人工智慧'],
        }

        matched = []
        missing = []
        earned = 0
        total = 0

        for item in context:
            tag = item.get('tag', '').lower()
            weight = item.get('weight', 1)
            total += weight

            keywords = CONTEXT_KEYWORDS.get(tag, [tag])
            found = any(kw in searchable for kw in keywords)

            if found:
                matched.append(tag)
                earned += weight
            else:
                missing.append(tag)

        return {
            'score': earned,
            'max': total,
            'matched': matched,
            'missing': missing,
        }

    def _check_constraints(self, candidate: dict,
                           constraints: dict) -> Dict:
        """
        檢查限制條件

        Args:
            constraints: { location: ['taipei', 'taiwan'], seniority_min_years: 2 }

        Returns: { pass: bool, flags: ['location_mismatch'] }
        """
        if not constraints:
            return {'pass': True, 'flags': []}

        flags = []

        # 地區檢查
        required_locations = constraints.get('location', [])
        if required_locations:
            candidate_location = (candidate.get('location', '') or '').lower()
            if candidate_location:
                location_match = any(
                    loc.lower() in candidate_location
                    for loc in required_locations
                )
                if not location_match:
                    flags.append(f'location_mismatch:{candidate_location}')

        return {
            'pass': len(flags) == 0,
            'flags': flags,
        }

    def _calc_search_relevance(self, candidate: dict, profile: dict) -> float:
        """
        搜尋相關性基線分

        當候選人資料稀疏（skills 為空、work_history 為空）但是通過
        搜尋引擎找到時，根據搜尋來源和基本資訊給予基線分數。

        邏輯：
        - 候選人的 title/bio/company 與 job_profile 的 role_name
          有文字重疊 → 給予基線分 15-35
        - 此分數代表 "雖然資料不足無法完整評分，但搜尋來源暗示有相關性"
        """
        role_name = profile.get('role_name', '').lower()
        if not role_name:
            return 0

        # 建立搜尋文本
        searchable = ' '.join([
            candidate.get('title', ''),
            candidate.get('bio', ''),
            candidate.get('company', ''),
        ]).lower()

        if not searchable.strip():
            return 15  # 完全無資料，給最低基線

        # 從 role_name 提取關鍵詞
        role_keywords = set(role_name.replace('-', ' ').split())
        # 排除常見停用詞
        stopwords = {'sr', 'sr.', 'senior', 'junior', 'mid', 'lead', 'staff',
                     'principal', 'chief', 'head', 'the', 'a', 'an', 'and',
                     'or', 'of', 'in', 'at', 'for', 'to', '(', ')', '（', '）'}
        role_keywords -= stopwords

        if not role_keywords:
            return 10

        # 計算 title/bio 與 role_name 的關鍵詞重疊
        matched_keywords = sum(1 for kw in role_keywords if kw in searchable)
        match_ratio = matched_keywords / len(role_keywords) if role_keywords else 0

        # 根據匹配度給分
        if match_ratio >= 0.5:
            return 45  # 標題高度相關（搜尋引擎找到 + 職稱匹配 → 至少 C 級）
        elif match_ratio > 0:
            return 35  # 部分相關
        else:
            # 檢查 profile 的技能是否出現在 title/bio 中
            all_skills = (
                [s['skill'] for s in profile.get('must_have', [])]
                + [s['skill'] for s in profile.get('core', [])]
            )
            # 也用 normalizer 提取
            searchable_skills = self.normalizer.extract_skills_from_text(searchable)
            profile_skill_set = set()
            for sk in all_skills:
                normalized = self.normalizer.normalize(sk)
                profile_skill_set.add(normalized)

            skill_overlap = bool(set(searchable_skills) & profile_skill_set)
            if skill_overlap:
                return 35  # 從 title/bio 提取到相關技能
            # 即使完全無法判斷，搜尋引擎找到代表有一定相關性
            return 25  # 搜尋引擎排名靠前 → 基線分

    def _calc_github_bonus(self, candidate: dict) -> int:
        """
        GitHub 活躍度加分（最多 +20）

        - public_repos >= 20: +3, >= 50: +5
        - followers >= 50: +3, >= 200: +5
        - recent_push within 90 days: +2
        - has quality repos (star > 10): +2
        - total_stars >= 50: +2, >= 200: +4
        - active contributor (push_count_90d > 5): +2
        """
        bonus = 0

        repos = candidate.get('public_repos', 0)
        if isinstance(repos, (int, float)):
            if repos >= 50:
                bonus += 5
            elif repos >= 20:
                bonus += 3

        followers = candidate.get('followers', 0)
        if isinstance(followers, (int, float)):
            if followers >= 200:
                bonus += 5
            elif followers >= 50:
                bonus += 3

        # 近期活動
        recent = candidate.get('recent_push', '')
        if recent:
            try:
                from datetime import datetime, timedelta
                push_date = datetime.strptime(recent[:10], '%Y-%m-%d')
                if (datetime.now() - push_date).days <= 90:
                    bonus += 2
            except (ValueError, TypeError):
                pass

        # 品質 repo（從 score_factors 取得）
        score_factors = candidate.get('score_factors', {})
        if isinstance(score_factors, dict):
            if score_factors.get('has_quality_repos'):
                bonus += 2

            # active contributor（90 天內 push > 5 次）
            if score_factors.get('is_active_contributor'):
                bonus += 2

        # 總 stars
        total_stars = (score_factors.get('total_stars', 0) if isinstance(score_factors, dict) else 0) \
                      or candidate.get('total_stars', 0)
        if isinstance(total_stars, (int, float)):
            if total_stars >= 200:
                bonus += 4
            elif total_stars >= 50:
                bonus += 2

        return min(bonus, GITHUB_BONUS_MAX)

    def _calc_grade(self, score: int) -> str:
        """計算等級"""
        if score >= GRADE_THRESHOLDS['A']:
            return 'A'
        elif score >= GRADE_THRESHOLDS['B']:
            return 'B'
        elif score >= GRADE_THRESHOLDS['C']:
            return 'C'
        else:
            return 'D'

    @staticmethod
    def score_to_detail_json(score_result: dict) -> str:
        """將評分結果轉為 JSON 字串（存入 Sheets）"""
        # 精簡版本，只保留關鍵資訊
        compact = {
            'matched': score_result.get('matched_skills', []),
            'missing': score_result.get('missing_critical', []),
            'rate': score_result.get('skill_match_rate', 0),
            'constraints': score_result.get('constraint_flags', []),
            'breakdown': {},
        }

        breakdown = score_result.get('breakdown', {})
        for key in ['must_have', 'core', 'nice_to_have']:
            if key in breakdown:
                b = breakdown[key]
                compact['breakdown'][key] = {
                    's': b.get('score', 0),
                    'm': b.get('max', 0),
                    'ok': b.get('matched', []),
                    'miss': b.get('missing', []),
                }

        if 'github_bonus' in breakdown:
            compact['gh_bonus'] = breakdown['github_bonus']

        return json.dumps(compact, ensure_ascii=False, separators=(',', ':'))

    @staticmethod
    def detail_json_to_display(json_str: str) -> dict:
        """將 JSON 字串轉為顯示用的結構"""
        try:
            data = json.loads(json_str) if isinstance(json_str, str) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

        display = {
            'matched_skills': data.get('matched', []),
            'missing_critical': data.get('missing', []),
            'skill_match_rate': data.get('rate', 0),
            'constraint_flags': data.get('constraints', []),
            'github_bonus': data.get('gh_bonus', 0),
            'sections': [],
        }

        label_map = {
            'must_have': '必備技能',
            'core': '核心技能',
            'nice_to_have': '加分技能',
        }

        for key in ['must_have', 'core', 'nice_to_have']:
            b = data.get('breakdown', {}).get(key, {})
            if b:
                display['sections'].append({
                    'label': label_map.get(key, key),
                    'score': b.get('s', 0),
                    'max': b.get('m', 0),
                    'matched': b.get('ok', []),
                    'missing': b.get('miss', []),
                })

        return display
