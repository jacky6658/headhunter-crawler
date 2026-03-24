#!/usr/bin/env python3
"""
Phase B 獨立腳本：下載缺 PDF 候選人的 LinkedIn 履歷

查詢 HR API 取得最近 7 天匯入、resume_files 為空、有 linkedin_url 的候選人，
用 Chrome CDP + JS click() 繞過 LinkedIn 商務浮層下載 PDF，再上傳到 HR 系統。

使用方式：
  python scripts/phase_b_pdf_download.py
  （自動從 .env 讀取 API_SECRET_KEY，或手動 export）

前置條件：
  - Chrome 以 CDP 模式啟動（port 9222），已登入 LinkedIn
  - pip install playwright requests
"""

import asyncio
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

# 加入 scripts 目錄到 path，以便 import linkedin_pdf_download
SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent

# 自動載入 .env
_env_file = PROJECT_DIR / ".env"
if _env_file.exists():
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
sys.path.insert(0, str(SCRIPT_DIR))

from linkedin_pdf_download import (
    download_pdf,
    upload_to_hr,
    browse_feed,
    human_delay,
    BATCH_SIZE,
    BATCH_REST,
    MIN_INTERVAL,
    MAX_INTERVAL,
)

# ──────────────────────────────────────────
# 設定
# ──────────────────────────────────────────
API_BASE = os.environ.get("API_BASE", "https://api-hr.step1ne.com")
API_KEY = os.environ.get("API_SECRET_KEY", "")
DAYS_LOOKBACK = int(os.environ.get("PHASE_B_DAYS", "7"))

if not API_KEY:
    print("ERROR: API_SECRET_KEY environment variable not set")
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

# Logging
PROJECT_DIR = SCRIPT_DIR.parent
LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
today_str = datetime.now().strftime("%Y-%m-%d")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"phase_b_{today_str}.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


def fetch_candidates_needing_pdf() -> list:
    """
    從 HR API 取得最近 N 天匯入、resume_files 為空、有 linkedin_url 或 github_url 的候選人。

    優化：從最新 ID 往回查，遇到超出時間範圍就停止，避免翻遍全部 2700+ 候選人。
    """
    cutoff = datetime.now() - timedelta(days=DAYS_LOOKBACK)
    candidates = []
    page_size = 200
    max_pages = 15  # 安全上限：最多翻 15 頁（3000 人）
    pages_fetched = 0
    out_of_range_count = 0

    # API sort=-id 不生效，所以從最後幾頁開始倒著掃（最新的在最後）
    # 先取 total 算出起始 offset
    try:
        r0 = requests.get(f"{API_BASE}/api/candidates?limit=1", headers=HEADERS, timeout=15)
        total = r0.json().get("total", 0)
    except Exception:
        total = 3000  # fallback

    # 從最後一頁往前掃
    start_offset = max(0, total - page_size)
    offset = start_offset
    log.info(f"Total candidates: {total}, starting from offset={offset}")

    while pages_fetched < max_pages and offset >= 0:
        url = f"{API_BASE}/api/candidates?limit={page_size}&offset={offset}"
        log.info(f"Fetching candidates (offset={offset})...")
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.error(f"API error: {e}")
            break

        rows = data.get("data", data.get("candidates", []))
        if not rows:
            break

        pages_fetched += 1
        page_out_of_range = 0

        for c in rows:
            # 檢查 created_at 是否在範圍內
            created_at = c.get("createdAt", c.get("created_at", ""))
            if not created_at:
                continue
            try:
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if created.replace(tzinfo=None) < cutoff:
                    page_out_of_range += 1
                    continue
            except (ValueError, TypeError):
                continue

            # 檢查 resume_files 是否為空
            resume_files = c.get("resumeFiles", c.get("resume_files", []))
            if resume_files:
                continue

            # LinkedIn URL 或 GitHub URL（至少要有一個）
            linkedin_url = c.get("linkedinUrl", c.get("linkedin_url", ""))
            github_url = c.get("githubUrl", c.get("github_url", ""))
            if not linkedin_url and not github_url:
                continue

            candidates.append({
                "id": c.get("id"),
                "name": c.get("name", "Unknown"),
                "linkedin_url": linkedin_url,
                "github_url": github_url,
                "grade": c.get("ai_grade", c.get("grade_level", "")),
            })

        # 如果整頁都超出時間範圍，表示已經翻到太舊的資料了
        if page_out_of_range == len(rows):
            log.info(f"All {len(rows)} candidates on this page are older than {DAYS_LOOKBACK} days. Stopping.")
            break

        # 往前翻一頁
        offset -= page_size
        if offset < 0:
            break

    log.info(f"Fetched {pages_fetched} pages, found {len(candidates)} candidates needing PDF")
    return candidates


