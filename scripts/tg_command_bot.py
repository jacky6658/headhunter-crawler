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


# ── 工具函式 ──────────────────────────────────────────────

def is_authorized(update: Update) -> bool:
    chat_id = update.effective_chat.id if update.effective_chat else 0
    user_id = str(update.effective_user.id) if update.effective_user else ""
    return chat_id == ALLOWED_CHAT_ID or user_id in ALLOWED_USERS


def parse_job_ids(text: str) -> list:
    # 支援 #233 和純數字 233
    ids = [int(x) for x in re.findall(r"#(\d+)", text)]
    if not ids:
        ids = [int(x) for x in re.findall(r"\b(\d{2,4})\b", text) if 40 <= int(x) <= 9999]
    return ids


def api_headers():
    return {"Authorization": f"Bearer {API_KEY}"}


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


async def run_claude(prompt: str, timeout: int = 1800) -> str:
    log.info(f"Claude CLI: {prompt[:80]}...")
    try:
        # 清除 CLAUDECODE 環境變數，避免 nested session 錯誤
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_SESSION", None)

        proc = await asyncio.create_subprocess_exec(
            CLAUDE_CLI, "-p", prompt, "--no-input", "--model", "sonnet",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORK_DIR,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[:500]
            output += f"\n\n⚠️ Exit code: {proc.returncode}\n{err}"
        log.info(f"Claude CLI done: {len(output)} chars, exit={proc.returncode}")
        return output
    except asyncio.TimeoutError:
        return f"❌ Claude 執行超時（{timeout}s）"
    except Exception as e:
        return f"❌ Claude 執行失敗: {e}"


def fetch_active_jobs() -> list:
    """從 HR API 取得所有招募中職缺"""
    import requests
    try:
        r = requests.get(f"{API_BASE}/api/jobs?status=招募中&limit=50",
                         headers=api_headers(), timeout=30)
        data = r.json()
        jobs = data if isinstance(data, list) else data.get("jobs", data.get("data", []))
        active = [j for j in jobs if j.get("job_status") == "招募中"]
        active.sort(key=lambda j: j.get("id", 0), reverse=True)
        return active
    except Exception as e:
        log.error(f"fetch_active_jobs failed: {e}")
        return []


# ── 按鈕面板 ──────────────────────────────────────────────

def build_help_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 選擇職缺跑閉環", callback_data="show_jobs")],
        [InlineKeyboardButton("🔍 系統狀態", callback_data="status"),
         InlineKeyboardButton("📋 職缺列表", callback_data="list_jobs")],
        [InlineKeyboardButton("🔄 跑全部閉環", callback_data="loop_all")],
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

<b>Slash 指令</b>
/loop 233 — 跑閉環
/status — 系統狀態
/search 姓名 — 查候選人
/job 233 — 查職缺
/jobs — 所有招募中職缺

<b>中文指令</b>
跑閉環 #233 / 系統狀態 / 查人 姓名 / 查職缺 #233

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

    task_status = f"🔄 {_running_task}" if _running_task else "💤 閒置"
    now = datetime.now().strftime("%H:%M")

    text = f"""<b>🔍 系統狀態</b>

{results['hr']} HR API
{results['crawler']} 爬蟲 (localhost:5001)
{results['cdp']} Chrome CDP

🤖 Bot: {task_status}
⏰ {now}"""
    await send_reply(update, context, text)


async def cmd_list_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """列出所有招募中職缺"""
    if not is_authorized(update):
        return
    jobs = fetch_active_jobs()
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
        jobs = fetch_active_jobs()
        if jobs:
            await send_reply(update, context, "🚀 <b>選擇要跑閉環的職缺：</b>",
                             reply_markup=build_job_keyboard(jobs))
        else:
            await send_reply(update, context, "⚠️ 請指定職缺 ID，例如：<code>/loop 233</code>")
        return
    await _start_loop(ids, update, context)


async def cmd_run_loop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """中文指令：跑閉環"""
    global _running_task
    if not is_authorized(update):
        return
    text = update.message.text if update.message else ""
    if "全部" in text:
        await _start_loop("all", update, context)
    else:
        ids = parse_job_ids(text)
        if not ids:
            jobs = fetch_active_jobs()
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
        display = " ".join(f"#{j}" for j in job_ids)
        prompt_part = "、".join(f"#{j}" for j in job_ids)

    _running_task = display
    await send_reply(update, context, f"🚀 <b>開始閉環：{display}</b>\n預計需要一段時間，完成後通知")

    prompt = f"""你是 Step1ne 獵頭系統的 AI 工程師。
讀取 /Users/user/.claude/MEMORY.md 了解系統配置。
對職缺 {prompt_part} 執行完整閉環流程（A→B→C→通知）。

注意事項：
- Phase B 後要驗證 work_history/education_details 是否補齊，沒有的話從 PDF 提取補上（PATCH）
- 用本地爬蟲 localhost:5001
- Phase B 每人間隔 60-90 秒，每 5 人休息 2 分鐘
- 完成後跑 notify_consultant.py 通知 score >= 60 的人
- 最後把結果摘要發到 TG（bot token: 8375770979:AAFuC3emSd05sjRxSyxpP6kTmd7LyKpA2cg, chat_id: -1003231629634, thread_id: 1247）
"""
    result = await run_claude(prompt, timeout=3600)
    summary = result[-2000:] if len(result) > 2000 else result
    await send_reply(update, context, f"✅ <b>閉環完成：{display}</b>\n\n<pre>{summary[:1500]}</pre>")

    _running_task = None
    if not _task_queue.empty():
        task_type, args, upd, ctx = await _task_queue.get()
        if task_type == "loop":
            await _execute_loop(args, upd, ctx)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/search 或 查人"""
    if not is_authorized(update):
        return
    text = (update.message.text or "")
    # 移除指令前綴
    for prefix in ["/search", "查人", "search"]:
        text = text.replace(prefix, "")
    text = text.strip()
    if not text:
        await send_reply(update, context, "⚠️ 請指定姓名，例如：<code>/search Shinji</code>")
        return

    await send_reply(update, context, f"🔍 搜尋: {text}...")
    import requests
    try:
        r = requests.get(f"{API_BASE}/api/candidates?search={text}&limit=5",
                         headers=api_headers(), timeout=15)
        items = r.json().get("data", [])
        if not items:
            await send_reply(update, context, f"❌ 找不到: {text}")
            return
        lines = [f"<b>🔍 搜尋結果: {text}</b>\n"]
        for c in items[:5]:
            cid = c.get("id", "?")
            name = c.get("name", "?")
            title = (c.get("current_title") or c.get("current_position") or "")[:40]
            status = c.get("status", "")
            score = ""
            ai = c.get("aiAnalysis")
            if ai:
                try:
                    if isinstance(ai, str):
                        ai = json.loads(ai)
                    jm = ai.get("job_matchings", [{}])
                    if jm:
                        score = f" | score={jm[0].get('match_score', '?')}"
                except:
                    pass
            lines.append(f"• <b>#{cid}</b> {name}\n  {title}\n  status={status}{score}")
        await send_reply(update, context, "\n".join(lines))
    except Exception as e:
        await send_reply(update, context, f"❌ 搜尋失敗: {e}")


async def cmd_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/job 或 查職缺"""
    if not is_authorized(update):
        return
    text = (update.message.text or "")
    job_ids = parse_job_ids(text)
    if not job_ids:
        await send_reply(update, context, "⚠️ 請指定職缺，例如：<code>/job 233</code>")
        return
    import requests
    for jid in job_ids[:3]:
        try:
            r = requests.get(f"{API_BASE}/api/jobs/{jid}", headers=api_headers(), timeout=15)
            d = r.json().get("data", r.json())
            text = f"""<b>📋 #{jid} {d.get('position_name','?')}</b>
客戶: {d.get('client_company','?')}
狀態: {d.get('job_status','?')}
地點: {d.get('location','?')}
薪資: {d.get('salary_range','')}
技能: {(d.get('key_skills') or '')[:80]}"""
            await send_reply(update, context, text)
        except Exception as e:
            await send_reply(update, context, f"❌ #{jid} 查詢失敗: {e}")


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

    elif data == "show_jobs" or data == "list_jobs":
        jobs = fetch_active_jobs()
        if jobs:
            text = f"🚀 <b>選擇職缺跑閉環（{len(jobs)} 個）</b>"
            await query.edit_message_text(text, parse_mode="HTML",
                                          reply_markup=build_job_keyboard(jobs))
        else:
            await query.edit_message_text("❌ 無招募中職缺", parse_mode="HTML")

    elif data == "loop_all":
        await query.edit_message_text("🚀 <b>即將跑全部招募中職缺的閉環...</b>", parse_mode="HTML")
        # 建立一個假的 update 來觸發閉環
        await _start_loop_from_callback("all", update, context)

    elif data.startswith("loop_"):
        jid = int(data.replace("loop_", ""))
        await query.edit_message_text(f"🚀 <b>即將跑 #{jid} 閉環...</b>", parse_mode="HTML")
        await _start_loop_from_callback([jid], update, context)


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
        display = " ".join(f"#{j}" for j in job_ids)
        prompt_part = "、".join(f"#{j}" for j in job_ids)

    _running_task = display
    await context.bot.send_message(chat_id=chat_id, message_thread_id=thread_id,
                                   text=f"🚀 <b>開始閉環：{display}</b>\n預計需要一段時間，完成後通知",
                                   parse_mode="HTML")

    prompt = f"""你是 Step1ne 獵頭系統的 AI 工程師。
讀取 /Users/user/.claude/MEMORY.md 了解系統配置。
對職缺 {prompt_part} 執行完整閉環流程（A→B→C→通知）。

注意事項：
- Phase B 後要驗證 work_history/education_details 是否補齊，沒有的話從 PDF 提取補上（PATCH）
- 用本地爬蟲 localhost:5001
- Phase B 每人間隔 60-90 秒，每 5 人休息 2 分鐘
- 完成後跑 notify_consultant.py 通知 score >= 60 的人
- 最後把結果摘要發到 TG（bot token: 8375770979:AAFuC3emSd05sjRxSyxpP6kTmd7LyKpA2cg, chat_id: -1003231629634, thread_id: 1247）
"""
    result = await run_claude(prompt, timeout=3600)
    summary = result[-2000:] if len(result) > 2000 else result
    await context.bot.send_message(chat_id=chat_id, message_thread_id=thread_id,
                                   text=f"✅ <b>閉環完成：{display}</b>\n\n<pre>{summary[:1500]}</pre>",
                                   parse_mode="HTML")
    _running_task = None


# ── 訊息路由 ──────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else "?"
    user = update.effective_user
    user_id = user.id if user else "?"
    user_name = user.first_name if user else "?"
    msg_text = update.message.text if update.message else "(no text)"
    log.info(f"MSG: chat={chat_id} user={user_id}({user_name}) text='{msg_text[:80]}'")

    if not is_authorized(update):
        return
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    if text.startswith("跑閉環") or text.startswith("跑闭环"):
        await cmd_run_loop(update, context)
    elif text in ("系統狀態", "系统状态", "status", "狀態"):
        await cmd_status(update, context)
    elif text.startswith("查人") or text.startswith("search"):
        await cmd_search(update, context)
    elif text.startswith("查職缺") or text.startswith("查职缺"):
        await cmd_job(update, context)
    elif text in ("職缺列表", "职缺列表", "jobs"):
        await cmd_list_jobs(update, context)
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

    # 按鈕回呼
    app.add_handler(CallbackQueryHandler(handle_callback))

    # 中文文字指令
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot v2 started, polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
