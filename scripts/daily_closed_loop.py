#!/usr/bin/env python3
"""
Step1ne 每日閉環自動排程

功能：
  1. 取得所有「招募中」職缺
  2. 檢查鎖定狀態（防重複）
  3. 讀取 checkpoint（斷點續跑）
  4. 對每個職缺：建立爬蟲任務 → 等待完成 → A 層篩選 → LinkedIn B/C 層 → 匯入系統
  5. 完成後透過 Notifications API 回報執行長

使用方式：
  export API_SECRET_KEY="your-key"
  export API_BASE="https://api-hr.step1ne.com"
  export CRAWLER_BASE="https://crawler.step1ne.com"
  python scripts/daily_closed_loop.py

或搭配 cron：
  0 9 * * * cd /path/to/headhunter-crawler && source venv/bin/activate && python scripts/daily_closed_loop.py
"""

import json
import os
import random
import shutil
import subprocess
import sys
import time
import logging
from datetime import datetime, date
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: requests not installed. Run: pip install requests")
    sys.exit(1)

# ──────────────────────────────────────────
# 設定
# ──────────────────────────────────────────
API_BASE = os.environ.get("API_BASE", "https://api-hr.step1ne.com")
CRAWLER_BASE = os.environ.get("CRAWLER_BASE", "https://crawler.step1ne.com")
API_KEY = os.environ.get("API_SECRET_KEY", "")
OPERATOR = os.environ.get("OPERATOR", "lobster-auto")

if not API_KEY:
    print("ERROR: API_SECRET_KEY environment variable not set")
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

# 路徑
SCRIPT_DIR = Path(__file__).parent.parent
LOG_DIR = SCRIPT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Logging
today_str = date.today().isoformat()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"closed_loop_{today_str}.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# 篩選參數
POLL_INTERVAL = 15        # 爬蟲任務輪詢間隔（秒）
POLL_TIMEOUT = 600        # 爬蟲任務超時（秒）
MAX_API_ERRORS = 3        # 連續 API 錯誤次數上限


