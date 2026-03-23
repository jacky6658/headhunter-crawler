#!/usr/bin/env python3
"""
LinkedIn Profile Scraper — DOM 結構化抓取 + PDF 生成 + HR API 上傳

透過 Playwright 連接本機已登入的 Chrome（CDP 協議），
從 LinkedIn 個人頁面 DOM 抓取結構化資料，生成 PDF 並上傳到 HR 系統。

使用方式（獨立執行）：
  python3 scripts/linkedin_profile_scraper.py \
    --linkedin-url "https://linkedin.com/in/xxx" \
    --candidate-id 3475 \
    --api-key "YOUR_API_KEY"

使用方式（import）：
  from scripts.linkedin_profile_scraper import scrape_and_upload
  result = scrape_and_upload(linkedin_url, candidate_id, api_key)

前置條件：
  - Chrome 以 CDP 模式啟動:
    /Applications/Google Chrome.app/Contents/MacOS/Google Chrome \
      --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-cdp
  - Chrome 已登入你自己的 LinkedIn 帳號
  - pip install playwright fpdf2
"""

import argparse
import asyncio
import json
import logging
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("Error: playwright not installed. Run: pip install playwright")
    sys.exit(1)

try:
    from fpdf import FPDF
except ImportError:
    print("Error: fpdf2 not installed. Run: pip install fpdf2")
    sys.exit(1)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────
# 路徑設定
# ──────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.parent
PDF_DIR = SCRIPT_DIR / "resumes" / "scraped_profiles"
PDF_DIR.mkdir(parents=True, exist_ok=True)

API_BASE = os.environ.get("API_BASE", "https://api-hr.step1ne.com")

# 人性化參數
SCROLL_PAUSE_MIN = 0.5
SCROLL_PAUSE_MAX = 1.5
PAGE_LOAD_MIN = 2.0
PAGE_LOAD_MAX = 4.0
ACTION_DELAY_MIN = 0.3
ACTION_DELAY_MAX = 0.8

