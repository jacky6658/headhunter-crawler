#!/usr/bin/env python3
"""
閉環驗證腳本 — 檢查今日 Pipeline 每個環節的輸出品質

用法：
  python3 scripts/verify_pipeline.py              # 檢查今天
  python3 scripts/verify_pipeline.py --days 7     # 檢查近 7 天
  python3 scripts/verify_pipeline.py --id 3785    # 檢查單一候選人
  python3 scripts/verify_pipeline.py --fix        # 自動修復可修復的問題

檢查項目：
  Phase A: 候選人基本資料完整度
  Phase B: PDF 是真 LinkedIn PDF（非截圖）
  Phase C: AI 深度分析品質
  Overall: 人選卡片 vs #1890 標準
"""

import argparse
import base64
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("pip install requests")
    sys.exit(1)

# ──────────────────────────────────────────
# 設定
# ──────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
_env_file = SCRIPT_DIR.parent / ".env"
if _env_file.exists():
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

API_BASE = os.environ.get("API_BASE", "https://api-hr.step1ne.com")
API_KEY = os.environ.get("API_SECRET_KEY", "PotfZ42-qPyY4uqSwqstpxllQB1alxVfjJsm3Mgp3HQ")
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

# #1890 標準：完整卡片應有的欄位
REQUIRED_FIELDS = {
    "basic": ["name", "location", "skills"],
    "work": ["workHistory"],  # 至少 1 段且有 description
    "education": ["educationJson"],  # 至少 1 段
    "pdf": ["resumeFiles"],  # 至少 1 個且是真 PDF
    "ai_analysis": ["aiAnalysis"],  # 完整 AI 深度分析
}

# AI 分析必須有的子欄位
AI_REQUIRED = {
    "candidate_evaluation": {
        "career_curve": ["summary", "pattern"],
        "personality": ["top3_strengths", "weaknesses"],
        "role_positioning": ["actual_role", "best_fit"],
        "salary_estimate": ["actual_years", "current_estimate"],
    },
    "job_matchings": {
        "_min_count": 1,
        "_item_required": ["job_id", "match_score", "verdict", "must_have"],
        "must_have_min": 3,
    },
    "recommendation": ["overall_pushability", "first_call_reason"],
}


