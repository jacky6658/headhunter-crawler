#!/usr/bin/env python3
"""
Phase C AI 分析腳本：龍蝦只需提供簡單參數，腳本負責組正確 JSON 格式 + PUT 寫入

使用方式（兩種模式）：

模式一：單人分析（龍蝦填參數）
  python3 scripts/phase_c_ai_analysis.py --candidate-id 3764 \
    --summary "資深IC5工程師，國際經驗豐富" \
    --strengths "IC5等級,國際工作經驗,12段工作經歷" \
    --gaps "非DE背景,地點台中非台北" \
    --score 45 --grade C --verdict "待確認"

模式二：批次處理（自動處理所有缺 AI 分析的候選人）
  python3 scripts/phase_c_ai_analysis.py --batch
  ⚠️ 批次模式需要龍蝦自己逐一分析，腳本會輸出待處理清單

前置條件：
  - pip install requests
  - .env 有 API_SECRET_KEY 或手動 export
"""

import argparse
import json
import os
import sys
import logging
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: requests not installed. Run: pip install requests")
    sys.exit(1)

# ──────────────────────────────────────────
# 自動載入 .env
# ──────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
_env_file = PROJECT_DIR / ".env"
if _env_file.exists():
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

# ──────────────────────────────────────────
# 設定
# ──────────────────────────────────────────
API_BASE = os.environ.get("API_BASE", "https://api-hr.step1ne.com")
API_KEY = os.environ.get("API_SECRET_KEY", "")
if not API_KEY:
    print("Error: API_SECRET_KEY not set")
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

