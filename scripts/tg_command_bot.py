#!/usr/bin/env python3
"""
TG Command Bot — 在群組接收指令，呼叫 Claude Code 執行閉環等任務

支援 slash 指令 + 中文指令 + 按鈕面板
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ── 設定 ──────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent

# 載入 .env
_env_file = PROJECT_DIR / ".env"
if _env_file.exists():
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

BOT_TOKEN = os.environ.get("TG_COMMAND_BOT_TOKEN", "8795142390:AAFJw6zm_eqnFwLOTiOSxGzWcdv5NSTAvQs")
ALLOWED_CHAT_ID = int(os.environ.get("TG_CHAT_ID", "-1003231629634"))
ALLOWED_USERS = os.environ.get("TG_ALLOWED_USERS", "8365775688").split(",")

CLAUDE_CLI = os.environ.get("CLAUDE_CLI", "/Users/user/.local/bin/claude")
WORK_DIR = str(PROJECT_DIR)
API_BASE = os.environ.get("API_BASE", "https://api-hr.step1ne.com")
API_KEY = os.environ.get("API_SECRET_KEY", "")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(PROJECT_DIR / "logs" / "tg_bot.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# 任務狀態
_running_task = None
_task_queue = asyncio.Queue()

# 錯誤偵測冷卻（防重複觸發）
_error_cooldown = {}  # {error_hash: timestamp}
ERROR_COOLDOWN_SECS = 1800  # 同類錯誤 30 分鐘內不重複

# 自訂角度等待輸入 {user_id: job_id}
_pending_custom_angle = {}

# 自由搜尋等待輸入 {user_id: True}
_pending_free_search = {}

# 公司定向搜尋等待輸入 {user_id: True}
_pending_company_search = {}

# 瀏覽人才庫等待輸入 {user_id: True}
_pending_browse_db = {}

# 修改職缺關鍵字等待輸入 {user_id: job_id}
_pending_edit_keywords = {}

# 已知的龍蝦 Bot IDs（偵測他們的錯誤訊息）
LOBSTER_BOT_IDS = {
    8342445243,   # hr-yuqi
    8375770979,   # notification bot
}

# 錯誤關鍵字
ERROR_KEYWORDS = [
    "❌", "ERROR", "失敗", "超時", "timeout", "離線",
    "中止", "崩潰", "crash", "exception", "無法連線",
    "API 無回應", "502", "503", "connection refused",
]


# ── 工具函式 ──────────────────────────────────────────────

def is_authorized(update: Update) -> bool:
    chat_id = update.effective_chat.id if update.effective_chat else 0
    user_id = str(update.effective_user.id) if update.effective_user else ""
    # 群組：只允許指定群組
    # 私訊：所有人都能用
    is_private = update.effective_chat and update.effective_chat.type == 'private'
    return is_private or chat_id == ALLOWED_CHAT_ID or user_id in ALLOWED_USERS


def parse_job_ids(text: str) -> list:
    # 支援 #233 和純數字 233
    ids = [int(x) for x in re.findall(r"#(\d+)", text)]
    if not ids:
        ids = [int(x) for x in re.findall(r"\b(\d{2,4})\b", text) if 40 <= int(x) <= 9999]
    return ids


def api_headers():
    return {"Authorization": f"Bearer {API_KEY}"}


PIPELINE_BASE = API_BASE + "/api/crawler/pipeline"
LOCAL_DB = 'step1ne_crawler'


# ── 本地 DB 查詢 ──
def db_query(sql, params=None):
    """查本地 PostgreSQL"""
    try:
        import psycopg2
        conn = psycopg2.connect(dbname=LOCAL_DB)
        cur = conn.cursor()
        cur.execute(sql, params or ())
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        conn.close()
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        log.error(f"DB error: {e}")
        return []


def db_get_active_jobs():
    """從本地 DB 取招募中職缺"""
    return db_query("SELECT * FROM jobs WHERE job_status='招募中' ORDER BY id DESC")


def db_get_job(job_id):
    """從本地 DB 取單一職缺"""
    rows = db_query("SELECT * FROM jobs WHERE id=%s OR synced_from_hr_id=%s", (job_id, job_id))
    return rows[0] if rows else {}


def db_get_candidates(job_id=None, limit=50):
    """從本地 DB 取人選"""
    if job_id:
        return db_query("SELECT * FROM candidates WHERE target_job_id=%s ORDER BY ai_score DESC NULLS LAST LIMIT %s", (job_id, limit))
    return db_query("SELECT * FROM candidates ORDER BY id DESC LIMIT %s", (limit,))


def db_search_candidates(query, limit=10):
    """從本地 DB 搜尋人選"""
    like = f"%{query}%"
    return db_query("""
        SELECT * FROM candidates
        WHERE lower(name) LIKE lower(%s) OR lower(current_title) LIKE lower(%s) OR lower(skills) LIKE lower(%s)
        ORDER BY ai_score DESC NULLS LAST LIMIT %s
    """, (like, like, like, limit))


def api_get(endpoint: str, retries: int = 3) -> dict:
    """HR API GET — 同步版（給非 async 函式用）"""
    import requests as _req
    import time as _time
    for i in range(retries):
        try:
            r = _req.get(f"{API_BASE}{endpoint}", headers=api_headers(), timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                _time.sleep(5)
                continue
            if r.status_code in (502, 503, 504) and i < retries - 1:
                _time.sleep(3)
                continue
            return r.json() if r.text else {}
        except Exception as e:
            if i < retries - 1:
                _time.sleep(3)
            else:
                return {"error": str(e)}


async def api_get_async(endpoint: str, retries: int = 2) -> dict:
    """HR API GET — async 版（不阻塞 event loop）"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: api_get(endpoint, retries))


async def send_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                     reply_markup=None, chat_id=None, thread_id=None):
    max_len = 4000
    if len(text) > max_len:
        text = text[:max_len] + "\n\n⚠️ 訊息過長已截斷"
    cid = chat_id or update.effective_chat.id
    tid = thread_id or (update.message.message_thread_id if update.message else None)
    await context.bot.send_message(
        chat_id=cid, message_thread_id=tid,
        text=text, parse_mode="HTML", reply_markup=reply_markup,
    )


async def run_script_async(cmd: list, timeout: int = 3600) -> tuple:
    """執行腳本，回傳 (stdout, stderr, exit_code)"""
    log.info(f"RUN: {' '.join(cmd[:5])}...")
    try:
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORK_DIR,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        log.info(f"RUN done: stdout={len(out)}, stderr={len(err)}, exit={proc.returncode}")
        return out, err, proc.returncode
    except asyncio.TimeoutError:
        log.error(f"RUN timeout ({timeout}s)")
        return "", f"❌ 執行超時（{timeout}s）", 1
    except Exception as e:
        log.error(f"RUN error: {e}")
        return "", f"❌ {e}", 1


def fetch_active_jobs() -> list:
    """優先從本地 DB，fallback 到 HR API"""
    # 1. 本地 DB
    jobs = db_get_active_jobs()
    if jobs:
        log.info(f"fetch_active_jobs: {len(jobs)} from local DB")
        return jobs

    # 2. Fallback: HR API
    log.info("fetch_active_jobs: local DB empty, trying HR API")
    data = api_get("/api/crawler/pipeline/jobs")
    if data.get("error"):
        log.error(f"fetch_active_jobs failed: {data['error']}")
        return []
    jobs_list = data if isinstance(data, list) else data.get("jobs", data.get("data", []))
    active = [j for j in jobs_list if j.get("job_status") == "招募中"]
    active.sort(key=lambda j: j.get("id", 0), reverse=True)
    return active


async def fetch_active_jobs_async() -> list:
    """async 版"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_active_jobs)


# ── 按鈕面板 ──────────────────────────────────────────────

def build_help_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 企業職缺搜尋", callback_data="show_jobs")],
        [InlineKeyboardButton("🔍 自由搜尋人才", callback_data="free_search_prompt")],
        [InlineKeyboardButton("🏢 公司定向搜尋", callback_data="company_search_prompt")],
        [InlineKeyboardButton("📚 瀏覽人才庫", callback_data="browse_db_prompt")],
        [InlineKeyboardButton("📊 人才庫總覽", callback_data="db_stats"),
         InlineKeyboardButton("🔄 系統狀態", callback_data="status")],
    ])


def build_job_keyboard(jobs: list, action: str = "loop") -> InlineKeyboardMarkup:
    """動態生成職缺按鈕（每行 2 個）"""
    buttons = []
    row = []
    for j in jobs:
        jid = j.get("id", 0)
        name = j.get("position_name", "?")[:12]
        client = (j.get("client_company") or "")[:6]
        label = f"#{jid} {name}"
        row.append(InlineKeyboardButton(label, callback_data=f"{action}_{jid}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("« 返回", callback_data="help")])
    return InlineKeyboardMarkup(buttons)


# ── 指令處理 ──────────────────────────────────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    text = """<b>🤖 Step1ne 閉環指令</b>

<b>閉環</b>
/loop 233 — 跑閉環
/jobs — 選擇職缺跑閉環

<b>查詢</b>
/status — 系統狀態
/search 姓名 — 查候選人
/job 233 — 查職缺

<b>優化</b>
優化關鍵字 #234 — AI 生成搜尋策略

<b>修復</b>
修復 [描述] — 手動呼叫 Claude 修復

<b>自動偵測</b>
🤖 龍蝦 bot 發送含 ❌/ERROR/失敗 的訊息時
→ 自動呼叫 Claude 診斷修復（30分鐘冷卻）

👇 或點下方按鈕："""
    await send_reply(update, context, text, reply_markup=build_help_keyboard())


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await send_reply(update, context, "⏳ 檢查系統狀態中...")

    import requests
    results = {}
    try:
        r = requests.get(f"{API_BASE}/api/health", timeout=10)
        results["hr"] = "✅" if r.status_code == 200 else "❌"
    except:
        results["hr"] = "❌"
    try:
        r = requests.get("http://localhost:5001/api/tasks", timeout=5)
        results["crawler"] = "✅" if r.status_code == 200 else "❌"
    except:
        results["crawler"] = "❌"
    try:
        requests.get("http://localhost:9222/json/version", timeout=5)
        results["cdp"] = "✅"
    except:
        results["cdp"] = "❌"

    # 本地 DB
    try:
        db_jobs = db_query("SELECT count(*) as c FROM jobs WHERE job_status='招募中'")
        db_cands = db_query("SELECT count(*) as c FROM candidates")
        db_status = f"✅ {db_jobs[0]['c']}職缺 / {db_cands[0]['c']}人選"
    except:
        db_status = "❌"

    task_status = f"🔄 {_running_task}" if _running_task else "💤 閒置"
    now = datetime.now().strftime("%H:%M")

    text = f"""<b>🔍 系統狀態</b>

{results['hr']} HR API
{results['crawler']} 爬蟲
{results['cdp']} Chrome CDP
📦 本地DB: {db_status}

