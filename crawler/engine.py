"""
搜尋引擎主控 — 整合 LinkedIn + GitHub + OCR + 去重 + Enrichment + 規則式評分

Pipeline:
  Phase 1: LinkedIn + GitHub 搜尋 → merge_and_dedup
  Phase 1.5: 相關性篩選
  Phase 2: ProfileEnricher 補完履歷（work_history, education, stability...）
  Phase 3: 規則式關鍵字評分 + match_tags 生成
"""
import json as _json
import logging
import os
import uuid
from datetime import datetime
from typing import Callable, List, Optional

from storage.models import Candidate, SearchTask
from crawler.anti_detect import AntiDetect
from crawler.linkedin import LinkedInSearcher
from crawler.github import GitHubSearcher
from crawler.ocr import CrawlerOCR
from crawler.dedup import DedupCache
from scoring.normalizer import SkillNormalizer
from scoring.engine import ScoringEngine
from scoring.job_profile import JobProfileManager

logger = logging.getLogger(__name__)


class SearchEngine:
    """整合所有搜尋來源 + Enrichment + 規則式評分"""

    def __init__(self, config: dict, task: SearchTask, job_context: dict = None):
        self.config = config
        self.task = task
        self.job_context = job_context  # Step1ne 職缺畫像 (Phase 0 提供)

        self.ad = AntiDetect(config)
        self.ocr = CrawlerOCR(config)
        self.dedup = DedupCache(
            config.get('dedup', {}).get('cache_file', 'data/dedup_cache.json')
        )

        self.linkedin_searcher = LinkedInSearcher(config, self.ad, self.ocr)
        self.github_searcher = GitHubSearcher(config, self.ad)

        # 技能評分系統 (fallback)
        base_dir = os.path.dirname(os.path.dirname(__file__))
        synonyms_path = os.path.join(base_dir, 'config', 'skills_synonyms.yaml')
        self.normalizer = SkillNormalizer(synonyms_path)
        self.scoring_engine = ScoringEngine(self.normalizer)
        self.profile_manager = JobProfileManager(
            os.path.join(base_dir, 'config', 'job_profiles')
        )

        # === Phase 2: ProfileEnricher (已實作模組) ===
        self.enricher = None
        enrichment_cfg = config.get('enrichment', {})
        if enrichment_cfg.get('enabled', False):
            try:
                from enrichment.profile_enricher import ProfileEnricher
                # ProfileEnricher 需要完整 config — enrichment_cfg 已包含 perplexity/jina/linkedin 子設定
                # 注意：不要用 config.get('perplexity', {}) 覆蓋，因為根層級沒有這些 key
                enricher_config = enrichment_cfg
                self.enricher = ProfileEnricher(enricher_config)
                logger.info("ProfileEnricher 已初始化")
            except Exception as e:
                logger.warning(f"ProfileEnricher 初始化失敗: {e}")

        self.on_progress: Optional[Callable] = None

    def execute(self) -> List[Candidate]:
        """
        執行搜尋任務 (4 Phase Pipeline):
          Phase 1: LinkedIn + GitHub 搜尋 → merge_and_dedup
          Phase 1.5: 相關性篩選
          Phase 2: ProfileEnricher 補完履歷
          Phase 3: 規則式關鍵字評分 + match_tags 生成
          Phase 4: 排序 + 儲存
        """
        skills = self.task.all_skills
        location_en = self.task.location
        location_zh = self.task.location_zh
        pages = self.task.pages
        brave_key = self.config.get('api_keys', {}).get('brave_api_key', '')

        total_phases = 3 if not self.enricher else 5
        logger.info(f"開始搜尋: {self.task.client_name}/{self.task.job_title} | "
                     f"技能={skills} | 地區={location_en} | 頁數={pages}")

        # 設定進度回呼
        if self.on_progress:
            self.linkedin_searcher.on_progress = self.on_progress
            self.github_searcher.on_progress = self.on_progress

        # ═══ Phase 1: 搜尋 ═══
        logger.info("[Phase 1] LinkedIn + GitHub 搜尋...")
        linkedin_result = self.linkedin_searcher.search_with_fallback(
            skills=skills,
            location_zh=location_zh,
            location_en=location_en,
            pages=pages,
            brave_key=brave_key,
            job_title=self.task.job_title,
            primary_skills=self.task.primary_skills,
            secondary_skills=self.task.secondary_skills,
        )
        linkedin_data = linkedin_result.get('data', [])

        github_result = self.github_searcher.search_users(
            skills=skills,
            location=location_en,
            pages=pages,
        )
        github_data = github_result.get('data', [])

        # 合併 + 去重
        candidates = self._merge_and_dedup(linkedin_data, github_data)

        logger.info(f"Phase 1 完成: LinkedIn={len(linkedin_data)} GitHub={len(github_data)} "
                     f"去重後={len(candidates)}")

        # ═══ Phase 1.5: 相關性篩選 ═══
        if candidates:
            before_count = len(candidates)
            candidates = self._filter_by_relevance(candidates)
            filtered_count = before_count - len(candidates)
            if filtered_count > 0:
                logger.info(f"[Phase 1.5] 相關性篩選: {before_count} → {len(candidates)} "
                           f"(過濾 {filtered_count} 位不相關候選人)")
            else:
                logger.info(f"[Phase 1.5] 相關性篩選: 全部 {len(candidates)} 位通過")

        # ═══ Phase 2: Enrichment (ProfileEnricher) ═══
        if self.enricher and candidates:
            logger.info(f"[Phase 2] ProfileEnricher 深度分析 {len(candidates)} 位候選人...")
            candidates = self._enrich_candidates(candidates)
        else:
            if not self.enricher:
                logger.info("[Phase 2] 跳過 (enrichment 未啟用)")
            else:
                logger.info("[Phase 2] 跳過 (無候選人)")

        # ═══ Phase 3: 規則式關鍵字評分 + match_tags ═══
        logger.info("[Phase 3] 規則式關鍵字評分 + match_tags 生成...")
        candidates = self._score_candidates(candidates)
        self._generate_match_tags(candidates)

        # ═══ Phase 4: 排序 ═══
        candidates.sort(key=lambda c: c.score, reverse=True)

        # 統計
        kw_grades = {}
        for c in candidates:
            g = c.grade or 'N/A'
            kw_grades[g] = kw_grades.get(g, 0) + 1
        logger.info(f"關鍵字評分結果: {kw_grades}")

        # 儲存去重快取
        self.dedup.save()

        return candidates

    def _score_candidates(self, candidates: List[Candidate]) -> List[Candidate]:
        """
        對所有候選人進行技能評分

        流程:
        1. 載入/生成 Job Profile（從任務的 skills）
        2. 對每個候選人計算分數
        3. 寫入 score + grade + score_detail
        """
        # 載入或生成 Job Profile
        job_profile = self.profile_manager.load_profile(
            client_name=self.task.client_name,
            job_title=self.task.job_title,
            primary_skills=self.task.primary_skills,
            secondary_skills=self.task.secondary_skills,
            location=self.task.location,
        )

        profile_data = job_profile.get('job_profile', job_profile)

        # 檢查是否有有效的 profile
        has_profile = bool(
            profile_data.get('must_have') or
            profile_data.get('core') or
            profile_data.get('nice_to_have')
        )

        if not has_profile:
            logger.warning("無有效的 Job Profile，跳過評分")
            return candidates

        logger.info(f"使用 Job Profile: must_have={len(profile_data.get('must_have', []))} "
                     f"core={len(profile_data.get('core', []))} "
                     f"nice_to_have={len(profile_data.get('nice_to_have', []))}")

        scored = 0
        for candidate in candidates:
            try:
                c_dict = candidate.to_dict()

                # GitHub 候選人可能有深度分析的額外資料
                # 已經在 _merge_and_dedup 中存入了 skills 欄位

                result = self.scoring_engine.score_candidate(c_dict, job_profile)
                candidate.score = result['total_score']
                candidate.grade = result['grade']
                candidate.score_detail = ScoringEngine.score_to_detail_json(result)
                scored += 1
            except Exception as e:
                logger.error(f"評分失敗 ({candidate.name}): {e}")

        logger.info(f"已評分 {scored}/{len(candidates)} 位候選人")
        return candidates

    # ── Phase 2: Enrichment ──────────────────────────────────

    def _enrich_candidates(self, candidates: List[Candidate]) -> List[Candidate]:
        """
        用 ProfileEnricher 補完候選人的工作經歷、教育背景、穩定性指標

        ProfileEnricher 已實作三層備援: LinkedIn API → Perplexity → Jina
        """
        # 轉為 dict list 給 ProfileEnricher
        cand_dicts = [c.to_dict() for c in candidates]

        def on_enrich_progress(completed, total, name):
            if self.on_progress:
                self.on_progress(completed, total, completed, f"enrichment ({name})")

        # 呼叫已實作的 enrich_batch
        enriched_list = self.enricher.enrich_batch(cand_dicts, on_progress=on_enrich_progress)

        # 寫回 Candidate 物件
        enriched_count = 0
        for i, enriched in enumerate(enriched_list):
            if not enriched:
                continue
            c = candidates[i]
            try:
                # 工作經歷
                wh = enriched.get('work_history', [])
                if wh and isinstance(wh, list):
                    c.work_history = wh

                # 教育背景
                ed = enriched.get('education_details', [])
                if ed and isinstance(ed, list):
                    c.education_details = ed

                # 數值指標
                c.years_experience = str(enriched.get('years_experience', ''))
                c.stability_score = str(enriched.get('stability_score', ''))
                c.job_changes = str(enriched.get('job_changes', ''))
                c.avg_tenure_months = str(enriched.get('avg_tenure_months', ''))
                c.recent_gap_months = str(enriched.get('recent_gap_months', ''))
                c.education = enriched.get('education', '')
                c.enrichment_source = enriched.get('_enrichment_source', '')

                # 合併新發現的技能
                new_skills = enriched.get('skills', '')
                if isinstance(new_skills, str) and new_skills:
                    new_skill_list = [s.strip() for s in new_skills.replace('、', ',').split(',') if s.strip()]
                elif isinstance(new_skills, list):
                    new_skill_list = new_skills
                else:
                    new_skill_list = []

                if new_skill_list:
                    existing = set(s.lower() for s in c.skills)
                    for ns in new_skill_list:
                        if ns.lower() not in existing:
                            c.skills.append(ns)
                            existing.add(ns.lower())

                # 更新職稱/公司 (如果 enrichment 提供了更完整的資料)
                if enriched.get('current_position') and not c.title:
                    c.title = enriched['current_position']
                if enriched.get('company') and not c.company:
                    c.company = enriched['company']

                if enriched.get('success', False):
                    enriched_count += 1

            except Exception as e:
                logger.error(f"Enrichment 寫回失敗 ({c.name}): {e}")
                c.enrichment_notes = f'enrichment error: {e}'

        logger.info(f"Phase 2 完成: {enriched_count}/{len(candidates)} 位成功充實")
        return candidates

    # ── Phase 3b: match_tags 生成 ─────────────────────────────

    def _generate_match_tags(self, candidates: List[Candidate]):
        """
        根據規則式評分結果，為每位候選人生成 match_tags
        寫入 ai_match_result 欄位（JSON），供 Step1ne AI 工作進度頁面使用

        match_tags 結構:
          skill_match: []    — 匹配到的技能列表
          title_match: bool  — 職缺名稱是否匹配
          experience_match: [] — 匹配到的工作經歷關鍵字
        """
        job_title = (self.task.job_title or '').lower()
        # 建立標題關鍵字集合（用於 title_match）
        title_words = set(w for w in job_title.split() if len(w) >= 2)

        for candidate in candidates:
            try:
                # skill_match: 從 score_detail 提取 matched_skills
                matched_skills = []
                if candidate.score_detail:
                    try:
                        detail = _json.loads(candidate.score_detail) if isinstance(candidate.score_detail, str) else candidate.score_detail
                        # score_detail 格式: {must_have: {matched:[], missing:[]}, core: {...}, ...}
                        for cat in ('must_have', 'core', 'nice_to_have'):
                            cat_data = detail.get(cat, {})
                            if isinstance(cat_data, dict):
                                matched_skills.extend(cat_data.get('matched', []))
                    except Exception:
                        pass

                # title_match: 候選人職稱是否包含職缺名稱關鍵字
                candidate_title = (getattr(candidate, 'title', '') or '').lower()
                title_match = False
                if title_words and candidate_title:
                    # 至少一半的職缺標題關鍵字出現在候選人職稱中
                    hit_count = sum(1 for w in title_words if w in candidate_title)
                    title_match = hit_count >= max(1, len(title_words) // 2)

                # experience_match: 從 work_history 提取相關工作經歷
                experience_match = []
                work_history = getattr(candidate, 'work_history', []) or []
                if isinstance(work_history, list):
                    all_skills_lower = set(s.lower() for s in (self.task.primary_skills or []))
                    for wh in work_history[:5]:  # 只看最近 5 段
                        if isinstance(wh, dict):
                            wh_title = (wh.get('title', '') or '').lower()
                            wh_desc = (wh.get('description', '') or '').lower()
                            wh_text = f"{wh_title} {wh_desc}"
                            # 檢查是否有技能或職稱關鍵字命中
                            hits = [s for s in all_skills_lower if s in wh_text]
                            if hits or any(w in wh_title for w in title_words if len(w) >= 3):
                                company = wh.get('company', '')
                                exp_label = f"{wh.get('title', '')} @ {company}" if company else wh.get('title', '')
                                if exp_label:
                                    experience_match.append(exp_label)

                # 寫入 ai_match_result
                match_tags = {
                    'skill_match': matched_skills,
                    'title_match': title_match,
                    'experience_match': experience_match,
                }
                candidate.ai_match_result = _json.dumps(
                    {'match_tags': match_tags},
                    ensure_ascii=False
                )

            except Exception as e:
                logger.error(f"match_tags 生成失敗 ({candidate.name}): {e}")

        tagged = sum(1 for c in candidates if c.ai_match_result)
        logger.info(f"match_tags 生成完成: {tagged}/{len(candidates)} 位")

    # ── Phase 1.5: 相關性篩選 ──────────────────────────────

    def _filter_by_relevance(self, candidates: List[Candidate]) -> List[Candidate]:
        """
        v3: Phase 1.5 — 根據職稱/技能做本地相關性篩選，過濾明顯不相關的候選人

        邏輯：
        - 沒有文字資訊 → 保留（給 enrichment 機會）
        - 有關鍵字命中 → 保留
        - 無命中 + 明確不相關職稱 → 過濾
        - 無命中 + 不確定 → 保留
        """
        import re

        # 建立搜尋關鍵字集合
        keywords = set()
        if self.task.job_title:
            for w in self.task.job_title.lower().split():
                if len(w) >= 2:
                    keywords.add(w)
        for skill in (self.task.primary_skills or []):
            keywords.add(skill.lower().strip())
        for skill in (self.task.secondary_skills or []):
            keywords.add(skill.lower().strip())

        if not keywords:
            return candidates  # 沒有關鍵字可篩 → 全部保留

        # 不相關職稱模式
        UNRELATED_PATTERNS = [
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

        kept = []
        for c in candidates:
            # 蒐集候選人的可搜尋文字
            text_parts = []
            if c.title:
                text_parts.append(c.title.lower())
            if c.bio:
                text_parts.append(c.bio.lower())
            if c.skills:
                if isinstance(c.skills, list):
                    text_parts.extend(s.lower() for s in c.skills)
                elif isinstance(c.skills, str):
                    text_parts.append(c.skills.lower())
            searchable = ' '.join(text_parts)

            # 沒有文字資訊 → 保留
            if not searchable.strip():
                kept.append(c)
                continue

            # 有關鍵字命中 → 保留
            has_match = any(kw in searchable for kw in keywords)
            if has_match:
                kept.append(c)
                continue

            # 無命中 — 檢查是否明確不相關
            is_unrelated = False
            for pattern in UNRELATED_PATTERNS:
                if re.search(pattern, searchable, re.IGNORECASE):
                    is_unrelated = True
                    break

            if is_unrelated:
                logger.debug(f"Phase 1.5 過濾: {c.name} (職稱不相關)")
                continue  # 過濾掉

            # 無命中 + 不確定 → 保留（給 enrichment/AI 評分機會）
            kept.append(c)

        return kept

    def _merge_and_dedup(self, linkedin_data: list, github_data: list) -> List[Candidate]:
        """合併 LinkedIn + GitHub 結果，去重"""
        candidates = []
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        today = datetime.now().strftime('%Y-%m-%d')

        # LinkedIn 候選人
        for item in linkedin_data:
            li_url = item.get('linkedin_url', '')
            if self.dedup.is_seen(linkedin_url=li_url):
                continue
            self.dedup.mark_seen(linkedin_url=li_url)

            # 基本驗證
            name = item.get('name', '').strip()
            if not name or len(name) < 2:
                continue

            source = 'li+ocr' if item.get('ocr_used') else 'linkedin'

            candidates.append(Candidate(
                id=str(uuid.uuid4()),
                name=name,
                source=source,
                linkedin_url=li_url,
                linkedin_username=item.get('linkedin_username', ''),
                location=item.get('location', ''),
                bio=item.get('bio', ''),
                company=item.get('company', ''),
                title=item.get('bio', ''),  # LinkedIn bio 通常是職稱
                skills=item.get('skills', []),
                client_name=self.task.client_name,
                job_title=self.task.job_title,
                task_id=self.task.id,
                search_date=today,
                status='new',
                created_at=now,
            ))

        # GitHub 候選人
        for item in github_data:
            gh_username = item.get('github_username', '')
            if self.dedup.is_seen(github_username=gh_username):
                continue
            self.dedup.mark_seen(github_username=gh_username)

            name = item.get('name', '').strip()
            if not name or len(name) < 2:
                continue

            skills = item.get('skills', [])
            if isinstance(skills, str):
                skills = [s.strip() for s in skills.split(',') if s.strip()]

            # 如果有 tech_stack（深度分析結果），合併到 skills
            tech_stack = item.get('tech_stack', [])
            if tech_stack:
                combined = list(set(skills + [t for t in tech_stack if isinstance(t, str)]))
                skills = combined

            candidates.append(Candidate(
                id=str(uuid.uuid4()),
                name=name,
                source='github',
                github_url=item.get('github_url', ''),
                github_username=gh_username,
                email=item.get('email', ''),
                location=item.get('location', ''),
                bio=item.get('bio', ''),
                company=item.get('company', ''),
                skills=skills,
                public_repos=item.get('public_repos', 0),
                followers=item.get('followers', 0),
                client_name=self.task.client_name,
                job_title=self.task.job_title,
                task_id=self.task.id,
                search_date=today,
                status='new',
                created_at=now,
            ))

        return candidates