DAYS_LOOKBACK = int(os.environ.get("PHASE_C_LOOKBACK", "7"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)


def api_get(path: str) -> dict:
    r = requests.get(f"{API_BASE}{path}", headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def build_ai_analysis(
    candidate: dict,
    job: dict,
    summary: str,
    strengths: list,
    gaps: list,
    score: int,
    grade: str,
    verdict: str,
    analyzed_by: str = "lobster-ai-phase-c"
) -> dict:
    """
    用固定模板組出正確的 ai_analysis JSON。
    龍蝦不需要知道格式細節，只要填：summary, strengths, gaps, score, grade, verdict
    """
    now = datetime.now().isoformat()

    # 從候選人資料提取
    name = candidate.get("name", "Unknown")
    work_history = candidate.get("workHistory", candidate.get("work_history", []))
    location = candidate.get("location", "未知")
    skills = candidate.get("skills", "")

    # 從職缺提取
    job_id = job.get("id", 0)
    job_title = job.get("title", job.get("job_title", "Unknown"))
    company = job.get("client_company", job.get("clientCompany", "Unknown"))
    exp_required = job.get("experience_required", job.get("experienceRequired", ""))
    salary_range = job.get("salary_range", job.get("salaryRange", ""))
    key_skills = job.get("key_skills", job.get("keySkills", ""))
    submission = job.get("submission_criteria", job.get("submissionCriteria", ""))

    # 組 career_curve details
    career_details = []
    for w in (work_history or [])[:5]:
        career_details.append({
            "company": w.get("company", "Unknown"),
            "industry": "Tech",
            "title": w.get("title", "Unknown"),
            "duration": w.get("duration", f"{w.get('duration_months', '?')} months"),
            "move_reason": "N/A"
        })
    if not career_details:
        career_details = [{"company": "見履歷", "industry": "N/A", "title": "N/A", "duration": "N/A", "move_reason": "N/A"}]

    # 組 must_have checks
    must_have = []
    if submission:
        # 從 submission_criteria 提取前 4 個條件
        lines = [l.strip().lstrip("- ") for l in submission.split("\n") if l.strip() and l.strip().startswith("-")][:4]
        for line in lines:
            # 簡單判斷是否命中
            line_lower = line.lower()
            skills_lower = (skills or "").lower()
            work_text = " ".join(w.get("title", "") + " " + w.get("company", "") for w in (work_history or []))
            full = f"{skills_lower} {work_text.lower()}"
            result = "warning"
            # 粗略判斷
            keywords = [w for w in line_lower.split() if len(w) > 3]
            hits = sum(1 for k in keywords if k in full)
            if hits >= len(keywords) * 0.5:
                result = "pass"
            elif hits == 0:
                result = "fail"

            must_have.append({
                "condition": line[:80],
                "actual": (skills or work_text)[:80] or "未提供",
                "result": result
            })

    if not must_have:
        must_have = [
            {"condition": exp_required or "見職缺要求", "actual": f"見履歷 ({len(work_history or [])} 段經歷)", "result": "warning"},
            {"condition": key_skills[:80] if key_skills else "見職缺要求", "actual": skills[:80] if skills else "未提供", "result": "warning"}
        ]

    # 組完整 payload
    payload = {
        "ai_analysis": {
            "version": "1.0",
            "analyzed_at": now,
            "analyzed_by": analyzed_by,
            "candidate_evaluation": {
                "career_curve": {
                    "summary": summary,
                    "pattern": _infer_pattern(work_history),
                    "details": career_details
                },
                "personality": {
                    "type": "待面談確認",
                    "top3_strengths": strengths[:3],
                    "weaknesses": gaps[:3],
                    "evidence": summary
                },
                "role_positioning": {
                    "actual_role": career_details[0]["title"] if career_details else "Unknown",
                    "spectrum_position": _infer_level(work_history),
                    "best_fit": strengths[:2],
                    "not_fit": gaps[:2]
                },
                "salary_estimate": {
                    "actual_years": _estimate_years(work_history),
                    "current_level": _infer_level(work_history),
                    "current_estimate": "待確認",
                    "expected_range": salary_range or "待確認",
                    "risks": gaps[:2]
                }
            },
            "job_matchings": [
                {
                    "job_id": job_id,
                    "job_title": job_title,
                    "company": company,
                    "match_score": score,
                    "verdict": verdict,
                    "company_analysis": f"{company}，要求{exp_required}經驗",
                    "must_have": must_have,
                    "nice_to_have": [
                        {"condition": "見職缺偏好", "actual": "見履歷", "result": "warning"}
                    ],
                    "strongest_match": ", ".join(strengths[:2]),
                    "main_gap": ", ".join(gaps[:2]),
                    "hard_block": gaps[0] if gaps and score < 30 else "無",
                    "salary_fit": f"職缺年薪 {salary_range}" if salary_range else "待確認"
                }
            ],
            "recommendation": {
                "summary_table": [
                    {
                        "job_id": job_id,
                        "job_title": job_title,
                        "company": company,
                        "score": score,
                        "verdict": verdict,
                        "priority": 1 if score >= 70 else (2 if score >= 50 else 3)
                    }
                ],
                "first_call_job_id": job_id,
                "first_call_reason": summary,
                "overall_pushability": "高" if score >= 70 else ("中" if score >= 50 else "低"),
                "pushability_detail": f"評分 {score}/100。優勢：{', '.join(strengths[:2])}。缺口：{', '.join(gaps[:2])}。",
                "fallback_note": f"Grade {grade}，{'強烈推薦面試' if score >= 80 else '建議聯繫確認' if score >= 50 else '放人才庫觀察'}"
            }
        }
    }
    return payload


def _infer_pattern(work_history: list) -> str:
    if not work_history:
        return "資訊不足"
    count = len(work_history)
    if count <= 2:
        return "穩定型"
    elif count <= 4:
        return "穩定上升"
    elif count <= 7:
        return "經驗豐富"
    else:
        return "跳槽頻繁（需確認）"


def _infer_level(work_history: list) -> str:
    if not work_history:
        return "待確認"
    titles = " ".join(w.get("title", "").lower() for w in work_history)
    if any(k in titles for k in ["director", "vp", "head of", "總監", "副總"]):
        return "Director+"
    if any(k in titles for k in ["manager", "lead", "principal", "staff", "經理", "主管"]):
        return "Manager/Lead"
    if any(k in titles for k in ["senior", "sr.", "資深"]):
        return "Senior"
    return "Mid"


def _estimate_years(work_history: list) -> int:
    if not work_history:
        return 0
    total_months = 0
    for w in work_history:
        m = w.get("duration_months")
        if m:
            try:
                total_months += int(m)
            except (ValueError, TypeError):
                pass
    if total_months > 0:
        return round(total_months / 12)
    # fallback: 用工作段數估算
    return len(work_history) * 2


def put_ai_analysis(candidate_id: int, payload: dict) -> bool:
    """PUT 寫入 AI 分析，回傳是否成功"""
    url = f"{API_BASE}/api/ai-agent/candidates/{candidate_id}/ai-analysis"
    try:
        r = requests.put(url, headers=HEADERS, json=payload, timeout=15)
        if r.status_code == 200:
            return True
        else:
            log.error(f"  PUT failed: HTTP {r.status_code} — {r.text[:200]}")
            return False
    except Exception as e:
        log.error(f"  PUT error: {e}")
        return False


def fetch_candidates_needing_analysis() -> list:
    """取得最近 N 天匯入、有 resumeFiles 但沒有 aiAnalysis 的候選人"""
    cutoff = datetime.now() - timedelta(days=DAYS_LOOKBACK)
    candidates = []
    offset = 0
    max_pages = 15

    for page in range(max_pages):
        data = api_get(f"/api/candidates?limit=200&offset={offset}&sort=-id")
        items = data.get("data", [])
        if not items:
            break

        for c in items:
            created = c.get("createdAt", c.get("created_at", ""))
            if created:
                try:
                    created_dt = datetime.fromisoformat(created.replace("Z", "+00:00").replace("+00:00", ""))
                except ValueError:
                    created_dt = datetime.now()
                if created_dt < cutoff:
                    log.info(f"  Reached cutoff date at offset {offset}, stopping")
                    return candidates

            resume_files = c.get("resumeFiles", c.get("resume_files", []))
            ai_analysis = c.get("aiAnalysis", c.get("ai_analysis", {}))

            # 有 PDF 但沒 AI 分析
            if resume_files and not ai_analysis:
                candidates.append({
                    "id": c.get("id"),
                    "name": c.get("name", "Unknown"),
                    "target_job_id": c.get("targetJobId", c.get("target_job_id")),
                })

        offset += 200

    return candidates


def run_single(args):
    """單人模式：龍蝦提供參數，腳本組 JSON + PUT"""
    cid = args.candidate_id
    log.info(f"Phase C — Single mode for #{cid}")

    # 拿候選人 full-profile
    try:
        profile_data = api_get(f"/api/ai-agent/candidates/{cid}/full-profile")
        candidate = profile_data.get("data", profile_data)
    except Exception as e:
        log.error(f"Cannot fetch profile for #{cid}: {e}")
        sys.exit(1)

    # 拿職缺
    job_id = args.job_id or candidate.get("targetJobId", candidate.get("target_job_id"))
    if not job_id:
        log.error(f"No job_id specified and candidate has no target_job_id")
        sys.exit(1)

    try:
        job_data = api_get(f"/api/jobs/{job_id}")
        job = job_data.get("data", job_data)
    except Exception as e:
        log.error(f"Cannot fetch job #{job_id}: {e}")
        sys.exit(1)

    # 解析參數
    strengths = [s.strip() for s in args.strengths.split(",") if s.strip()]
    gaps = [g.strip() for g in args.gaps.split(",") if g.strip()]

    # 組 JSON
    payload = build_ai_analysis(
        candidate=candidate,
        job=job,
        summary=args.summary,
        strengths=strengths,
        gaps=gaps,
        score=args.score,
        grade=args.grade,
        verdict=args.verdict,
        analyzed_by=args.analyzed_by or "lobster-ai-phase-c"
    )

    # 驗證
    analysis = payload["ai_analysis"]
    assert analysis.get("version") == "1.0"
    assert analysis.get("analyzed_at")
    assert analysis.get("analyzed_by")
    assert isinstance(analysis.get("candidate_evaluation"), dict)
    assert isinstance(analysis.get("job_matchings"), list)
    assert isinstance(analysis.get("recommendation"), dict)
    log.info("  JSON validation passed ✅")

    # PUT
    ok = put_ai_analysis(cid, payload)
    if ok:
        log.info(f"✅ #{cid} {candidate.get('name', '?')} — score:{args.score} grade:{args.grade} verdict:{args.verdict}")
    else:
        log.error(f"❌ #{cid} failed to write")
        sys.exit(1)


def run_batch():
    """批次模式：列出所有缺 AI 分析的候選人，給龍蝦逐一處理"""
    log.info(f"Phase C — Batch mode (lookback: {DAYS_LOOKBACK} days)")

    candidates = fetch_candidates_needing_analysis()
    log.info(f"Found {len(candidates)} candidates needing AI analysis")

    if not candidates:
        log.info("All candidates have AI analysis. Done. ✅")
        return

    print("\n=== 待處理清單 ===")
    print("對每個人執行：")
    print("python3 scripts/phase_c_ai_analysis.py --candidate-id ID \\")
    print('  --summary "..." --strengths "s1,s2,s3" --gaps "g1,g2" \\')
    print("  --score N --grade X --verdict Y\n")

    for c in candidates:
        job_id = c.get("target_job_id", "?")
        print(f"  #{c['id']} {c['name']} (job: {job_id})")

    print(f"\n共 {len(candidates)} 人待處理")


def main():
    parser = argparse.ArgumentParser(description="Phase C AI Analysis — 格式保證正確的寫入工具")
    parser.add_argument("--batch", action="store_true", help="批次模式：列出所有缺 AI 分析的候選人")
    parser.add_argument("--candidate-id", type=int, help="候選人 ID")
    parser.add_argument("--job-id", type=int, help="職缺 ID（不填則從候選人的 target_job_id 讀取）")
    parser.add_argument("--summary", type=str, help="3 句話職涯摘要")
    parser.add_argument("--strengths", type=str, help="優勢（逗號分隔）")
    parser.add_argument("--gaps", type=str, help="缺口（逗號分隔）")
    parser.add_argument("--score", type=int, help="匹配分數 0-100")
    parser.add_argument("--grade", type=str, choices=["A+", "A", "B", "C", "D"], help="等級")
    parser.add_argument("--verdict", type=str, help="推薦/條件式/待確認/不適合")
    parser.add_argument("--analyzed-by", type=str, default="lobster-ai-phase-c", help="分析者名稱")

    args = parser.parse_args()

    if args.batch:
        run_batch()
    elif args.candidate_id:
        if not all([args.summary, args.strengths, args.gaps, args.score is not None, args.grade, args.verdict]):
            parser.error("單人模式需要所有參數：--summary, --strengths, --gaps, --score, --grade, --verdict")
        run_single(args)
    else:
        parser.error("需要 --batch 或 --candidate-id")


if __name__ == "__main__":
    main()