🤖 Bot: {task_status}
⏰ {now}"""
    await send_reply(update, context, text)


async def cmd_list_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """列出所有招募中職缺"""
    if not is_authorized(update):
        return
    jobs = await fetch_active_jobs_async()
    if not jobs:
        await send_reply(update, context, "❌ 沒有招募中的職缺或 API 無回應")
        return

    lines = [f"<b>📋 招募中職缺（{len(jobs)} 個）</b>\n"]
    for j in jobs:
        jid = j.get("id", "?")
        name = j.get("position_name", "?")
        client = j.get("client_company", "")
        priority = j.get("priority", "")
        emoji = "🔥" if priority == "高" else "📌"
        lines.append(f"{emoji} <b>#{jid}</b> {name}\n    {client}")

    await send_reply(update, context, "\n".join(lines),
                     reply_markup=build_job_keyboard(jobs))


async def cmd_loop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """slash 指令 /loop"""
    if not is_authorized(update):
        return
    text = update.message.text if update.message else ""
    # /loop 233 或 /loop 233 234
    ids = parse_job_ids(text)
    if not ids:
        # 沒帶 ID，顯示職缺按鈕讓他選
        jobs = await fetch_active_jobs_async()
        if jobs:
            await send_reply(update, context, "🚀 <b>選擇要跑閉環的職缺：</b>",
                             reply_markup=build_job_keyboard(jobs))
        else:
            await send_reply(update, context, "⚠️ 請指定職缺 ID，例如：<code>/loop 233</code>")
        return
    await _start_loop(ids, update, context)


async def cmd_run_loop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """中文指令：跑閉環"""
    if not is_authorized(update):
        return
    text = update.message.text if update.message else ""
    if "全部" in text:
        await _start_loop("all", update, context)
    else:
        ids = parse_job_ids(text)
        if not ids:
            jobs = await fetch_active_jobs_async()
            if jobs:
                await send_reply(update, context, "🚀 <b>選擇要跑閉環的職缺：</b>",
                                 reply_markup=build_job_keyboard(jobs))
            else:
                await send_reply(update, context, "⚠️ 請指定職缺，例如：<code>跑閉環 #233</code>")
            return
        await _start_loop(ids, update, context)


async def _start_loop(job_ids, update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _running_task
    if _running_task:
        await send_reply(update, context, f"⚠️ 目前有任務在跑：{_running_task}\n排入佇列等候")
        await _task_queue.put(("loop", job_ids, update, context))
        return
    await _execute_loop(job_ids, update, context)


async def _execute_loop(job_ids, update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _running_task
    if job_ids == "all":
        display = "全部招募中職缺"
        prompt_part = "所有招募中的職缺"
    else:
        # 查詢職缺名稱（優先本地 DB）
        job_names = []
        for jid in job_ids:
            j = db_get_job(jid)
            if j:
                name = j.get("position_name", "")
                client = j.get("client_company", "")
                job_names.append(f"#{jid} {name}（{client}）" if client else f"#{jid} {name}")
            else:
                job_names.append(f"#{jid}")
        display = "\n".join(job_names) if len(job_names) > 1 else (job_names[0] if job_names else " ".join(f"#{j}" for j in job_ids))
        prompt_part = "、".join(f"#{j}" for j in job_ids)

    _running_task = display
    await send_reply(update, context, f"🚀 <b>開始閉環</b>\n{display}\n\n⏳ 直接執行 Python 腳本，完成後通知")

    # 直接跑 daily_closed_loop.py
    def _extract_lines(output, keywords, limit=10):
        lines = []
        for line in output.split("\n"):
            if any(kw in line for kw in keywords):
                lines.append(line.strip())
        return "\n".join(lines[-limit:]) or "（無結果）"

    # ── Phase A+B: daily_closed_loop.py（已內含搜尋+篩選+匯入+PDF 下載）──
    cmd_ab = ["python3", "scripts/daily_closed_loop.py"]
    if job_ids != "all":
        for jid in job_ids:
            cmd_ab.extend(["--job-id", str(jid)])

    await send_reply(update, context, f"🔍 <b>Phase A+B</b> 搜尋→篩選→匯入→PDF 下載中...\n（這步最久，請耐心等候）")
    out_ab, err_ab, code_ab = await run_script_async(cmd_ab, timeout=3600)

    ab_output = out_ab + err_ab
    # 優先顯示 JOB_RESULT 行（結構化摘要）
    job_results = [line.strip() for line in ab_output.split("\n") if "JOB_RESULT" in line]
    if job_results:
        # 去掉 [JOB_RESULT] 前綴和 log timestamp，只保留內容
        clean_results = []
        for jr in job_results:
            idx = jr.find("[JOB_RESULT]")
            if idx >= 0:
                clean_results.append(jr[idx + 13:].strip())
            else:
                clean_results.append(jr.strip())
        ab_summary = "\n".join(clean_results[-15:])  # 最多顯示 15 個職缺
    else:
        ab_summary = _extract_lines(ab_output, ["passed", "Import", "A-layer", "Dedup", "PDF", "uploaded", "completed", "Error", "error", "failed", "No candidates", "Done"])
    # 加上 SUMMARY 區段
    summary_lines = _extract_lines(ab_output, ["SUMMARY", "Jobs processed", "Total searched", "A-layer passed", "Imported", "Hit rate", "Failed", "Errors"])
    if "SUMMARY" in summary_lines:
        ab_summary += "\n\n" + summary_lines

    await send_reply(update, context, f"{'✅' if code_ab == 0 else '⚠️'} <b>Phase A+B 完成</b>\n<pre>{ab_summary[:2000]}</pre>")

    # ── Phase C: AI 分析（用 claude -p）──
    cmd_c = ["python3", "scripts/phase_c_claude_analysis.py"]
    if job_ids != "all":
        for jid in job_ids:
            cmd_c.extend(["--job-id", str(jid)])
    else:
        cmd_c.append("--batch")

    await send_reply(update, context, f"🧠 <b>Phase C</b> AI 分析中...")
    out_c, err_c, code_c = await run_script_async(cmd_c, timeout=1800)

    c_summary = _extract_lines(out_c + err_c, ["score=", "verified", "Phase C", "Processed", "Success", "Error", "failed"])
    await send_reply(update, context, f"{'✅' if code_c == 0 else '⚠️'} <b>Phase C 完成</b>\n<pre>{c_summary[:800]}</pre>")

    # ── 通知 ──
    await send_reply(update, context, f"📢 發送通知中...")
    await run_script_async(["python3", "scripts/notify_consultant.py", "--batch"], timeout=60)

    # ── 最終報告：查本地 DB 取人選資料 ──
    report_lines = [f"✅ <b>一條龍閉環完成</b>\n{display}\n"]
    report_lines.append(f"A+B: {'✅' if code_ab == 0 else '❌'} | C: {'✅' if code_c == 0 else '❌'} | 通知: ✅\n")

    target_ids = job_ids if job_ids != "all" else []
    if target_ids:
        for jid in target_ids:
            try:
                # 優先查本地 DB
                candidates = db_get_candidates(job_id=jid, limit=50)
                if not candidates:
                    # Fallback HR API
                    data = await api_get_async(f"/api/crawler/pipeline/candidates?target_job_id={jid}&limit=50")
                    candidates = data.get("data", [])

                today = datetime.now().strftime("%Y-%m-%d")
                today_candidates = [c for c in candidates if today in str(c.get("created_at", ""))]

                # 按 AI 分數排序
                scored = [c for c in today_candidates if c.get("ai_score")]
                scored.sort(key=lambda c: c.get("ai_score", 0), reverse=True)
                all_today = scored + [c for c in today_candidates if not c.get("ai_score")]

                if all_today:
                    top = [c for c in scored if (c.get("ai_score") or 0) >= 70]
                    report_lines.append(f"<b>📋 #{jid} 搜尋 {len(all_today)} 人 → 推薦 {len(top)} 人</b>\n")

                    if top:
                        report_lines.append("🏆 <b>推薦名單：</b>")
                        for i, c in enumerate(top[:10], 1):
                            name = c.get("name", "?")
                            title = (c.get("current_title") or c.get("current_position") or "")[:35]
                            company = (c.get("current_company") or "")[:15]
                            score = c.get("ai_score", "")
                            grade = c.get("ai_grade", "")
                            linkedin = c.get("linkedin_url", "")
                            rec = (c.get("ai_recommendation") or "")[:40]
                            report_lines.append(f"\n{i}. <b>{name}</b> | {title}")
                            if company:
                                report_lines.append(f"   🏢 {company} | AI: {score}分 {grade}")
                            else:
                                report_lines.append(f"   AI: {score}分 {grade}")
                            if linkedin:
                                report_lines.append(f"   🔗 {linkedin}")
                            if rec:
                                report_lines.append(f"   💡 {rec}")
                else:
                    report_lines.append(f"<b>#{jid}</b> 今日無新匯入人選")
            except:
                pass

    await send_reply(update, context, "\n".join(report_lines))

    _running_task = None
    if not _task_queue.empty():
        task_type, args, upd, ctx = await _task_queue.get()
        if task_type == "loop":
            await _execute_loop(args, upd, ctx)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/search 或 查人 — 多源 OSINT 搜尋特定人"""
    if not is_authorized(update):
        return
    text = (update.message.text or "")
    for prefix in ["/search", "查人", "search"]:
        text = text.replace(prefix, "")
    text = text.strip()
    if not text:
        await send_reply(update, context, "⚠️ 請指定姓名，例如：\n<code>/search 林柏瑋</code>\n<code>/search David Chen</code>")
        return

    await send_reply(update, context, f"🔍 <b>OSINT 搜尋: {text}</b>\n\n搜尋中（本地 DB → GitHub → CakeResume）...")

    import requests as _req
    lines = [f"<b>🔍 {text} — OSINT 搜尋結果</b>\n"]
    found_anything = False

    # ── 1. 本地 DB 搜尋 ──
    try:
        r = _req.get("http://localhost:5001/api/candidates/search",
                     params={'q': text, 'limit': 5}, timeout=5)
        if r.status_code == 200:
            db_results = r.json().get('data', [])
            if db_results:
                found_anything = True
                lines.append(f"📦 <b>人才庫: {len(db_results)} 匹配</b>")
                for c in db_results[:3]:
                    name = c.get('name', '?')
                    bio = (c.get('bio') or c.get('title') or '')[:35]
                    linkedin = c.get('linkedin_url', '')
                    github = c.get('github_url', '')
                    cake = c.get('cakeresume_url', '')
                    email = c.get('email', '')
                    company = c.get('company', '')

                    lines.append(f"\n  <b>{name}</b>")
                    if bio: lines.append(f"  {bio}")
                    if company: lines.append(f"  🏢 {company}")
                    if email: lines.append(f"  📧 {email}")
                    if linkedin: lines.append(f"  🔗 {linkedin}")
                    if github: lines.append(f"  🐙 {github}")
                    if cake: lines.append(f"  🍰 {cake}")
                lines.append("")
    except Exception:
        pass

    # ── 2. GitHub 搜尋（用英文名字部分） ──
    try:
        import re as _re
        eng_parts = _re.findall(r'[A-Za-z]+', text)
        search_q = '+'.join(eng_parts[:2]) if eng_parts else text
        r = _req.get(f"http://localhost:5001/api/health", timeout=2)  # just check server

        # Direct GitHub API search
        import os
        gh_headers = {'Accept': 'application/vnd.github.v3+json'}
        gh_tokens = os.environ.get('GITHUB_TOKENS', '').split(',')
        if gh_tokens and gh_tokens[0].strip():
            gh_headers['Authorization'] = f'token {gh_tokens[0].strip()}'

        for q in [search_q, text.replace(' ', '+')]:
            import urllib.request, json as _json, ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(
                f'https://api.github.com/search/users?q={q}+location:Taiwan&per_page=3',
                headers=gh_headers)
            try:
                resp = urllib.request.urlopen(req, timeout=8, context=ctx)
                data = _json.loads(resp.read())
                if data.get('total_count', 0) > 0:
                    found_anything = True
                    lines.append(f"🐙 <b>GitHub 搜尋:</b>")
                    for u in data['items'][:3]:
                        login = u.get('login', '')
                        gh_url = u.get('html_url', '')
                        gh_name = u.get('name') or login
                        lines.append(f"  <b>{gh_name}</b> → {gh_url}")
                    lines.append("")
                    break
            except Exception:
                continue
    except Exception:
        pass

    # ── 3. Step1ne 系統搜尋 ──
    try:
        data = await api_get_async(f"/api/crawler/pipeline/search?q={text}&limit=3")
        items = data.get("data", [])
        if items:
            found_anything = True
            lines.append(f"📋 <b>Step1ne 系統: {len(items)} 匹配</b>")
            for c in items[:3]:
                cid = c.get("id", "?")
                name = c.get("name", "?")
                title = (c.get("current_title") or c.get("current_position") or "")[:40]
                linkedin = c.get("linkedinUrl") or c.get("linkedin_url") or ""
                lines.append(f"  <b>{name}</b> — {title}")
                if linkedin:
                    lines.append(f"  🔗 {linkedin}")
    except Exception:
        pass

    # ── 結果輸出 ──
    if not found_anything:
        lines.append(f"❌ 所有來源都找不到「{text}」")
        lines.append(f"\n💡 建議用「🔍 自由搜尋人才」啟動多源爬蟲搜尋")

    await send_reply(update, context, "\n".join(lines))


