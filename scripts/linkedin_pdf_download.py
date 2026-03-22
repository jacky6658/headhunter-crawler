#!/usr/bin/env python3
"""
LinkedIn PDF 履歷自動下載腳本（人性化版 v7）

透過 Playwright 連接本機已登入的 Chrome（CDP 協議），
模擬人類操作 LinkedIn，自動下載候選人履歷 PDF。

使用方式：
  1. 啟動 Chrome CDP 模式（見下方說明）
  2. 編輯 candidates 清單
  3. 執行: python scripts/linkedin_pdf_download.py

前置條件：
  - Chrome 以 CDP 模式啟動:
    /Applications/Google Chrome.app/Contents/MacOS/Google Chrome \\
      --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-cdp
  - Chrome 已登入你自己的 LinkedIn 帳號
  - pip install playwright
"""

import asyncio
import os
import random
import shutil
import sys
import time
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("Error: playwright not installed. Run: pip install playwright")
    sys.exit(1)

# ──────────────────────────────────────────
# 設定區：填入候選人清單
# ──────────────────────────────────────────
candidates = [
    # (候選人ID, "姓名", "LinkedIn URL", "評級"),
    # (3003, "Kun Yen", "https://www.linkedin.com/in/yenkun/", "B+"),
]

# API 設定
API_BASE = os.environ.get("API_BASE", "https://api-hr.step1ne.com")
API_KEY = os.environ.get("API_SECRET_KEY", "")

# 路徑設定
SCRIPT_DIR = Path(__file__).parent.parent
PDF_DIR = SCRIPT_DIR / "resumes" / "linkedin_pdfs"
BACKUP_DIR = SCRIPT_DIR / "resumes" / "pending_upload"
PDF_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# 人性化參數
MIN_INTERVAL = 45   # 最短間隔（秒）
MAX_INTERVAL = 90   # 最長間隔（秒）
BATCH_SIZE = 5      # 一批最多幾人
BATCH_REST = 600    # 批次間休息（秒）
SCROLL_MIN = 5      # 閱讀模擬最短（秒）
SCROLL_MAX = 12     # 閱讀模擬最長（秒）
HOVER_MIN = 0.5     # hover 最短（秒）
HOVER_MAX = 1.5     # hover 最長（秒）


async def human_delay(min_s: float, max_s: float):
    """隨機等待，模擬人類行為"""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def simulate_reading(page):
    """模擬閱讀：隨機滾動頁面"""
    scroll_time = random.uniform(SCROLL_MIN, SCROLL_MAX)
    end_time = time.time() + scroll_time
    while time.time() < end_time:
        scroll_amount = random.randint(200, 600)
        await page.mouse.wheel(0, scroll_amount)
        await human_delay(0.5, 1.5)
    # 滾回頂部
    await page.evaluate("window.scrollTo(0, 0)")
    await human_delay(0.5, 1.0)


async def browse_feed(page):
    """瀏覽 LinkedIn feed（模擬正常用戶行為）"""
    print("  [feed] Browsing LinkedIn feed...")
    await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
    await human_delay(2, 4)
    for _ in range(random.randint(2, 5)):
        await page.mouse.wheel(0, random.randint(300, 800))
        await human_delay(1, 3)


async def try_save_to_pdf(page) -> bool:
    """嘗試用 LinkedIn 原生的「存為 PDF」下載（一度/非一度皆可）"""
    try:
        # 找「更多」按鈕
        more_btn = page.locator("button:has-text('More'), button:has-text('更多')").first
        if await more_btn.count() == 0:
            return False
        await more_btn.hover()
        await human_delay(HOVER_MIN, HOVER_MAX)
        await more_btn.click()
        await human_delay(0.8, 1.5)

        # 找「存為 PDF」
        save_pdf = page.locator("text=Save to PDF, text=存為 PDF").first
        if await save_pdf.count() == 0:
            # 關閉選單
            await page.keyboard.press("Escape")
            return False
        await save_pdf.hover()
        await human_delay(HOVER_MIN, HOVER_MAX)

        # 開始監聽下載
        async with page.expect_download(timeout=15000) as download_info:
            await save_pdf.click()
        download = await download_info.value
        return download
    except Exception:
        return False


