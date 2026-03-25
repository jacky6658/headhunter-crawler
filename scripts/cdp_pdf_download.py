#!/usr/bin/env python3
"""
Phase B: Download LinkedIn PDFs via Chrome CDP (JS click More → Save to PDF)
Then upload to HR system via resume-parse API.
"""
import asyncio
import base64
import json
import os
import random
import sys
import time
import requests
from pathlib import Path

# Config
API_BASE = os.environ.get("API_BASE", "https://api-hr.step1ne.com")
API_KEY = os.environ.get("API_SECRET_KEY", "PotfZ42-qPyY4uqSwqstpxllQB1alxVfjJsm3Mgp3HQ")
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

# Load candidate list
with open("/tmp/job233_imported.json") as f:
    candidates = json.load(f)

PDF_DIR = Path("/Users/user/clawd/headhunter-crawler/resumes/linkedin_pdfs")
PDF_DIR.mkdir(parents=True, exist_ok=True)


async def download_pdf(page, linkedin_url, save_path):
    """Navigate to profile, click More → Save to PDF"""
    try:
        await page.goto(linkedin_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(random.uniform(3, 5))

        # Scroll to simulate reading
        for _ in range(random.randint(2, 4)):
            await page.mouse.wheel(0, random.randint(200, 500))
            await asyncio.sleep(random.uniform(0.5, 1.0))
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(1)

        # Method A: JS click More button
        clicked_more = await page.evaluate("""() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            const moreBtn = buttons.find(b =>
                b.textContent.trim() === 'More' ||
                b.textContent.trim() === '更多' ||
                (b.getAttribute('aria-label') || '').includes('More actions') ||
                (b.getAttribute('aria-label') || '').includes('更多動作')
            );
            if (moreBtn) { moreBtn.click(); return true; }
            return false;
        }""")

        if not clicked_more:
            return False, "More button not found"

        await asyncio.sleep(random.uniform(1.5, 2.5))

        # Click Save to PDF with download interception
        try:
            async with page.expect_download(timeout=15000) as dl:
                found_save = await page.evaluate("""() => {
                    const items = Array.from(document.querySelectorAll('li, div, span, a'));
                    const saveItem = items.find(el =>
                        el.textContent.trim() === '存為 PDF' ||
                        el.textContent.trim() === 'Save to PDF'
                    );
                    if (saveItem) { saveItem.click(); return true; }
                    return false;
                }""")

                if not found_save:
                    await page.keyboard.press("Escape")
                    return False, "Save to PDF not found"

            download = await dl.value
            await download.save_as(str(save_path))

            # Verify it's a real LinkedIn PDF (not screenshot)
            size = save_path.stat().st_size
            if size > 300000:  # > 300KB likely screenshot
                return False, f"PDF too large ({size} bytes), likely screenshot"
            if size < 1000:  # < 1KB likely error
                return False, f"PDF too small ({size} bytes)"

            return True, f"OK ({size} bytes)"

        except asyncio.TimeoutError:
            await page.keyboard.press("Escape")
            return False, "Download timeout"

    except Exception as e:
        return False, str(e)[:100]


def upload_pdf_to_hr(candidate_id, pdf_path):
    """Upload PDF to HR system via resume-parse"""
    try:
        import subprocess
        result = subprocess.run([
            "curl", "-s", "-X", "POST",
            f"{API_BASE}/api/candidates/{candidate_id}/resume-parse",
            "-H", f"Authorization: Bearer {API_KEY}",
            "-F", f"file=@{pdf_path};type=application/pdf",
            "-F", "format=linkedin",
            "-F", "uploaded_by=lobster-auto",
            "-w", "%{http_code}"
        ], capture_output=True, text=True, timeout=60)

        status_code = result.stdout[-3:] if len(result.stdout) >= 3 else "???"
        return status_code.startswith("2"), status_code
    except Exception as e:
        return False, str(e)[:50]


async def main():
    from playwright.async_api import async_playwright

    results = []
    pdf_success = 0
    upload_success = 0

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = await context.new_page()

        for i, c in enumerate(candidates):
            cid = c["id"]
            name = c["name"]
            linkedin = c["linkedin"]

            if not linkedin:
                print(f"[{i+1}/{len(candidates)}] #{cid} {name} — no LinkedIn URL, skip")
                results.append({"id": cid, "name": name, "pdf": False, "reason": "no URL"})
                continue

            print(f"[{i+1}/{len(candidates)}] #{cid} {name}", flush=True)

            pdf_path = PDF_DIR / f"{cid}_{name.replace(' ', '_')}.pdf"
            ok, msg = await download_pdf(page, linkedin, pdf_path)
            print(f"  PDF: {ok} — {msg}", flush=True)

            if ok and pdf_path.exists():
                pdf_success += 1

                # Upload to HR
                up_ok, up_code = upload_pdf_to_hr(cid, pdf_path)
                print(f"  Upload: {up_ok} (HTTP {up_code})", flush=True)
                if up_ok:
                    upload_success += 1
                results.append({"id": cid, "name": name, "pdf": True, "uploaded": up_ok})
            else:
                results.append({"id": cid, "name": name, "pdf": False, "reason": msg})

            # Rate limiting: 60-90 sec between profiles
            if i < len(candidates) - 1:
                wait = random.uniform(60, 90)
                print(f"  wait {wait:.0f}s", flush=True)
                await asyncio.sleep(wait)

            # Batch break every 5
            if (i + 1) % 5 == 0 and i < len(candidates) - 1:
                extra = random.uniform(120, 180)
                print(f"  batch break {extra:.0f}s", flush=True)
                await asyncio.sleep(extra)

        await page.close()

    # Save results
    with open("/tmp/job233_pdf_results.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n=== Phase B Summary ===")
    print(f"Total: {len(candidates)}")
    print(f"PDF downloaded: {pdf_success}")
    print(f"PDF uploaded: {upload_success}")
    print(f"Failed: {len(candidates) - pdf_success}")

if __name__ == "__main__":
    asyncio.run(main())