async def cmd_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/job 或 查職缺"""
    if not is_authorized(update):
        return
    text = (update.message.text or "")
    job_ids = parse_job_ids(text)
    if not job_ids:
        await send_reply(update, context, "⚠️ 請指定職缺，例如：<code>/job 233</code>")
        return
    for jid in job_ids[:3]:
        data = await api_get_async(f"/api/crawler/pipeline/jobs/{jid}")
        d = data.get("data", data)
        if d.get("error"):
            await send_reply(update, context, f"❌ #{jid} 查詢失敗: {d['error']}")
        else:
            text = f"""<b>📋 #{jid} {d.get('position_name','?')}</b>
客戶: {d.get('client_company','?')}
狀態: {d.get('job_status','?')}
地點: {d.get('location','?')}
薪資: {d.get('salary_range','')}
技能: {(d.get('key_skills') or '')[:80]}"""
            await send_reply(update, context, text)


# ── 自由搜尋 + 即時推薦 ─────────────────────────────────────

def _parse_search_intent(text: str) -> dict:
    """
    解析自由搜尋輸入，提取職缺和技能
    例: "找 Golang 後端工程師要會 K8s" → {job_title: '後端工程師', primary_skills: ['Golang'], secondary_skills: ['Kubernetes']}
    例: "Senior Backend Engineer Golang Kubernetes" → {job_title: 'Senior Backend Engineer', primary_skills: ['Golang', 'Kubernetes']}
    """
    import re as _re

    # 移除常見前綴
    for prefix in ['找', '搜尋', '搜索', '幫我找', '我要找', '搜', 'find', 'search']:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()

    # 已知技能關鍵字（用於分離技能和職稱）
    known_skills = {
        'golang', 'go', 'python', 'java', 'javascript', 'typescript', 'rust', 'c++', 'c#',
        'ruby', 'php', 'swift', 'kotlin', 'scala', 'react', 'vue', 'angular', 'node.js',
        'docker', 'kubernetes', 'k8s', 'aws', 'gcp', 'azure', 'terraform',
        'postgresql', 'mysql', 'mongodb', 'redis', 'elasticsearch', 'graphql', 'grpc',
        'machine learning', 'deep learning', 'pytorch', 'tensorflow',
        'flutter', 'react native', 'ios', 'android',
        'devops', 'sre', 'ci/cd', 'jenkins', 'linux', 'git',
    }

    words = text.split()
    skills = []
    title_parts = []

    for w in words:
        wl = w.lower().strip('，。、,.')
        if wl in known_skills:
            skills.append(w)
        elif any(wl in ks for ks in known_skills):
            skills.append(w)
        else:
            title_parts.append(w)

    # 如果沒分離出技能，把所有都當技能
    if not skills and title_parts:
        skills = title_parts
        title_parts = []

    job_title = ' '.join(title_parts) if title_parts else ''
    primary = skills[:3]
    secondary = skills[3:]

    return {
        'job_title': job_title,
        'primary_skills': primary,
        'secondary_skills': secondary,
        'all_text': text,
    }


async def _instant_recommend(skills: list, update: Update, context: ContextTypes.DEFAULT_TYPE,
                              job_title: str = ''):
    """從 DB 秒回推薦候選人"""
    import requests as _req
    try:
        skills_str = ','.join(skills)
        r = _req.get("http://localhost:5001/api/candidates/recommend",
                     params={'skills': skills_str, 'limit': 10, 'min_grade': 'B'}, timeout=5)
        if r.status_code != 200:
            return

        data = r.json()
        candidates = data.get('data', [])
        total = data.get('total', 0)

        if not candidates:
            await send_reply(update, context,
                f"📭 人才庫中尚無符合 <b>{', '.join(skills)}</b> 的 A/B 級候選人\n\n⏳ 已啟動多源搜尋，新結果稍後通知")
            return

        lines = [f"⚡ <b>即時推薦</b> — 人才庫 {total} 位符合\n"]
        for i, c in enumerate(candidates[:10], 1):
            name = c.get('name', '?')
            title = (c.get('title') or c.get('bio') or '')[:35]
            grade = c.get('grade', '?')
            score = c.get('score', 0)
            source = c.get('source', '')
            linkedin = c.get('linkedin_url', '')
            email = c.get('email', '')
            company = (c.get('company') or '')[:15]

            github_url = c.get('github_url', '')
            github_user = c.get('github_username', '')
            cake_url = c.get('cakeresume_url', '')

            # 聯繫方式快速標記
            contacts = []
            if email: contacts.append('📧')
            if linkedin: contacts.append('🔗')
            if github_url or github_user: contacts.append('🐙')
            if cake_url: contacts.append('🍰')
            contact_flag = ' '.join(contacts) if contacts else '❌無聯繫'

            lines.append(f"{i}. <b>{name}</b> [{grade}:{score}分] {contact_flag}")
            if title:
                lines.append(f"   {title}")
            if company:
                lines.append(f"   🏢 {company}")
            if email:
                lines.append(f"   📧 {email}")
            if linkedin:
                lines.append(f"   🔗 {linkedin}")
            if github_url:
                lines.append(f"   🐙 {github_url}")
            elif github_user:
                lines.append(f"   🐙 https://github.com/{github_user}")
            if cake_url:
                lines.append(f"   🍰 {cake_url}")

        lines.append(f"\n⏳ 同時已啟動多源即時搜尋（LinkedIn + GitHub + CakeResume），完成後通知新結果")
        await send_reply(update, context, "\n".join(lines))

    except Exception as e:
        log.error(f"instant_recommend error: {e}")


async def _start_crawler_search(skills: list, job_title: str, location: str,
                                  update: Update, context: ContextTypes.DEFAULT_TYPE,
                                  pages: int = 3, start_page: int = None):
    """
    背景啟動多源爬蟲搜尋

    Args:
        pages: 搜尋頁數 (預設 3，擴大範圍)
        start_page: 從第幾頁開始（隨機化，每次爬不同範圍）
    """
    import requests as _req
    import random

    # 隨機化 start_page — 每次從不同頁數開始，降低撈到相同人的機率
    if start_page is None:
        start_page = random.randint(0, 5)

    try:
        payload = {
            'job_title': job_title or ' '.join(skills),
            'primary_skills': skills[:3],
            'secondary_skills': skills[3:],
            'location': location,
            'location_zh': '台灣' if 'taiwan' in location.lower() else location,
            'pages': pages,
            'start_page': start_page,
            'client_name': '自由搜尋',
            'schedule_type': 'once',
        }
        r = _req.post("http://localhost:5001/api/tasks", json=payload, timeout=10)
        if r.status_code in (200, 201):
            data = r.json()
            task_id = data.get('id', '?')
            log.info(f"Free search task created: {task_id}")
            return task_id
        else:
            log.error(f"Create task failed: {r.status_code} {r.text[:200]}")
            return None
    except Exception as e:
        log.error(f"start_crawler_search error: {e}")
        return None


async def cmd_free_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理自由搜尋文字輸入"""
    if not is_authorized(update):
        return

    text = (update.message.text or '').strip()

    # 解析輸入
    intent = _parse_search_intent(text)
    skills = intent['primary_skills'] + intent['secondary_skills']

    if not skills:
        await send_reply(update, context, "⚠️ 無法辨識技能關鍵字，請重新輸入\n例: <code>Golang 後端 Kubernetes</code>")
        return

    job_title = intent['job_title'] or ' '.join(skills[:2])
    await send_reply(update, context,
        f"🔍 <b>搜尋中...</b>\n"
        f"職缺: {job_title}\n"
        f"技能: {', '.join(skills)}\n\n"
        f"1️⃣ 先從人才庫推薦...\n"
        f"2️⃣ 同時啟動多源爬蟲搜尋")

    # 1. 即時推薦（秒回）
    await _instant_recommend(skills, update, context, job_title)

    # 2. 背景啟動爬蟲 + 進度追蹤
    task_id = await _start_crawler_search(skills, job_title, 'Taiwan', update, context)
    if task_id:
        await send_reply(update, context,
            f"🚀 多源搜尋任務已啟動\n"
            f"📋 任務 ID: <code>{task_id}</code>\n"
            f"🔄 搜尋來源: LinkedIn + GitHub + CakeResume\n\n"
            f"⏳ 預計 3-8 分鐘完成，完成後自動推送新候選人")

        # 背景輪詢進度並回報
        asyncio.create_task(_poll_and_notify(task_id, skills, update, context))