async def search_linkedin_url(page, name: str) -> str:
    """用 Google 搜尋候選人的 LinkedIn URL"""
    import re
    query = f'"{name}" site:linkedin.com/in'
    search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}"

    try:
        await page.goto(search_url, wait_until="domcontentloaded")
        await asyncio.sleep(2)

        # 從搜尋結果提取 LinkedIn URL
        linkedin_url = await page.evaluate("""
            () => {
                const links = Array.from(document.querySelectorAll('a[href*="linkedin.com/in/"]'));
                for (const a of links) {
                    const href = a.href;
                    const match = href.match(/https?:\\/\\/(www\\.)?linkedin\\.com\\/in\\/[\\w-]+/);
                    if (match) return match[0];
                }
                return '';
            }
        """)
        return linkedin_url
    except Exception as e:
        log.warning(f"  Google search failed: {e}")
        return ""


def _safe_text(text: str) -> str:
    """移除非 Latin-1 字元，避免 fpdf2 Helvetica 字型報錯（中文、特殊符號等）"""
    return "".join(c if ord(c) < 256 else "?" for c in text)


async def generate_github_pdf(candidate_id: int, name: str, github_url: str) -> str | None:
    """用 GitHub profile 資料生成 PDF（當無法取得 LinkedIn PDF 時的備援）

    策略：
    1. 從 GitHub API 抓 user profile + top repos
    2. 嘗試用 GitHub username 的真名（name 欄位）搜 Google 補 LinkedIn
    3. 用 fpdf2 生成簡易 PDF，內含 profile + repos 資訊
    """
    try:
        username = github_url.rstrip("/").split("/")[-1]
        r = requests.get(f"https://api.github.com/users/{username}", timeout=10)
        if r.status_code != 200:
            log.warning(f"  GitHub API returned {r.status_code} for {username}")
            return None
        user = r.json()

        # 用 fpdf2 生成簡易 PDF（Helvetica 只支援 Latin-1，中文用 ? 替代）
        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()

        # Header
        display_name = user.get("name") or name
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, _safe_text(display_name), new_x="LMARGIN", new_y="NEXT")

        pdf.set_font("Helvetica", "", 11)
        if user.get("bio"):
            pdf.cell(0, 8, _safe_text(user["bio"][:120]), new_x="LMARGIN", new_y="NEXT")
        if user.get("company"):
            pdf.cell(0, 8, _safe_text(f"Company: {user['company']}"), new_x="LMARGIN", new_y="NEXT")
        if user.get("location"):
            pdf.cell(0, 8, _safe_text(f"Location: {user['location']}"), new_x="LMARGIN", new_y="NEXT")
        if user.get("blog"):
            pdf.cell(0, 8, _safe_text(f"Website: {user['blog']}"), new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 8, f"GitHub: {github_url}", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 8, f"Public repos: {user.get('public_repos', 0)} | Followers: {user.get('followers', 0)} | Following: {user.get('following', 0)}", new_x="LMARGIN", new_y="NEXT")
        if user.get("created_at"):
            pdf.cell(0, 8, f"Member since: {user['created_at'][:10]}", new_x="LMARGIN", new_y="NEXT")

        # Top repos
        repos_r = requests.get(f"https://api.github.com/users/{username}/repos?sort=stars&per_page=10", timeout=10)
        if repos_r.status_code == 200:
            repos = repos_r.json()[:10]
            if repos:
                pdf.ln(5)
                pdf.set_font("Helvetica", "B", 13)
                pdf.cell(0, 8, "Top Repositories", new_x="LMARGIN", new_y="NEXT")
                pdf.set_font("Helvetica", "", 10)
                for repo in repos:
                    stars = repo.get("stargazers_count", 0)
                    lang = repo.get("language", "") or ""
                    desc = _safe_text((repo.get("description") or "")[:80])
                    repo_name = _safe_text(repo.get("name", ""))
                    pdf.cell(0, 7, f"  {repo_name} ({lang}, {stars} stars) - {desc}", new_x="LMARGIN", new_y="NEXT")

                # 統計語言分佈
                langs = {}
                for repo in repos_r.json():
                    lang = repo.get("language")
                    if lang:
                        langs[lang] = langs.get(lang, 0) + 1
                if langs:
                    pdf.ln(3)
                    pdf.set_font("Helvetica", "B", 11)
                    pdf.cell(0, 8, "Language Distribution", new_x="LMARGIN", new_y="NEXT")
                    pdf.set_font("Helvetica", "", 10)
                    sorted_langs = sorted(langs.items(), key=lambda x: -x[1])[:8]
                    lang_str = ", ".join(f"{l} ({c})" for l, c in sorted_langs)
                    pdf.cell(0, 7, f"  {lang_str}", new_x="LMARGIN", new_y="NEXT")

        pdf_dir = PROJECT_DIR / "resumes" / "linkedin_pdfs"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        # 檔名也要過濾特殊字元
        safe_name = "".join(c if c.isalnum() or c in " -_" else "" for c in name)
        pdf_path = pdf_dir / f"{candidate_id}_{safe_name}.pdf"
        pdf.output(str(pdf_path))
        log.info(f"  Generated GitHub PDF: {pdf_path.name}")
        return str(pdf_path)
    except Exception as e:
        log.warning(f"  GitHub PDF generation failed: {e}")
        return None


