"""
ContextualScorer — 綜合職缺匹配評分器

功能:
1. 從 Step1ne 取得職缺的 JD + 企業畫像 + 人才畫像
2. 用 Perplexity 做 5 維度加權評分
3. 產出 ai_match_result (JSONB) — 與 Step1ne AiMatchResult 介面完全一致
4. 產出人選分析報告文字 (寫入 notes)
5. 多職缺自動匹配推薦 (Top N)
6. 分類面談問題生成
"""

import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

from .perplexity_client import PerplexityClient
from .prompts import JOB_MATCH_PROMPT, JOB_MATCH_SYSTEM_PROMPT, JOB_MATCH_USER_PROMPT, ANALYSIS_REPORT_TEMPLATE

logger = logging.getLogger(__name__)

# 評分等級對應
GRADE_MAP = {
    range(85, 101): ('S', '強力推薦'),
    range(80, 85): ('A+', '強力推薦'),
    range(70, 80): ('A', '推薦'),
    range(55, 70): ('B', '觀望'),
    range(0, 55): ('C', '不推薦'),
}


def get_grade_and_recommendation(score: int) -> tuple:
    """根據分數取得等級和推薦"""
    score = max(0, min(100, score))
    for score_range, (grade, rec) in GRADE_MAP.items():
        if score in score_range:
            return grade, rec
    return 'C', '不推薦'