# ──────────────────────────────────────────
# API helpers
# ──────────────────────────────────────────
def api_get(path):
    r = requests.get(f"{API_BASE}{path}", headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def get_candidates(days=1, candidate_id=None):
    """取得要檢查的候選人"""
    if candidate_id:
        data = api_get(f"/api/candidates/{candidate_id}")
        c = data.get("data", data)
        return [c] if c else []

    # 用 offset 從最新往回找
    all_candidates = []
    cutoff = datetime.now() - timedelta(days=days)

    for offset in range(0, 5000, 200):
        data = api_get(f"/api/candidates?limit=200&offset={offset}")
        items = data.get("data", [])
        if not items:
            break

        for c in items:
            created = c.get("createdAt", c.get("created_at", ""))
            if not created:
                continue
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00").replace("+00:00", ""))
            except (ValueError, TypeError):
                continue

            if dt >= cutoff:
                all_candidates.append(c)

        # 如果這頁最早的已超過 cutoff，停止
        if items:
            last_created = items[-1].get("createdAt", items[-1].get("created_at", ""))
            if last_created:
                try:
                    last_dt = datetime.fromisoformat(last_created.replace("Z", "+00:00").replace("+00:00", ""))
                    if last_dt < cutoff:
                        break
                except (ValueError, TypeError):
                    pass

    return all_candidates


# ──────────────────────────────────────────
# 驗證函數
# ──────────────────────────────────────────
def check_phase_a(c):
    """Phase A：基本資料完整度"""
    issues = []
    cid = c.get("id", "?")

    name = c.get("name", "")
    if not name or len(name) < 2:
        issues.append("name 空或太短")

    location = c.get("location", "")
    if not location:
        issues.append("location 空")

    skills = c.get("skills", "")
    if not skills or len(str(skills)) < 3:
        issues.append("skills 空")

    years = c.get("years", 0) or c.get("years_experience", 0) or 0
    if not years or years == 0:
        issues.append("years = 0")

    target_job = c.get("targetJobId", c.get("target_job_id"))
    if not target_job:
        issues.append("targetJobId 空")

    talent_level = c.get("talentLevel", c.get("talent_level", ""))
    if not talent_level:
        issues.append("talentLevel 空")

    return issues


def check_phase_b(c):
    """Phase B：PDF 品質"""
    issues = []
    rf = c.get("resumeFiles") or c.get("resume_files") or []

    if not rf:
        issues.append("沒有 PDF 附件")
        return issues

    for i, f in enumerate(rf):
        if not isinstance(f, dict):
            issues.append(f"resumeFiles[{i}] 格式錯誤")
            continue

        data = f.get("data", "")
        filename = f.get("filename", "")
        size = len(data) if data else 0

        if not data:
            issues.append(f"PDF[{i}] data 空")
            continue

        # 檢查是否是真 PDF（%PDF- 的 base64 = JVBERi）
        if not data.startswith("JVBERi"):
            issues.append(f"PDF[{i}] 不是有效 PDF（header 不對）")
            continue

        # 檢查是否是截圖（page.pdf 產的通常 > 200KB，LinkedIn 原生 PDF < 150KB）
        try:
            raw_size = len(base64.b64decode(data))
            if raw_size > 200 * 1024 and "_page" in filename:
                issues.append(f"PDF[{i}] 疑似 page.pdf() 截圖（{raw_size//1024}KB, filename 含 _page）")
        except Exception:
            issues.append(f"PDF[{i}] base64 解碼失敗")

    return issues


def check_phase_c(c):
    """Phase C：AI 深度分析品質"""
    issues = []
    ai = c.get("aiAnalysis") or c.get("ai_analysis") or c.get("existing_ai_analysis") or {}

    if not ai:
        issues.append("沒有 AI 深度分析")
        return issues

    # 檢查 version
    if ai.get("version") != "1.0":
        issues.append(f"version 不是 1.0（是 {ai.get('version')}）")

    # 檢查 analyzed_by
    if not ai.get("analyzed_by"):
        issues.append("analyzed_by 空")

    # 檢查 candidate_evaluation
    ce = ai.get("candidate_evaluation", {})
    if not ce:
        issues.append("candidate_evaluation 空")
    else:
        # career_curve
        cc = ce.get("career_curve", {})
        if not cc:
            issues.append("career_curve 空")
        else:
            if not cc.get("summary") or len(str(cc.get("summary", ""))) < 10:
                issues.append("career_curve.summary 太短或空")
            if not cc.get("pattern"):
                issues.append("career_curve.pattern 空")

        # personality
        p = ce.get("personality", {})
        if not p:
            issues.append("personality 空")
        else:
            strengths = p.get("top3_strengths", [])
            if not strengths or len(strengths) < 2:
                issues.append(f"personality.top3_strengths 不足（{len(strengths)} 個）")
            weaknesses = p.get("weaknesses", [])
            if not weaknesses:
                issues.append("personality.weaknesses 空")

        # role_positioning
        rp = ce.get("role_positioning", {})
        if not rp:
            issues.append("role_positioning 空")
        else:
            if not rp.get("actual_role"):
                issues.append("role_positioning.actual_role 空")

        # salary_estimate
        se = ce.get("salary_estimate", {})
        if not se:
            issues.append("salary_estimate 空")
        else:
            if not se.get("current_estimate"):
                issues.append("salary_estimate.current_estimate 空")

    # 檢查 job_matchings
    jm = ai.get("job_matchings", [])
    if not jm:
        issues.append("job_matchings 空陣列")
    elif not isinstance(jm, list):
        issues.append("job_matchings 不是陣列")
    else:
        m = jm[0]
        if not m.get("job_id"):
            issues.append("job_matchings[0].job_id 空")
        if not m.get("match_score") and m.get("match_score") != 0:
            issues.append("job_matchings[0].match_score 空")
        if not m.get("verdict"):
            issues.append("job_matchings[0].verdict 空")

        must_have = m.get("must_have", [])
        if not must_have:
            issues.append("must_have 空")
        elif len(must_have) < 3:
            issues.append(f"must_have 不足 3 條（只有 {len(must_have)} 條）")
        else:
            for i, mh in enumerate(must_have):
                if not mh.get("condition"):
                    issues.append(f"must_have[{i}].condition 空")
                if not mh.get("result"):
                    issues.append(f"must_have[{i}].result 空")

        if not m.get("salary_fit") or m.get("salary_fit") == "ok":
            issues.append("salary_fit 太簡略（不能只寫 ok）")

    # 檢查 recommendation
    rec = ai.get("recommendation", {})
    if not rec:
        issues.append("recommendation 空")
    else:
        if not rec.get("overall_pushability"):
            issues.append("recommendation.overall_pushability 空")
        if not rec.get("first_call_reason"):
            issues.append("recommendation.first_call_reason 空")

    return issues


def check_work_history(c):
    """工作經歷完整度"""
    issues = []
    wh = c.get("workHistory", c.get("work_history", []))

    if not wh:
        issues.append("workHistory 空（0 段）")
        return issues

    if len(wh) < 1:
        issues.append(f"workHistory 只有 {len(wh)} 段")

    has_description = False
    for i, w in enumerate(wh):
        if not w.get("company"):
            issues.append(f"workHistory[{i}].company 空")
        if not w.get("title"):
            issues.append(f"workHistory[{i}].title 空")
        if w.get("description") and len(str(w["description"])) > 10:
            has_description = True

    if not has_description and len(wh) > 0:
        issues.append("workHistory 全部沒有 description")

    return issues


def check_education(c):
    """教育背景完整度"""
    issues = []
    ed = c.get("educationJson", c.get("education_details", []))

    if not ed:
        issues.append("educationJson 空")
        return issues

    for i, e in enumerate(ed):
        if not e.get("school"):
            issues.append(f"educationJson[{i}].school 空")

    return issues


# ──────────────────────────────────────────
# 主程式
# ──────────────────────────────────────────
def verify_candidate(c, verbose=False):
    """驗證單一候選人，回傳 (score, issues_by_phase)"""
    cid = c.get("id", "?")
    name = c.get("name", "?")

    results = {
        "Phase A（基本資料）": check_phase_a(c),
        "Phase B（PDF 履歷）": check_phase_b(c),
        "Phase C（AI 分析）": check_phase_c(c),
        "工作經歷": check_work_history(c),
        "教育背景": check_education(c),
    }

    total_issues = sum(len(v) for v in results.values())
    total_checks = 5
    passed = sum(1 for v in results.values() if not v)

    # 計算完整度分數（滿分 100）
    score = int((passed / total_checks) * 100)

    # 跟 #1890 比較的等級
    if score == 100:
        grade = "🟢 完整"
    elif score >= 60:
        grade = "🟡 部分完整"
    elif score >= 40:
        grade = "🟠 缺很多"
    else:
        grade = "🔴 半成品"

    return {
        "id": cid,
        "name": name,
        "score": score,
        "grade": grade,
        "passed": passed,
        "total": total_checks,
        "results": results,
        "total_issues": total_issues,
    }


def print_report(verifications, verbose=False):
    """印出驗證報告"""
    total = len(verifications)
    if not total:
        print("❌ 沒有找到候選人")
        return

    perfect = sum(1 for v in verifications if v["score"] == 100)
    partial = sum(1 for v in verifications if 40 <= v["score"] < 100)
    broken = sum(1 for v in verifications if v["score"] < 40)

    print()
    print("=" * 60)
    print(f"  閉環驗證報告 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
    print()

    # 總覽
    print(f"  總人數：{total}")
    print(f"  🟢 完整卡片：{perfect} ({perfect*100//total}%)")
    print(f"  🟡🟠 部分完整：{partial} ({partial*100//total}%)")
    print(f"  🔴 半成品：{broken} ({broken*100//total}%)")
    print()

    # Phase 統計
    phase_stats = {}
    for v in verifications:
        for phase, issues in v["results"].items():
            if phase not in phase_stats:
                phase_stats[phase] = {"pass": 0, "fail": 0, "common_issues": {}}
            if not issues:
                phase_stats[phase]["pass"] += 1
            else:
                phase_stats[phase]["fail"] += 1
                for issue in issues:
                    phase_stats[phase]["common_issues"][issue] = phase_stats[phase]["common_issues"].get(issue, 0) + 1

    print("  各環節通過率：")
    for phase, stats in phase_stats.items():
        p = stats["pass"]
        f = stats["fail"]
        pct = p * 100 // (p + f) if (p + f) > 0 else 0
        icon = "✅" if pct == 100 else "⚠️" if pct >= 50 else "❌"
        print(f"    {icon} {phase}: {p}/{p+f} 通過 ({pct}%)")

        # 顯示最常見問題（前 3）
        if stats["common_issues"] and (verbose or pct < 100):
            sorted_issues = sorted(stats["common_issues"].items(), key=lambda x: -x[1])
            for issue, count in sorted_issues[:3]:
                print(f"       → {issue} ({count} 人)")
    print()

    # 詳細列表（半成品和部分完整）
    if verbose or broken > 0:
        print("  詳細清單：")
        for v in sorted(verifications, key=lambda x: x["score"]):
            issues_str = ""
            if v["total_issues"] > 0:
                all_issues = []
                for phase_issues in v["results"].values():
                    all_issues.extend(phase_issues)
                issues_str = f" | 問題: {', '.join(all_issues[:3])}"
                if len(all_issues) > 3:
                    issues_str += f" (+{len(all_issues)-3})"
            print(f"    #{v['id']} {v['name'][:15]:15} | {v['grade']} ({v['score']}%) | {v['passed']}/{v['total']} 通過{issues_str}")
        print()

    print("=" * 60)
    print(f"  標準：以 #1890 Zedd pai 為基準")
    print(f"  🟢 = 跟 #1890 一樣完整")
    print(f"  🔴 = 半成品，不可推給顧問")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="閉環驗證腳本")
    parser.add_argument("--days", type=int, default=1, help="檢查幾天（預設 1 = 今天）")
    parser.add_argument("--id", type=int, help="檢查單一候選人 ID")
    parser.add_argument("--fix", action="store_true", help="自動修復可修復的問題")
    parser.add_argument("--verbose", "-v", action="store_true", help="詳細輸出")
    parser.add_argument("--json", action="store_true", help="JSON 輸出")
    args = parser.parse_args()

    print(f"正在檢查候選人（{'ID ' + str(args.id) if args.id else '近 ' + str(args.days) + ' 天'}）...")

    candidates = get_candidates(days=args.days, candidate_id=args.id)

    if not candidates:
        print("❌ 沒有找到候選人")
        return

    print(f"找到 {len(candidates)} 人，開始驗證...")

    verifications = []
    for c in candidates:
        # 如果只有基本資料，需要拿 full profile
        cid = c.get("id")
        if cid and not c.get("aiAnalysis"):
            try:
                full = api_get(f"/api/ai-agent/candidates/{cid}/full-profile")
                c = full.get("data", c)
            except Exception:
                pass

        v = verify_candidate(c, verbose=args.verbose)
        verifications.append(v)

    if args.json:
        print(json.dumps(verifications, ensure_ascii=False, indent=2))
    else:
        print_report(verifications, verbose=args.verbose)


if __name__ == "__main__":
    main()
