#!/usr/bin/env python3
"""
Phase C 完成後通知顧問 — Telegram 群組通知

match_score >= 60 的人選會即時通知到群組
全部跑完後發彙總報告

使用方式：
  # 單人通知
  python3 scripts/notify_consultant.py --candidate-id 1748

  # 批次（Phase C 跑完後呼叫）
  python3 scripts/notify_consultant.py --batch --min-score 60

  # 測試
  python3 scripts/notify_consultant.py --test
"""

import os
import sys
import json
import argparse
import logging
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: pip install requests")
    sys.exit(1)

# 自動載入 .env
SCRIPT_DIR = Path(__file__).parent
_env_file = SCRIPT_DIR.parent / ".env"
if _env_file.exists():
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

# Config
API_BASE = os.environ.get("API_BASE", "https://api-hr.step1ne.com")
API_KEY = os.environ.get("API_SECRET_KEY", "")
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "8342445243:AAErYaMxOSO7p5cZUwYLBsRJqiOkH73nqSc")
CHAT_ID = os.environ.get("TG_CHAT_ID", "-1003231629634")
THREAD_ID = int(os.environ.get("TG_THREAD_ID", "1247"))
MIN_SCORE = 60
HR_URL = os.environ.get("HR_URL", "https://hr.step1ne.com")

log = logging.getLogger("notify")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def api_get(path):
    r = requests.get(f"{API_BASE}{path}", headers={"Authorization": f"Bearer {API_KEY}"}, timeout=15)
    r.raise_for_status()
    return r.json()


def send_telegram(text, parse_mode="HTML"):
    """發送 Telegram 訊息到群組"""
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "message_thread_id": THREAD_ID,
            "text": text,
            "parse_mode": parse_mode,
        },
        timeout=10,
    )
    if r.status_code == 200 and r.json().get("ok"):
        log.info("TG sent OK")
        return True
    else:
        log.error(f"TG send failed: {r.text[:200]}")
        return False


def get_candidate_summary(candidate_id):
    """取得候選人完整資訊，回傳摘要 dict"""
    data = api_get(f"/api/candidates/{candidate_id}")
    c = data.get("data", data)

    ai = c.get("aiAnalysis") or c.get("ai_analysis") or c.get("existing_ai_analysis") or {}
    rf = c.get("resumeFiles") or c.get("resume_files") or []
    wh = c.get("workHistory") or c.get("work_history") or []
    ed = c.get("educationJson") or c.get("education_details") or []

    # 從 AI 分析取分數
    jm = ai.get("job_matchings", [])
    best_match = max(jm, key=lambda x: x.get("match_score", 0)) if jm else {}
    score = best_match.get("match_score", 0)
    verdict = best_match.get("verdict", "未評估")
    job_id = best_match.get("job_id", c.get("targetJobId", "?"))
    job_title = best_match.get("job_title", "")
    company = best_match.get("company", "")

    # career curve summary
    ce = ai.get("candidate_evaluation", {})
    cc = ce.get("career_curve", {})
    career_summary = cc.get("summary", "")

    # 顧問指派（築楽→Jacky，其他→Phoebe）
    consultant = c.get("consultant", "")
    if not consultant:
        if "築楽" in (company or "") or "築樂" in (company or ""):
            consultant = "Jacky"
        else:
            consultant = "Phoebe"

    return {
        "id": c.get("id"),
        "name": c.get("name", "?"),
        "score": score,
        "verdict": verdict,
        "job_id": job_id,
        "job_title": job_title or c.get("targetJobLabel", "?"),
        "company": company,
        "talent_level": c.get("talentLevel", "?"),
        "location": c.get("location", "?"),
        "current_title": (wh[0].get("title", "?") if wh else c.get("position", "?")),
        "current_company": (wh[0].get("company", "?") if wh else "?"),
        "career_summary": career_summary[:80] if career_summary else "見 AI 分析",
        "consultant": consultant,
        "has_pdf": bool(rf),
        "has_ai": bool(ai),
        "has_work": bool(wh),
        "years": c.get("years", "?"),
    }


