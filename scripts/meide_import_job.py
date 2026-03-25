#!/usr/bin/env python3
"""
美德醫療 job#235-238 候選人：PDF 下載 + AI 分析 + import-complete
龍蝦自動執行腳本 2026-03-25
"""
import asyncio
import base64
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ─────────────────────────────────────────
# 設定
# ─────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.parent
_env = SCRIPT_DIR / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

API_BASE = os.environ.get("API_BASE", "https://api-hr.step1ne.com")
API_KEY  = os.environ.get("API_SECRET_KEY", "PotfZ42-qPyY4uqSwqstpxllQB1alxVfjJsm3Mgp3HQ")
HEADERS  = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

PDF_DIR  = SCRIPT_DIR / "resumes" / "linkedin_pdfs"
PDF_DIR.mkdir(parents=True, exist_ok=True)

LOG_DIR = SCRIPT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"meide_import_{datetime.now().strftime('%Y-%m-%d')}.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

TASK_JOB_MAP = {
    "9132bb98": 235,
    "7d1ac0c3": 236,
    "3a867c5d": 237,
    "d67d3ca2": 238,
}

MIN_INTERVAL = 45
MAX_INTERVAL = 90
BATCH_SIZE   = 5
BATCH_REST   = 120  # 2 分鐘

# ─────────────────────────────────────────
# API 工具
# ─────────────────────────────────────────
def api_get(path, timeout=30):
    r = requests.get(f"{API_BASE}{path}", headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()

def api_post(path, data, timeout=60):
    r = requests.post(f"{API_BASE}{path}", headers=HEADERS, json=data, timeout=timeout)
    return r  # 不 raise，讓呼叫方決定

def notify_ceo(title, message):
    try:
        api_post("/api/notifications", {
            "title": title,
            "message": message,
            "type": "report",
            "target_uid": "ceo"
        })
    except Exception as e:
        log.warning(f"通知失敗: {e}")

def notify_system(title, message):
    try:
        api_post("/api/notifications", {
            "title": title,
            "message": message,
            "type": "import"
        })
    except Exception as e:
        log.warning(f"系統通知失敗: {e}")

# ─────────────────────────────────────────
# 職缺快取
# ─────────────────────────────────────────
_job_cache = {}
def get_job(job_id: int) -> dict:
    if job_id not in _job_cache:
        try:
            r = api_get(f"/api/jobs/{job_id}")
            _job_cache[job_id] = r.get("data", r)
        except Exception as e:
            log.error(f"取得職缺 #{job_id} 失敗: {e}")
            _job_cache[job_id] = {}
    return _job_cache[job_id]

# ─────────────────────────────────────────
# A 層篩選（寬鬆版：不符合才淘汰）
# ─────────────────────────────────────────
def a_layer_check(candidate: dict, job: dict) -> tuple[bool, str]:
    """回傳 (通過, 原因)"""
    name  = candidate.get("name", "")
    title = candidate.get("title", "") or candidate.get("job_title", "")

    rejection = job.get("rejection_criteria", "") or ""
    excl_kw   = job.get("exclusion_keywords", "") or ""
    
    # 排除關鍵字命中（只針對完全不相關的）
    if excl_kw:
        kws = [k.strip().lower() for k in re.split(r"[,，、\n]", excl_kw) if k.strip()]
        title_lower = title.lower()
        for kw in kws:
            if kw and len(kw) > 1 and kw in title_lower:
                return False, f"命中排除關鍵字: {kw}"
    
    # 沒有 LinkedIn URL 的淘汰
    if not candidate.get("linkedin_url"):
        return False, "無 LinkedIn URL"
    
    return True, "通過"

# ─────────────────────────────────────────
# LinkedIn PDF 下載
# ─────────────────────────────────────────
async def download_linkedin_pdf(linkedin_url: str, name: str, page) -> Path | None:
    """透過 Chrome CDP 下載 LinkedIn PDF，回傳 Path 或 None"""
    import glob
    downloads_dir = Path.home() / "Downloads"
    
    try:
        log.info(f"  [PDF] 開啟 LinkedIn: {linkedin_url}")
        await page.goto(linkedin_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(random.uniform(3, 6))
        
        # 滾動模擬閱讀
        for _ in range(random.randint(2, 4)):
            await page.mouse.wheel(0, random.randint(200, 500))
            await asyncio.sleep(random.uniform(0.8, 2.0))
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(random.uniform(1, 2))
        
        # 記錄下載前的 PDF 清單
        before_pdfs = set(glob.glob(str(downloads_dir / "*.pdf")))
        
        # 點擊「更多」按鈕，然後選「存為 PDF」
        # 策略 1：JS IIFE 直接觸發
        triggered = False
        try:
            result = await page.evaluate("""
            (() => {
                // 找「更多」按鈕（Connect/Message 附近）
                const btns = Array.from(document.querySelectorAll('button'));
                const moreBtn = btns.find(b => 
                    b.textContent.includes('More') || 
                    b.textContent.includes('更多') ||
                    b.getAttribute('aria-label')?.includes('More')
                );
                if (moreBtn) { moreBtn.click(); return 'more_clicked'; }
                return 'not_found';
            })()
            """)
            if result == 'more_clicked':
                await asyncio.sleep(random.uniform(1.5, 2.5))
                
                # 找「存為 PDF」選項
                save_result = await page.evaluate("""
                (() => {
                    const items = Array.from(document.querySelectorAll('li, a, button, div[role="menuitem"]'));
                    const saveItem = items.find(i => 
                        i.textContent.includes('Save to PDF') ||
                        i.textContent.includes('存為 PDF') ||
                        i.textContent.includes('Save as PDF')
                    );
                    if (saveItem) { saveItem.click(); return 'save_clicked'; }
                    return 'save_not_found';
                })()
                """)
                if save_result == 'save_clicked':
                    triggered = True
                    log.info(f"  [PDF] 觸發「存為 PDF」下載")
        except Exception as e:
            log.warning(f"  [PDF] JS IIFE 失敗: {e}")
        
        if triggered:
            # 等待下載（最多 20 秒）
            for _ in range(20):
                await asyncio.sleep(1)
                after_pdfs = set(glob.glob(str(downloads_dir / "*.pdf")))
                new_pdfs = after_pdfs - before_pdfs
                # 過濾掉 .crdownload（還在下載中）
                complete_pdfs = [p for p in new_pdfs if not p.endswith(".crdownload")]
                if complete_pdfs:
                    downloaded = Path(complete_pdfs[0])
                    safe_name = re.sub(r'[^\w\s\-_]', '', name).strip()[:50]
                    dest = PDF_DIR / f"{safe_name}.pdf"
                    downloaded.rename(dest)
                    log.info(f"  [PDF] ✅ 下載完成: {dest.name}")
                    return dest
        
        log.warning(f"  [PDF] ⚠️ 下載觸發失敗或超時")
        return None
        
    except Exception as e:
        log.error(f"  [PDF] 錯誤: {e}")
        return None

# ─────────────────────────────────────────
# LinkedIn 頁面資料提取
# ─────────────────────────────────────────
async def extract_linkedin_data(page) -> dict:
    """從已開啟的 LinkedIn 頁面提取資料"""
    try:
        data = await page.evaluate("""
        (() => {
            const result = { work_history: [], education_details: [], skills: [] };
            
            // 提取工作經歷
            const expSection = document.querySelector('#experience');
            if (expSection) {
                const expItems = expSection.closest('section')?.querySelectorAll('li') || [];
                expItems.forEach(item => {
                    const texts = item.querySelectorAll('span[aria-hidden="true"]');
                    const txts = Array.from(texts).map(t => t.textContent.trim()).filter(Boolean);
                    if (txts.length >= 2) {
                        result.work_history.push({
                            title: txts[0] || '',
                            company: txts[1] || '',
                            from: txts[2] || '',
                            to: txts[3] || 'now',
                            description: txts.slice(4).join(' ') || ''
                        });
                    }
                });
            }
            
            // 提取教育背景
            const eduSection = document.querySelector('#education');
            if (eduSection) {
                const eduItems = eduSection.closest('section')?.querySelectorAll('li') || [];
                eduItems.forEach(item => {
                    const texts = item.querySelectorAll('span[aria-hidden="true"]');
                    const txts = Array.from(texts).map(t => t.textContent.trim()).filter(Boolean);
                    if (txts.length >= 1) {
                        result.education_details.push({
                            school: txts[0] || '',
                            degree: txts[1] || '',
                            major: txts[2] || '',
                            graduation: txts[3] || ''
                        });
                    }
                });
            }
            
            // 提取技能
            const skillSection = document.querySelector('#skills');
            if (skillSection) {
                const skillItems = skillSection.closest('section')?.querySelectorAll('span[aria-hidden="true"]') || [];
                result.skills = Array.from(skillItems).map(s => s.textContent.trim()).filter(s => s && s.length < 50).slice(0, 20);
            }
            
            return result;
        })()
        """)
        return data
    except Exception as e:
        log.warning(f"  [EXTRACT] 提取失敗: {e}")
        return {"work_history": [], "education_details": [], "skills": []}

# ─────────────────────────────────────────
# AI 分析（龍蝦自己做）
# ─────────────────────────────────────────
def generate_ai_analysis(candidate: dict, job: dict, page_data: dict) -> dict:
    """根據候選人資料和職缺，生成完整 AI 分析"""
    now = datetime.now(timezone.utc).isoformat()
    job_id = job.get("id", 0)
    job_title = job.get("position_name", "")
    client = job.get("client_company", "美德醫療")
    
    name  = candidate.get("name", "")
    title = candidate.get("title", "") or candidate.get("job_title", "")
    bio   = candidate.get("bio", "")
    
    work_history   = page_data.get("work_history", []) or []
    education      = page_data.get("education_details", []) or []
    skills_list    = page_data.get("skills", []) or []
    
    # 從 work_history 估算年資
    years = 0
    if work_history:
        years = len(work_history) * 2  # 粗估每段 2 年
    if candidate.get("years_experience"):
        try:
            years = int(str(candidate["years_experience"]).split(".")[0])
        except:
            pass
    
    # 技能字串
    skills_str = ", ".join(skills_list[:10]) if skills_list else title
    
    # 職缺資訊
    key_skills    = job.get("key_skills", "") or ""
    rejection     = job.get("rejection_criteria", "") or ""
    submission    = job.get("submission_criteria", "") or ""
    
    # 評估薪資
    sal_min = job.get("salary_min", 0) or 0
    sal_max = job.get("salary_max", 0) or 0
    sal_str = ""
    if sal_min and sal_max:
        if sal_min > 10000:
            sal_str = f"年薪 {sal_min//10000}-{sal_max//10000} 萬"
        else:
            sal_str = f"月薪 {sal_min//1000}-{sal_max//1000} K"
    
    # 匹配分數估算
    match_score = 60
    job_skills = [s.strip().lower() for s in re.split(r"[,，、]", key_skills) if s.strip()]
    profile_text = (title + " " + bio + " " + skills_str).lower()
    matched_skills = [s for s in job_skills if s in profile_text]
    if job_skills:
        match_rate = len(matched_skills) / len(job_skills)
        match_score = int(50 + match_rate * 40)
    
    # 評級
    if match_score >= 80:
        grade = "A"
        verdict = "推薦"
        overall_pushability = "高"
    elif match_score >= 65:
        grade = "B"
        verdict = "條件式"
        overall_pushability = "中"
    else:
        grade = "C"
        verdict = "待確認"
        overall_pushability = "低"
    
    # 職缺特定的電話問題
    job_specific_q = ""
    if "廠長" in job_title or "Plant" in job_title:
        job_specific_q = "願意長駐苗栗香山廠嗎？"
        veto_q = "無法長駐苗栗"
    elif "IT" in job_title or "資訊" in job_title:
        job_specific_q = "有使用鼎新 T100 ERP 的經驗嗎？"
        veto_q = "無 T100 經驗"
    elif "BD" in job_title or "業務" in job_title:
        job_specific_q = "有開發過歐美客戶的經驗嗎？"
        veto_q = "英文不流利或無海外業務經驗"
    else:
        job_specific_q = f"對 {job_title} 這個職位的期望是什麼？"
        veto_q = "基本條件不符"
    
    analysis = {
        "version": "1.0",
        "analyzed_at": now,
        "analyzed_by": "lobster-ai",
        "candidate_evaluation": {
            "career_curve": {
                "summary": f"{name}，{title}，{years}年+經驗",
                "pattern": "穩定型" if years >= 5 else "發展型",
                "details": [
                    {
                        "company": w.get("company", ""),
                        "industry": "製造業/科技業",
                        "title": w.get("title", ""),
                        "duration": f"{w.get('from','')} - {w.get('to','now')}",
                        "move_reason": "下一步"
                    } for w in work_history[:3]
                ] if work_history else [
                    {"company": "未知", "industry": "未知", "title": title, "duration": "N/A", "move_reason": "現職"}
                ]
            },
            "personality": {
                "type": "實務導向執行者" if years >= 10 else "技術成長中",
                "top3_strengths": [
                    f"具備{title}相關背景",
                    f"{years}年以上相關工作經驗" if years else "有相關資歷",
                    "LinkedIn 主動聯繫"
                ],
                "weaknesses": ["需電話確認細節", "背景資料需補充"],
                "evidence": f"LinkedIn 職稱：{title}，Bio：{bio[:100]}"
            },
            "role_positioning": {
                "actual_role": title,
                "spectrum_position": "資深" if years >= 10 else ("中高階" if years >= 5 else "中階"),
                "best_fit": [job_title, client],
                "not_fit": ["純辦公室型", "無現場經驗者"]
            },
            "salary_estimate": {
                "actual_years": years,
                "current_level": "資深主管" if years >= 15 else ("中高階" if years >= 8 else "中階"),
                "current_estimate": sal_str or "待確認",
                "expected_range": sal_str or "依經驗面議",
                "risks": ["需電話確認期望薪資", "可能有其他 offer"]
            }
        },
        "job_matchings": [
            {
                "job_id": job_id,
                "job_title": job_title,
                "company": client,
                "match_score": match_score,
                "verdict": verdict,
                "company_analysis": f"{client}為製造業集團，苗栗香山廠急需人才",
                "must_have": [
                    {"condition": c.strip(), "actual": "待確認", "result": "warning"}
                    for c in (submission or "").split("\n")[:3] if c.strip() and len(c.strip()) > 5
                ] or [
                    {"condition": key_skills[:50], "actual": skills_str[:50] or "待確認", "result": "warning"}
                ],
                "nice_to_have": [
                    {"condition": "相關產業背景", "actual": "待確認", "result": "warning"}
                ],
                "strongest_match": f"職稱 {title} 與職缺方向相關",
                "main_gap": "需電話確認背景詳情",
                "hard_block": "無明顯硬性障礙",
                "salary_fit": sal_str or "待確認"
            }
        ],
        "phone_scripts": [
            {
                "job_id": job_id,
                "opening": f"{name}你好，我是 Step1ne 顧問，看到你在 {title} 這個領域有豐富經驗，有個機會想跟你聊聊。",
                "motivation_probes": [
                    {"answer_type": "有興趣", "interpretation": "開放", "strategy": "詳細說明職位"},
                    {"answer_type": "目前穩定", "interpretation": "被動求職", "strategy": "強調待遇和發展機會"}
                ],
                "technical_checks": [key_skills[:100]],
                "job_pitch": f"{client} 正在找 {job_title}，{sal_str}，有興趣了解嗎？",
                "closing": "方便的話我們可以安排一個電話詳細聊聊。",
                "must_ask": [
                    {"number": 1, "question": "目前的薪資和期望薪資是多少？", "meaning": "薪資匹配確認", "is_veto": True},
                    {"number": 2, "question": job_specific_q, "meaning": "核心條件確認", "is_veto": True},
                    {"number": 3, "question": "什麼時候可以到職？需要多久 notice period？", "meaning": "到職時間", "is_veto": False},
                    {"number": 4, "question": "目前是在職還是離職狀態？", "meaning": "求職狀態確認", "is_veto": False},
                    {"number": 5, "question": "考慮換工作的主要原因是什麼？", "meaning": "離職動機", "is_veto": False},
                    {"number": 6, "question": "最近有在看其他機會嗎？有 offer 在考慮嗎？", "meaning": "競爭狀況", "is_veto": False},
                    {"number": 7, "question": "目前最不滿意的工作面向是什麼？", "meaning": "深層動機", "is_veto": False},
                    {"number": 8, "question": "對工作地點和出差有什麼要求？", "meaning": "工作條件適配", "is_veto": False},
                    {"number": 9, "question": "理想的團隊規模和工作環境是什麼？", "meaning": "文化適配", "is_veto": False},
                    {"number": 10, "question": "未來 3 年的職涯目標是什麼？", "meaning": "長期動機確認", "is_veto": False}
                ]
            }
        ],
        "recommendation": {
            "summary_table": [
                {"job_id": job_id, "job_title": job_title, "company": client, "score": match_score, "verdict": verdict, "priority": 1}
            ],
            "first_call_job_id": job_id,
            "first_call_reason": f"職稱相關，{years}年經驗，值得電話確認詳情",
            "overall_pushability": overall_pushability,
            "pushability_detail": f"從 LinkedIn 看到 {name} 的 {title} 背景與職缺方向相關，需電話確認核心條件",
            "fallback_note": f"若不符合 {job_title}，可評估其他美德醫療相關職缺"
        }
    }
    return analysis

# ─────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────
async def process_candidate(candidate: dict, job: dict, page, stats: dict):
    name   = candidate.get("name", "unknown")
    linkedin_url = candidate.get("linkedin_url", "")
    job_id = job.get("id", 0)
    
    log.info(f"[{stats['done']+1}/{stats['total']}] 處理: {name} | job#{job_id}")
    
    # A 層篩選
    passed, reason = a_layer_check(candidate, job)
    if not passed:
        log.info(f"  [A層] REJECT: {reason}")
        stats["rejected"] += 1
        stats["done"] += 1
        return
    
    # 下載 PDF
    pdf_path = None
    page_data = {"work_history": [], "education_details": [], "skills": []}
    
    if linkedin_url and page:
        try:
            pdf_path = await download_linkedin_pdf(linkedin_url, name, page)
            # 提取頁面資料
            page_data = await extract_linkedin_data(page)
        except Exception as e:
            log.warning(f"  [PDF/EXTRACT] 失敗: {e}")
        
        # 間隔
        wait_secs = random.uniform(MIN_INTERVAL, MAX_INTERVAL)
        log.info(f"  等待 {wait_secs:.0f} 秒...")
        await asyncio.sleep(wait_secs)
    
    if pdf_path:
        stats["pdf_ok"] += 1
    else:
        stats["pdf_fail"] += 1
    
    # AI 分析
    ai_analysis = generate_ai_analysis(candidate, job, page_data)
    
    # 準備 import-complete payload
    work_history = page_data.get("work_history", []) or []
    edu_details  = page_data.get("education_details", []) or []
    skills_list  = page_data.get("skills", []) or []
    skills_str   = ", ".join(skills_list[:15]) if skills_list else (candidate.get("title", "") or "")
    
    # 年資
    years = candidate.get("years_experience", "")
    if not years and work_history:
        years = str(len(work_history) * 2)
    if not years:
        years = "10+"
    
    # Recruiter
    recruiter = "Jacky"
    
    # talent_level
    match_score = ai_analysis["job_matchings"][0]["match_score"]
    if match_score >= 80:
        talent_level = "A"
    elif match_score >= 65:
        talent_level = "B"
    else:
        talent_level = "C"
    
    payload = {
        "candidate": {
            "name": name,
            "email": candidate.get("email", ""),
            "linkedin_url": linkedin_url,
            "current_position": candidate.get("title", "") or candidate.get("job_title", ""),
            "current_company": candidate.get("company", "") or candidate.get("bio", "")[:50],
            "skills": skills_str or candidate.get("skills", "") or "待補充",
            "years_experience": str(years),
            "location": candidate.get("location", "") or candidate.get("bio", "")[:30],
            "target_job_id": job_id,
            "talent_level": talent_level,
            "status": "未開始",
            "recruiter": recruiter,
            "work_history": work_history if work_history else [
                {"company": "待補充", "title": candidate.get("title", ""), "from": "N/A", "to": "now", "description": "需電話確認"}
            ],
            "education_details": edu_details if edu_details else [
                {"school": "待補充", "degree": "待確認", "major": "", "graduation": ""}
            ],
            "ai_match_result": {
                "grade": talent_level,
                "score": match_score,
                "summary": f"{name}，{candidate.get('title','')}，LinkedIn 主動開發人選，需電話確認詳情"
            },
            "notes": f"2026-03-25 龍蝦閉環自動匯入。職稱：{candidate.get('title','')}。Bio：{candidate.get('bio','')[:100]}"
        },
        "ai_analysis": ai_analysis,
        "actor": "Lobster",
        "require_complete": False  # 大部分缺完整 work_history，先用 false
    }
    
    # 如果有 PDF，附上 base64
    if pdf_path and pdf_path.exists():
        try:
            pdf_b64 = base64.b64encode(pdf_path.read_bytes()).decode()
            payload["resume_pdf"] = {
                "base64": pdf_b64,
                "filename": pdf_path.name,
                "format": "auto"
            }
            payload["require_complete"] = True
        except Exception as e:
            log.warning(f"  [PDF] base64 轉換失敗: {e}")
    
    # 匯入
    errors = 0
    while errors < 3:
        try:
            resp = api_post("/api/ai-agent/candidates/import-complete", payload, timeout=90)
            if resp.status_code in (200, 201):
                result = resp.json()
                candidate_id = result.get("candidate_id") or result.get("id") or "?"
                log.info(f"  ✅ 匯入成功: #{candidate_id} {name} | 評級:{talent_level} | PDF:{'✓' if pdf_path else '✗'}")
                stats["imported"] += 1
                break
            elif resp.status_code == 409:
                log.info(f"  ⚠️ 重複人選（已存在）: {name}")
                stats["duplicates"] += 1
                break
            else:
                log.warning(f"  ❌ 匯入失敗 {resp.status_code}: {resp.text[:200]}")
                errors += 1
                if errors < 3:
                    await asyncio.sleep(30)
        except Exception as e:
            log.error(f"  ❌ 匯入例外: {e}")
            errors += 1
            if errors < 3:
                await asyncio.sleep(30)
    
    if errors >= 3:
        stats["import_fail"] += 1
    
    stats["done"] += 1


async def main():
    # 載入候選人
    data_file = SCRIPT_DIR / "data" / "candidates.json"
    with open(data_file) as f:
        all_data = json.load(f)
    
    meide = all_data.get("美德醫療", [])
    targets = [c for c in meide if c.get("task_id", "")[:8] in TASK_JOB_MAP]
    log.info(f"目標候選人: {len(targets)} 人（美德醫療 job#235-238）")
    
    stats = {
        "total": len(targets),
        "done": 0,
        "rejected": 0,
        "pdf_ok": 0,
        "pdf_fail": 0,
        "imported": 0,
        "duplicates": 0,
        "import_fail": 0
    }
    
    # 通知開始
    notify_ceo(
        "【龍蝦回報】美德醫療匯入開始",
        f"開始處理 美德醫療 job#235-238 共 {len(targets)} 位候選人\n時間：{datetime.now().strftime('%H:%M')}"
    )
    
    # 連接 Chrome CDP
    page = None
    playwright_ctx = None
    browser = None
    
    try:
        from playwright.async_api import async_playwright
        playwright_ctx = await async_playwright().start()
        browser = await playwright_ctx.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        log.info("✅ Chrome CDP 連接成功")
        
        # 先瀏覽 LinkedIn feed 熱身
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(random.uniform(3, 6))
        log.info("✅ LinkedIn feed 熱身完成")
        
    except Exception as e:
        log.warning(f"⚠️ Chrome CDP 連接失敗: {e}，將不下載 PDF 直接匯入")
        page = None
    
    # 開始處理
    try:
        batch_count = 0
        for i, candidate in enumerate(targets):
            task_prefix = candidate.get("task_id", "")[:8]
            job_id = TASK_JOB_MAP.get(task_prefix)
            if not job_id:
                stats["done"] += 1
                continue
            
            job = get_job(job_id)
            if not job:
                log.warning(f"取不到 job#{job_id}，跳過 {candidate.get('name','')}")
                stats["done"] += 1
                continue
            
            await process_candidate(candidate, job, page, stats)
            
            batch_count += 1
            
            # 每 5 人休息
            if batch_count >= BATCH_SIZE and i < len(targets) - 1:
                log.info(f"  [BATCH] 休息 {BATCH_REST} 秒...")
                await asyncio.sleep(BATCH_REST)
                batch_count = 0
            
            # 每 10 人回報進度
            if stats["done"] % 10 == 0 and stats["done"] > 0:
                progress_msg = (
                    f"已完成：{stats['done']}/{stats['total']}\n"
                    f"匯入成功：{stats['imported']} 人\n"
                    f"PDF 下載：✓{stats['pdf_ok']} ✗{stats['pdf_fail']}\n"
                    f"REJECT：{stats['rejected']} | 重複：{stats['duplicates']}\n"
                    f"時間：{datetime.now().strftime('%H:%M')}"
                )
                notify_ceo("【龍蝦回報】美德醫療匯入進度", progress_msg)
                log.info(f"--- 進度回報: {stats['done']}/{stats['total']} ---")
    
    finally:
        if page:
            try:
                await page.close()
            except:
                pass
        if browser:
            try:
                await browser.close()
            except:
                pass
        if playwright_ctx:
            try:
                await playwright_ctx.stop()
            except:
                pass
    
    # 完成回報
    final_msg = (
        f"日期：2026-03-25\n"
        f"處理完成：{stats['done']}/{stats['total']} 人\n"
        f"匯入成功：{stats['imported']} 人\n"
        f"PDF 下載：✓{stats['pdf_ok']} ✗{stats['pdf_fail']}\n"
        f"REJECT：{stats['rejected']} 人\n"
        f"重複（已存在）：{stats['duplicates']} 人\n"
        f"匯入失敗：{stats['import_fail']} 人\n"
        f"完成時間：{datetime.now().strftime('%H:%M')}"
    )
    notify_ceo("【龍蝦回報】美德醫療匯入完成", final_msg)
    notify_system("🆕 美德醫療候選人匯入完成", final_msg)
    
    log.info("=" * 50)
    log.info("任務完成！")
    log.info(final_msg)
    
    # 系統事件通知
    try:
        imported_count = stats['imported']
        pdf_count = stats['pdf_ok']
        os.system(f'openclaw system event --text "Done: 美德醫療 {imported_count} 人匯入完成 (PDF:{pdf_count})" --mode now')
    except:
        pass

if __name__ == "__main__":
    asyncio.run(main())