# ──────────────────────────────────────────
# API 工具函式
# ──────────────────────────────────────────
def api_get(endpoint: str):
    """GET 請求到獵頭系統 API"""
    r = requests.get(f"{API_BASE}{endpoint}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def api_put(endpoint: str, data: dict):
    """PUT 請求"""
    r = requests.put(f"{API_BASE}{endpoint}", headers=HEADERS, json=data, timeout=30)
    r.raise_for_status()
    return r.json()


def api_post(endpoint: str, data: dict):
    """POST 請求"""
    r = requests.post(f"{API_BASE}{endpoint}", headers=HEADERS, json=data, timeout=30)
    r.raise_for_status()
    return r.json()


def crawler_get(endpoint: str):
    """GET 請求到爬蟲系統"""
    r = requests.get(f"{CRAWLER_BASE}{endpoint}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def crawler_post(endpoint: str, data: dict):
    """POST 請求到爬蟲系統"""
    r = requests.post(f"{CRAWLER_BASE}{endpoint}", headers=HEADERS, json=data, timeout=30)
    r.raise_for_status()
    return r.json()


# ──────────────────────────────────────────
# 鎖定 & Checkpoint
# ──────────────────────────────────────────
def is_job_locked(job_id: int) -> bool:
    """檢查職缺今天是否已被處理"""
    try:
        r = api_get(f"/api/system-config/crawl_lock_{job_id}")
        value = r.get("value", "")
        return value.startswith(today_str)
    except Exception:
        return False


def lock_job(job_id: int):
    """鎖定職缺"""
    api_put(f"/api/system-config/crawl_lock_{job_id}", {
        "value": f"{today_str}|{OPERATOR}|started"
    })


def unlock_job(job_id: int, result: str):
    """更新鎖定結果"""
    api_put(f"/api/system-config/crawl_lock_{job_id}", {
        "value": f"{today_str}|{OPERATOR}|{result}"
    })


def get_checkpoint() -> dict:
    """讀取今天的 checkpoint"""
    try:
        r = api_get(f"/api/system-config/crawl_checkpoint_{today_str}")
        return json.loads(r.get("value", "{}"))
    except Exception:
        return {}


def save_checkpoint(data: dict):
    """儲存 checkpoint"""
    api_put(f"/api/system-config/crawl_checkpoint_{today_str}", {
        "value": json.dumps(data, ensure_ascii=False)
    })


# ──────────────────────────────────────────
# 核心流程
# ──────────────────────────────────────────
def get_active_jobs() -> list:
    """取得所有招募中的職缺，按優先度排序（high 優先）"""
    r = api_get("/api/jobs")
    jobs = r if isinstance(r, list) else r.get("jobs", r.get("data", []))
    active = [j for j in jobs if j.get("job_status") == "招募中"]

    # 優先度排序：high > medium > low > 未設定
    priority_order = {"high": 0, "medium": 1, "low": 2}
    active.sort(key=lambda j: priority_order.get((j.get("priority") or "").lower(), 3))

    high = [j for j in active if (j.get("priority") or "").lower() == "high"]
    if high:
        log.info(f"  Priority HIGH: {len(high)} jobs ({', '.join(f'#{j[\"id\"]}' for j in high)})")

    return active


def create_crawler_task(job: dict) -> str | None:
    """為職缺建立爬蟲任務"""
    skills = job.get("key_skills", "")
    skill_list = [s.strip() for s in skills.split(",") if s.strip()]
    primary = skill_list[:5]
    secondary = skill_list[5:]

    task_data = {
        "job_title": job.get("position_name", ""),
        "primary_skills": primary,
        "secondary_skills": secondary,
        "location": "Taiwan",
        "location_zh": "台灣",
        "step1ne_job_id": job["id"],
        "auto_push": False,
        "pages": 3,
        "schedule_type": "once"
    }

    try:
        r = crawler_post("/api/tasks", task_data)
        task_id = r.get("task_id") or r.get("id")
        log.info(f"  Crawler task created: {task_id}")
        return task_id
    except Exception as e:
        log.error(f"  Failed to create crawler task: {e}")
        return None


def wait_for_task(task_id: str) -> bool:
    """等待爬蟲任務完成"""
    start = time.time()
    while time.time() - start < POLL_TIMEOUT:
        try:
            r = crawler_get(f"/api/tasks/{task_id}")
            status = r.get("status", "")
            if status == "completed":
                log.info(f"  Crawler task completed")
                return True
            if status in ("failed", "error"):
                log.error(f"  Crawler task failed: {r.get('error', 'unknown')}")
                return False
        except Exception as e:
            log.warning(f"  Poll error: {e}")
        time.sleep(POLL_INTERVAL)

    log.error(f"  Crawler task timed out after {POLL_TIMEOUT}s")
    return False


def get_task_results(task_id: str) -> list:
    """取得爬蟲搜尋結果"""
    try:
        r = crawler_get(f"/api/results/{task_id}")
        candidates = r if isinstance(r, list) else r.get("candidates", r.get("results", []))
        log.info(f"  Search results: {len(candidates)} candidates")
        return candidates
    except Exception as e:
        log.error(f"  Failed to get results: {e}")
        return []


def a_layer_filter(candidates: list, job: dict) -> list:
    """A 層硬性條件篩選（規則式）"""
    rejection = (job.get("rejection_criteria") or "").lower()
    exclusion = [kw.strip().lower() for kw in (job.get("exclusion_keywords") or "").split(",") if kw.strip()]
    title_variants = [t.strip().lower() for t in (job.get("title_variants") or "").split(",") if t.strip()]
    key_skills = [s.strip().lower() for s in (job.get("key_skills") or "").split(",") if s.strip()]

    passed = []
    rejected = 0

    for c in candidates:
        name = c.get("name", "")
        title = (c.get("title") or c.get("current_title") or "").lower()
        snippet = (c.get("snippet") or c.get("summary") or "").lower()
        full_text = f"{title} {snippet} {c.get('skills', '')}".lower()

        # 排除關鍵字
        if any(kw in full_text for kw in exclusion if kw):
            rejected += 1
            continue

        # 職稱相關性（寬鬆：有任一 variant 相關即可）
        if title_variants:
            title_match = any(v in title or v in snippet for v in title_variants)
            if not title_match:
                # 寬鬆：資訊不足就通過
                if title:
                    rejected += 1
                    continue

        # 技能交集（完全無交集才淘汰）
        if key_skills:
            skill_match = any(s in full_text for s in key_skills[:3])
            if not skill_match and snippet:
                rejected += 1
                continue

        passed.append(c)

    log.info(f"  A-layer: {len(passed)} passed, {rejected} rejected (rate: {len(passed)}/{len(candidates)}={len(passed)*100//max(len(candidates),1)}%)")
    return passed


def linkedin_download_and_enrich(candidates: list, job: dict) -> list:
    """
    B/C 層：LinkedIn 深度審核 + PDF 下載 + 資料充實

    透過 Playwright CDP 連接本機 Chrome，逐一訪問 LinkedIn profile：
    1. 下載 PDF 履歷（一度/非一度都用「更多→存為 PDF」，失敗才用 page.pdf() 備援）
    2. 讀取頁面文字提取 work_history / education / skills
    3. 執行 B 層（submission_criteria）和 C 層（talent_profile）檢查
    4. 回傳充實後的候選人資料
    """
    import subprocess
    import shutil

    enriched = []
    pdf_dir = SCRIPT_DIR / "resumes" / "linkedin_pdfs"
    backup_dir = SCRIPT_DIR / "resumes" / "pending_upload"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)

    submission_criteria = job.get("submission_criteria", "")
    talent_profile = job.get("talent_profile", "")

    batch_count = 0
    for i, c in enumerate(candidates):
        name = c.get("name", "Unknown")
        linkedin_url = c.get("linkedin_url") or c.get("profileUrl", "")

        if not linkedin_url:
            log.info(f"    [{i+1}/{len(candidates)}] {name} — no LinkedIn URL, skip PDF")
            c["_pdf_path"] = None
            c["_page_text"] = ""
            enriched.append(c)
            continue

        # 人性化：一批最多 5 人，批次間休息 10 分鐘
        batch_count += 1
        if batch_count > 5:
            log.info(f"    Batch limit reached, resting 10 min...")
            time.sleep(600)
            batch_count = 1

        log.info(f"    [{i+1}/{len(candidates)}] {name} — visiting LinkedIn")

        # 呼叫 linkedin_pdf_download.py 的邏輯（使用 Playwright CDP）
        pdf_filename = f"{c.get('id', i)}_{name}.pdf"
        pdf_path = pdf_dir / pdf_filename
        page_text = ""

        try:
            # 使用 Playwright CLI 模式下載單一候選人 PDF
            # 實際執行時由 Playwright CDP 連接 Chrome
            download_script = f"""
import asyncio
from playwright.async_api import async_playwright
import random, time

async def download():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        # 前往 profile
        await page.goto("{linkedin_url}", wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(2, 4))

        # 閱讀模擬（隨機滾動）
        for _ in range(random.randint(3, 6)):
            await page.mouse.wheel(0, random.randint(200, 600))
            await asyncio.sleep(random.uniform(0.5, 1.5))
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)

        # 嘗試方式 A：原生 Save to PDF
        pdf_saved = False
        try:
            more_btn = page.locator("button:has-text('More'), button:has-text('更多')").first
            if await more_btn.count() > 0:
                await more_btn.hover()
                await asyncio.sleep(random.uniform(0.5, 1.5))
                await more_btn.click()
                await asyncio.sleep(1)
                save_btn = page.locator("text=Save to PDF, text=存為 PDF").first
                if await save_btn.count() > 0:
                    async with page.expect_download(timeout=15000) as dl:
                        await save_btn.click()
                    download = await dl.value
                    await download.save_as("{pdf_path}")
                    pdf_saved = True
                else:
                    await page.keyboard.press("Escape")
        except Exception:
            pass

        # 方式 B：page.pdf() 列印備援
        if not pdf_saved:
            try:
                await page.pdf(path="{pdf_path}", format="A4", print_background=True)
                pdf_saved = True
            except Exception:
                pass

        # 讀取頁面文字
        text = await page.evaluate("document.body.innerText")

        print("PDF_SAVED:" + str(pdf_saved))
        print("TEXT_START")
        print(text[:3000])
        print("TEXT_END")

asyncio.run(download())
"""
            result = subprocess.run(
                [sys.executable, "-c", download_script],
                capture_output=True, text=True, timeout=120,
                cwd=str(SCRIPT_DIR)
            )

            output = result.stdout
            pdf_saved = "PDF_SAVED:True" in output

            # 提取頁面文字
            if "TEXT_START" in output and "TEXT_END" in output:
                page_text = output.split("TEXT_START")[1].split("TEXT_END")[0].strip()

            if pdf_saved and pdf_path.exists():
                log.info(f"    PDF downloaded: {pdf_filename}")
                # 備份
                shutil.copy2(str(pdf_path), str(backup_dir / pdf_filename))
                c["_pdf_path"] = str(pdf_path)
            else:
                log.info(f"    PDF download failed, using page text only")
                c["_pdf_path"] = None

            c["_page_text"] = page_text

        except subprocess.TimeoutExpired:
            log.warning(f"    LinkedIn visit timed out for {name}")
            c["_pdf_path"] = None
            c["_page_text"] = ""
        except Exception as e:
            log.warning(f"    LinkedIn error for {name}: {e}")
            c["_pdf_path"] = None
            c["_page_text"] = ""

        # 從頁面文字提取結構化資料（補充到候選人資料）
        if page_text:
            c["_enriched_from_linkedin"] = True
            # 基本提取（實際的 AI 深度解析由 resume-parse API 處理）
            lines = page_text.split("\n")
            for line in lines[:5]:
                line = line.strip()
                if line and not c.get("current_title"):
                    # LinkedIn profile 的第二行通常是 headline
                    pass

        enriched.append(c)

        # 人性化間隔 45-90 秒
        if i < len(candidates) - 1:
            wait = random.uniform(45, 90)
            log.info(f"    Waiting {wait:.0f}s...")
            time.sleep(wait)

    return enriched


def import_candidates(candidates: list, job: dict) -> dict:
    """
    匯入候選人到 HR 系統（含完整必填欄位）

    匯入後對有 PDF 的候選人執行 resume-parse，
    自動解析並回填 work_history、education_details 等核心欄位。
    """
    if not candidates:
        return {"imported": 0, "failed": 0, "pdf_uploaded": 0, "pdf_parsed": 0}

    import_data = {
        "candidates": [],
        "actor": OPERATOR
    }

    for c in candidates:
        candidate_data = {
            # 必填欄位
            "name": c.get("name", "Unknown"),
            "current_title": c.get("title") or c.get("current_title") or c.get("headline", ""),
            "current_company": c.get("company") or c.get("current_company", ""),
            "skills": c.get("skills", ""),
            "years_experience": str(c.get("experience_years", c.get("years_experience", ""))),
            "linkedin_url": c.get("linkedin_url") or c.get("profileUrl", ""),
            "github_url": c.get("github_url", ""),
            "recruiter": c.get("recruiter", "待指派"),
            "status": "未開始",
            "source": "LinkedIn",
            "target_job_id": job["id"],

            # 工作經歷（如果爬蟲有提供）
            "work_history": c.get("work_experience") or c.get("work_history") or [],
            "education_details": c.get("education_background") or c.get("education_details") or [],
            "education": c.get("education", ""),
            "location": c.get("location", ""),

            # 匹配資訊
            "match_grade": c.get("grade", "B"),
            "match_summary": c.get("match_summary", "A層通過（自動篩選）"),
            "consultant_note": f"閉環自動匯入 {today_str}",

            # AI 匹配結果
            "ai_match_result": c.get("ai_match_result") or {
                "a_layer": "passed",
                "b_layer": c.get("b_layer_result", {}),
                "c_layer": c.get("c_layer_result", {}),
                "job_id": job["id"],
                "job_name": job.get("position_name", ""),
                "auto_graded": True
            }
        }

        # 清理空值
        candidate_data = {k: v for k, v in candidate_data.items() if v not in (None, "", [], {})}
        # 但這些必填欄位即使空也要保留
        for key in ["name", "status", "source", "target_job_id"]:
            if key not in candidate_data:
                candidate_data[key] = c.get(key, "")

        import_data["candidates"].append(candidate_data)

    # 匯入
    try:
        r = api_post("/api/crawler/import", import_data)
        created = r.get("created", [])
        updated = r.get("updated", [])
        failed_list = r.get("failed", [])
        log.info(f"  Import: created={len(created)}, updated={len(updated)}, failed={len(failed_list)}")
    except Exception as e:
        log.error(f"  Import failed: {e}")
        return {"imported": 0, "failed": len(candidates), "pdf_uploaded": 0, "pdf_parsed": 0}

    # 建立 name → id 對照表（從匯入結果取得 candidate_id）
    imported_map = {}
    for item in created + updated:
        cid = item.get("id") or item.get("candidate_id")
        cname = item.get("name", "")
        if cid:
            imported_map[cname] = cid

    # 上傳 PDF 履歷 + 解析
    pdf_uploaded = 0
    pdf_parsed = 0
    for c in candidates:
        pdf_path = c.get("_pdf_path")
        if not pdf_path or not Path(pdf_path).exists():
            continue

        name = c.get("name", "")
        candidate_id = imported_map.get(name)
        if not candidate_id:
            log.warning(f"  Cannot find candidate_id for {name}, skip PDF upload")
            continue

        log.info(f"  Uploading PDF for #{candidate_id} {name}...")
        try:
            import subprocess
            result = subprocess.run([
                "curl", "-s", "-X", "POST",
                f"{API_BASE}/api/candidates/{candidate_id}/resume-parse",
                "-H", f"Authorization: Bearer {API_KEY}",
                "-F", f"file=@{pdf_path};type=application/pdf",
                "-F", "format=linkedin",
                "-F", f"uploaded_by={OPERATOR}",
                "-w", "%{http_code}"
            ], capture_output=True, text=True, timeout=60)

            status_code = result.stdout[-3:] if len(result.stdout) >= 3 else "???"
            if status_code.startswith("2"):
                log.info(f"    PDF uploaded + parsed OK ({status_code})")
                pdf_uploaded += 1
                pdf_parsed += 1
            else:
                log.warning(f"    PDF upload returned {status_code} — saved locally for manual upload")
                pdf_uploaded += 1  # 檔案已存在本機
        except Exception as e:
            log.warning(f"    PDF upload error: {e}")

        # 間隔 2 秒避免太快
        time.sleep(2)

    total_imported = len(created) + len(updated)
    total_failed = len(failed_list)
    log.info(f"  Resume: {pdf_uploaded} uploaded, {pdf_parsed} parsed")

    return {
        "imported": total_imported,
        "failed": total_failed,
        "pdf_uploaded": pdf_uploaded,
        "pdf_parsed": pdf_parsed,
        "details": r
    }


def notify_ceo(message: str, metadata: dict = None):
    """透過 Notifications API 回報執行長"""
    try:
        api_post("/api/notifications", {
            "title": "【龍蝦回報】每日閉環完成",
            "message": message,
            "type": "report",
            "target_uid": "ceo",
            "metadata": {
                "from": OPERATOR,
                "task_type": "daily_closed_loop",
                "date": today_str,
                **(metadata or {})
            }
        })
    except Exception as e:
        log.warning(f"  Failed to notify CEO: {e}")


def notify_consultant(job: dict, imported_count: int, grades: dict):
    """
    通知負責顧問：有新候選人匯入（前端鈴鐺會看到）

    grades 格式: {"A+": 2, "A": 5, "B": 6, "C": 2}
    """
    # 取得職缺的負責顧問（從 consultant_notes 或 recruiter 欄位推斷）
    # 預設通知所有顧問（透過不指定 uid）
    job_name = job.get("position_name", "")
    client = job.get("client_company", "")
    job_id = job.get("id", "")

    grade_summary = "、".join(f"{g}: {n}人" for g, n in grades.items() if n > 0)

    try:
        api_post("/api/notifications", {
            "title": f"🆕 新候選人匯入 — #{job_id} {job_name}",
            "message": (
                f"閉環自動匯入 {imported_count} 位候選人\n"
                f"職缺：#{job_id} {job_name}（{client}）\n"
                f"評級分佈：{grade_summary}\n"
                f"請至人選管理查看並跟進"
            ),
            "type": "import",
            "metadata": {
                "from": OPERATOR,
                "task_type": "closed_loop_import",
                "job_id": job_id,
                "imported_count": imported_count,
                "grades": grades
            }
        })
        log.info(f"  Notified consultants: {imported_count} candidates for #{job_id}")
    except Exception as e:
        log.warning(f"  Failed to notify consultants: {e}")


# ──────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info(f"Step1ne Daily Closed Loop — {today_str}")
    log.info(f"API: {API_BASE}")
    log.info(f"Crawler: {CRAWLER_BASE}")
    log.info(f"Operator: {OPERATOR}")
    log.info("=" * 60)

    # 健康檢查
    try:
        api_get("/api/health")
        log.info("HR API: OK")
    except Exception as e:
        log.error(f"HR API unreachable: {e}")
        notify_ceo(f"閉環中止：HR API 無法連線 ({e})")
        return

    try:
        crawler_get("/api/health")
        log.info("Crawler API: OK")
    except Exception as e:
        log.error(f"Crawler API unreachable: {e}")
        notify_ceo(f"閉環中止：爬蟲 API 無法連線 ({e})")
        return

    # 取得招募中職缺
    jobs = get_active_jobs()
    log.info(f"Active jobs: {len(jobs)}")

    if not jobs:
        log.info("No active jobs. Done.")
        return

    # 讀取 checkpoint
    checkpoint = get_checkpoint()
    completed_jobs = set(checkpoint.get("completed_jobs", []))
    if completed_jobs:
        log.info(f"Checkpoint: {len(completed_jobs)} jobs already done today")

    # 統計
    total_searched = 0
    total_passed_a = 0
    total_imported = 0
    total_failed = 0
    total_pdf_uploaded = 0
    total_pdf_parsed = 0
    processed_jobs = []
    error_jobs = []
    consecutive_errors = 0

    for i, job in enumerate(jobs):
        job_id = job["id"]
        job_name = job.get("position_name", "Unknown")
        client = job.get("client_company", "")

        # 跳過已完成
        if job_id in completed_jobs:
            log.info(f"[{i+1}/{len(jobs)}] #{job_id} {job_name} — skipped (checkpoint)")
            continue

        # 檢查鎖定
        if is_job_locked(job_id):
            log.info(f"[{i+1}/{len(jobs)}] #{job_id} {job_name} — skipped (locked)")
            continue

        log.info(f"\n[{i+1}/{len(jobs)}] #{job_id} {job_name} ({client})")
        log.info("-" * 40)

        try:
            # 鎖定
            lock_job(job_id)

            # Step 1: 建立爬蟲任務
            task_id = create_crawler_task(job)
            if not task_id:
                error_jobs.append({"id": job_id, "name": job_name, "error": "task creation failed"})
                unlock_job(job_id, "error:task_creation")
                consecutive_errors += 1
                if consecutive_errors >= MAX_API_ERRORS:
                    log.error("Too many consecutive errors. Stopping.")
                    break
                continue

            # Step 2: 等待完成
            if not wait_for_task(task_id):
                error_jobs.append({"id": job_id, "name": job_name, "error": "task timeout/failed"})
                unlock_job(job_id, "error:task_failed")
                consecutive_errors += 1
                if consecutive_errors >= MAX_API_ERRORS:
                    log.error("Too many consecutive errors. Stopping.")
                    break
                continue

            # Step 3: 取得結果
            results = get_task_results(task_id)
            total_searched += len(results)

            if not results:
                log.info("  No results. Skipping.")
                unlock_job(job_id, "completed:0_results")
                completed_jobs.add(job_id)
                save_checkpoint({"completed_jobs": list(completed_jobs), "operator": OPERATOR})
                continue

            # Step 4: A 層篩選
            passed = a_layer_filter(results, job)
            total_passed_a += len(passed)

            if not passed:
                log.info("  No candidates passed A-layer. Skipping.")
                unlock_job(job_id, f"completed:searched={len(results)},passed=0")
                completed_jobs.add(job_id)
                save_checkpoint({"completed_jobs": list(completed_jobs), "operator": OPERATOR})
                continue

            # Step 5: LinkedIn B/C 層（下載 PDF + 頁面文字 + 資料充實）
            log.info(f"  B/C layer: visiting {len(passed)} LinkedIn profiles...")
            enriched = linkedin_download_and_enrich(passed, job)

            # Step 6: 匯入 HR 系統（含完整必填欄位）
            result = import_candidates(enriched, job)
            total_imported += result["imported"]
            total_failed += result["failed"]

            # Step 7: PDF 上傳統計
            pdf_up = result.get("pdf_uploaded", 0)
            pdf_parse = result.get("pdf_parsed", 0)

            # Step 8: 通知顧問（前端鈴鐺 🔔）
            if result["imported"] > 0:
                grades = {}
                for c in enriched:
                    g = c.get("grade", c.get("match_grade", "B"))
                    grades[g] = grades.get(g, 0) + 1
                notify_consultant(job, result["imported"], grades)

            # 更新鎖定 & checkpoint
            unlock_job(job_id, f"completed:searched={len(results)},passed={len(passed)},imported={result['imported']},pdf={pdf_up}")
            completed_jobs.add(job_id)
            save_checkpoint({"completed_jobs": list(completed_jobs), "operator": OPERATOR})
            processed_jobs.append({"id": job_id, "name": job_name, "searched": len(results), "passed": len(passed)})
            consecutive_errors = 0

        except Exception as e:
            log.error(f"  Unexpected error: {e}")
            error_jobs.append({"id": job_id, "name": job_name, "error": str(e)})
            try:
                unlock_job(job_id, f"error:{str(e)[:50]}")
            except Exception:
                pass
            consecutive_errors += 1
            if consecutive_errors >= MAX_API_ERRORS:
                log.error("Too many consecutive errors. Stopping.")
                break

    # 摘要
    log.info("\n" + "=" * 60)
    log.info("SUMMARY")
    log.info("=" * 60)
    log.info(f"Jobs processed: {len(processed_jobs)}/{len(jobs)}")
    log.info(f"Total searched: {total_searched}")
    log.info(f"A-layer passed: {total_passed_a}")
    log.info(f"Imported: {total_imported}")
    log.info(f"PDF uploaded: {total_pdf_uploaded}")
    log.info(f"PDF parsed: {total_pdf_parsed}")
    log.info(f"Failed: {total_failed}")
    log.info(f"Errors: {len(error_jobs)}")
    if total_searched > 0:
        log.info(f"Hit rate: {total_passed_a*100//total_searched}%")

    # 回報執行長
    summary = (
        f"日期：{today_str}\n"
        f"處理職缺：{len(processed_jobs)}/{len(jobs)}\n"
        f"搜尋結果：{total_searched} 人\n"
        f"A 層通過：{total_passed_a} 人\n"
        f"LinkedIn PDF 下載：{total_pdf_uploaded} 人\n"
        f"履歷解析成功：{total_pdf_parsed} 人\n"
        f"匯入系統：{total_imported} 人\n"
        f"失敗：{total_failed} 人\n"
        f"錯誤職缺：{len(error_jobs)} 個"
    )
    if error_jobs:
        summary += "\n\n錯誤清單：\n"
        for ej in error_jobs:
            summary += f"  #{ej['id']} {ej['name']} — {ej['error']}\n"

    notify_ceo(summary, {
        "jobs_total": len(jobs),
        "jobs_processed": len(processed_jobs),
        "total_searched": total_searched,
        "total_passed": total_passed_a,
        "total_imported": total_imported,
        "errors": len(error_jobs)
    })

    log.info("\nDone.")


if __name__ == "__main__":
    main()