class ContextualScorer:
    """
    綜合職缺匹配評分器
    與 Step1ne SCORING-GUIDE.md 的 ai_match_result 格式完全一致
    """

    # v3: 無效候選人名稱黑名單 — 爬蟲有時會抓到 LinkedIn 登入頁或廣告
    INVALID_NAME_PATTERNS = [
        'LinkedIn', '登入', '註冊', 'Sign in', 'Sign up', 'Log in',
        'HRnetGroup', '加入 LinkedIn', '同意並加入', 'Join LinkedIn',
        'People also viewed', '其他人也看了', 'LinkedIn Premium',
    ]

    # v4 P0: 明確不相關的職稱模式（用於預篩，避免浪費 AI API call）
    UNRELATED_TITLE_PATTERNS = [
        r'\b(sales|salesperson|業務|銷售)\b',
        r'\b(marketing|行銷)\b',
        r'\b(hr|human resources|人資|人力資源)\b',
        r'\b(legal|法務|律師|lawyer|attorney)\b',
        r'\b(nurse|護理|護士|醫師|doctor|physician)\b',
        r'\b(teacher|教師|教授|professor)\b',
        r'\b(driver|司機|駕駛)\b',
        r'\b(chef|廚師|cook)\b',
        r'\b(receptionist|前台|櫃台)\b',
    ]

    def __init__(self, config: dict, step1ne_client=None):
        """
        Args:
            config: enrichment 設定區塊
            step1ne_client: Step1neClient 實例（用於取得職缺資料）
        """
        self.config = config
        self.step1ne = step1ne_client

        perplexity_key = config.get('perplexity', {}).get('api_key', '')
        perplexity_config = config.get('perplexity', {})
        self.perplexity = PerplexityClient(perplexity_key, perplexity_config) if perplexity_key else None

        # v4 P1: scoring 用較便宜的模型（sonar），enrichment 維持 sonar-pro
        self.scoring_model = config.get('perplexity', {}).get('scoring_model', 'sonar')

        self._jobs_cache = {}  # {job_id: job_data}
        self._all_jobs_cache = None  # 快取所有 Open 職缺
        self._job_system_cache = {}  # v4 P3: 快取 job system prompt

    def score_with_job_context(self, enriched_candidate: dict, job_id: int) -> dict:
        """
        對指定職缺做深度匹配評分

        Args:
            enriched_candidate: ProfileEnricher 產出的充實候選人資料
            job_id: Step1ne 職缺 ID

        Returns:
            dict: {
                'ai_match_result': {...},  # JSONB — 完全對應 AiMatchResult 介面
                'talent_level': str,        # S/A+/A/B/C
                'report': str,              # 人選分析報告文字
                'success': bool,
            }
        """
        # 取得職缺資料
        job = self._fetch_job_context(job_id)
        if not job:
            logger.warning(f"無法取得職缺 {job_id} 的資料，使用簡化評分")
            return self._simple_score(enriched_candidate)

        # 用 Perplexity 做深度評分
        if self.perplexity and self.perplexity.is_available():
            return self._ai_score(enriched_candidate, job)

        # 無 Perplexity → 規則式評分
        return self._rule_based_score(enriched_candidate, job)

    def score_with_task_context(self, enriched_candidate: dict, task: dict) -> dict:
        """
        根據爬蟲任務 context 評分

        如果任務有 step1ne_job_id → 取得完整職缺資訊
        如果沒有 → 用任務的 primary_skills/secondary_skills 做簡化評分
        """
        job_id = task.get('step1ne_job_id')
        if job_id:
            return self.score_with_job_context(enriched_candidate, int(job_id))

        # 簡化評分（沒有 job_id）
        return self._simple_score(enriched_candidate, task)

    def recommend_jobs(self, enriched_candidate: dict, top_n: int = 3) -> list:
        """
        自動匹配所有開放職缺，回傳 Top N 推薦

        流程:
        1. 取得所有 Open 職缺
        2. 本地預篩 (skill overlap)
        3. Top N 用 Perplexity 深度評分

        Returns:
            list of dict: [{
                'job_id': int,
                'position_name': str,
                'client_company': str,
                'match_score': int,
                'recommendation': str,
                'match_reasons': [str],
                'ai_match_result': dict,
            }]
        """
        # 取得所有開放職缺
        all_jobs = self._fetch_all_open_jobs()
        if not all_jobs:
            logger.warning("沒有開放職缺可匹配")
            return []

        # 提取候選人技能
        candidate_skills = self._extract_skills(enriched_candidate)

        # 本地預篩
        scored_jobs = []
        for job in all_jobs:
            quick_score = self._quick_skill_match(candidate_skills, job)
            scored_jobs.append((quick_score, job))

        # 排序取 Top N
        scored_jobs.sort(key=lambda x: x[0], reverse=True)
        top_jobs = scored_jobs[:top_n]

        # 深度評分
        recommendations = []
        for quick_score, job in top_jobs:
            if self.perplexity and self.perplexity.is_available():
                result = self._ai_score(enriched_candidate, job)
                ai_match = result.get('ai_match_result', {})
            else:
                result = self._rule_based_score(enriched_candidate, job)
                ai_match = result.get('ai_match_result', {})

            recommendations.append({
                'job_id': job.get('id'),
                'position_name': job.get('position_name', ''),
                'client_company': job.get('client_company', ''),
                'match_score': ai_match.get('score', quick_score),
                'recommendation': ai_match.get('recommendation', ''),
                'match_reasons': ai_match.get('strengths', [])[:2],
                'ai_match_result': ai_match,
            })

        # 按最終分數排序
        recommendations.sort(key=lambda x: x['match_score'], reverse=True)
        return recommendations

    def should_ai_score(self, enriched: dict, job: dict) -> bool:
        """
        P0: 預篩 — 判斷候選人是否值得花 API call 做 AI 評分

        回傳 True = 需要 AI 評分, False = 跳過（用 rule-based 評分）

        跳過條件（同時滿足才跳過）：
        1. quick_skill_match 分數 = 0（完全無技能重疊）
        2. 候選人有足夠已知資訊（≥3 個技能），代表我們有足夠信心判斷不相關
        或
        1. 職稱明確不相關（sales/HR/legal 等）且 quick_score < 10
        """
        import re

        # 提取候選人技能
        candidate_skills = self._extract_skills(enriched)
        quick_score = self._quick_skill_match(candidate_skills, job)

        # 條件 1: quick_score = 0 且有足夠已知技能
        if quick_score == 0 and len(candidate_skills) >= 3:
            name = enriched.get('name', '?')
            logger.info(f"P0 預篩跳過: {name} (quick_score=0, 有 {len(candidate_skills)} 個已知技能但完全不匹配)")
            return False

        # 條件 2: 職稱明確不相關 + quick_score 很低
        if quick_score < 10:
            title = (enriched.get('current_position') or enriched.get('title', '')).lower()
            if title:
                for pattern in self.UNRELATED_TITLE_PATTERNS:
                    if re.search(pattern, title, re.IGNORECASE):
                        name = enriched.get('name', '?')
                        logger.info(f"P0 預篩跳過: {name} (職稱不相關: {title[:40]}, quick_score={quick_score})")
                        return False

        return True

    def _ai_score(self, enriched: dict, job: dict) -> dict:
        """用 Perplexity AI 做深度評分（v3: 三道閘門 + v4: prompt 拆分）"""
        try:
            # v3: 過濾無效候選人名稱
            candidate_name = enriched.get('name', '')
            for pattern in self.INVALID_NAME_PATTERNS:
                if pattern.lower() in candidate_name.lower():
                    logger.info(f"跳過無效候選人: {candidate_name} (匹配黑名單: {pattern})")
                    return {
                        'ai_match_result': {
                            'score': 0, 'grade': 'C', 'recommendation': '不推薦',
                            'job_id': job.get('id'), 'job_title': job.get('position_name', ''),
                            'conclusion': f'無效候選人：名稱「{candidate_name}」為 LinkedIn 系統頁面，非真實候選人',
                            'relevance_check': {'is_relevant': False, 'relevance_note': '非真實候選人'},
                            'evaluated_by': 'Crawler-enricher-v4',
                        },
                        'talent_level': 'C',
                        'report': f'無效候選人: {candidate_name}',
                        'success': True,
                    }

            # 組合候選人資料文字
            candidate_profile = self._format_candidate_profile(enriched)

            # v4 P3: 拆分 prompt — system（job context）+ user（candidate）
            job_id = job.get('id') or job.get('jobId') or job.get('job_id')

            if job_id and job_id not in self._job_system_cache:
                self._job_system_cache[job_id] = JOB_MATCH_SYSTEM_PROMPT.format(
                    position_name=job.get('position_name', ''),
                    client_company=job.get('client_company', ''),
                    talent_profile=job.get('talent_profile', '（未提供）'),
                    job_description=job.get('job_description', '（未提供）'),
                    company_profile=job.get('company_profile', '（未提供）'),
                    consultant_notes=job.get('consultant_notes', '（無）'),
                    key_skills=job.get('key_skills', ''),
                    experience_required=job.get('experience_required', ''),
                    location=job.get('location', '（未指定）'),
                )
                logger.info(f"v4 P3: job system prompt cached for job_id={job_id}")

            system_prompt = self._job_system_cache.get(job_id)
            user_prompt = JOB_MATCH_USER_PROMPT.format(candidate_profile=candidate_profile)

            # v4 P1: scoring 用 sonar（便宜）; P3: 拆分 system/user prompt
            raw = self.perplexity.analyze_profile(
                '', user_prompt,
                model_override=self.scoring_model,
                system_prompt=system_prompt,
            )

            if not raw or not raw.get('success'):
                logger.warning(f"Perplexity 評分失敗: {raw.get('error', '?')}")
                return self._rule_based_score(enriched, job)

            # v3: 三道閘門後處理
            relevance_check = raw.get('relevance_check', {})
            if not isinstance(relevance_check, dict):
                relevance_check = {}

            raw_score = int(raw.get('score', 50))
            capped = False
            cap_reasons = []

            # 閘門 A: 相關性 — 不相關則 max 25
            if relevance_check.get('is_relevant') is False:
                if raw_score > 25:
                    logger.info(f"閘門A(相關性): {enriched.get('name', '?')} 不相關, {raw_score} → 25")
                    raw_score = 25
                    capped = True
                    cap_reasons.append('相關性不符')

            # 閘門 B: 地理位置 — 海外無台灣關聯 max 40
            location_gate = relevance_check.get('location_gate', '')
            if 'fail' in str(location_gate).lower():
                if raw_score > 40:
                    logger.info(f"閘門B(地點): {enriched.get('name', '?')} 地點不符, {raw_score} → 40")
                    raw_score = min(raw_score, 40)
                    capped = True
                    cap_reasons.append('工作地點不符')

            # 閘門 C: Overqualified — 嚴重超資格扣 15-25 分
            seniority_gate = relevance_check.get('seniority_gate', '')
            if 'overqualified' in str(seniority_gate).lower():
                penalty = 20
                logger.info(f"閘門C(職級): {enriched.get('name', '?')} overqualified, {raw_score} → {raw_score - penalty}")
                raw_score = max(0, raw_score - penalty)
                cap_reasons.append('段位過高(overqualified)')

            if capped or cap_reasons:
                raw['score'] = raw_score
                if raw_score < 40:
                    raw['recommendation'] = '不推薦'
                elif raw_score < 55:
                    raw['recommendation'] = '不推薦'

            # 建構 ai_match_result
            return self._build_result(raw, enriched, job)

        except Exception as e:
            logger.error(f"AI 評分異常: {e}", exc_info=True)
            return self._rule_based_score(enriched, job)

    def _build_result(self, raw: dict, enriched: dict, job: dict) -> dict:
        """從 Perplexity 回傳建構最終結果"""
        score = int(raw.get('score', 50))
        grade, recommendation = get_grade_and_recommendation(score)

        # 確保 recommendation 使用 Perplexity 回傳的（如果合法）
        raw_rec = raw.get('recommendation', '')
        if raw_rec in ('強力推薦', '推薦', '觀望', '不推薦'):
            recommendation = raw_rec

        ai_match_result = {
            'score': score,
            'grade': grade,  # v3: 明確寫入 grade 到 ai_match_result
            'recommendation': recommendation,
            'job_id': job.get('id'),
            'job_title': raw.get('job_title') or job.get('position_name', ''),
            'matched_skills': raw.get('matched_skills', []),
            'missing_skills': raw.get('missing_skills', []),
            'strengths': raw.get('strengths', []),
            'probing_questions': raw.get('probing_questions', []),
            'salary_fit': raw.get('salary_fit', ''),
            'career_trajectory': raw.get('career_trajectory', {}),
            'company_dna_analysis': raw.get('company_dna_analysis', {}),
            'conclusion': raw.get('conclusion', ''),
            'evaluated_at': datetime.now().isoformat() + 'Z',
            'evaluated_by': 'Crawler-enricher-v4',
        }

        # 生成報告
        report = self.generate_report(enriched, ai_match_result, job)

        return {
            'ai_match_result': ai_match_result,
            'talent_level': grade,
            'report': report,
            'success': True,
        }

    def _rule_based_score(self, enriched: dict, job: dict = None) -> dict:
        """規則式評分（無 Perplexity 時的降級方案）"""
        candidate_skills = self._extract_skills(enriched)
        job_skills = []
        if job:
            job_skills_str = job.get('key_skills', '')
            job_skills = [s.strip().lower() for s in job_skills_str.replace('、', ',').split(',') if s.strip()]

        # 技能匹配
        matched = []
        missing = []
        for skill in job_skills:
            if any(skill in cs for cs in candidate_skills):
                matched.append(skill)
            else:
                missing.append(skill)

        # 計算分數
        skill_rate = len(matched) / max(len(job_skills), 1)
        base_score = int(skill_rate * 70)  # 技能最多 70 分

        # 可觸達性加分
        has_linkedin = bool(enriched.get('linkedin_url') or enriched.get('_enrichment_source'))
        has_github = bool(enriched.get('github_url'))
        reach_score = 0
        if has_linkedin and has_github:
            reach_score = 10
        elif has_linkedin:
            reach_score = 6

        # 活躍加分
        activity_score = 3 if has_github else 2

        total_score = min(100, base_score + reach_score + activity_score)
        grade, recommendation = get_grade_and_recommendation(total_score)

        position_name = job.get('position_name', '未知職缺') if job else '未知職缺'
        client_company = job.get('client_company', '') if job else ''

        ai_match_result = {
            'score': total_score,
            'recommendation': recommendation,
            'job_id': job.get('id') if job else None,
            'job_title': position_name,
            'matched_skills': matched,
            'missing_skills': missing,
            'strengths': [f'技能匹配率 {skill_rate:.0%}'] if matched else [],
            'probing_questions': [
                '[初步] 目前是否在職、是否 Open to Work？',
                '[初步] 期望薪資範圍與最快到職時間？',
                '[技術] 請詳述您的核心技術經驗',
            ],
            'salary_fit': '需進一步確認',
            'conclusion': f'規則式評分（AI 評分不可用）。技能匹配率 {skill_rate:.0%}，建議人工複查。',
            'evaluated_at': datetime.now().isoformat() + 'Z',
            'evaluated_by': 'Crawler-rule-scorer',
        }

        report = self.generate_report(enriched, ai_match_result, job or {})

        return {
            'ai_match_result': ai_match_result,
            'talent_level': grade,
            'report': report,
            'success': True,
        }

    def _simple_score(self, enriched: dict, task: dict = None) -> dict:
        """簡化評分（沒有 job 資料時）"""
        job_mock = {}
        if task:
            primary = task.get('primary_skills', [])
            secondary = task.get('secondary_skills', [])
            all_skills = primary + secondary
            job_mock = {
                'position_name': task.get('job_title', ''),
                'client_company': task.get('client_name', ''),
                'key_skills': '、'.join(all_skills),
            }
        return self._rule_based_score(enriched, job_mock)

    def generate_report(self, enriched: dict, ai_match: dict, job: dict) -> str:
        """
        生成人選分析報告文字

        Args:
            enriched: ProfileEnricher 的輸出
            ai_match: ai_match_result JSONB
            job: Step1ne 職缺資料

        Returns:
            str: 格式化純文字報告
        """
        score = ai_match.get('score', 0)
        grade, _ = get_grade_and_recommendation(score)

        # 格式化優勢
        strengths = ai_match.get('strengths', [])
        strengths_text = '\n'.join(f'- {s}' for s in strengths) if strengths else '- 待分析'

        # 格式化待確認
        missing = ai_match.get('missing_skills', [])
        missing_text = '\n'.join(f'- {m}' for m in missing) if missing else '- 無'

        # 格式化面談問題
        questions = ai_match.get('probing_questions', [])
        questions_text = '\n'.join(f'- {q}' for q in questions) if questions else '- 待生成'

        # 技能摘要
        skills = enriched.get('skills', '')
        if isinstance(skills, list):
            skills = '、'.join(skills)
        skills_summary = skills[:80] + '...' if len(skills) > 80 else skills

        try:
            report = ANALYSIS_REPORT_TEMPLATE.format(
                score=score,
                grade=grade,
                date=datetime.now().strftime('%Y-%m-%d'),
                source=enriched.get('_enrichment_source', 'AI'),
                position_name=job.get('position_name', '未指定'),
                client_company=job.get('client_company', ''),
                current_position=enriched.get('current_position', '未知'),
                company=enriched.get('company', '未知'),
                years_experience=enriched.get('years_experience', '?'),
                job_changes=enriched.get('job_changes', '?'),
                avg_tenure_months=enriched.get('avg_tenure_months', '?'),
                education=enriched.get('education', '未知'),
                skills_summary=skills_summary,
                strengths=strengths_text,
                missing_items=missing_text,
                stability_score=enriched.get('stability_score', '?'),
                recent_gap_months=enriched.get('recent_gap_months', '?'),
                probing_questions=questions_text,
                job_recommendations='（見 AI 匹配結語分頁）',
                conclusion=ai_match.get('conclusion', '待分析'),
            )
        except KeyError as e:
            logger.warning(f"報告模板欄位缺失: {e}")
            report = f"【AI評分 {score}分 / {grade}】{datetime.now().strftime('%Y-%m-%d')}\n{ai_match.get('conclusion', '')}"

        return report

    def _fetch_job_context(self, job_id: int) -> Optional[dict]:
        """從 Step1ne 取得職缺資料（含快取）"""
        if job_id in self._jobs_cache:
            return self._jobs_cache[job_id]

        if not self.step1ne:
            logger.warning("Step1ne client 未設定，無法取得職缺資料")
            return None

        try:
            job = self.step1ne.fetch_job_detail(job_id)
            if job:
                self._jobs_cache[job_id] = job
                return job
        except Exception as e:
            logger.error(f"取得職缺 {job_id} 失敗: {e}")

        return None

    def _fetch_all_open_jobs(self) -> list:
        """取得所有 Open 狀態的職缺"""
        if self._all_jobs_cache is not None:
            return self._all_jobs_cache

        if not self.step1ne:
            return []

        try:
            jobs = self.step1ne.fetch_jobs(status='Open')
            self._all_jobs_cache = jobs or []
            return self._all_jobs_cache
        except Exception as e:
            logger.error(f"取得職缺列表失敗: {e}")
            return []

    def _extract_skills(self, enriched: dict) -> list:
        """從充實資料中提取技能列表（正規化小寫）"""
        skills = enriched.get('skills', '')
        if isinstance(skills, list):
            return [s.strip().lower() for s in skills if s.strip()]
        return [s.strip().lower() for s in skills.replace('、', ',').split(',') if s.strip()]

    def _quick_skill_match(self, candidate_skills: list, job: dict) -> int:
        """
        本地快速技能匹配（不呼叫 API）
        用於預篩，回傳 0-100 的粗略匹配分數
        """
        job_skills_str = job.get('key_skills', '')
        job_skills = [s.strip().lower() for s in job_skills_str.replace('、', ',').split(',') if s.strip()]

        if not job_skills:
            return 30  # 無技能要求，給中等分

        matched = sum(1 for js in job_skills if any(js in cs for cs in candidate_skills))
        return int((matched / len(job_skills)) * 100)

    @staticmethod
    def _format_candidate_profile(enriched: dict) -> str:
        """
        將充實資料格式化為 prompt 用的文字

        增強版：
        - 每段工作經歷附帶公司規模/產業提示（供 AI 做公司 DNA 分析）
        - 職涯軌跡摘要（方向 + 穩定度模式）
        """
        parts = []
        parts.append(f"姓名: {enriched.get('name', '?')}")
        parts.append(f"現職: {enriched.get('current_position', '?')} @ {enriched.get('company', '?')}")
        parts.append(f"地點: {enriched.get('location', '?')}")
        parts.append(f"年資: {enriched.get('years_experience', '?')} 年")
        parts.append(f"學歷: {enriched.get('education', '?')}")

        skills = enriched.get('skills', '')
        if isinstance(skills, list):
            skills = '、'.join(skills)
        parts.append(f"技能: {skills}")

        # 工作經歷（增強版：附帶公司背景提示）
        work_history = enriched.get('work_history', [])
        if work_history:
            parts.append("\n工作經歷（請注意各公司的規模、產業、文化特徵，用於公司 DNA 適配分析）:")
            titles_progression = []  # 追蹤職稱變化
            for wh in work_history[:6]:
                company_name = wh.get('company', '?')
                title = wh.get('title', '?')
                duration = wh.get('duration', '?')
                desc = wh.get('description', '')

                parts.append(f"  - {title} @ {company_name} ({duration})")
                if desc:
                    parts.append(f"    職責: {desc[:150]}")

                titles_progression.append(title)

            # 職涯軌跡摘要
            if len(titles_progression) >= 2:
                parts.append(f"\n  [職涯軌跡提示] 職稱變化: {' → '.join(reversed(titles_progression))}")
        else:
            parts.append("\n工作經歷: (無資料 — 未經 enrichment 或 enrichment 失敗)")

        # 學歷
        edu = enriched.get('education_details', [])
        if edu:
            parts.append("\n學歷:")
            for e in edu:
                parts.append(f"  - {e.get('school', '?')} {e.get('degree', '?')} {e.get('field', '')} ({e.get('year', '?')})")

        # 穩定性指標
        stability = enriched.get('stability_score', '?')
        avg_tenure = enriched.get('avg_tenure_months', '?')
        job_changes = enriched.get('job_changes', '?')
        recent_gap = enriched.get('recent_gap_months', '?')

        parts.append(f"\n穩定性指標:")
        parts.append(f"  - 穩定度分數: {stability}/100")
        parts.append(f"  - 平均任期: {avg_tenure} 個月")
        parts.append(f"  - 總換工作次數: {job_changes} 次")
        parts.append(f"  - 最近空窗: {recent_gap} 個月")

        # 穩定度模式提示
        try:
            avg_m = float(avg_tenure) if avg_tenure and avg_tenure != '?' else 0
            jc = int(job_changes) if job_changes and job_changes != '?' else 0
            gap_m = float(recent_gap) if recent_gap and recent_gap != '?' else 0

            flags = []
            if avg_m > 0 and avg_m < 18:
                flags.append("平均任期偏短（<18個月）")
            if jc > 5:
                flags.append(f"換工作頻繁（{jc}次）")
            if gap_m > 6:
                flags.append(f"近期空窗較長（{gap_m}個月）")
            if avg_m >= 36:
                flags.append("任期穩定（平均>3年）")

            if flags:
                parts.append(f"  - [穩定度提示] {'; '.join(flags)}")
        except (ValueError, TypeError):
            pass

        # LinkedIn/GitHub 觸達性
        parts.append("\n觸達管道:")
        if enriched.get('linkedin_url'):
            parts.append(f"  - LinkedIn: {enriched['linkedin_url']}")
        if enriched.get('github_url'):
            parts.append(f"  - GitHub: {enriched['github_url']}")
        if enriched.get('email'):
            parts.append(f"  - Email: {enriched['email']}")
        if not enriched.get('linkedin_url') and not enriched.get('github_url'):
            parts.append("  - (無公開聯繫管道)")

        return '\n'.join(parts)

    def clear_cache(self):
        """清除職缺快取"""
        self._jobs_cache.clear()
        self._all_jobs_cache = None
        self._job_system_cache.clear()  # v4: 清除 system prompt 快取