async def _poll_and_notify(task_id: str, skills: list, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """背景輪詢任務進度，完成後推送結果"""
    import requests as _req
    try:
        last_progress = -1
        for _ in range(120):  # 最多等 10 分鐘 (120 * 5s)
            await asyncio.sleep(5)
            try:
                r = _req.get(f"http://localhost:5001/api/tasks/{task_id}/status", timeout=5)
                if r.status_code != 200:
                    continue
                status = r.json()
                progress = status.get('progress', 0)
                detail = status.get('progress_detail', '')
                task_status = status.get('status', '')

                # 進度回報（每 20% 報一次）
                if progress >= last_progress + 20 and progress < 100:
                    last_progress = progress
                    await send_reply(update, context,
                        f"🔄 搜尋進度 {progress}%\n{detail}")

                # 完成
                if task_status == 'completed':
                    result_count = status.get('last_result_count', 0)
                    li_count = status.get('linkedin_count', 0)
                    gh_count = status.get('github_count', 0)

                    # 關鍵：只取「這次任務新找到的人」(按 task_id 過濾，排除重複)
                    new_candidates = []
                    dup_count = 0
                    try:
                        rr = _req.get(f"http://localhost:5001/api/candidates/by-task/{task_id}",
                                      params={'limit': 20, 'sort_by': 'has_email', 'only_new': 'true'},
                                      timeout=5)
                        if rr.status_code == 200:
                            body = rr.json()
                            new_candidates = body.get('data', [])
                            dup_count = body.get('duplicates_filtered', 0)
                    except Exception:
                        pass

                    lines = [
                        f"✅ <b>多源搜尋完成</b>\n",
                        f"📊 搜尋到 {result_count} 人 (LinkedIn {li_count} + GitHub {gh_count})",
                        f"♻️ 已在人才庫中: {dup_count} 位 (跳過)",
                        f"🆕 本次新找到: {len(new_candidates)} 位\n",
                    ]

                    # 如果沒有新的人，提示使用者
                    if not new_candidates and dup_count > 0:
                        lines.append(f"💡 本次爬到的 {dup_count} 人都已在人才庫中（即時推薦已顯示）")
                        lines.append(f"   建議換不同關鍵字或公司名試試")

                    if new_candidates:
                        lines.append("<b>🏆 本次新結果:</b>")
                        for i, c in enumerate(new_candidates[:10], 1):
                            name = c.get('name', '?')
                            grade = c.get('grade', '?')
                            score = c.get('score', 0)
                            github_url = c.get('github_url', '')
                            github_user = c.get('github_username', '')
                            linkedin = c.get('linkedin_url', '')
                            cake_url = c.get('cakeresume_url', '')
                            email = c.get('email', '')
                            source = c.get('source', '')
                            bio = (c.get('bio') or c.get('title') or '')[:40]

                            # 來源標籤
                            src_tags = []
                            if 'github' in source: src_tags.append('GitHub')
                            if 'cakeresume' in source: src_tags.append('CakeResume')
                            if 'linkedin' in source: src_tags.append('LinkedIn')
                            if 'conference' in source: src_tags.append('Conference')
                            src_str = '+'.join(src_tags) if src_tags else source

                            # 聯繫方式統計
                            contacts = []
                            if email: contacts.append('📧')
                            if linkedin: contacts.append('🔗')
                            if github_url or github_user: contacts.append('🐙')
                            if cake_url: contacts.append('🍰')
                            contact_str = ' '.join(contacts) if contacts else '❌ 無聯繫方式'

                            lines.append(f"\n{i}. <b>{name}</b> [{grade}:{score}分] ({src_str})")
                            if bio:
                                lines.append(f"   {bio}")
                            lines.append(f"   {contact_str}")
                            if email:
                                lines.append(f"   📧 {email}")
                            if linkedin:
                                lines.append(f"   🔗 {linkedin}")
                            if github_url:
                                lines.append(f"   🐙 {github_url}")
                            elif github_user:
                                lines.append(f"   🐙 https://github.com/{github_user}")
                            if cake_url:
                                lines.append(f"   🍰 {cake_url}")

                    await send_reply(update, context, "\n".join(lines))
                    return

                # 失敗
                if task_status in ('failed', 'stopped'):
                    err = status.get('error_message', '未知錯誤')
                    await send_reply(update, context, f"❌ 搜尋任務失敗: {err}")
                    return

            except Exception:
                continue

        await send_reply(update, context, f"⚠️ 搜尋任務 {task_id} 超時（10分鐘），請到 Web UI 查看結果")

    except Exception as e:
        log.error(f"poll_and_notify error: {e}")


async def cmd_browse_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """瀏覽人才庫 — 只從 DB 搜尋，不啟動爬蟲（快速）"""
    if not is_authorized(update):
        return

    text = (update.message.text or '').strip()
    if not text:
        return

    import requests as _req
    try:
        r = _req.get("http://localhost:5001/api/candidates/search",
                     params={'q': text, 'limit': 15}, timeout=5)
        if r.status_code != 200:
            await send_reply(update, context, "❌ 搜尋失敗")
            return

        data = r.json()
        candidates = data.get('data', [])
        total = data.get('total', 0)

        if not candidates:
            await send_reply(update, context,
                f"📭 人才庫中找不到「{text}」\n\n💡 試試「🔍 自由搜尋人才」啟動爬蟲去找")
            return

        lines = [f"📚 <b>人才庫搜尋: {text}</b>\n{total} 人匹配，顯示前 {min(15, total)} 位\n"]

        for i, c in enumerate(candidates[:15], 1):
            name = c.get('name', '?')
            grade = c.get('grade', '?')
            score = c.get('score', 0)
            bio = (c.get('bio') or c.get('title') or '')[:40]
            company = (c.get('company') or '')[:15]
            linkedin = c.get('linkedin_url', '')
            github_url = c.get('github_url', '')
            cake_url = c.get('cakeresume_url', '')
            email = c.get('email', '')

            contacts = []
            if email: contacts.append('📧')
            if linkedin: contacts.append('🔗')
            if github_url: contacts.append('🐙')
            if cake_url: contacts.append('🍰')
            flag = ' '.join(contacts) if contacts else '❌無聯繫'

            lines.append(f"\n{i}. <b>{name}</b> [{grade}:{score}分] {flag}")
            if bio:
                lines.append(f"   {bio}")
            if company:
                lines.append(f"   🏢 {company}")
            if email:
                lines.append(f"   📧 {email}")
            if linkedin:
                lines.append(f"   🔗 {linkedin}")
            if github_url:
                lines.append(f"   🐙 {github_url}")
            if cake_url:
                lines.append(f"   🍰 {cake_url}")

        await send_reply(update, context, "\n".join(lines))
    except Exception as e:
        await send_reply(update, context, f"❌ 錯誤: {e}")


async def cmd_company_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理公司定向搜尋文字輸入"""
    if not is_authorized(update):
        return

    text = (update.message.text or '').strip()
    parts = text.split()

    if len(parts) < 2:
        await send_reply(update, context,
            "⚠️ 請輸入公司名 + 職位/技能\n例: <code>三竹資訊 Java</code>")
        return

    # 第一個詞當公司名（支援中文公司名），剩下當技能
    # 智慧拆分：中文公司名可能 2-4 個字，英文公司名 1 個詞
    company = parts[0]
    skills = parts[1:]

    # 如果前兩個都是中文且像公司名，合併
    if len(parts) >= 3 and all(any('\u4e00' <= c <= '\u9fff' for c in p) for p in parts[:2]):
        company = parts[0] + parts[1]
        skills = parts[2:]

    await send_reply(update, context,
        f"🏢 <b>公司定向搜尋</b>\n"
        f"🎯 目標公司: {company}\n"
        f"🔧 職位/技能: {', '.join(skills)}\n\n"
        f"1️⃣ 從人才庫搜尋曾在 {company} 的人...\n"
        f"2️⃣ 同時啟動多源搜尋")

    # 1. DB 搜尋：用公司名 + 技能搜
    import requests as _req
    try:
        # 先用模糊搜尋 API（更適合公司定向搜尋）
        q = f"{company} {' '.join(skills)}"
        r = _req.get("http://localhost:5001/api/candidates/search",
                     params={'q': q, 'limit': 20}, timeout=5)
        candidates = r.json().get('data', []) if r.status_code == 200 else []

        # Fallback: recommend API
        if not candidates:
            all_skills = [company] + skills
            skills_str = ','.join(all_skills)
            r = _req.get("http://localhost:5001/api/candidates/recommend",
                         params={'skills': skills_str, 'limit': 10, 'min_grade': 'D', 'local_only': 'true'},
                         timeout=5)
            if r.status_code == 200:
                candidates = r.json().get('data', [])

        if candidates:
            # 優先顯示 company/bio/work_history 含公司名的
            company_lower = company.lower()
            prioritized = []
            others = []
            for c in candidates:
                c_company = (c.get('company', '') or '').lower()
                c_bio = (c.get('bio', '') or '').lower()
                c_wh = str(c.get('work_history', '')).lower()
                if company_lower in c_company or company_lower in c_bio or company_lower in c_wh:
                    prioritized.append(c)
                else:
                    others.append(c)

            final = prioritized + others

            if final:
                lines = [f"⚡ <b>人才庫匹配</b> — {len(final)} 位\n"]
                if prioritized:
                    lines.append(f"🎯 其中 {len(prioritized)} 位有 {company} 相關經歷\n")
                for i, c in enumerate(final[:10], 1):
                    name = c.get('name', '?')
                    grade = c.get('grade', '?')
                    score = c.get('score', 0)
                    bio = (c.get('bio') or c.get('title') or '')[:35]
                    comp = (c.get('company') or '')[:15]
                    linkedin = c.get('linkedin_url', '')
                    github_url = c.get('github_url', '')
                    cake_url = c.get('cakeresume_url', '')
                    email = c.get('email', '')

                    is_target = c in prioritized
                    flag = '🎯' if is_target else ''

                    lines.append(f"{i}. {flag}<b>{name}</b> [{grade}:{score}分]")
                    if bio:
                        lines.append(f"   {bio}")
                    if comp:
                        lines.append(f"   🏢 {comp}")
                    if email:
                        lines.append(f"   📧 {email}")
                    if linkedin:
                        lines.append(f"   🔗 {linkedin}")
                    if github_url:
                        lines.append(f"   🐙 {github_url}")
                    if cake_url:
                        lines.append(f"   🍰 {cake_url}")

                await send_reply(update, context, "\n".join(lines))
            else:
                await send_reply(update, context,
                    f"📭 人才庫中尚無 {company} + {', '.join(skills)} 的候選人")
    except Exception as e:
        log.error(f"Company search DB error: {e}")

    # 2. 背景多源搜尋：用多種 query 組合
    # 把公司名加入搜尋技能，讓 LinkedIn/CakeResume 也能搜到
    search_skills = skills + [company]
    job_title = f"{company} {' '.join(skills)}"
    task_id = await _start_crawler_search(search_skills, job_title, 'Taiwan', update, context)

    if task_id:
        await send_reply(update, context,
            f"🚀 多源搜尋已啟動\n"
            f"🔄 搜尋: \"{company}\" + \"{' '.join(skills)}\"\n"
            f"📋 任務 ID: <code>{task_id}</code>\n\n"
            f"⏳ 完成後自動推送結果")
        asyncio.create_task(_poll_and_notify(task_id, search_skills, update, context))


async def _start_multisource_search_for_job(job_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """路徑 1: 從 Step1ne 職缺取技能 → 建立多源搜尋任務 → 進度追蹤 → 完成通知"""
    import requests as _req

    # 取得職缺資料
    job = db_get_job(job_id)
    if not job:
        job = (await api_get_async(f"/api/crawler/pipeline/jobs/{job_id}")).get("data", {})

    position = job.get("position_name", "") or f"Job #{job_id}"
    client = job.get("client_company", "") or ""
    skills_str = job.get("key_skills", "")
    location = job.get("location", "Taiwan") or "Taiwan"

    # 解析技能
    skills = [s.strip() for s in skills_str.split(",") if s.strip()] if skills_str else []
    if not skills:
        # 從職缺名稱提取
        skills = [s.strip() for s in position.split() if len(s.strip()) >= 2][:3]

    if not skills:
        await send_reply(update, context, f"⚠️ #{job_id} 無法識別搜尋技能，請用「自由搜尋人才」手動輸入")
        return

    # 建立多源搜尋任務
    primary = skills[:3]
    secondary = skills[3:]
    task_id = await _start_crawler_search(primary + secondary, position, location, update, context)

    if task_id:
        await send_reply(update, context,
            f"🚀 <b>#{job_id} {position}</b> 多源搜尋已啟動\n"
            f"🏢 {client}\n"
            f"🔧 技能: {', '.join(skills[:5])}\n"
            f"📋 任務 ID: <code>{task_id}</code>\n"
            f"🔄 搜尋來源: LinkedIn + GitHub + CakeResume\n\n"
            f"⏳ 預計 3-8 分鐘，完成後自動推送結果")

        # 背景進度追蹤 + 完成通知
        asyncio.create_task(_poll_and_notify(task_id, skills, update, context))
    else:
        await send_reply(update, context, f"❌ #{job_id} 搜尋任務建立失敗")


async def _run_job_with_custom_keywords(job_id: int, keywords_text: str,
                                          update: Update, context: ContextTypes.DEFAULT_TYPE):
    """使用自訂關鍵字啟動職缺搜尋"""
    import re as _re
    # 解析使用者輸入的關鍵字（空格/逗號分隔）
    skills = [s.strip() for s in _re.split(r'[,，\s]+', keywords_text) if s.strip()]
    if not skills:
        await send_reply(update, context, "⚠️ 無法識別關鍵字")
        return

    job = db_get_job(job_id)
    position = job.get("position_name", f"Job #{job_id}") if job else f"Job #{job_id}"
    location = (job.get("location", "Taiwan") if job else "Taiwan") or "Taiwan"

    await send_reply(update, context,
        f"✏️ <b>使用自訂關鍵字搜尋</b>\n"
        f"📋 #{job_id} {position}\n"
        f"🔧 新關鍵字: {', '.join(skills)}\n\n"
        f"正在從人才庫查找...")

    # 1. 即時推薦
    await _instant_recommend(skills, update, context, position)

    # 2. 啟動多源搜尋
    task_id = await _start_crawler_search(skills, position, location, update, context)
    if task_id:
        await send_reply(update, context,
            f"🚀 多源搜尋已啟動\n"
            f"📋 任務 ID: <code>{task_id}</code>\n"
            f"🔄 搜尋: {', '.join(skills)}\n\n"
            f"⏳ 完成後自動推送結果")
        asyncio.create_task(_poll_and_notify(task_id, skills, update, context))


async def _show_job_preview(job_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """顯示職缺資訊 + 關鍵字，讓使用者選擇直接搜或修改關鍵字"""
    query = update.callback_query
    job = db_get_job(job_id)
    if not job:
        job = (await api_get_async(f"/api/crawler/pipeline/jobs/{job_id}")).get("data", {})

    if not job:
        await query.edit_message_text(f"❌ 找不到 #{job_id}")
        return

    position = job.get("position_name", "?")
    client = job.get("client_company", "")
    skills = job.get("key_skills", "") or "(未設定)"
    location = job.get("location", "") or "Taiwan"
    salary = job.get("salary_range", "") or ""

    text = f"""📋 <b>#{job_id} {position}</b>
🏢 {client}
📍 {location}"""
    if salary:
        text += f"\n💰 {salary}"
    text += f"\n\n🔧 <b>目前搜尋關鍵字:</b>\n<code>{skills}</code>\n\n選擇操作："

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ 使用預設關鍵字搜尋", callback_data=f"runjob_{job_id}")],
        [InlineKeyboardButton("✏️ 修改關鍵字後搜尋", callback_data=f"editjob_{job_id}")],
        [InlineKeyboardButton("« 返回職缺列表", callback_data="show_jobs")],
    ])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


async def _instant_recommend_for_job(job_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """路徑 1: 從職缺取技能 → 秒回推薦 + 啟動爬蟲"""
    job = db_get_job(job_id)
    if not job:
        job = (await api_get_async(f"/api/crawler/pipeline/jobs/{job_id}")).get("data", {})

    skills_str = job.get("key_skills", "")
    position = job.get("position_name", "")
    client = job.get("client_company", "")

    # 解析技能
    skills = [s.strip() for s in skills_str.split(",") if s.strip()] if skills_str else []
    if not skills:
        skills = [s.strip() for s in position.split() if len(s.strip()) >= 2]

    if skills:
        await send_reply(update, context,
            f"⚡ <b>#{job_id} {position}</b>\n"
            f"🏢 {client}\n"
            f"🔧 {', '.join(skills[:5])}\n\n"
            f"正在從人才庫查找匹配候選人...")
        await _instant_recommend(skills, update, context, position)


# ── 按鈕回呼 ──────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理按鈕點擊"""
    query = update.callback_query
    await query.answer()

    if not is_authorized(update):
        return

    data = query.data
    log.info(f"CALLBACK: {data} from user={query.from_user.id}")

    if data == "help":
        text = "<b>🤖 Step1ne 閉環指令</b>\n\n👇 選擇操作："
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=build_help_keyboard())

    elif data == "status":
        # 直接執行狀態檢查
        import requests
        results = {}
        try:
            r = requests.get(f"{API_BASE}/api/health", timeout=10)
            results["hr"] = "✅" if r.status_code == 200 else "❌"
        except:
            results["hr"] = "❌"
        try:
            r = requests.get("http://localhost:5001/api/tasks", timeout=5)
            results["crawler"] = "✅" if r.status_code == 200 else "❌"
        except:
            results["crawler"] = "❌"
        try:
            requests.get("http://localhost:9222/json/version", timeout=5)
            results["cdp"] = "✅"
        except:
            results["cdp"] = "❌"
        task_status = f"🔄 {_running_task}" if _running_task else "💤 閒置"
        now = datetime.now().strftime("%H:%M")
        text = f"""<b>🔍 系統狀態</b>

{results['hr']} HR API
{results['crawler']} 爬蟲
{results['cdp']} Chrome CDP
🤖 Bot: {task_status}
⏰ {now}"""
        await query.edit_message_text(text, parse_mode="HTML",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« 返回", callback_data="help")]]))

    elif data == "free_search_prompt":
        _pending_free_search[query.from_user.id] = True
        cancel_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ 取消", callback_data="cancel_free_search")],
            [InlineKeyboardButton("« 返回主選單", callback_data="help")],
        ])
        await query.edit_message_text(
            "🔍 <b>自由搜尋人才</b>\n\n"
            "請輸入職缺名稱和/或技能關鍵字，例如：\n"
            "<code>Golang 後端工程師 Kubernetes</code>\n"
            "<code>React Senior Frontend</code>\n"
            "<code>DevOps AWS Docker</code>\n\n"
            "系統會同時從人才庫推薦 + 啟動多源搜尋（LinkedIn + GitHub + CakeResume）",
            parse_mode="HTML",
            reply_markup=cancel_kb,
        )

    elif data == "cancel_free_search":
        _pending_free_search.pop(query.from_user.id, None)
        await query.edit_message_text("✅ 已取消", parse_mode="HTML")

    elif data == "browse_db_prompt":
        _pending_browse_db[query.from_user.id] = True
        cancel_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ 取消", callback_data="cancel_browse")],
            [InlineKeyboardButton("« 返回主選單", callback_data="help")],
        ])
        await query.edit_message_text(
            "📚 <b>瀏覽人才庫</b>\n\n"
            "輸入關鍵字瀏覽人才庫，可搜尋:\n"
            "• 技能: <code>Golang Kubernetes</code>\n"
            "• 公司: <code>精誠資訊</code>\n"
            "• 姓名: <code>林柏瑋</code>\n"
            "• 職位: <code>後端工程師</code>\n\n"
            "只從已爬過的人才庫搜，不啟動新爬蟲（快速）",
            parse_mode="HTML",
            reply_markup=cancel_kb,
        )

    elif data == "cancel_browse":
        _pending_browse_db.pop(query.from_user.id, None)
        await query.edit_message_text("✅ 已取消", parse_mode="HTML")

    elif data == "company_search_prompt":
        _pending_company_search[query.from_user.id] = True
        cancel_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ 取消", callback_data="cancel_company_search")],
            [InlineKeyboardButton("« 返回主選單", callback_data="help")],
        ])
        await query.edit_message_text(
            "🏢 <b>公司定向搜尋</b>\n\n"
            "輸入格式: <b>公司名 + 職位/技能</b>\n\n"
            "範例：\n"
            "<code>三竹資訊 Java</code>\n"
            "<code>精誠資訊 後端工程師</code>\n"
            "<code>Appier Golang Backend</code>\n"
            "<code>Dcard React Frontend</code>\n\n"
            "系統會用多種 query 組合搜尋曾在該公司任職的人",
            parse_mode="HTML",
            reply_markup=cancel_kb,
        )

    elif data == "cancel_company_search":
        _pending_company_search.pop(query.from_user.id, None)
        await query.edit_message_text("✅ 已取消", parse_mode="HTML")

    elif data == "db_stats":
        import requests as _req
        try:
            r = _req.get("http://localhost:5001/api/dashboard/stats", timeout=5)
            stats = r.json() if r.status_code == 200 else {}
        except:
            stats = {}
        grades = stats.get('grades', {})
        sources = stats.get('sources', {})
        text = f"""📊 <b>人才庫總覽</b>

👥 總候選人: <b>{stats.get('total_candidates', 0)}</b>
📋 職缺數: {stats.get('scheduled_tasks', 0)}
🆕 今日新增: {stats.get('today_new', 0)}

<b>等級分布:</b>
  🅰️ A: {grades.get('A', 0)} | 🅱️ B: {grades.get('B', 0)}
  ©️ C: {grades.get('C', 0)} | 🅳 D: {grades.get('D', 0)}

<b>來源分布:</b>
  💼 LinkedIn: {sources.get('linkedin', 0)}
  🐙 GitHub: {sources.get('github', 0)}
  🍰 CakeResume: {sources.get('cakeresume', 0)}"""
        await query.edit_message_text(text, parse_mode="HTML",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« 返回", callback_data="help")]]))

    elif data == "show_jobs" or data == "list_jobs":
        jobs = await fetch_active_jobs_async()
        if jobs:
            text = f"🚀 <b>選擇職缺搜尋（{len(jobs)} 個）</b>\n\n選擇後立即推薦人才庫匹配 + 啟動多源搜尋"
            await query.edit_message_text(text, parse_mode="HTML",
                                          reply_markup=build_job_keyboard(jobs, action="loop"))
        else:
            await query.edit_message_text("❌ 無招募中職缺", parse_mode="HTML")

    elif data == "show_jobs_profile":
        jobs = await fetch_active_jobs_async()
        if jobs:
            text = f"📋 <b>選擇職缺查看搜尋策略（{len(jobs)} 個）</b>"
            await query.edit_message_text(text, parse_mode="HTML",
                                          reply_markup=build_job_keyboard(jobs, action="profile"))
        else:
            await query.edit_message_text("❌ 無招募中職缺", parse_mode="HTML")

    elif data == "show_jobs_optimize":
        jobs = await fetch_active_jobs_async()
        if jobs:
            text = f"🧠 <b>選擇職缺 AI 優化關鍵字（{len(jobs)} 個）</b>"
            await query.edit_message_text(text, parse_mode="HTML",
                                          reply_markup=build_job_keyboard(jobs, action="optimize"))
        else:
            await query.edit_message_text("❌ 無招募中職缺", parse_mode="HTML")

    elif data == "loop_all":
        await query.edit_message_text("🚀 <b>即將跑全部招募中職缺的閉環...</b>", parse_mode="HTML")
        await _start_loop_from_callback("all", update, context)

    elif data.startswith("optimize_"):
        jid = int(data.replace("optimize_", ""))
        await query.edit_message_text(f"🧠 <b>呼叫 YuQi 優化 #{jid} 關鍵字中...</b>", parse_mode="HTML")
        await _optimize_keywords_via_yuqi(jid, update, context)

    elif data.startswith("profile_"):
        jid = int(data.replace("profile_", ""))
        await query.edit_message_text(f"📋 <b>查看 #{jid} 搜尋策略...</b>", parse_mode="HTML")
        await _generate_profile_from_callback(jid, update, context)

    elif data.startswith("loop_"):
        # 選職缺後顯示職缺資訊 + 關鍵字，讓使用者選擇「直接搜」或「修改關鍵字」
        jid = int(data.replace("loop_", ""))
        await _show_job_preview(jid, update, context)

    elif data.startswith("runjob_"):
        # 使用預設關鍵字直接搜
        jid = int(data.replace("runjob_", ""))
        await query.edit_message_text(f"🚀 <b>#{jid} 搜尋啟動中...</b>", parse_mode="HTML")
        await _instant_recommend_for_job(jid, update, context)
        await _start_multisource_search_for_job(jid, update, context)

    elif data.startswith("editjob_"):
        # 修改關鍵字後再搜
        jid = int(data.replace("editjob_", ""))
        _pending_edit_keywords[query.from_user.id] = jid
        job = db_get_job(jid)
        pos = job.get("position_name", "") if job else ""
        current_skills = job.get("key_skills", "") if job else ""
        cancel_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ 取消", callback_data=f"canceledit_{jid}")],
            [InlineKeyboardButton("« 返回職缺資訊", callback_data=f"loop_{jid}")],
        ])
        await query.edit_message_text(
            f"✏️ <b>修改 #{jid} {pos} 的搜尋關鍵字</b>\n\n"
            f"🔧 目前關鍵字:\n<code>{current_skills}</code>\n\n"
            f"請輸入新的關鍵字（空格或逗號分隔）:\n"
            f"例: <code>Java Spring Boot Backend</code>",
            parse_mode="HTML",
            reply_markup=cancel_kb,
        )

    elif data.startswith("canceledit_"):
        jid = int(data.replace("canceledit_", ""))
        _pending_edit_keywords.pop(query.from_user.id, None)
        await query.edit_message_text("✅ 已取消修改關鍵字", parse_mode="HTML")

    elif data.startswith("fb_"):
        await _handle_feedback_callback(query, context)

    elif data == "show_jobs_custom_angle":
        jobs = await fetch_active_jobs_async()
        if jobs:
            text = f"📝 <b>選擇職缺自訂搜尋角度（{len(jobs)} 個）</b>\n\n選好後請輸入搜尋關鍵字"
            await query.edit_message_text(text, parse_mode="HTML",
                                          reply_markup=build_job_keyboard(jobs, action="customangle"))
        else:
            await query.edit_message_text("❌ 無招募中職缺", parse_mode="HTML")

    elif data.startswith("customangle_"):
        jid = int(data.replace("customangle_", ""))
        _pending_custom_angle[query.from_user.id] = jid
        job = db_get_job(jid)
        job_name = job.get("position_name", "?") if job else "?"
        await query.edit_message_text(
            f"📝 <b>自訂搜尋角度 — #{jid} {job_name}</b>\n\n"
            f"請直接輸入搜尋關鍵字，例如：\n"
            f"<code>hotel operations pre-opening Taiwan</code>\n\n"
            f"系統會自動加上 <code>site:linkedin.com/in/</code> 前綴並建立新角度。\n"
            f"輸入 <code>取消</code> 可取消。",
            parse_mode="HTML"
        )


