"""
資料模型 — Candidate + SearchTask dataclass
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Optional


@dataclass
class Candidate:
    """爬蟲找到的候選人"""
    id: str = ""                       # 唯一 ID (uuid)
    name: str = ""
    source: str = ""                   # "linkedin" / "github" / "li+ocr"
    github_url: str = ""
    github_username: str = ""
    linkedin_url: str = ""
    linkedin_username: str = ""
    email: str = ""
    location: str = ""
    bio: str = ""
    company: str = ""
    title: str = ""                    # 職稱 / headline
    skills: List[str] = field(default_factory=list)
    public_repos: int = 0
    followers: int = 0
    recent_push: str = ""
    top_repos: List[str] = field(default_factory=list)

    # 搜尋來源追蹤
    client_name: str = ""              # 客戶名稱
    job_title: str = ""                # 職缺名稱
    task_id: str = ""                  # 關聯任務 ID
    search_date: str = ""             # 搜尋日期

    # 狀態
    status: str = "new"                # new / imported / reviewed / skipped
    created_at: str = ""

    # 評分（技能評分系統）
    score: int = 0                     # 總分 0-100
    grade: str = ""                    # A / B / C / D / ""(未評分)
    score_detail: str = ""             # JSON 字串: 評分細項

    # === Enrichment (Phase 2: ProfileEnricher) ===
    work_history: List[dict] = field(default_factory=list)       # 工作經歷
    education_details: List[dict] = field(default_factory=list)  # 教育背景
    years_experience: str = ""         # 總年資
    stability_score: str = ""          # 穩定度分數
    job_changes: str = ""              # 換工作次數
    avg_tenure_months: str = ""        # 平均任期月數
    recent_gap_months: str = ""        # 最近空窗月數
    education: str = ""                # 最高學歷摘要
    enrichment_source: str = ""        # enrichment 來源: perplexity / jina / linkedin_api
    enrichment_notes: str = ""         # enrichment 額外備註

    # === AI 評分 (Phase 3: ContextualScorer) ===
    ai_score: int = 0                  # AI 匹配分數 0-100
    ai_grade: str = ""                 # AI 評等: A / B / C / D
    ai_recommendation: str = ""        # 強力推薦 / 推薦 / 觀望 / 不推薦
    ai_match_result: str = ""          # JSON 字串: 完整 AI 匹配結果
    ai_report: str = ""                # 人選分析報告（文字）

    def to_dict(self) -> dict:
        return asdict(self)

    def to_sheets_row(self) -> list:
        """轉為 Google Sheets 一行資料"""
        import json as _json
        return [
            self.id,
            self.name,
            self.source,
            self.github_url,
            self.linkedin_url,
            self.email,
            self.location,
            self.bio,
            self.company,
            self.title,
            ", ".join(self.skills) if isinstance(self.skills, list) else self.skills,
            self.public_repos,
            self.followers,
            self.job_title,
            self.search_date,
            self.task_id,
            self.status,
            self.created_at,
            self.score,
            self.grade,
            self.score_detail,
            # Enrichment
            _json.dumps(self.work_history, ensure_ascii=False) if self.work_history else "",
            _json.dumps(self.education_details, ensure_ascii=False) if self.education_details else "",
            self.years_experience,
            self.stability_score,
            self.job_changes,
            self.avg_tenure_months,
            self.recent_gap_months,
            self.education,
            self.enrichment_source,
            # AI 評分
            self.ai_score,
            self.ai_grade,
            self.ai_recommendation,
            self.ai_match_result,
            self.ai_report,
        ]

    @staticmethod
    def sheets_header() -> list:
        return [
            "id", "name", "source", "github_url", "linkedin_url",
            "email", "location", "bio", "company", "title",
            "skills", "public_repos", "followers",
            "job_title", "search_date", "task_id", "status", "created_at",
            "score", "grade", "score_detail",
            # Enrichment
            "work_history", "education_details",
            "years_experience", "stability_score", "job_changes",
            "avg_tenure_months", "recent_gap_months", "education",
            "enrichment_source",
            # AI 評分
            "ai_score", "ai_grade", "ai_recommendation",
            "ai_match_result", "ai_report",
        ]


@dataclass
class SearchTask:
    """爬蟲搜尋任務"""
    id: str = ""                       # 唯一 ID (uuid)
    client_name: str = ""              # 客戶名稱
    job_title: str = ""                # 職缺名稱
    primary_skills: List[str] = field(default_factory=list)   # 主技能 (AND)
    secondary_skills: List[str] = field(default_factory=list) # 次技能 (OR)
    location: str = "Taiwan"
    location_zh: str = "台灣"
    pages: int = 3                     # 搜尋頁數

    # 排程
    schedule_type: str = "once"        # once / interval / daily / weekly
    schedule_time: str = ""            # HH:MM
    schedule_interval_hours: int = 6   # 間隔小時數
    schedule_weekdays: List[int] = field(default_factory=list)  # 0=Mon..6=Sun

    # Step1ne 系統整合
    step1ne_job_id: Optional[int] = None  # Step1ne 系統的 job ID
    auto_push: bool = False               # 完成後自動推送到系統

    # 狀態
    status: str = "pending"            # pending / running / completed / failed / paused
    progress: int = 0                  # 0-100
    progress_detail: str = ""          # e.g. "LinkedIn 第 3/5 頁"
    last_run: str = ""
    last_result_count: int = 0
    error_message: str = ""
    created_at: str = ""
    updated_at: str = ""

    # 執行結果
    linkedin_count: int = 0
    github_count: int = 0
    ocr_count: int = 0                 # OCR 補充的數量

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'SearchTask':
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid_fields}
        return cls(**filtered)

    @property
    def all_skills(self) -> List[str]:
        return self.primary_skills + self.secondary_skills


@dataclass
class ProcessedRecord:
    """已處理紀錄（用於去重追蹤）"""
    linkedin_url: str = ""
    github_url: str = ""
    name: str = ""
    client_name: str = ""              # 來源客戶
    job_title: str = ""                # 來源職缺
    imported_at: str = ""              # 匯入時間
    status: str = "new"                # new / imported / skipped
    system_id: Optional[int] = None    # Step1ne 系統中的 candidate ID

    def to_sheets_row(self) -> list:
        return [
            self.linkedin_url,
            self.github_url,
            self.name,
            self.client_name,
            self.job_title,
            self.imported_at,
            self.status,
            self.system_id or "",
        ]

    @staticmethod
    def sheets_header() -> list:
        return [
            "linkedin_url", "github_url", "name",
            "client_name", "job_title",
            "imported_at", "status", "system_id",
        ]