async def download_pdf(page, candidate_id: int, name: str, url: str) -> str | None:
    """下載單一候選人的 LinkedIn PDF"""
    pdf_path = PDF_DIR / f"{candidate_id}_{name}.pdf"

    print(f"  [profile] Navigating to {url}")
    await page.goto(url, wait_until="domcontentloaded")
    await human_delay(2, 4)

    # 閱讀模擬
    await simulate_reading(page)

    # 嘗試方式 A：原生下載
    download = await try_save_to_pdf(page)
    if download and download is not True:
        await download.save_as(str(pdf_path))
        print(f"  [pdf] Native download saved: {pdf_path.name}")
    else:
        # 方式 B：page.pdf() 列印備援
        print(f"  [pdf] No 'Save to PDF' button, using page.pdf() fallback")
        try:
            await page.pdf(path=str(pdf_path), format="A4", print_background=True)
            print(f"  [pdf] Print-to-PDF saved: {pdf_path.name}")
        except Exception as e:
            print(f"  [pdf] FAILED: {e}")
            return None

    # 備份
    backup_path = BACKUP_DIR / pdf_path.name
    shutil.copy2(str(pdf_path), str(backup_path))

    return str(pdf_path)


async def upload_to_hr(candidate_id: int, pdf_path: str):
    """上傳 PDF 到 HR 系統"""
    if not API_KEY:
        print(f"  [upload] Skipped (no API_KEY)")
        return

    import subprocess
    result = subprocess.run([
        "curl", "-s", "-X", "POST",
        f"{API_BASE}/api/candidates/{candidate_id}/resume-parse",
        "-H", f"Authorization: Bearer {API_KEY}",
        "-F", f"file=@{pdf_path};type=application/pdf",
        "-F", "format=linkedin",
        "-F", "uploaded_by=龍蝦-AutoLoop",
        "-w", "%{http_code}"
    ], capture_output=True, text=True, timeout=30)

    status_code = result.stdout[-3:] if len(result.stdout) >= 3 else "???"
    if status_code.startswith("2"):
        print(f"  [upload] Success ({status_code})")
    else:
        print(f"  [upload] Failed ({status_code}) - PDF saved locally for manual upload")


async def main():
    if not candidates:
        print("No candidates defined. Edit the 'candidates' list in this script.")
        return

    print(f"=== LinkedIn PDF Download (Human-like v7) ===")
    print(f"Candidates: {len(candidates)}")
    print(f"PDF dir: {PDF_DIR}")
    print(f"Backup dir: {BACKUP_DIR}")
    print()

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        except Exception as e:
            print(f"ERROR: Cannot connect to Chrome CDP on port 9222")
            print(f"Start Chrome with: --remote-debugging-port=9222")
            print(f"Detail: {e}")
            return

        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        # 先瀏覽 feed
        await browse_feed(page)

        for i, (cid, name, url, grade) in enumerate(candidates):
            batch_num = i // BATCH_SIZE
            batch_pos = i % BATCH_SIZE

            # 批次間休息
            if i > 0 and batch_pos == 0:
                print(f"\n--- Batch break ({BATCH_REST}s) ---\n")
                await asyncio.sleep(BATCH_REST)
                await browse_feed(page)

            print(f"\n[{i+1}/{len(candidates)}] #{cid} {name} ({grade})")

            # 下載 PDF
            pdf_path = await download_pdf(page, cid, name, url)

            # 上傳
            if pdf_path:
                await upload_to_hr(cid, pdf_path)

            # 人性化間隔
            if i < len(candidates) - 1:
                wait = random.uniform(MIN_INTERVAL, MAX_INTERVAL)
                print(f"  [wait] {wait:.0f}s before next...")
                await asyncio.sleep(wait)

                # 偶爾瀏覽 feed
                if random.random() < 0.3:
                    await browse_feed(page)

        print(f"\n=== Done! {len(candidates)} candidates processed ===")
        print(f"PDFs: {PDF_DIR}")
        print(f"Backups: {BACKUP_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