# ── 回饋處理 ──────────────────────────────────────────────

FEEDBACK_MAP = {
    "fb_ok": ("suitable", None, "✅ 合適"),
    "fb_exp": ("unsuitable", "experience", "❌ 經驗不足"),
    "fb_ind": ("unsuitable", "industry", "❌ 產業不符"),
    "fb_loc": ("unsuitable", "location", "❌ 地區不對"),
    "fb_oth": ("unsuitable", "other", "❌ 其他原因"),
}


async def _handle_feedback_callback(query, context: ContextTypes.DEFAULT_TYPE):
    """處理候選人回饋按鈕"""
    data = query.data  # e.g. fb_ok_1234_7
    parts = data.split("_")
    # fb_{type}_{candidate_id}_{job_id}
    if len(parts) < 4:
        await query.answer("❌ 格式錯誤")
        return

    fb_key = f"{parts[0]}_{parts[1]}"
    candidate_id = int(parts[2])
    job_id = int(parts[3])
    user_name = query.from_user.first_name if query.from_user else "unknown"

    fb_info = FEEDBACK_MAP.get(fb_key)
    if not fb_info:
        await query.answer("❌ 未知回饋類型")
        return

    verdict, reason_code, label = fb_info

    # 存入 candidate_feedback 表
    try:
        import psycopg2
        conn = psycopg2.connect(dbname=LOCAL_DB)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO candidate_feedback (candidate_id, job_id, verdict, reason_code, feedback_by, created_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
        """, (candidate_id, job_id, verdict, reason_code, user_name))
        conn.commit()
        conn.close()
        log.info(f"Feedback saved: candidate={candidate_id} job={job_id} verdict={verdict} reason={reason_code} by={user_name}")
    except Exception as e:
        log.error(f"Failed to save feedback: {e}")
        await query.answer(f"❌ 儲存失敗: {e}")
        return

    # 更新訊息：標記已回饋
    try:
        original_text = query.message.text or ""
        await query.edit_message_text(
            original_text + f"\n\n📝 <b>回饋：{label}</b>（by {user_name}）",
            parse_mode="HTML",
        )
    except Exception:
        pass

    await query.answer(f"已記錄：{label}")


async def _handle_custom_angle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理自訂搜尋角度的文字輸入"""
    user_id = update.effective_user.id
    job_id = _pending_custom_angle.pop(user_id, None)
    if not job_id:
        return False  # 不是在等自訂角度輸入

    text = (update.message.text or "").strip()

    if text in ("取消", "cancel"):
        await send_reply(update, context, "❌ 已取消自訂角度")
        return True

    # 建立新角度
    import hashlib
    angle_id = f"custom_{hashlib.md5(text.encode()).hexdigest()[:8]}"
    query_template = f'site:linkedin.com/in/ {text}'

    try:
        import psycopg2
        conn = psycopg2.connect(dbname=LOCAL_DB)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO search_angles (job_id, angle_id, angle_name, query_template, priority, enabled)
            VALUES (%s, %s, %s, %s, 0, TRUE)
            ON CONFLICT (job_id, angle_id) DO UPDATE SET
                query_template = EXCLUDED.query_template,
                angle_name = EXCLUDED.angle_name,
                enabled = TRUE
        """, (job_id, angle_id, f"自訂：{text[:30]}", query_template))
        conn.commit()

        # 查現有角度數量
        cur.execute("SELECT COUNT(*) FROM search_angles WHERE job_id = %s AND enabled = TRUE", (job_id,))
        total = cur.fetchone()[0]
        conn.close()

        job = db_get_job(job_id)
        job_name = job.get("position_name", "?") if job else "?"

        await send_reply(update, context,
            f"✅ <b>已建立自訂搜尋角度</b>\n\n"
            f"📋 職缺：#{job_id} {job_name}\n"
            f"🔍 角度：<code>{query_template}</code>\n"
            f"📊 目前共 {total} 個搜尋角度\n\n"
            f"下次跑閉環時會自動使用此角度。"
        )
        log.info(f"Custom angle created: job={job_id} angle={angle_id} query={query_template}")
    except Exception as e:
        log.error(f"Failed to create custom angle: {e}")
        await send_reply(update, context, f"❌ 建立失敗: {e}")

    return True


async def _optimize_keywords_via_yuqi(jid: int, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """呼叫 YuQi 龍蝦做 AI 關鍵字優化"""
    global _running_task
    _running_task = f"AI優化關鍵字 #{jid}"

    job = db_get_job(jid)
    job_name = job.get("position_name", "?") if job else "?"
    client = job.get("client_company", "") if job else ""

    # 呼叫 openclaw agent（YuQi）
    prompt = f"優化關鍵字 #{jid} {job_name}（{client}）。讀取本地 DB step1ne_crawler 的 jobs 表和 candidates 表，分析搜尋結果品質，生成優化後的 search_primary 和 search_secondary，寫回 DB。完成後回報變更內容。"

    try:
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        out, err, code = await run_script_async(
            ["openclaw", "agent", "--agent", "hr-yuqi", "--message", prompt, "--local"],
            timeout=180
        )

        result = out.strip() or err.strip()
        if code == 0 and result:
            # 讀取更新後的關鍵字
            updated_job = db_get_job(jid)
            new_primary = (updated_job.get("search_primary", "") or "")[:200] if updated_job else ""

            msg = f"✅ <b>#{jid} {job_name} 關鍵字優化完成</b>\n\n"
            if new_primary:
                msg += f"<b>新關鍵字：</b>\n{new_primary}\n\n"
            msg += f"<pre>{result[:800]}</pre>"
        else:
            msg = f"⚠️ <b>#{jid} 優化結果</b>\n<pre>{(result or '無回應')[:800]}</pre>"

        await send_reply(update, context, msg)
    except Exception as e:
        await send_reply(update, context, f"❌ 呼叫 YuQi 失敗: {e}")

    _running_task = None


async def _generate_profile_from_callback(jid: int, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """從按鈕觸發查看搜尋策略"""
    global _running_task
    query = update.callback_query
    chat_id = query.message.chat.id
    thread_id = query.message.message_thread_id

    if _running_task:
        await context.bot.send_message(chat_id=chat_id, message_thread_id=thread_id,
                                       text=f"⚠️ 目前有任務在跑：{_running_task}", parse_mode="HTML")
        return

    _running_task = f"查看搜尋策略 #{jid}"

    cmd = ["python3", "scripts/generate_job_profile.py", "--job-id", str(jid)]
    out, err, code = await run_script_async(cmd, timeout=120)

    output = out + err
    if "Saved" in output:
        # 讀取生成的 profile 顯示關鍵內容
        import glob
        lines = [f"✅ <b>#{jid} 搜尋策略生成完成</b>\n"]
        files = glob.glob(f"{WORK_DIR}/config/job_profiles/auto_generated/*.yaml")
        # 取最新的
        files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        if files:
            try:
                with open(files[0]) as f:
                    content = f.read()
                # 提取重點段落
                in_section = False
                for line in content.split("\n"):
                    stripped = line.strip()
                    if any(kw in stripped for kw in ["primary_keywords:", "title_variants:", "must_have:"]):
                        in_section = True
                        lines.append(f"\n<b>{stripped}</b>")
                        continue
                    if in_section and stripped.startswith("- "):
                        lines.append(f"  {stripped}")
                    elif in_section and not stripped.startswith("- ") and stripped and not stripped.startswith("#"):
                        in_section = False
            except:
                pass

        await context.bot.send_message(chat_id=chat_id, message_thread_id=thread_id,
                                       text="\n".join(lines)[:3000], parse_mode="HTML")
    else:
        await context.bot.send_message(chat_id=chat_id, message_thread_id=thread_id,
                                       text=f"⚠️ 生成失敗\n<pre>{output[-500:]}</pre>", parse_mode="HTML")

    _running_task = None


async def _start_loop_from_callback(job_ids, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """從按鈕觸發閉環"""
    global _running_task
    query = update.callback_query
    chat_id = query.message.chat.id
    thread_id = query.message.message_thread_id

    if _running_task:
        await context.bot.send_message(chat_id=chat_id, message_thread_id=thread_id,
                                       text=f"⚠️ 目前有任務在跑：{_running_task}", parse_mode="HTML")
        return

    if job_ids == "all":
        display = "全部招募中職缺"
        prompt_part = "所有招募中的職缺"
    else:
        import requests as _req
        job_names = []
        for jid in job_ids:
            try:
                r = _req.get(f"{API_BASE}/api/crawler/pipeline/jobs/{jid}", headers=api_headers(), timeout=10)
                d = r.json().get("data", r.json())
                name = d.get("position_name", "")
                client = d.get("client_company", "")
                job_names.append(f"#{jid} {name}（{client}）" if client else f"#{jid} {name}")
            except:
                job_names.append(f"#{jid}")
        display = "\n".join(job_names) if len(job_names) > 1 else (job_names[0] if job_names else " ".join(f"#{j}" for j in job_ids))
        prompt_part = "、".join(f"#{j}" for j in job_ids)

    _running_task = display
    await context.bot.send_message(chat_id=chat_id, message_thread_id=thread_id,
                                   text=f"🚀 <b>開始閉環</b>\n{display}\n\n⏳ 直接執行 Python 腳本，完成後通知",
                                   parse_mode="HTML")

    async def _send(text):
        await context.bot.send_message(chat_id=chat_id, message_thread_id=thread_id, text=text, parse_mode="HTML")

    def _extract(output, keywords, limit=10):
        lines = []
        for line in output.split("\n"):
            if any(kw in line for kw in keywords):
                lines.append(line.strip())
        return "\n".join(lines[-limit:]) or "（無結果）"

    # Phase A+B
    cmd_ab = ["python3", "scripts/daily_closed_loop.py"]
    if job_ids != "all":
        for jid in job_ids:
            cmd_ab.extend(["--job-id", str(jid)])

    await _send(f"🔍 <b>Phase A+B</b> 搜尋→篩選→匯入→PDF 下載中...\n（這步最久，請耐心等候）")
    out_ab, err_ab, code_ab = await run_script_async(cmd_ab, timeout=3600)
    ab_output = out_ab + err_ab
    # 優先顯示 JOB_RESULT 行
    job_results = [line.strip() for line in ab_output.split("\n") if "JOB_RESULT" in line]
    if job_results:
        clean_results = []
        for jr in job_results:
            idx = jr.find("[JOB_RESULT]")
            if idx >= 0:
                clean_results.append(jr[idx + 13:].strip())
            else:
                clean_results.append(jr.strip())
        ab_summary = "\n".join(clean_results[-15:])
    else:
        ab_summary = _extract(ab_output, ["passed", "Import", "A-layer", "Dedup", "PDF", "uploaded", "completed", "Error", "error", "failed", "No candidates", "Done"])
    summary_part = _extract(ab_output, ["SUMMARY", "Jobs processed", "Total searched", "A-layer passed", "Imported", "Hit rate", "Failed", "Errors"])
    if "SUMMARY" in summary_part:
        ab_summary += "\n\n" + summary_part
    await _send(f"{'✅' if code_ab == 0 else '⚠️'} <b>Phase A+B</b>\n<pre>{ab_summary[:2000]}</pre>")

    # Phase C
    cmd_c = ["python3", "scripts/phase_c_claude_analysis.py"]
    if job_ids != "all":
        for jid in job_ids:
            cmd_c.extend(["--job-id", str(jid)])
    else:
        cmd_c.append("--batch")

    await _send(f"🧠 <b>Phase C</b> AI 分析中...")
    out_c, err_c, code_c = await run_script_async(cmd_c, timeout=1800)
    c_summary = _extract(out_c + err_c, ["score=", "verified", "Processed", "Success", "Error", "failed"])
    await _send(f"{'✅' if code_c == 0 else '⚠️'} <b>Phase C</b>\n<pre>{c_summary[:800]}</pre>")

    # 通知
    await _send(f"📢 發送通知中...")
    await run_script_async(["python3", "scripts/notify_consultant.py", "--batch"], timeout=60)

    # 最終報告
    report_lines = [f"✅ <b>一條龍閉環完成</b>\n{display}\n"]
    report_lines.append(f"A+B: {'✅' if code_ab == 0 else '❌'} | C: {'✅' if code_c == 0 else '❌'} | 通知: ✅\n")

    target_ids = job_ids if job_ids != "all" else []
    if target_ids:
        for jid in target_ids:
            try:
                data = await api_get_async(f"/api/crawler/pipeline/candidates?target_job_id={jid}&limit=50")
                candidates = data.get("data", [])
                today = datetime.now().strftime("%Y-%m-%d")
                today_candidates = [c for c in candidates if today in (c.get("createdAt") or c.get("created_at") or "")]

                if today_candidates:
                    report_lines.append(f"<b>📋 #{jid} 今日匯入 {len(today_candidates)} 人</b>")
                    for c in today_candidates[:15]:
                        cid = c.get("id", "?")
                        name = c.get("name", "?")
                        title = (c.get("current_title") or c.get("current_position") or "")[:30]
                        ai = c.get("aiAnalysis") or c.get("ai_analysis")
                        score = ""
                        if ai:
                            try:
                                if isinstance(ai, str):
                                    ai = json.loads(ai)
                                jm = ai.get("job_matchings", [{}])
                                if jm:
                                    s = jm[0].get("match_score", "")
                                    v = jm[0].get("verdict", "")
                                    score = f" | {s}分 {v}"
                            except:
                                pass
                        has_pdf = "📄" if (c.get("resumeFiles") or c.get("resume_files")) else "  "
                        report_lines.append(f"  {has_pdf} <b>#{cid}</b> {name}\n     {title}{score}")
                else:
                    report_lines.append(f"<b>#{jid}</b> 今日無新匯入人選")
            except:
                pass

    await _send("\n".join(report_lines))
    _running_task = None


# ── 智慧關鍵字生成 ────────────────────────────────────────

async def cmd_generate_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """優化關鍵字：用 Claude 生成職缺搜尋 profile"""
    global _running_task
    if not is_authorized(update):
        return

    text = (update.message.text or "")
    job_ids = parse_job_ids(text)
    if not job_ids:
        await send_reply(update, context, "⚠️ 請指定職缺，例如：<code>優化關鍵字 #234</code>")
        return

    if _running_task:
        await send_reply(update, context, f"⚠️ 目前有任務在跑：{_running_task}")
        return

    display = " ".join(f"#{j}" for j in job_ids)
    _running_task = f"查看搜尋策略 {display}"
    await send_reply(update, context, f"🧠 <b>生成搜尋策略</b> {display}\n用 Claude 分析職缺 → 產出多語言關鍵字...")

    cmd = ["python3", "scripts/generate_job_profile.py"]
    for jid in job_ids:
        cmd.extend(["--job-id", str(jid)])

    out, err, code = await run_script_async(cmd, timeout=300)

    output = out + err
    if "Saved" in output:
        # 顯示生成的 profile 內容
        lines = [f"✅ <b>搜尋策略生成完成</b> {display}\n"]
        for jid in job_ids:
            import glob
            files = glob.glob(f"{WORK_DIR}/config/job_profiles/auto_generated/*{jid}*") or \
                    glob.glob(f"{WORK_DIR}/config/job_profiles/auto_generated/*.yaml")
            for fp in files[-1:]:
                try:
                    with open(fp) as f:
                        content = f.read()
                    # 提取關鍵部分
                    if "primary_keywords" in content:
                        lines.append(f"<b>#{jid} 搜尋關鍵字</b>")
                        for line in content.split("\n"):
                            if "- " in line and any(kw in content[:content.index(line)+1] for kw in ["primary_keywords", "title_variants", "skill_variants"]):
                                lines.append(f"  {line.strip()}")
                except:
                    pass
        await send_reply(update, context, "\n".join(lines)[:3000])
    else:
        await send_reply(update, context, f"⚠️ 生成失敗\n<pre>{output[-500:]}</pre>")

    _running_task = None


# ── 錯誤偵測 + 自動修復 ──────────────────────────────────

def _error_hash(text: str) -> str:
    """產生錯誤摘要 hash（用於冷卻判斷）"""
    import hashlib
    # 取前 100 字做 hash，忽略時間戳等變動部分
    clean = re.sub(r'\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}', '', text)[:100]
    return hashlib.md5(clean.encode()).hexdigest()[:8]


def _is_error_message(text: str) -> bool:
    """判斷訊息是否包含錯誤關鍵字"""
    return any(kw.lower() in text.lower() for kw in ERROR_KEYWORDS)


def _is_in_cooldown(error_text: str) -> bool:
    """檢查同類錯誤是否在冷卻期"""
    h = _error_hash(error_text)
    last = _error_cooldown.get(h, 0)
    return (time.time() - last) < ERROR_COOLDOWN_SECS


def _set_cooldown(error_text: str):
    """設定錯誤冷卻"""
    h = _error_hash(error_text)
    _error_cooldown[h] = time.time()


async def handle_lobster_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """偵測龍蝦 bot 的錯誤訊息，自動呼叫 Claude 修復"""
    global _running_task
    text = update.message.text or ""

    if _is_in_cooldown(text):
        log.info(f"  -> Error in cooldown, skip")
        return

    if _running_task:
        log.info(f"  -> Claude busy with {_running_task}, skip auto-fix")
        return

    _set_cooldown(text)
    error_preview = text[:200]
    bot_name = update.effective_user.first_name if update.effective_user else "龍蝦"

    log.info(f"🚨 Lobster error detected from {bot_name}: {error_preview}")

    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id

    async def _reply(msg):
        await context.bot.send_message(chat_id=chat_id, message_thread_id=thread_id, text=msg, parse_mode="HTML")

    # ── 判斷錯誤類型 → 執行對應修復 ──
    text_lower = text.lower()
    fix_actions = []
    fix_results = []

    # 爬蟲離線
    if any(kw in text_lower for kw in ["爬蟲", "crawler", "localhost:5001", "connection refused"]):
        fix_actions.append("restart_crawler")

    # Chrome CDP 離線
    if any(kw in text_lower for kw in ["cdp", "chrome", "9222", "playwright", "browser"]):
        fix_actions.append("check_cdp")

    # HR API 問題
    if any(kw in text_lower for kw in ["api-hr", "502", "503", "timeout", "api 無回應", "api unreachable"]):
        fix_actions.append("check_hr_api")

    # PDF 下載失敗
    if any(kw in text_lower for kw in ["pdf", "download", "下載失敗", "save to pdf"]):
        fix_actions.append("retry_pdf")

    # 閉環失敗
    if any(kw in text_lower for kw in ["閉環", "closed loop", "import failed", "匯入失敗"]):
        fix_actions.append("check_pipeline")

    # TG Bot / 通知問題
    if any(kw in text_lower for kw in ["telegram", "tg", "通知失敗", "ssl_certificate"]):
        fix_actions.append("check_tg")

    if not fix_actions:
        fix_actions.append("general_check")

    await _reply(f"🔧 <b>偵測到錯誤</b>\n<pre>{error_preview}</pre>\n\n⏳ 自動修復中：{', '.join(fix_actions)}")
    _running_task = f"自動修復：{', '.join(fix_actions)}"

    for action in fix_actions:
        if action == "restart_crawler":
            out, err, code = await run_script_async(["bash", "-c",
                "cd /Users/user/clawd/headhunter-crawler && "
                "if curl -s -m 3 http://localhost:5001/api/tasks > /dev/null 2>&1; then "
                "  echo 'crawler OK'; "
                "else "
                "  echo 'restarting...'; nohup python3 app.py > /dev/null 2>&1 & sleep 3; "
                "  if curl -s -m 3 http://localhost:5001/api/tasks > /dev/null 2>&1; then echo 'restart OK'; else echo 'restart FAILED'; fi; "
                "fi"
            ], timeout=15)
            fix_results.append(f"🔄 爬蟲: {(out+err).strip()}")

        elif action == "check_cdp":
            out, err, code = await run_script_async(["bash", "-c",
                "if curl -s -m 3 http://localhost:9222/json/version > /dev/null 2>&1; then "
                "  echo '✅ CDP 正常'; "
                "else "
                "  echo '❌ CDP 離線，需手動重啟 Chrome'; "
                "fi"
            ], timeout=10)
            fix_results.append(f"🖥️ CDP: {(out+err).strip()}")

        elif action == "check_hr_api":
            out, err, code = await run_script_async(["bash", "-c",
                "for i in 1 2 3; do "
                "  code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 https://api-hr.step1ne.com/api/health); "
                "  if [ \"$code\" = '200' ]; then echo \"✅ HR API 恢復 (attempt $i)\"; exit 0; fi; "
                "  sleep 3; "
                "done; echo '❌ HR API 仍然離線'"
            ], timeout=30)
            fix_results.append(f"🌐 HR API: {(out+err).strip()}")

        elif action == "retry_pdf":
            out, err, code = await run_script_async([
                "python3", "scripts/phase_b_pdf_download.py"
            ], timeout=600)
            pdf_count = (out + err).count("PDF downloaded") + (out + err).count("uploaded")
            fix_results.append(f"📄 PDF 重跑: {pdf_count} 份處理")

        elif action == "check_pipeline":
            out, err, code = await run_script_async(["bash", "-c",
                "ps aux | grep daily_closed_loop | grep -v grep | wc -l | xargs echo '閉環進程:' && "
                "tail -5 /Users/user/clawd/headhunter-crawler/logs/closed_loop_$(date +%Y-%m-%d).log 2>/dev/null || echo '無今日 log'"
            ], timeout=10)
            fix_results.append(f"🔄 閉環: {(out+err).strip()}")

        elif action == "check_tg":
            import requests as _req
            try:
                r = _req.get("https://api.telegram.org/bot8795142390:AAFJw6zm_eqnFwLOTiOSxGzWcdv5NSTAvQs/getMe", timeout=5)
                fix_results.append(f"📱 TG Bot: {'✅ 正常' if r.status_code == 200 else '❌ 異常'}")
            except:
                fix_results.append(f"📱 TG Bot: ❌ 無法連線")

        elif action == "general_check":
            # 全面快速檢查
            checks = []
            for name, url in [("HR API", "https://api-hr.step1ne.com/api/health"),
                              ("爬蟲", "http://localhost:5001/api/tasks"),
                              ("CDP", "http://localhost:9222/json/version")]:
                try:
                    import requests as _req
                    r = _req.get(url, timeout=5)
                    checks.append(f"{'✅' if r.status_code == 200 else '❌'} {name}")
                except:
                    checks.append(f"❌ {name}")
            fix_results.append("快速檢查: " + " | ".join(checks))

    result_text = "\n".join(fix_results)
    await _reply(f"🔧 <b>自動修復結果</b>\n\n{result_text}")
    _running_task = None


async def cmd_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """手動觸發修復：修復 [描述]"""
    global _running_task
    if not is_authorized(update):
        return

    text = (update.message.text or "")
    for prefix in ["修復", "修复", "fix", "/fix"]:
        text = text.replace(prefix, "")
    text = text.strip()

    if not text:
        if update.message.reply_to_message and update.message.reply_to_message.text:
            text = update.message.reply_to_message.text
        else:
            await send_reply(update, context, "⚠️ 請描述問題，例如：<code>修復 爬蟲一直 timeout</code>\n或回覆錯誤訊息並打 <code>修復</code>")
            return

    if _running_task:
        await send_reply(update, context, f"⚠️ 目前有任務在跑：{_running_task}")
        return

    _running_task = f"手動修復：{text[:30]}..."
    await send_reply(update, context, f"🔧 <b>開始修復</b>\n<pre>{text[:300]}</pre>\n\n⏳ Claude 診斷中...")

    prompt = f"""你是 Step1ne 獵頭系統的 AI 工程師。
讀取 /Users/user/.claude/MEMORY.md 了解系統配置。

用戶回報以下問題需要修復：

---
{text[:1500]}
---

請：
1. 診斷問題原因（查 log、測 API、檢查服務狀態）
2. 嘗試修復
3. 驗證修復結果
4. 回報摘要
"""

    result = await run_claude(prompt, timeout=600)

    if result.strip():
        summary = result[-1500:] if len(result) > 1500 else result
        await send_reply(update, context, f"🔧 <b>修復結果</b>\n\n<pre>{summary[:1500]}</pre>")
    else:
        await send_reply(update, context, f"🔧 <b>修復完成</b>\n\n⚠️ 沒有回傳摘要，請到系統確認")

    _running_task = None


# ── 訊息路由 ──────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else "?"
    user = update.effective_user
    user_id = user.id if user else "?"
    user_name = user.first_name if user else "?"
    is_bot = user.is_bot if user else False
    msg_text = update.message.text if update.message else "(no text)"
    log.info(f"MSG: chat={chat_id} user={user_id}({user_name}) bot={is_bot} text='{msg_text[:80]}'")

    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    # ── 龍蝦 Bot 錯誤偵測（自動）──
    if is_bot and user_id and int(user_id) in LOBSTER_BOT_IDS:
        if _is_error_message(text):
            await handle_lobster_error(update, context)
        return  # 龍蝦的非錯誤訊息一律忽略

    # ── 忽略自己的訊息 ──
    if is_bot:
        return

    # ── 人類指令 ──
    if not is_authorized(update):
        return

    # 檢查是否在等修改職缺關鍵字
    if update.effective_user and update.effective_user.id in _pending_edit_keywords:
        job_id = _pending_edit_keywords.pop(update.effective_user.id)
        if text == '取消':
            await send_reply(update, context, "✅ 已取消")
            return
        # 用新關鍵字啟動搜尋
        await _run_job_with_custom_keywords(job_id, text, update, context)
        return

    # 檢查是否在等瀏覽人才庫輸入
    if update.effective_user and update.effective_user.id in _pending_browse_db:
        del _pending_browse_db[update.effective_user.id]
        if text == '取消':
            await send_reply(update, context, "✅ 已取消")
            return
        await cmd_browse_db(update, context)
        return

    # 檢查是否在等公司定向搜尋輸入
    if update.effective_user and update.effective_user.id in _pending_company_search:
        del _pending_company_search[update.effective_user.id]
        if text == '取消':
            await send_reply(update, context, "✅ 已取消")
            return
        await cmd_company_search_text(update, context)
        return

    # 檢查是否在等自由搜尋輸入
    if update.effective_user and update.effective_user.id in _pending_free_search:
        del _pending_free_search[update.effective_user.id]
        if text == '取消':
            await send_reply(update, context, "✅ 已取消")
            return
        await cmd_free_search_text(update, context)
        return

    # 檢查是否在等自訂角度輸入
    if update.effective_user and update.effective_user.id in _pending_custom_angle:
        handled = await _handle_custom_angle_input(update, context)
        if handled:
            return

    if text.startswith("找") or text.startswith("搜尋人才") or text.startswith("搜人"):
        await cmd_free_search_text(update, context)
        return
    elif text.startswith("跑閉環") or text.startswith("跑闭环"):
        await cmd_run_loop(update, context)
    elif text in ("系統狀態", "系统状态", "status", "狀態"):
        await cmd_status(update, context)
    elif text.startswith("查人") or text.startswith("search"):
        await cmd_search(update, context)
    elif text.startswith("查職缺") or text.startswith("查职缺"):
        await cmd_job(update, context)
    elif text in ("職缺列表", "职缺列表", "jobs"):
        await cmd_list_jobs(update, context)
    elif text.startswith("修復") or text.startswith("修复") or text.startswith("fix"):
        await cmd_fix(update, context)
    elif text.startswith("優化關鍵字") or text.startswith("优化关键字") or text.startswith("generate profile"):
        await cmd_generate_profile(update, context)
    elif text in ("幫助", "帮助", "help"):
        await cmd_help(update, context)


# ── 主程式 ────────────────────────────────────────────────

def main():
    log.info("Starting TG Command Bot v2...")
    log.info(f"  Bot token: {BOT_TOKEN[:10]}...")
    log.info(f"  Allowed chat: {ALLOWED_CHAT_ID}")
    log.info(f"  Claude CLI: {CLAUDE_CLI}")

    app = Application.builder().token(BOT_TOKEN).build()

    # Slash 指令
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("loop", cmd_loop))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("job", cmd_job))
    app.add_handler(CommandHandler("jobs", cmd_list_jobs))
    app.add_handler(CommandHandler("fix", cmd_fix))

    # 按鈕回呼
    app.add_handler(CallbackQueryHandler(handle_callback))

    # 中文文字指令
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot v2 started, polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
