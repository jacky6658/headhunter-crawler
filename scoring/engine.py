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
    'A': 80,  # 強推薦
    'B': 60,  # 推薦
    'C': 40,  # 待評估
    # D: < 40  不推薦
}

# must_have 缺失懲罰因子
MUST_HAVE_PENALTY = 0.7  # 每缺 1 個 must_have → 總分 × 0.7

# GitHub 活躍度加分上限
GITHUB_BONUS_MAX = 10


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
        penalty = MUST_HAVE_PENALTY ** len(missing_must_have)
        penalized_score = raw_score * penalty

        # 6. GitHub 活躍度加分
        github_bonus = 0
        if candidate.get('source') == 'github' or candidate.get('github_username'):
            github_bonus = self._calc_github_bonus(candidate)
            penalized_score = min(100, penalized_score + github_bonus)

        # 7. 四捨五入
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

        # 6. work_history 提取（Phase 2 enrichment 結果）
        work_history = candidate.get('work_history', [])
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

    def _calc_github_bonus(self, candidate: dict) -> int:
        """
        GitHub 活躍度加分（最多 +10）

        - public_repos >= 20: +3
        - followers >= 50: +3
        - recent_push within 90 days: +2
        - has quality repos (star > 10): +2
        """
        bonus = 0

        repos = candidate.get('public_repos', 0)
        if isinstance(repos, (int, float)) and repos >= 20:
            bonus += 3

        followers = candidate.get('followers', 0)
        if isinstance(followers, (int, float)) and followers >= 50:
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
        if score_factors.get('has_quality_repos'):
            bonus += 2

        # 總 stars
        total_stars = score_factors.get('total_stars', 0) or \
                      candidate.get('total_stars', 0)
        if isinstance(total_stars, (int, float)) and total_stars >= 50:
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