async def run_phase_b(candidates: list):
    """對缺 PDF 的候選人逐一下載 LinkedIn PDF 並上傳"""
    from playwright.async_api import async_playwright
    import random

    log.info(f"=== Phase B: Download PDFs for {len(candidates)} candidates ===")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        except Exception as e:
            log.error(f"Cannot connect to Chrome CDP on port 9222: {e}")
            log.error("Start Chrome with: --remote-debugging-port=9222")
            return {"success": 0, "failed": 0, "github_pdf": 0}

        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        # 先瀏覽 feed
        await browse_feed(page)

        success = 0
        failed = 0
        github_pdf = 0

        for i, c in enumerate(candidates):
            cid = c["id"]
            name = c["name"]
            linkedin_url = c.get("linkedin_url", "")
            github_url = c.get("github_url", "")
            grade = c.get("grade", "")

            # 批次間休息
            if i > 0 and i % BATCH_SIZE == 0:
                log.info(f"--- Batch break ({BATCH_REST}s) ---")
                await asyncio.sleep(BATCH_REST)
                await browse_feed(page)

            log.info(f"[{i+1}/{len(candidates)}] #{cid} {name} ({grade})")

            pdf_path = None

            # 策略 1：有 LinkedIn URL → 直接下載
            if linkedin_url:
                pdf_path = await download_pdf(page, cid, name, linkedin_url)

            # 策略 2：沒有 LinkedIn URL → Google 搜尋補上
            if not pdf_path and not linkedin_url and name:
                log.info(f"  No LinkedIn URL, searching Google...")
                found_url = await search_linkedin_url(page, name)
                if found_url:
                    log.info(f"  Found LinkedIn: {found_url}")
                    linkedin_url = found_url
                    # 更新 HR 系統的 linkedin_url
                    try:
                        requests.patch(
                            f"{API_BASE}/api/candidates/{cid}",
                            headers=HEADERS,
                            json={"linkedin_url": found_url},
                            timeout=10
                        )
                    except Exception:
                        pass
                    pdf_path = await download_pdf(page, cid, name, found_url)
                    await human_delay(2, 4)

            # 策略 3：都失敗 → 用 GitHub 資料生成 PDF
            if not pdf_path and github_url:
                log.info(f"  Fallback: generating PDF from GitHub profile")
                pdf_path = await generate_github_pdf(cid, name, github_url)
                if pdf_path:
                    github_pdf += 1

            # 上傳
            if pdf_path:
                await upload_to_hr(cid, pdf_path)
                success += 1
                log.info(f"  OK: PDF downloaded + uploaded")
            else:
                failed += 1
                log.warning(f"  FAIL: no PDF generated for {name}")

            # 人性化間隔
            if i < len(candidates) - 1:
                wait = random.uniform(MIN_INTERVAL, MAX_INTERVAL)
                log.info(f"  Waiting {wait:.0f}s...")
                await asyncio.sleep(wait)

                # 偶爾瀏覽 feed
                if random.random() < 0.3:
                    await browse_feed(page)

    return {"success": success, "failed": failed, "github_pdf": github_pdf}


def main():
    log.info(f"Phase B PDF Download — {today_str}")
    log.info(f"Lookback: {DAYS_LOOKBACK} days")
    log.info(f"API: {API_BASE}")

    # Step 1: 查詢缺 PDF 的候選人
    candidates = fetch_candidates_needing_pdf()
    log.info(f"Found {len(candidates)} candidates needing PDF")

    if not candidates:
        log.info("No candidates need PDF download. Done.")
        return

    for c in candidates:
        log.info(f"  #{c['id']} {c['name']} — {c['linkedin_url']}")

    # Step 2: 下載 + 上傳
    result = asyncio.run(run_phase_b(candidates))

    # Step 3: 摘要
    log.info(f"\n=== Phase B Summary ===")
    log.info(f"Total: {len(candidates)}")
    log.info(f"Success: {result['success']} (LinkedIn: {result['success'] - result['github_pdf']}, GitHub PDF: {result['github_pdf']})")
    log.info(f"Failed: {result['failed']}")


if __name__ == "__main__":
    main()