# 中文字型路徑（依優先順序嘗試）
FONT_CANDIDATES = [
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


# ──────────────────────────────────────────
# 資料結構
# ──────────────────────────────────────────
@dataclass
class WorkEntry:
    company: str = ""
    title: str = ""
    duration: str = ""
    description: str = ""


@dataclass
class EducationEntry:
    school: str = ""
    degree: str = ""
    field_of_study: str = ""
    duration: str = ""


@dataclass
class ProfileData:
    name: str = ""
    headline: str = ""
    location: str = ""
    about: str = ""
    work_history: List[WorkEntry] = field(default_factory=list)
    education: List[EducationEntry] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    linkedin_url: str = ""


# ──────────────────────────────────────────
# 人性化操作
# ──────────────────────────────────────────
async def human_delay(min_s: float = ACTION_DELAY_MIN, max_s: float = ACTION_DELAY_MAX):
    """隨機等待，模擬人類行為"""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def smooth_scroll(page, distance: int = 500, steps: int = 5):
    """平滑滾動頁面"""
    step_size = distance // steps
    for _ in range(steps):
        await page.mouse.wheel(0, step_size + random.randint(-20, 20))
        await asyncio.sleep(random.uniform(0.05, 0.15))


async def dismiss_overlays(page):
    """關閉所有浮層/彈窗"""
    dismiss_scripts = [
        # LinkedIn 訊息面板
        """
        (() => {
            const msgOverlay = document.querySelector('.msg-overlay-bubble-header__control--new-convo-btn');
            if (msgOverlay) {
                const closeBtn = document.querySelector('.msg-overlay-bubble-header__control[data-control-name="overlay.close_conversation_window"]');
                if (closeBtn) closeBtn.click();
            }
            // 關閉訊息彈窗
            const msgClose = document.querySelector('button[data-control-name="overlay.close_conversation_window"]');
            if (msgClose) msgClose.click();
        })()
        """,
        # 通用 modal dismiss
        """
        (() => {
            const dismissBtns = document.querySelectorAll(
                'button[aria-label="Dismiss"], button[aria-label="關閉"], ' +
                'button.msg-overlay-bubble-header__control--close, ' +
                'button.artdeco-toast-item__dismiss, ' +
                'button.artdeco-modal__dismiss'
            );
            dismissBtns.forEach(btn => btn.click());
        })()
        """,
        # 最小化訊息面板
        """
        (() => {
            const minimize = document.querySelector('.msg-overlay-bubble-header__button--minimize');
            if (minimize) minimize.click();
            // 折疊所有 msg-overlay
            document.querySelectorAll('.msg-overlay-conversation-bubble--is-active .msg-overlay-bubble-header__control')
                .forEach(btn => btn.click());
        })()
        """,
    ]
    for script in dismiss_scripts:
        try:
            await page.evaluate(script)
            await asyncio.sleep(0.3)
        except Exception:
            pass


async def scroll_to_load_all(page):
    """滾動頁面展開所有 section"""
    print("  [scroll] Scrolling to load all sections...")

    # 先展開「Show all」按鈕
    await page.evaluate("""
        (() => {
            const showMoreBtns = document.querySelectorAll(
                'button.pv-profile-section__see-more-inline, ' +
                'button.inline-show-more-text__button, ' +
                'a[data-control-name="see_more"], ' +
                'button[aria-expanded="false"]'
            );
            showMoreBtns.forEach(btn => {
                if (btn.textContent.includes('more') || btn.textContent.includes('顯示更多') ||
                    btn.textContent.includes('Show all') || btn.textContent.includes('see more')) {
                    btn.click();
                }
            });
        })()
    """)
    await human_delay(1.0, 2.0)

    # 逐步滾動到底部
    prev_height = 0
    for i in range(20):
        current_height = await page.evaluate("document.body.scrollHeight")
        if current_height == prev_height and i > 3:
            break
        prev_height = current_height

        await smooth_scroll(page, random.randint(400, 700))
        await human_delay(SCROLL_PAUSE_MIN, SCROLL_PAUSE_MAX)

        # 偶爾點擊「Show more」按鈕
        if i % 3 == 0:
            await page.evaluate("""
                (() => {
                    const btns = document.querySelectorAll(
                        'button.inline-show-more-text__button, ' +
                        'button.pv-profile-section__see-more-inline'
                    );
                    btns.forEach(btn => {
                        try { btn.click(); } catch(e) {}
                    });
                })()
            """)
            await human_delay(0.5, 1.0)

    # 滾回頂部
    await page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
    await human_delay(1.0, 2.0)
    print("  [scroll] Done.")


# ──────────────────────────────────────────
# DOM 資料抓取
# ──────────────────────────────────────────
async def extract_profile_data(page, linkedin_url: str) -> ProfileData:
    """從 LinkedIn 頁面 DOM 抓取結構化資料"""
    profile = ProfileData(linkedin_url=linkedin_url)

    # 姓名
    profile.name = await _safe_text(page, "h1")
    print(f"  [extract] Name: {profile.name}")

    # 職稱/headline
    profile.headline = await _safe_text(page, ".text-body-medium")
    if not profile.headline:
        for sel in [".top-card-layout__headline", "[data-anonymize='headline']"]:
            profile.headline = await _safe_text(page, sel)
            if profile.headline:
                break
    print(f"  [extract] Headline: {profile.headline}")

    # Location
    profile.location = await _safe_text(
        page, ".text-body-small.inline.t-black--light.break-words"
    )
    if not profile.location:
        profile.location = await _safe_text(page, ".top-card-layout__first-subline")

    # About / Summary
    profile.about = await _extract_about(page)
    if profile.about:
        print(f"  [extract] About: {profile.about[:80]}...")

    # 工作經歷
    profile.work_history = await _extract_work_history(page)
    print(f"  [extract] Work history: {len(profile.work_history)} entries")

    # 教育背景
    profile.education = await _extract_education(page)
    print(f"  [extract] Education: {len(profile.education)} entries")

    # 技能
    profile.skills = await _extract_skills(page)
    print(f"  [extract] Skills: {len(profile.skills)} items")

    return profile


async def _safe_text(page, selector: str) -> str:
    """安全地從 selector 取得文字"""
    try:
        el = await page.query_selector(selector)
        if el:
            text = await el.inner_text()
            return text.strip() if text else ""
    except Exception:
        pass
    return ""


async def _extract_about(page) -> str:
    """提取 About / Summary"""
    # 先嘗試點擊 About section 的 "see more"
    try:
        await page.evaluate("""
            (() => {
                const aboutSection = document.querySelector('#about');
                if (aboutSection) {
                    const seeMore = aboutSection.closest('section')?.querySelector('button.inline-show-more-text__button');
                    if (seeMore) seeMore.click();
                }
            })()
        """)
        await asyncio.sleep(0.5)
    except Exception:
        pass

    # 提取 About 文字
    about_text = await page.evaluate("""
        (() => {
            // 方式1: 新版 LinkedIn
            const aboutSection = document.querySelector('#about');
            if (aboutSection) {
                const section = aboutSection.closest('section');
                if (section) {
                    const spans = section.querySelectorAll(
                        '.inline-show-more-text span[aria-hidden="true"], ' +
                        '.pv-shared-text-with-see-more span.visually-hidden, ' +
                        'div.display-flex span[aria-hidden="true"]'
                    );
                    for (const span of spans) {
                        const t = span.innerText?.trim();
                        if (t && t.length > 20) return t;
                    }
                    // fallback: 取整個 section 的 text
                    const content = section.querySelector('.pv-shared-text-with-see-more, .display-flex.full-width');
                    if (content) return content.innerText?.trim() || '';
                }
            }
            // 方式2: 舊版
            const pv = document.querySelector('.pv-about-section .pv-about__summary-text');
            if (pv) return pv.innerText?.trim() || '';
            const pvAlt = document.querySelector('.pv-about-section');
            if (pvAlt) return pvAlt.innerText?.trim() || '';
            return '';
        })()
    """)
    return about_text or ""


async def _extract_work_history(page) -> List[WorkEntry]:
    """提取工作經歷"""
    entries = await page.evaluate("""
        (() => {
            const results = [];

            // 找 Experience section
            const expSection = document.querySelector('#experience');
            if (!expSection) return results;
            const section = expSection.closest('section');
            if (!section) return results;

            // 每個經歷項目
            const items = section.querySelectorAll(':scope > div > ul > li');
            for (const item of items) {
                // 檢查是否是多職位（同一公司）
                const subItems = item.querySelectorAll(':scope > div > div > ul > li');
                if (subItems.length > 0) {
                    // 多職位格式：公司在外層
                    const companyEl = item.querySelector('div > a > div > span > span:first-child, div > div > span > span:first-child');
                    const company = companyEl?.innerText?.trim() || '';

                    for (const sub of subItems) {
                        const spans = sub.querySelectorAll('div span.visually-hidden, div a span span:first-child');
                        const texts = [];
                        for (const s of spans) {
                            const t = s.innerText?.trim();
                            if (t) texts.push(t);
                        }
                        // 也嘗試取得直接的 span 文字
                        const allSpans = sub.querySelectorAll('span[aria-hidden="true"]');
                        const altTexts = [];
                        for (const s of allSpans) {
                            const t = s.innerText?.trim();
                            if (t && !t.includes('logo')) altTexts.push(t);
                        }
                        const combined = texts.length > altTexts.length ? texts : altTexts;
                        results.push({
                            company: company,
                            title: combined[0] || '',
                            duration: combined.find(t => /\\d{4}|present|至今|mos|yrs|年|月/.test(t.toLowerCase())) || '',
                            description: ''
                        });
                    }
                } else {
                    // 單職位格式
                    const allSpans = item.querySelectorAll('span[aria-hidden="true"]');
                    const texts = [];
                    for (const s of allSpans) {
                        const t = s.innerText?.trim();
                        if (t && !t.includes('logo')) texts.push(t);
                    }
                    if (texts.length >= 2) {
                        results.push({
                            company: texts[1] || '',
                            title: texts[0] || '',
                            duration: texts.find(t => /\\d{4}|present|至今|mos|yrs|年|月/.test(t.toLowerCase())) || '',
                            description: ''
                        });
                    }
                }
            }
            return results;
        })()
    """)

    return [WorkEntry(**e) for e in (entries or [])]


async def _extract_education(page) -> List[EducationEntry]:
    """提取教育背景"""
    entries = await page.evaluate("""
        (() => {
            const results = [];
            const eduSection = document.querySelector('#education');
            if (!eduSection) return results;
            const section = eduSection.closest('section');
            if (!section) return results;

            const items = section.querySelectorAll(':scope > div > ul > li');
            for (const item of items) {
                const spans = item.querySelectorAll('span[aria-hidden="true"]');
                const texts = [];
                for (const s of spans) {
                    const t = s.innerText?.trim();
                    if (t && !t.includes('logo')) texts.push(t);
                }
                if (texts.length >= 1) {
                    // 通常: [學校, 學位+科系, 時間]
                    const school = texts[0] || '';
                    let degree = '';
                    let fieldOfStudy = '';
                    if (texts.length >= 2) {
                        const degreeField = texts[1] || '';
                        if (degreeField.includes(',')) {
                            const parts = degreeField.split(',');
                            degree = parts[0].trim();
                            fieldOfStudy = parts.slice(1).join(',').trim();
                        } else {
                            degree = degreeField;
                        }
                    }
                    const duration = texts.find(t => /\\d{4}/.test(t)) || '';
                    results.push({ school, degree, field_of_study: fieldOfStudy, duration });
                }
            }
            return results;
        })()
    """)

    return [EducationEntry(**e) for e in (entries or [])]


async def _extract_skills(page) -> List[str]:
    """提取技能"""
    skills = await page.evaluate("""
        (() => {
            const results = [];

            // 方式1: Skills section
            const skillsSection = document.querySelector('#skills');
            if (skillsSection) {
                const section = skillsSection.closest('section');
                if (section) {
                    const items = section.querySelectorAll('li span[aria-hidden="true"]');
                    for (const item of items) {
                        const t = item.innerText?.trim();
                        if (t && t.length < 60 && !t.includes('endorsement') &&
                            !t.includes('Show all') && !/^\\d+$/.test(t)) {
                            results.push(t);
                        }
                    }
                }
            }

            // 方式2: 舊版 selector
            if (results.length === 0) {
                const oldSkills = document.querySelectorAll(
                    '.pv-skill-categories-section .pv-skill-category-entity__name span'
                );
                for (const s of oldSkills) {
                    const t = s.innerText?.trim();
                    if (t) results.push(t);
                }
            }

            // 去重
            return [...new Set(results)];
        })()
    """)

    return skills or []


# ──────────────────────────────────────────
# PDF 生成 (fpdf2)
# ──────────────────────────────────────────
def _find_font() -> Optional[str]:
    """找到可用的中文字型"""
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def generate_pdf(profile: ProfileData, output_path: str) -> str:
    """用 fpdf2 生成格式化的 A4 PDF"""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # 載入中文字型
    font_path = _find_font()
    font_family = "Arial"
    if font_path:
        try:
            pdf.add_font("CJK", "", font_path, uni=True)
            pdf.add_font("CJK", "B", font_path, uni=True)
            font_family = "CJK"
            print(f"  [pdf] Using font: {font_path}")
        except Exception as e:
            print(f"  [pdf] Font load failed ({e}), falling back to Arial")
    else:
        print("  [pdf] No CJK font found, using Arial (Chinese may not render)")

    def set_title_font():
        pdf.set_font(font_family, "B", 20)

    def set_section_font():
        pdf.set_font(font_family, "B", 13)

    def set_body_font():
        pdf.set_font(font_family, "", 10)

    def set_small_font():
        pdf.set_font(font_family, "", 9)

    def add_section_header(title: str):
        pdf.ln(4)
        set_section_font()
        pdf.set_fill_color(41, 98, 255)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(0, 8, f"  {title}", fill=True, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(2)

    # === Header ===
    set_title_font()
    pdf.cell(0, 12, profile.name or "Unknown", new_x="LMARGIN", new_y="NEXT")

    if profile.headline:
        pdf.set_font(font_family, "", 12)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(0, 7, profile.headline, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

    if profile.location:
        set_small_font()
        pdf.set_text_color(120, 120, 120)
        pdf.cell(0, 5, profile.location, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

    if profile.linkedin_url:
        set_small_font()
        pdf.set_text_color(0, 102, 204)
        pdf.cell(0, 5, profile.linkedin_url, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

    pdf.ln(3)
    # 分隔線
    pdf.set_draw_color(200, 200, 200)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(3)

    # === About ===
    if profile.about:
        add_section_header("About / Summary")
        set_body_font()
        pdf.multi_cell(0, 5, profile.about)
        pdf.ln(2)

    # === Work History ===
    if profile.work_history:
        add_section_header("Work Experience")
        for i, w in enumerate(profile.work_history):
            set_body_font()
            pdf.set_font(font_family, "B", 10)
            title_text = w.title or "N/A"
            pdf.cell(0, 6, title_text, new_x="LMARGIN", new_y="NEXT")

            set_body_font()
            if w.company:
                pdf.cell(0, 5, w.company, new_x="LMARGIN", new_y="NEXT")
            if w.duration:
                set_small_font()
                pdf.set_text_color(100, 100, 100)
                pdf.cell(0, 5, w.duration, new_x="LMARGIN", new_y="NEXT")
                pdf.set_text_color(0, 0, 0)
            if w.description:
                set_small_font()
                pdf.multi_cell(0, 4.5, w.description)

            if i < len(profile.work_history) - 1:
                pdf.ln(2)

    # === Education ===
    if profile.education:
        add_section_header("Education")
        for i, e in enumerate(profile.education):
            pdf.set_font(font_family, "B", 10)
            pdf.cell(0, 6, e.school or "N/A", new_x="LMARGIN", new_y="NEXT")

            set_body_font()
            degree_text = e.degree
            if e.field_of_study:
                degree_text += f", {e.field_of_study}"
            if degree_text:
                pdf.cell(0, 5, degree_text, new_x="LMARGIN", new_y="NEXT")
            if e.duration:
                set_small_font()
                pdf.set_text_color(100, 100, 100)
                pdf.cell(0, 5, e.duration, new_x="LMARGIN", new_y="NEXT")
                pdf.set_text_color(0, 0, 0)

            if i < len(profile.education) - 1:
                pdf.ln(2)

    # === Skills ===
    if profile.skills:
        add_section_header("Skills")
        set_body_font()
        # 每行顯示多個技能
        skills_text = "  |  ".join(profile.skills)
        pdf.multi_cell(0, 5, skills_text)

    # Footer
    pdf.ln(5)
    set_small_font()
    pdf.set_text_color(150, 150, 150)
    from datetime import datetime
    pdf.cell(0, 4, f"Generated from LinkedIn on {datetime.now().strftime('%Y-%m-%d %H:%M')}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)

    pdf.output(output_path)
    print(f"  [pdf] Saved: {output_path}")
    return output_path


# ──────────────────────────────────────────
# HR API 上傳
# ──────────────────────────────────────────
def upload_resume_pdf(candidate_id: int, pdf_path: str, api_key: str) -> bool:
    """上傳 PDF 到 HR 系統的 resume-parse endpoint"""
    url = f"{API_BASE}/api/candidates/{candidate_id}/resume-parse"
    print(f"  [upload] POST {url}")

    result = subprocess.run([
        "curl", "-s", "-X", "POST", url,
        "-H", f"Authorization: Bearer {api_key}",
        "-F", f"file=@{pdf_path};type=application/pdf",
        "-F", "format=linkedin",
        "-F", "uploaded_by=linkedin-scraper",
        "-w", "\n%{http_code}"
    ], capture_output=True, text=True, timeout=30)

    lines = result.stdout.strip().split("\n")
    status_code = lines[-1] if lines else "???"
    body = "\n".join(lines[:-1]) if len(lines) > 1 else ""

    if status_code.startswith("2"):
        print(f"  [upload] Resume PDF uploaded ({status_code})")
        return True
    else:
        print(f"  [upload] Resume upload failed ({status_code}): {body[:200]}")
        return False


def patch_candidate_data(candidate_id: int, profile: ProfileData, api_key: str) -> bool:
    """PATCH 候選人的 work_history 和 education_details"""
    url = f"{API_BASE}/api/candidates/{candidate_id}"
    print(f"  [patch] PATCH {url}")

    # 組裝 work_history
    work_history = []
    for w in profile.work_history:
        work_history.append({
            "company": w.company,
            "title": w.title,
            "duration": w.duration,
            "description": w.description,
        })

    # 組裝 education_details
    education_details = []
    for e in profile.education:
        education_details.append({
            "school": e.school,
            "degree": e.degree,
            "field_of_study": e.field_of_study,
            "duration": e.duration,
        })

    payload = {
        "work_history": work_history,
        "education_details": education_details,
    }

    result = subprocess.run([
        "curl", "-s", "-X", "PATCH", url,
        "-H", f"Authorization: Bearer {api_key}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps(payload, ensure_ascii=False),
        "-w", "\n%{http_code}"
    ], capture_output=True, text=True, timeout=30)

    lines = result.stdout.strip().split("\n")
    status_code = lines[-1] if lines else "???"
    body = "\n".join(lines[:-1]) if len(lines) > 1 else ""

    if status_code.startswith("2"):
        print(f"  [patch] Candidate data updated ({status_code})")
        return True
    else:
        print(f"  [patch] Candidate update failed ({status_code}): {body[:200]}")
        return False


# ──────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────
async def _scrape_profile(linkedin_url: str) -> ProfileData:
    """連接 Chrome CDP，抓取 LinkedIn profile"""
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
        except Exception as e:
            print(f"ERROR: Cannot connect to Chrome CDP on port 9222")
            print(f"Start Chrome with: --remote-debugging-port=9222")
            raise ConnectionError(f"CDP connection failed: {e}")

        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        # 導航到 LinkedIn profile
        print(f"  [nav] Going to {linkedin_url}")
        await page.goto(linkedin_url, wait_until="domcontentloaded")
        await human_delay(PAGE_LOAD_MIN, PAGE_LOAD_MAX)

        # 關閉浮層
        await dismiss_overlays(page)
        await human_delay(0.5, 1.0)

        # 滾動展開所有 section
        await scroll_to_load_all(page)

        # 再次關閉可能出現的浮層
        await dismiss_overlays(page)

        # 抓取結構化資料
        profile = await extract_profile_data(page, linkedin_url)

        return profile


async def _async_scrape_and_upload(
    linkedin_url: str,
    candidate_id: int,
    api_key: str,
) -> Dict:
    """async 版本的完整流程"""
    print(f"\n{'='*60}")
    print(f"LinkedIn Profile Scraper")
    print(f"URL: {linkedin_url}")
    print(f"Candidate ID: {candidate_id}")
    print(f"{'='*60}\n")

    # Step 1: 抓取 profile
    print("[Step 1/4] Scraping LinkedIn profile...")
    profile = await _scrape_profile(linkedin_url)

    if not profile.name:
        print("WARNING: Could not extract name from profile")

    # Step 2: 生成 PDF
    print("\n[Step 2/4] Generating PDF...")
    safe_name = (profile.name or "unknown").replace(" ", "_").replace("/", "_")
    pdf_filename = f"{candidate_id}_{safe_name}_linkedin.pdf"
    pdf_path = str(PDF_DIR / pdf_filename)
    generate_pdf(profile, pdf_path)

    # Step 3: 上傳 PDF
    print("\n[Step 3/4] Uploading resume PDF...")
    upload_ok = upload_resume_pdf(candidate_id, pdf_path, api_key)

    # Step 4: PATCH 候選人資料
    print("\n[Step 4/4] Patching candidate data...")
    patch_ok = patch_candidate_data(candidate_id, profile, api_key)

    result = {
        "success": upload_ok or patch_ok,
        "profile": asdict(profile),
        "pdf_path": pdf_path,
        "upload_ok": upload_ok,
        "patch_ok": patch_ok,
    }

    print(f"\n{'='*60}")
    print(f"Done! Name: {profile.name}")
    print(f"  Work entries: {len(profile.work_history)}")
    print(f"  Education entries: {len(profile.education)}")
    print(f"  Skills: {len(profile.skills)}")
    print(f"  PDF: {pdf_path}")
    print(f"  Upload: {'OK' if upload_ok else 'FAILED'}")
    print(f"  Patch: {'OK' if patch_ok else 'FAILED'}")
    print(f"{'='*60}\n")

    return result


def scrape_and_upload(
    linkedin_url: str,
    candidate_id: int,
    api_key: str,
) -> Dict:
    """
    同步入口（可被 import 使用）

    Usage:
        from scripts.linkedin_profile_scraper import scrape_and_upload
        result = scrape_and_upload(
            "https://linkedin.com/in/xxx",
            candidate_id=3475,
            api_key="YOUR_KEY"
        )
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # 如果已在 async context，用 thread 執行
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(
                asyncio.run,
                _async_scrape_and_upload(linkedin_url, candidate_id, api_key),
            )
            return future.result(timeout=120)
    else:
        return asyncio.run(
            _async_scrape_and_upload(linkedin_url, candidate_id, api_key)
        )


# ──────────────────────────────────────────
# CLI 入口
# ──────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Scrape LinkedIn profile, generate PDF, upload to HR API"
    )
    parser.add_argument(
        "--linkedin-url", required=True,
        help="LinkedIn profile URL (e.g. https://linkedin.com/in/xxx)"
    )
    parser.add_argument(
        "--candidate-id", type=int, required=True,
        help="HR system candidate ID"
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("API_SECRET_KEY", ""),
        help="HR API key (or set API_SECRET_KEY env var)"
    )
    parser.add_argument(
        "--api-base",
        default=os.environ.get("API_BASE", "https://api-hr.step1ne.com"),
        help="HR API base URL"
    )

    args = parser.parse_args()

    if not args.api_key:
        print("ERROR: --api-key is required (or set API_SECRET_KEY env var)")
        sys.exit(1)

    # 覆寫全域 API_BASE
    global API_BASE
    API_BASE = args.api_base

    result = scrape_and_upload(
        linkedin_url=args.linkedin_url,
        candidate_id=args.candidate_id,
        api_key=args.api_key,
    )

    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()