def format_recommend_msg(s):
    """格式化推薦通知"""
    pdf_icon = "✅" if s["has_pdf"] else "❌"
    ai_icon = "✅" if s["has_ai"] else "❌"
    work_icon = "✅" if s["has_work"] else "❌"

    return f"""🔔 <b>新人選推薦 — 請顧問盡快聯繫</b> @behe10 @jackyyuqi

📋 職缺：#{s['job_id']} {s['job_title']}
👤 人選：#{s['id']} {s['name']}
📊 匹配分數：<b>{s['score']}/100</b>（{s['verdict']}）
🏷 評級：{s['talent_level']}
📍 地點：{s['location']}
💼 現職：{s['current_title']} @ {s['current_company']}
📝 摘要：{s['career_summary']}

✅ 資料：PDF {pdf_icon} | AI分析 {ai_icon} | 經歷 {work_icon}

👉 指派顧問：<b>{s['consultant']}</b>"""


def format_summary_report(results):
    """格式化彙總報告"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    recommended = [r for r in results if r["score"] >= MIN_SCORE]
    archived = [r for r in results if r["score"] < MIN_SCORE]

    lines = [f"📊 <b>Phase C 完成報告 — {now}</b>"]
    lines.append("")
    lines.append(f"✅ 本輪處理：{len(results)} 人")
    lines.append(f"🔔 推薦聯繫（≥{MIN_SCORE}分）：{len(recommended)} 人")
    lines.append(f"📁 存入人才庫（<{MIN_SCORE}分）：{len(archived)} 人")

    if recommended:
        lines.append("")
        lines.append("<b>推薦名單：</b>")
        for i, r in enumerate(recommended, 1):
            lines.append(f"{i}. #{r['id']} {r['name']} | {r['job_title'][:20]} | {r['score']}分 | {r['consultant']}")

    return "\n".join(lines)


def notify_single(candidate_id):
    """通知單一候選人"""
    s = get_candidate_summary(candidate_id)
    log.info(f"#{s['id']} {s['name']} — score: {s['score']}")

    if s["score"] >= MIN_SCORE:
        msg = format_recommend_msg(s)
        send_telegram(msg)
        log.info(f"✅ Notified (score {s['score']} >= {MIN_SCORE})")
    else:
        log.info(f"⏭ Skipped (score {s['score']} < {MIN_SCORE})")

    return s


def notify_batch(candidate_ids=None, min_score=None):
    """批次通知"""
    if min_score is not None:
        global MIN_SCORE
        MIN_SCORE = min_score

    if candidate_ids is None:
        # 自動找今天有 AI 分析的候選人
        log.info("Finding today's candidates with AI analysis...")
        # 從最新的 ID 往回找
        candidate_ids = []
        for offset in range(0, 600, 200):
            try:
                data = api_get(f"/api/candidates?limit=200&offset={offset}")
                items = data.get("data", [])
                today = datetime.now().strftime("%Y-%m-%d")
                for c in items:
                    created = c.get("createdAt", c.get("created_at", ""))
                    if today not in created:
                        continue
                    ai = c.get("aiAnalysis") or c.get("ai_analysis") or c.get("existing_ai_analysis")
                    if ai:
                        candidate_ids.append(c.get("id"))
            except Exception as e:
                log.warning(f"offset {offset} failed: {e}")
                break

        log.info(f"Found {len(candidate_ids)} candidates with AI analysis today")

    results = []
    for cid in candidate_ids:
        try:
            s = notify_single(cid)
            results.append(s)
        except Exception as e:
            log.error(f"#{cid} failed: {e}")

    # 發彙總
    if results:
        summary = format_summary_report(results)
        send_telegram(summary)
        log.info(f"Summary sent: {len(results)} candidates")

    return results


def test():
    """測試通知"""
    send_telegram("🧪 Phase C 通知系統測試 — 連線正常 ✅")
    log.info("Test message sent")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase C 完成後通知顧問")
    parser.add_argument("--candidate-id", type=int, help="單一候選人 ID")
    parser.add_argument("--batch", action="store_true", help="批次通知今天所有有 AI 分析的候選人")
    parser.add_argument("--ids", type=str, help="指定 ID 列表，逗號分隔")
    parser.add_argument("--min-score", type=int, default=60, help="最低推薦分數（預設 60）")
    parser.add_argument("--test", action="store_true", help="測試通知")
    args = parser.parse_args()

    if args.test:
        test()
    elif args.candidate_id:
        notify_single(args.candidate_id)
    elif args.batch:
        ids = [int(x) for x in args.ids.split(",")] if args.ids else None
        notify_batch(ids, args.min_score)
    else:
        parser.print_help()
