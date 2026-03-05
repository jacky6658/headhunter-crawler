"""
搜尋引擎主控 — 整合 LinkedIn + GitHub + OCR + 去重 + 技能評分
Worker 進程內的主要入口
"""
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
    """整合所有搜尋來源 + 技能評分"""

    def __init__(self, config: dict, task: SearchTask):
        self.config = config
        self.task = task

        self.ad = AntiDetect(config)
        self.ocr = CrawlerOCR(config)
        self.dedup = DedupCache(
            config.get('dedup', {}).get('cache_file', 'data/dedup_cache.json')
        )

        self.linkedin_searcher = LinkedInSearcher(config, self.ad, self.ocr)
        self.github_searcher = GitHubSearcher(config, self.ad)

        # 技能評分系統
        base_dir = os.path.dirname(os.path.dirname(__file__))
        synonyms_path = os.path.join(base_dir, 'config', 'skills_synonyms.yaml')
        self.normalizer = SkillNormalizer(synonyms_path)
        self.scoring_engine = ScoringEngine(self.normalizer)
        self.profile_manager = JobProfileManager(
            os.path.join(base_dir, 'config', 'job_profiles')
        )

        self.on_progress: Optional[Callable] = None

    def execute(self) -> List[Candidate]:
        """
        執行搜尋任務:
        1. LinkedIn 搜尋（4 層備援）
        2. GitHub 搜尋（多 token + 深度分析）
        3. 合併 + 去重
        4. 技能評分
        5. 按分數排序
        """
        skills = self.task.all_skills
        location_en = self.task.location
        location_zh = self.task.location_zh
        pages = self.task.pages
        brave_key = self.config.get('api_keys', {}).get('brave_api_key', '')

        logger.info(f"開始搜尋: {self.task.client_name}/{self.task.job_title} | "
                     f"技能={skills} | 地區={location_en} | 頁數={pages}")

        # 設定進度回呼
        if self.on_progress:
            self.linkedin_searcher.on_progress = self.on_progress
            self.github_searcher.on_progress = self.on_progress

        # 1. LinkedIn 搜尋
        logger.info("[1/3] LinkedIn 搜尋...")
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
        linkedin_source = linkedin_result.get('source', '')

        # 2. GitHub 搜尋（含深度分析）
        logger.info("[2/3] GitHub 搜尋 + 深度分析...")
        github_result = self.github_searcher.search_users(
            skills=skills,
            location=location_en,
            pages=pages,
        )
        github_data = github_result.get('data', [])

        # 3. 合併 + 去重
        candidates = self._merge_and_dedup(linkedin_data, github_data)

        logger.info(f"搜尋完成: LinkedIn={len(linkedin_data)} GitHub={len(github_data)} "
                     f"去重後={len(candidates)}")

        # 4. 技能評分
        logger.info("[3/3] 技能評分...")
        candidates = self._score_candidates(candidates)

        # 5. 按分數排序（高 → 低）
        candidates.sort(key=lambda c: c.score, reverse=True)

        logger.info(f"評分完成: A={sum(1 for c in candidates if c.grade == 'A')} "
                     f"B={sum(1 for c in candidates if c.grade == 'B')} "
                     f"C={sum(1 for c in candidates if c.grade == 'C')} "
                     f"D={sum(1 for c in candidates if c.grade == 'D')}")

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
