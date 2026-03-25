#!/usr/bin/env python3
"""
Chrome CDP batch enrichment - uses body text parsing (selectors fail on LinkedIn)
"""
import asyncio
import json
import random
import sys
from pathlib import Path

LINKEDIN_URLS = [
    "https://www.linkedin.com/in/masaki-ikeno-furano",
    "https://www.linkedin.com/in/hiroki-hiramatsu-aba6b018",
    "https://www.linkedin.com/in/naho-nishikawa-262182278",
    "https://www.linkedin.com/in/tomoyaasai",
    "https://www.linkedin.com/in/kenji-koshiishi-9bb905160",
    "https://www.linkedin.com/in/paulyu2",
    "https://www.linkedin.com/in/minori-haruta-383307258",
    "https://www.linkedin.com/in/masakazu-yoshimura-5a0006160",
    "https://www.linkedin.com/in/simon-johnson-927b4",
    "https://www.linkedin.com/in/andrew-hankinson-09b89",
    "https://www.linkedin.com/in/kazunori-shibata-7a62617b",
    "https://www.linkedin.com/in/shawn-lawlor-3444a68",
    "https://www.linkedin.com/in/yoshihide-saotome-1a61408",
    "https://www.linkedin.com/in/toru-nagai-37450613",
    "https://www.linkedin.com/in/tomishi-kato-45626018a",
    "https://www.linkedin.com/in/atsuhiko-shimizu-6a8b65234",
    "https://www.linkedin.com/in/koji-seki-5b94a1b",
    "https://www.linkedin.com/in/靖志-梶原-31a5baa2",
    "https://www.linkedin.com/in/mitsuru-ozaki-5b4231136",
    "https://www.linkedin.com/in/shinji-iijima-3a3a29158",
    "https://www.linkedin.com/in/matsuda-takao-95547152",
    "https://www.linkedin.com/in/yoshifumi-takama-8b5338163",
    "https://www.linkedin.com/in/watanabe-yoshiyuki-4914a2157",
    "https://www.linkedin.com/in/yasuhiro-otake-27ba75174",
    "https://www.linkedin.com/in/yoshikazu-ueda-666293119",
    "https://www.linkedin.com/in/kanji-yoshikawa-04146618",
]


def parse_profile_text(text, url):
    """Parse LinkedIn profile from body innerText"""
    lines = [l.strip() for l in text.split('\n') if l.strip()]

    # Find name - usually appears after navigation items, before headline
    # Pattern: name appears twice (profile card header + sidebar)
    name = ""
    headline = ""
    location = ""

    # Skip nav items, find first meaningful content block
    skip_nav = ["首頁", "我的人脈", "職缺", "訊息", "通知", "我", "商務功能",
                "前往內容", "Premium", "通知", "搜尋", "Home", "My Network",
                "Jobs", "Messaging", "Notifications", "Me", "Business"]

    content_lines = []
    nav_done = False
    for line in lines:
        if not nav_done:
            if any(n in line for n in skip_nav) or len(line) < 2 or line.isdigit():
                continue
            if "電郵" in line or "更新" in line or "確認" in line:
                continue
            nav_done = True

        if nav_done:
            content_lines.append(line)

    # First non-nav line is usually the name
    if content_lines:
        name = content_lines[0]

    # Second substantial line (not "更多", "連線", etc.) is usually headline
    action_words = ["更多", "連線", "訊息", "關注", "前往", "More", "Connect",
                    "Message", "Follow", "以 $"]
    for line in content_lines[1:10]:
        if any(a in line for a in action_words):
            continue
        if len(line) > 5:
            headline = line
            break

    # Location - usually has country/city pattern
    for line in content_lines[1:15]:
        if any(loc in line for loc in ["日本", "Japan", "Tokyo", "台灣", "Taiwan",
                                        "Singapore", "United States", "China",
                                        "Hong Kong", "Korea", "Australia",
                                        "北海道", "大阪", "東京", "千葉", "埼玉",
                                        "神奈川"]):
            location = line
            break
        # Also match "都道府県" style locations
        if "·" in line and len(line) < 50:
            location = line
            break

    # Extract experience section text (first 2000 chars)
    exp_text = text[:4000]

    return {
        "name": name,
        "headline": headline,
        "location": location,
        "linkedin_url": url,
        "body_text": text[:5000],
        "success": True
    }


async def extract_profile(page, url):
    """Visit and extract profile via body text"""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(random.uniform(3, 5))

        # Simulate reading
        for _ in range(random.randint(2, 4)):
            await page.mouse.wheel(0, random.randint(200, 500))
            await asyncio.sleep(random.uniform(0.3, 0.8))
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)

        # Get page title and body text
        title = await page.title()
        body = await page.evaluate("document.body.innerText")

        if "authwall" in page.url.lower() or "login" in page.url.lower():
            return {"linkedin_url": url, "success": False, "error": "auth_wall"}

        return parse_profile_text(body, url)

    except Exception as e:
        return {"linkedin_url": url, "success": False, "error": str(e)}


async def main():
    from playwright.async_api import async_playwright

    results = []

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = await context.new_page()

        for i, url in enumerate(LINKEDIN_URLS):
            print(f"[{i+1}/{len(LINKEDIN_URLS)}] {url}", flush=True)

            data = await extract_profile(page, url)
            results.append(data)

            name = data.get("name", "?")
            headline = (data.get("headline") or "")[:60]
            location = data.get("location", "")
            ok = data.get("success", False)
            print(f"  -> {name} | {headline} | {location} | ok={ok}", flush=True)

            # Rate limiting
            if i < len(LINKEDIN_URLS) - 1:
                wait = random.uniform(25, 50)
                print(f"  wait {wait:.0f}s", flush=True)
                await asyncio.sleep(wait)

            # Batch break every 5
            if (i + 1) % 5 == 0 and i < len(LINKEDIN_URLS) - 1:
                extra = random.uniform(60, 90)
                print(f"  batch break {extra:.0f}s", flush=True)
                await asyncio.sleep(extra)

        await page.close()

    # Save
    output = Path("/tmp/job233_enriched.json")
    with open(output, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    ok = [r for r in results if r.get("success")]
    print(f"\n=== Summary ===")
    print(f"Total: {len(results)}, Success: {len(ok)}, Failed: {len(results)-len(ok)}")
    for r in ok:
        print(f"  {r.get('name','?')} | {(r.get('headline') or '')[:50]} | {r.get('location','')}")

if __name__ == "__main__":
    asyncio.run(main())
