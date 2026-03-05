# HeadHunter Crawler — 獨立爬蟲模組技術架構文件

> 版本: v1.0 | 建立日期: 2026-03-04 | **全部完成: 2026-03-05**

---

## 目錄

1. [專案概述](#1-專案概述)
2. [技術決策](#2-技術決策)
3. [系統架構圖](#3-系統架構圖)
4. [專案目錄結構](#4-專案目錄結構)
5. [模組詳細設計](#5-模組詳細設計)
6. [資料流程](#6-資料流程)
7. [Google Sheets 儲存設計](#7-google-sheets-儲存設計)
8. [Web UI 設計](#8-web-ui-設計)
9. [REST API 規格](#9-rest-api-規格)
10. [Step1ne 系統整合](#10-step1ne-系統整合)
11. [反偵測機制](#11-反偵測機制)
12. [OCR 模組設計](#12-ocr-模組設計)
13. [併發與排程架構](#13-併發與排程架構)
14. [現有程式碼漏洞與修復](#14-現有程式碼漏洞與修復)
15. [實施計畫（9 階段）](#15-實施計畫9-階段)
16. [製作流程](#16-製作流程)
17. [驗證方式](#17-驗證方式)
18. [來源檔案對照表](#18-來源檔案對照表)
19. [安裝與部署](#19-安裝與部署)

---

## 1. 專案概述

### 目的

從現有 Step1ne Headhunter System 中抽離爬蟲功能，建成一個**純 Python、本地運行、不接 AI** 的獨立自動化工具。

### 核心功能

- 透過 Flask Web UI 設定搜尋條件與排程
- LinkedIn + GitHub 雙來源人才搜尋
- OCR 圖像辨識（截圖提取、履歷圖片、CAPTCHA）
- 結果存入 Google Sheets（多客戶分離）
- 支援一台電腦同時爬多個職缺（多進程併發）
- 可選擇性連結 Step1ne 系統 API 拉取職缺 / 回推候選人
- 模組完全獨立，不整合進現有系統

### 設計原則

- **零 AI 依賴** — 純自動化腳本，不呼叫任何 LLM API
- **設定外部化** — 所有參數抽到 YAML，不硬編碼
- **可選整合** — Step1ne 系統 API 連結完全可選，不連也能獨立運作
- **多人共用** — 多同事可各自啟動，共寫同一份 Google Sheets

---

## 2. 技術決策

| 項目 | 決定 | 理由 |
|------|------|------|
| 語言 | Python 3.10+ | 與原始爬蟲腳本一致，生態豐富 |
| Web 框架 | Flask + Jinja2 | 輕量、快速開發、適合本地工具 |
| 前端 | Tailwind CSS CDN + Chart.js | 無需 build step，儀表板風格 |
| 儲存 | Google Sheets (gspread) | 雲端共享、免費、多人可同時查看 |
| 瀏覽器自動化 | Playwright (Chromium) | 反偵測能力強、穩定、支援 headless |
| 排程 | APScheduler | 純 Python、支援 cron/interval/date |
| 併發 | multiprocessing.Pool | 各 worker 自帶 browser，進程隔離 |
| OCR | pytesseract + Pillow | 開源、支援中英文、免費 |
| 反偵測 | playwright-stealth | 覆蓋 10+ 瀏覽器指紋點 |
| 設定 | YAML (PyYAML) | 人類可讀、比 JSON 更適合設定檔 |
| AI | **不接** | 純自動化，降低複雜度和成本 |

### 依賴套件

```
flask>=3.0
flask-cors>=4.0
gspread>=6.0
google-auth>=2.0
playwright>=1.40.0
apscheduler>=3.10
pyyaml>=6.0
pytesseract>=0.3.10
Pillow>=10.0
playwright-stealth>=1.0.6
```

---

## 3. 系統架構圖

### 主架構

```
┌───────────────────────────────────────────────────────────────┐
│  Flask Web Server (localhost:5000)                             │
│                                                               │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────────┐    │
│  │   Web UI     │  │  REST API    │  │  Task Manager   │    │
│  │  (Jinja2 +   │  │  (Flask      │  │  (APScheduler)  │    │
│  │  Tailwind +  │  │   Blueprint) │  │                 │    │
│  │  Chart.js)   │  │              │  │  cron / interval│    │
│  └──────┬───────┘  └──────┬───────┘  └───────┬─────────┘    │
│         │                 │                   │              │
│         └─────────────────┼───────────────────┘              │
│                           │                                   │
│  ┌────────────────────────┴────────────────────────────────┐ │
│  │  Process Pool (multiprocessing.Pool, N workers)         │ │
│  │                                                         │ │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐    │ │
│  │  │  Worker 1   │  │  Worker 2   │  │  Worker 3   │    │ │
│  │  │             │  │             │  │             │    │ │
│  │  │ +Playwright │  │ +Playwright │  │ +Playwright │    │ │
│  │  │  Browser    │  │  Browser    │  │  Browser    │    │ │
│  │  │ +OCR Engine │  │ +OCR Engine │  │ +OCR Engine │    │ │
│  │  │             │  │             │  │             │    │ │
│  │  │ 職缺 A 爬蟲 │  │ 職缺 B 爬蟲 │  │ 職缺 C 爬蟲 │    │ │
│  │  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘    │ │
│  │         └────────────────┼────────────────┘            │ │
│  │                          ▼                              │ │
│  │              POST /api/internal/results                 │ │
│  │              (Worker → Flask 統一寫入)                   │ │
│  └─────────────────────────────────────────────────────────┘ │
│                           │                                   │
│                           ▼                                   │
│                  Google Sheets API                             │
│                  (gspread 批次寫入)                            │
└───────────────────────────────────────────────────────────────┘
       │                │                        │
       ▼                ▼                        ▼
┌────────────┐  ┌──────────────────┐   ┌──────────────────┐
│  Step1ne   │  │    LinkedIn      │   │   GitHub API     │
│  系統 API  │  │                  │   │                  │
│            │  │  搜尋 4 層備援：  │   │  REST API 搜尋   │
│ GET /jobs  │  │  1. Playwright   │   │  多 token 輪換   │
│ POST /cand │  │  2. Google urllib│   │  並行抓取詳細     │
│  (可選)    │  │  3. Bing urllib  │   │                  │
└────────────┘  │  4. Brave API    │   └──────────────────┘
                └──────────────────┘
```

### 多人共用架構

```
同事 A (本機) ──各自的 Flask──→ gspread ──→ 同一份 Google Sheets
同事 B (本機) ──各自的 Flask──→ gspread ──↗
                                                │
                              Step1ne 系統定時讀取 Sheets
                              （直接用 gspread 或 Sheets API）
```

---

## 4. 專案目錄結構

```
headhunter-crawler/
│
├── ARCHITECTURE.md              # 本文件：完整技術架構 + 實施計畫
├── app.py                       # Flask 應用入口
├── requirements.txt             # Python 依賴套件
│
├── config/
│   ├── default.yaml             # 所有可配置參數（延遲、超時、語言集等）
│   ├── skills_synonyms.yaml     # 技能同義詞字典（300+ 映射）
│   └── user_agents.txt          # 33 個 User-Agent 字串
│
├── crawler/
│   ├── __init__.py
│   ├── engine.py                # 搜尋引擎主控（整合 LinkedIn + GitHub + OCR）
│   ├── linkedin.py              # LinkedIn 4 層搜尋（Playwright→Google→Bing→Brave）
│   ├── github.py                # GitHub API 搜尋（多 token 輪換）
│   ├── ocr.py                   # OCR 模組（截圖辨識、履歷圖片、CAPTCHA）
│   ├── profile_reader.py        # 個人檔案讀取（GitHub / LinkedIn 頁面）
│   ├── anti_detect.py           # 反偵測（UA輪換、延遲、CAPTCHA偵測、代理、stealth）
│   ├── browser_pool.py          # Playwright 瀏覽器池管理
│   └── dedup.py                 # 持久化去重快取（JSON）
│
├── storage/
│   ├── __init__.py
│   ├── sheets_store.py          # Google Sheets CRUD（gspread）
│   └── models.py                # 資料類別（Candidate, SearchTask, ProcessedRecord）
│
├── integration/
│   ├── __init__.py
│   └── step1ne_client.py        # Step1ne 系統 API 客戶端（讀職缺、回寫候選人）
│
├── scheduler/
│   ├── __init__.py
│   ├── task_manager.py          # APScheduler 排程管理
│   └── worker.py                # 爬蟲 worker 進程
│
├── api/
│   ├── __init__.py
│   └── routes.py                # REST API（Flask Blueprint）
│
├── web/
│   ├── __init__.py
│   ├── views.py                 # Flask 頁面路由
│   ├── templates/
│   │   ├── base.html            # 基礎模板（導航列、Tailwind CDN）
│   │   ├── dashboard.html       # 總覽儀表板
│   │   ├── tasks.html           # 任務管理 + 新增 Modal
│   │   ├── results.html         # 結果瀏覽 + 篩選
│   │   └── settings.html        # 系統設定
│   └── static/
│       ├── css/
│       │   └── custom.css       # 自訂樣式
│       └── js/
│           └── app.js           # 前端互動邏輯
│
├── data/
│   ├── dedup_cache.json         # 去重快取（自動產生）
│   ├── tasks.json               # 排程任務設定（自動產生）
│   └── checkpoints.json         # 斷點續爬（自動產生）
│
└── logs/
    └── crawler.log              # 日誌（rotating，最多 5 個備份 x 10MB）
```

---

## 5. 模組詳細設計

### 5.1 `crawler/anti_detect.py` — 反偵測工具

**來源提取：**
- `search-plan-executor.py`: USER_AGENTS(L34-40), get_browser_headers()(L47-58), anti_scraping_delay()(L99-100), is_captcha_page()(L102-106), _SSL_CTX(L43-45)
- `profile-reader.py`: STEALTH_JS(L40-64), _human_delay()(L73-76), _human_scroll()(L79-98), _random_mouse_wiggle()(L101-114)

**功能：**
```python
class AntiDetect:
    def __init__(self, config: dict):
        self.user_agents: list       # 從 user_agents.txt 載入 33+
        self.proxy_list: list        # 代理列表
        self.proxy_strategy: str     # round_robin / random
        self.ssl_verify: bool        # 可配置 SSL 驗證
        self._proxy_index: int = 0   # 代理輪換索引
        self._backoff_state: dict    # 指數退避狀態

    # HTTP 工具
    def get_ssl_context(self) -> ssl.SSLContext
    def get_browser_headers(self, extra=None) -> dict
    def get_random_ua(self) -> str
    def get_next_proxy(self) -> Optional[str]
    def http_get(self, url, extra_headers=None) -> tuple[str, int]
    def http_get_json(self, url, extra_headers=None) -> tuple[dict, int]

    # 延遲 & 退避
    def request_delay(self)                          # 請求間隔
    def page_delay(self)                             # 翻頁間隔
    def candidate_delay(self)                        # 候選人間隔
    def exponential_backoff(self, attempt: int)      # 指數退避（取代固定 15s）

    # CAPTCHA 偵測
    def is_captcha_page(self, text: str) -> bool     # 從 config 載入指標

    # Playwright 反偵測
    STEALTH_JS: str                                  # 注入 JS 腳本
    def human_delay(self, min_s=1.5, max_s=4.0)
    def human_scroll(self, page, distance=None)
    def random_mouse_wiggle(self, page)
    def apply_stealth(self, context)                 # 整合 playwright-stealth
```

**改進（vs 原始碼）：**
| 原始 | 改進 |
|------|------|
| 5 個 UA 硬編碼 | 33+ 個從檔案載入 |
| SSL 永久停用 (`CERT_NONE`) | 可配置，預設啟用 |
| 固定 `sleep(15)` on 429 | 指數退避 2^n，上限 120s |
| 無代理支援 | 代理列表 + round_robin/random 輪換 |
| 僅蓋 `webdriver` 一個屬性 | 整合 playwright-stealth 覆蓋 10+ 指紋 |
| 用 `print()` 輸出 | Python logging 模組 |

---

### 5.2 `crawler/linkedin.py` — LinkedIn 搜尋

**來源提取（全部從 search-plan-executor.py）：**
- `build_linkedin_query()` (L320-356)
- `expand_skill_synonyms()` (L310-318)
- `search_linkedin_via_playwright()` (L483-564)
- `search_linkedin_via_google()` (L570-606)
- `search_linkedin_via_bing()` (L609-637)
- `search_linkedin_via_brave()` (L640-687)
- `search_linkedin_with_fallback()` (L690-750)
- `clean_linkedin_url()` (L407-418)
- `extract_linkedin_urls_from_html()` (L440-477)

**功能：**
```python
class LinkedInSearcher:
    def __init__(self, config, anti_detect, ocr=None, skill_synonyms=None):
        ...

    # 查詢建構
    def build_query(self, skills, location) -> str
    def expand_skill_synonyms(self, skill) -> list    # 從 YAML 載入

    # URL 工具
    @staticmethod
    def clean_url(href) -> Optional[str]
    @staticmethod
    def extract_urls_from_html(html) -> list

    # 4 層搜尋
    def search_via_playwright(self, skills, location, pages, browser=None) -> dict
    def search_via_google(self, skills, location, pages) -> dict
    def search_via_bing(self, skills, location, pages) -> dict
    def search_via_brave(self, skills, brave_key, location, pages) -> dict
    def search_with_fallback(self, skills, location_zh, location_en, pages, brave_key) -> dict

    # 進度回呼
    on_progress: Callable   # 回報 (current_page, total_pages, found_count)
```

**4 層備援流程：**
```
Playwright (真實 Chrome) → 成功 ────→ 結果
    ↓ 失敗/結果不足
Google urllib ──────────→ 成功 ────→ 結果
    ↓ CAPTCHA/結果不足
Bing urllib ───────────→ 成功 ────→ 補充結果
    ↓
Brave API (有 key 時) ─→ 補充 ────→ 合併結果
```

**改進：**
- 地區不再硬編碼 "Taiwan"，從任務設定讀取
- 同義詞從 YAML 載入（不硬編碼 dict）
- 429 用指數退避（不再固定 15 秒）
- CAPTCHA 偵測後通知用戶（不只靜默停止）
- **被登入牆擋住時，截圖 + OCR 提取可見資訊**
- 接受外部 browser context（從瀏覽器池取用）

---

### 5.3 `crawler/github.py` — GitHub 搜尋

**來源提取（全部從 search-plan-executor.py）：**
- `get_github_headers()` (L124-128)
- `check_github_rate_limit()` (L130-134)
- `_github_search_page()` (L139-157)
- `fetch_github_user_detail()` (L159-191)
- `search_github_users()` (L193-230)
- `build_github_queries()` (L369-401)
- `_GITHUB_LANGUAGES` (L359-364)

**功能：**
```python
class GitHubSearcher:
    def __init__(self, config, anti_detect):
        self.tokens: list          # 多 token 輪換
        self._current_token_idx: int = 0

    # Token 管理
    def get_headers(self) -> dict
    def rotate_token(self)                  # 403 時切換下一個 token
    def check_rate_limit(self) -> tuple

    # 搜尋
    def build_queries(self, skills, location) -> list
    def search_page(self, query, page) -> tuple
    def fetch_user_detail(self, username) -> Optional[dict]
    def search_users(self, skills, location, pages) -> dict

    # 進度回呼
    on_progress: Callable
```

**改進：**
- 多 GitHub token 輪換（403 自動切換）
- 語言集從 config 載入（50+，補 Zig/Kotlin/Nim 等）
- worker 數量可配置（原始硬編碼 4）

---

### 5.4 `crawler/ocr.py` — OCR 圖像辨識（全新）

```python
class CrawlerOCR:
    """
    使用 pytesseract + Pillow 進行圖像文字辨識。
    系統需安裝 Tesseract-OCR 引擎 + 中英文語言包。
    """

    def __init__(self, config):
        self.enabled: bool
        self.tesseract_cmd: str     # Tesseract 執行檔路徑

    def extract_from_screenshot(self, screenshot_bytes) -> dict:
        """
        場景 1: LinkedIn 頁面截圖 → 提取姓名/職稱/公司/地區
        - 被登入牆擋住時，頁面仍可能顯示部分資訊
        - 截圖後 OCR 提取可見文字
        - 用正則匹配出結構化資料
        返回: { name, title, company, location, raw_text }
        """

    def extract_from_resume_image(self, image_path) -> dict:
        """
        場景 2: 履歷圖片/掃描 PDF → 提取文字
        - 支援 PNG/JPG/PDF（掃描版）
        - 中英文混合辨識（chi_tra + eng 語言包）
        返回: { raw_text, detected_skills, detected_name, detected_company }
        """

    def solve_simple_captcha(self, captcha_image_bytes) -> Optional[str]:
        """
        場景 3: 簡單文字 CAPTCHA 辨識
        - 圖片前處理：灰階、二值化、降噪
        - reCAPTCHA / hCaptcha 不支援（返回 None）
        返回: 辨識出的文字 或 None
        """
```

**OCR 整合點：**
- `linkedin.py`: 被登入牆擋 → `page.screenshot()` → `ocr.extract_from_screenshot()`
- `linkedin.py` / `github.py`: 遇簡單 CAPTCHA → `ocr.solve_simple_captcha()`
- `profile_reader.py`: 讀取不完整 → 截圖輔助提取

---

### 5.5 `crawler/profile_reader.py` — 個人檔案讀取

**來源提取（全部從 profile-reader.py）：**
- `ProfileReader` class (L126-430)
- `read_github_profile()` (L226-314) — 讀取 pinned repos、README、貢獻圖
- `read_linkedin_profile()` (L320-430) — 讀取姓名、headline、經歷
- 移除 `enrich_candidate_for_scoring()` (L435-504) — 不接 AI

**改進：**
- 接受外部 browser（從瀏覽器池取用，不自己建 browser）
- context rotation interval 可配置（原始固定 5）
- 超時可配置
- 讀取不完整時，用 OCR 從截圖補充資訊

---

### 5.6 `crawler/browser_pool.py` — 瀏覽器池（全新）

```python
class BrowserPool:
    """
    管理 Playwright 瀏覽器實例。
    因 Playwright 不能跨進程共享，
    實際上每個 worker 進程各自初始化自己的 browser。
    此 Pool 管理的是進程數量上限。
    """

    def __init__(self, max_browsers: int = 3, headless: bool = True):
        ...

    def create_browser(self) -> Browser     # Worker 進程啟動時呼叫
    def close_browser(self, browser)        # Worker 結束時關閉
    def get_pool_status(self) -> dict       # 當前使用狀態
```

---

### 5.7 `crawler/dedup.py` — 去重快取（全新）

```python
class DedupCache:
    """
    JSON 持久化去重快取。
    跨次執行不重複抓同一人。
    """

    def __init__(self, cache_file: str):
        self.linkedin_urls: set
        self.github_usernames: set

    def is_seen(self, linkedin_url=None, github_username=None) -> bool
    def mark_seen(self, linkedin_url=None, github_username=None)
    def save(self)                          # 寫入 JSON
    def load(self)                          # 從 JSON 讀取
    def clear(self, source=None)            # 清除快取（可指定來源）
    def stats(self) -> dict                 # { linkedin: 1234, github: 567 }
```

---

### 5.8 `crawler/engine.py` — 搜尋引擎主控

```python
class SearchEngine:
    """
    整合 LinkedIn + GitHub + OCR + 去重。
    Worker 進程內的主要入口。
    """

    def __init__(self, config, task: SearchTask):
        self.linkedin_searcher = LinkedInSearcher(...)
        self.github_searcher = GitHubSearcher(...)
        self.ocr = CrawlerOCR(...)
        self.dedup = DedupCache(...)
        self.profile_reader = ProfileReader(...)

    def execute(self, on_progress=None) -> list[Candidate]:
        """
        執行流程：
        1. LinkedIn 搜尋（4 層備援）
        2. GitHub 搜尋（多 token）
        3. 合併結果
        4. 去重過濾
        5. OCR 補充不完整資料
        6. 回傳 Candidate 列表
        """

    def _merge_and_dedup(self, linkedin_results, github_results) -> list
    def _ocr_enrich(self, candidates) -> list
```

---

## 6. 資料流程

### 6.1 搜尋任務執行流程

```
用戶在 Web UI 建立任務
       │
       ▼
Task Manager (APScheduler)
       │
       ▼ (排程觸發或立即執行)
Process Pool → 分配 Worker
       │
       ▼
Worker 初始化:
  - 建立 Playwright Browser
  - 載入 OCR 引擎
  - 載入設定
       │
       ▼
SearchEngine.execute()
  ├── 1. LinkedIn 搜尋 ──→ 4 層備援
  │     └── 被登入牆擋 → OCR 截圖提取
  ├── 2. GitHub 搜尋 ──→ 多 token 輪換
  ├── 3. 合併 + 去重
  ├── 4. OCR 補充不完整資料
  └── 5. 回傳 Candidate[]
       │
       ▼
Worker POST /api/internal/results ──→ Flask
       │
       ▼
Flask 統一寫入 Google Sheets
  ├── 查「已處理紀錄」去重
  ├── 新候選人 → 寫入客戶工作表
  └── 同步寫入「已處理紀錄」
       │
       ▼
UI 即時顯示結果
```

### 6.2 Step1ne 系統整合流程

```
用戶在 Settings 設定 API 位址
       │
       ▼
新增任務 → 選「從系統匯入」
       │
       ▼
GET /api/system/jobs
  └── 代理轉發 → Step1ne GET /api/jobs
       │
       ▼
用戶勾選職缺 → 自動帶入 search_primary / search_secondary
       │
       ▼
建立 SearchTask → 爬蟲執行
       │
       ▼
爬蟲完成 → 可選「推送到系統」
       │
       ▼
POST /api/system/push
  └── 代理轉發 → Step1ne POST /api/candidates/bulk
```

---

## 7. Google Sheets 儲存設計

### 工作表結構

```
Google Sheets 文件
│
├── 📋 已處理紀錄（獨立分頁，用於去重）
│   欄位:
│   linkedin_url | github_url | name | client_name | job_title |
│   imported_at | status (new/imported/skipped) | system_id
│
├── 客戶A（工作表，自動建立）
│   欄位:
│   id | name | source | github_url | linkedin_url | email |
│   location | bio | company | title | skills | public_repos |
│   followers | job_title | search_date | task_id | status | created_at
│   說明: 同一客戶的多個職缺混在同一工作表，用 job_title 欄位區分
│
├── 客戶B（工作表）
│
└── 客戶C（工作表）
```

### `storage/sheets_store.py` 設計

```python
class SheetsStore:
    def __init__(self, spreadsheet_id, credentials_file):
        self.gc = gspread.service_account(filename=credentials_file)
        self.spreadsheet = self.gc.open_by_key(spreadsheet_id)

    # 客戶工作表
    def get_or_create_client_sheet(self, client_name) -> Worksheet
    def list_clients(self) -> list[str]                # = 工作表名稱列表

    # 候選人 CRUD
    def write_candidates(self, client_name, candidates: list[Candidate])
    def read_candidates(self, client_name=None, job_title=None, ...) -> list
    def update_candidate_status(self, client_name, candidate_id, status)

    # 已處理紀錄（去重）
    def is_processed(self, linkedin_url=None, github_url=None) -> bool
    def add_processed(self, record: ProcessedRecord)
    def update_processed_status(self, url, status)

    # 統計
    def get_stats(self) -> dict    # 總數、今日新增、客戶分佈等
```

### 寫入安全

- **單一寫入者模式**: Workers POST 結果到 Flask → Flask 統一寫入 Sheets
- **批次寫入**: 累積後用 `worksheet.append_rows()` 批次 append
- **API 限制應對**:
  - 寫入: ~60 requests/min → batch update
  - 讀取: ~300 requests/min → 本地快取

---

## 8. Web UI 設計

**風格**: 數據儀表板（類似 Grafana / Metabase），深色側邊欄 + 白色內容區

**技術**: Flask Jinja2 + Tailwind CSS CDN + Chart.js

### 導航列（所有頁面共用）

```
┌─────────────────────────────────────────────────────────────┐
│  🔍 HeadHunter Crawler    Dashboard  Tasks  Results  Settings │
└─────────────────────────────────────────────────────────────┘
```

### 8.1 Dashboard（總覽儀表板）

```
┌─────────────────────────────────────────────────────────────┐
│  Dashboard                            最後更新: 2 分鐘前 🔄  │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐      │
│  │ 候選人總數 │ │ 今日新增  │ │ 執行中任務 │ │ 排程任務  │      │
│  │   358    │ │   +25    │ │    2     │ │    5     │      │
│  │  ↑12%    │ │ LinkedIn:15│ │ 客戶A/Java│ │ 下次: 18:00│     │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘      │
│                                                             │
│  ┌─────────────────────────┐ ┌─────────────────────────┐   │
│  │ 客戶分佈（圓餅圖）        │ │ 來源分佈（LinkedIn/GitHub）│   │
│  │   Chart.js Doughnut     │ │   Chart.js Doughnut     │   │
│  └─────────────────────────┘ └─────────────────────────┘   │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ 最近執行紀錄                                           │  │
│  │ 時間  │ 客戶 │ 職缺     │ 結果    │ 狀態              │  │
│  │ 15:30 │ 客戶A│Java 工程師│ +10人  │ ✅ 完成           │  │
│  │ NOW   │ 客戶C│ DevOps   │ 爬取中..│ 🔄 60%           │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

自動刷新: 每 30 秒 AJAX 輪詢 `GET /api/tasks` + `GET /api/candidates?today=true`

### 8.2 Tasks（任務管理）

```
┌─────────────────────────────────────────────────────────────┐
│  Tasks                                    [＋ 新增任務] 按鈕  │
├─────────────────────────────────────────────────────────────┤
│  篩選: [客戶▼] [狀態▼] [排程▼] [🔍搜尋]                      │
│                                                             │
│  任務列表:                                                   │
│  客戶 │ 職缺     │ 技能        │ 排程    │ 狀態 │ 操作       │
│  客戶A│Java工程師 │Java,Spring  │每日09:00│ ✅  │ ▶ ⚙ 🗑    │
│  客戶B│前端工程師 │React,TS     │每6小時  │ ⏸  │ ▶ ⚙ 🗑    │
│                                                             │
│  操作: ▶=立即執行  ⚙=編輯  🗑=刪除                            │
│                                                             │
│  展開詳情:                                                   │
│  進度: ████████░░ 80%  LinkedIn: 15人 / GitHub: 8人         │
│  目前: LinkedIn 第 4/5 頁 | 上次: 23 人（OCR: 3 人）         │
└─────────────────────────────────────────────────────────────┘
```

**新增任務 Modal（兩種模式）：**

模式 1: **從系統 API 匯入**
- 載入 Step1ne 系統職缺列表
- 勾選職缺 → 自動帶入 search_primary / search_secondary
- 可微調技能、地區、頁數

模式 2: **手動輸入**
- 輸入客戶名稱、職缺名稱
- 手動輸入主要技能 (AND) + 次要技能 (OR)

兩種模式共用排程設定: 立即執行 / 每日 / 每週 / 間隔

### 8.3 Results（結果瀏覽）

```
┌─────────────────────────────────────────────────────────────┐
│  Results                      [推送到系統▲] [匯出 JSON▼]     │
├─────────────────────────────────────────────────────────────┤
│  篩選: [客戶▼] [職缺▼] [來源▼] [狀態▼] [日期從] [日期到]      │
│                                                             │
│  358 筆結果  □全選                                           │
│  □│名稱     │來源    │公司   │地區 │技能       │日期          │
│  □│John Doe │LinkedIn│Google│台北 │Java,Spring│03/04         │
│  □│Jane Wu  │GitHub  │Shopee│台北 │Python,K8s │03/04         │
│  □│Bob Chen │LI+OCR  │TSMC  │新竹 │React,TS   │03/03         │
│                                                             │
│  批次: [標記已匯入] [推送選取] [匯出選取]                      │
│  分頁: ← 1 2 3 4 5 → │ 每頁 [50▼]                           │
│                                                             │
│  展開詳情:                                                   │
│  John Doe | 來源: LinkedIn                                   │
│  Bio, 技能, LinkedIn/GitHub 連結                              │
│  [開啟 LinkedIn↗] [開啟 GitHub↗] [推送到系統] [標記已匯入]     │
└─────────────────────────────────────────────────────────────┘
```

### 8.4 Settings（系統設定）

```
┌─────────────────────────────────────────────────────────────┐
│  Settings                                       [儲存設定]   │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─ Step1ne 系統連結 ─────────────────────────────────────┐ │
│  │ API 位址: [________________________]  [測試連線]        │ │
│  │ □ 爬蟲完成後自動推送候選人                               │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                             │
│  ┌─ API Keys ─────────────────────────────────────────────┐ │
│  │ GitHub Token(s): [+新增]                                │ │
│  │ Brave Search API Key: [________________________]        │ │
│  │ Google Sheets ID: [________________________]            │ │
│  │ Service Account JSON: [選擇檔案]                        │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                             │
│  ┌─ 爬蟲設定 ─────────────────────────────────────────────┐ │
│  │ 瀏覽器池: [3▼]  地區: [Taiwan▼]  Headless: [✅]        │ │
│  │ OCR: [✅啟用]                                           │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                             │
│  ┌─ 反偵測設定 ───────────────────────────────────────────┐ │
│  │ 請求間隔: [2.0]~[5.0]s  SSL驗證: [□]  代理: [□啟用]    │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                             │
│  ┌─ 去重快取 ─────────────────────────────────────────────┐ │
│  │ LinkedIn: 1,234  GitHub: 567                            │ │
│  │ [清除 LinkedIn] [清除 GitHub] [全部清除]                  │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

---

## 9. REST API 規格

### 候選人

| Method | Path | 說明 |
|--------|------|------|
| `GET` | `/api/candidates` | 列表（篩選: client, job_title, status, date_from, date_to） |
| `GET` | `/api/candidates/:id` | 單筆詳情 |
| `PATCH` | `/api/candidates/:id` | 更新狀態 |
| `POST` | `/api/candidates/export` | 批次匯出 JSON |

### 任務管理

| Method | Path | 說明 |
|--------|------|------|
| `GET` | `/api/tasks` | 排程任務列表 |
| `POST` | `/api/tasks` | 建立任務（含 client_name + job_title） |
| `PATCH` | `/api/tasks/:id` | 更新任務 |
| `DELETE` | `/api/tasks/:id` | 刪除任務 |
| `POST` | `/api/tasks/:id/run` | 立即執行 |
| `GET` | `/api/tasks/:id/status` | 任務進度（即時） |

### 客戶 & 已處理紀錄

| Method | Path | 說明 |
|--------|------|------|
| `GET` | `/api/clients` | 列出所有客戶（= Sheets 工作表名稱） |
| `GET` | `/api/processed` | 已處理紀錄列表 |
| `PATCH` | `/api/processed/:id` | 更新匯入狀態 |

### Step1ne 系統整合（代理轉發）

| Method | Path | 說明 |
|--------|------|------|
| `GET` | `/api/system/jobs` | 代理 → Step1ne `GET /api/jobs` |
| `POST` | `/api/system/push` | 代理 → Step1ne `POST /api/candidates/bulk` |

### 內部 & 健康檢查

| Method | Path | 說明 |
|--------|------|------|
| `POST` | `/api/internal/results` | Worker 回報結果（內部用） |
| `GET` | `/api/health` | 健康檢查 |

---

## 10. Step1ne 系統整合

### `integration/step1ne_client.py`

```python
class Step1neClient:
    """
    連結 Step1ne Headhunter System API。
    完全可選 — 不設定 API 位址時不影響爬蟲獨立運作。
    """

    def __init__(self, api_base_url: str = None):
        self.api_base = api_base_url

    def is_connected(self) -> bool
    def fetch_jobs(self, status='招募中') -> list
    def fetch_job_detail(self, job_id: int) -> dict
    def push_candidates(self, candidates: list, actor='Crawler') -> dict
```

### 現有可用的 Step1ne API

| Endpoint | 說明 | 認證 |
|----------|------|------|
| `GET /api/jobs` | 返回所有職缺 | 無需 |
| `GET /api/jobs/:id` | 單一職缺詳情 | 無需 |
| `POST /api/candidates/bulk` | 批次回寫候選人 | 無需 |

### 職缺欄位對應

| Step1ne 欄位 | 爬蟲用途 |
|-------------|---------|
| `search_primary` | → 主要技能 (AND) |
| `search_secondary` | → 次要技能 (OR) |
| `key_skills` | → 參考技能 |
| `client_company` | → 客戶名稱 |
| `position_name` | → 職缺名稱 |
| `location` | → 搜尋地區 |

---

## 11. 反偵測機制

### 策略層級

| 層級 | 機制 | 實現 |
|------|------|------|
| 1 | UA 輪換 | 33+ 個 UA，每次請求隨機 |
| 2 | 請求間隔 | 隨機延遲 2-5 秒 |
| 3 | 翻頁間隔 | 隨機延遲 3-6 秒 |
| 4 | 候選人間隔 | 10-20 秒長停頓 |
| 5 | 指數退避 | 429/403 → 2^n 秒，上限 120s |
| 6 | Playwright Stealth | 覆蓋 webdriver、plugins、languages、chrome runtime 等 10+ 指紋點 |
| 7 | 人類模擬 | 隨機滑鼠晃動、不均勻滾動、偶爾回滾 |
| 8 | Context 輪換 | 每 5 位候選人換 browser context（UA、viewport） |
| 9 | 代理輪換 | round_robin / random 策略 |
| 10 | CAPTCHA 偵測 | 10 個關鍵字指標 + OCR 嘗試解決 |
| 11 | SSL 可配置 | 預設啟用，雲端環境可關閉 |

---

## 12. OCR 模組設計

### 三個使用場景

```
場景 1: LinkedIn 截圖提取
被登入牆擋住 → page.screenshot() → OCR → 提取可見的姓名/職稱/公司

場景 2: 履歷圖片辨識
掃描版 PDF / 圖片履歷 → OCR → 提取文字 → 正則比對技能/姓名/公司

場景 3: 簡單 CAPTCHA
文字型驗證碼 → 灰階+二值化+降噪 → OCR → 辨識文字
(reCAPTCHA / hCaptcha 不支援，返回 None)
```

### 系統需求

```bash
# macOS
brew install tesseract
brew install tesseract-lang   # 包含 chi_tra (繁體中文)

# Ubuntu/Debian
sudo apt install tesseract-ocr tesseract-ocr-chi-tra

# Windows
# 從 https://github.com/UB-Mannheim/tesseract/wiki 下載安裝
```

---

## 13. 併發與排程架構

### 併發模型

```
                    Flask (主進程)
                        │
                        ▼
              multiprocessing.Pool(N)
              ┌─────────┼─────────┐
              ▼         ▼         ▼
          Worker 1  Worker 2  Worker 3
          (進程)    (進程)    (進程)
              │         │         │
              ▼         ▼         ▼
          Browser   Browser   Browser
          (各自)    (各自)    (各自)
```

**為什麼不共享 Browser？**
Playwright browser 實例不能跨 Python 進程共享。每個 worker 進程必須建立自己的 browser。

### `scheduler/task_manager.py`

```python
class TaskManager:
    def __init__(self, config):
        self.scheduler = BackgroundScheduler()
        self.pool = multiprocessing.Pool(processes=config['browser_pool_size'])
        self.tasks: dict = {}          # task_id → SearchTask

    # 排程管理
    def add_task(self, task: SearchTask) -> str
    def remove_task(self, task_id: str)
    def update_task(self, task_id: str, updates: dict)
    def run_now(self, task_id: str)

    # 狀態
    def get_task_status(self, task_id) -> dict
    def get_all_tasks(self) -> list

    # 持久化
    def save_tasks(self)               # → data/tasks.json
    def load_tasks(self)               # ← data/tasks.json
    def save_checkpoint(self, task_id, checkpoint)
    def load_checkpoint(self, task_id) -> dict
```

### `scheduler/worker.py`

```python
def crawler_worker_init():
    """Worker 進程初始化：建立 Playwright browser"""

def execute_search_task(task_config: dict) -> dict:
    """
    在 worker 內執行爬蟲任務。
    完成後 POST 結果到 Flask /api/internal/results。
    """

def crawler_worker_cleanup():
    """進程結束時關閉 browser"""
```

### 排程類型

| 類型 | 說明 | APScheduler Trigger |
|------|------|-------------------|
| `once` | 立即執行一次 | `date` |
| `daily` | 每日指定時間 | `cron(hour=H, minute=M)` |
| `weekly` | 每週指定天數 | `cron(day_of_week=...)` |
| `interval` | 每 N 小時 | `interval(hours=N)` |

---

## 14. 現有程式碼漏洞與修復

| # | 問題 | 現狀 | 修復方案 |
|---|------|------|----------|
| 1 | 無代理支援 | 單一 IP 容易被封 | 新增代理列表 + 輪換策略 |
| 2 | SSL 永久停用 | L43-45 硬編碼 `CERT_NONE` | 改為可配置，預設啟用 |
| 3 | 無指數退避 | L586 固定 `sleep(15)` | 2^attempt 秒，上限 120s |
| 4 | 地區硬編碼 | 多處寫死 "Taiwan"/"台灣" | 從任務設定讀取 |
| 5 | 同義詞硬編碼 | 300+ 映射寫在 .py 內 | 抽到 YAML，可在 UI 編輯 |
| 6 | UA 太少 | 只有 5 個 | 擴充到 33+，從檔案載入 |
| 7 | 無持久去重 | 每次重跑重抓 | JSON 持久化快取 |
| 8 | 無任務恢復 | 中斷後從頭來 | Checkpoint 機制 |
| 9 | CAPTCHA 被動 | 偵測到只停止 | OCR 嘗試解決 + 自動切層 + 通知用戶 |
| 10 | LinkedIn 登入牆 | login_required 就跳過 | OCR 截圖提取可見資訊 + UI 顯示「受限」 |
| 11 | GitHub 單 token | 403 就停 | 多 token 輪換 |
| 12 | 無結果驗證 | 可能抓到公司頁面 | 基本驗證（名稱非空、URL 格式） |
| 13 | 記憶體洩漏 | browser context 未必關閉 | try/finally + atexit cleanup |
| 14 | 無 logging | 用 `print()` | Python logging + rotating file |
| 15 | 瀏覽器指紋淺 | 只蓋 webdriver 一個屬性 | 整合 playwright-stealth 覆蓋 10+ 指紋 |

---

## 15. 實施計畫（9 階段）

### 實施進度總覽

| Phase | 名稱 | 狀態 | 完成日期 |
|-------|------|------|---------|
| 1 | 基礎建設 + 設定外部化 | ✅ 完成 | 2026-03-04 |
| 2 | 核心爬蟲模組 | ✅ 完成 | 2026-03-04 |
| 3 | Step1ne 系統 API 整合 | ✅ 完成 | 2026-03-04 |
| 4 | Google Sheets 儲存層 | ✅ 完成 | 2026-03-04 |
| 5 | 併發 + 排程 | ✅ 完成 | 2026-03-04 |
| 6 | REST API | ✅ 完成 | 2026-03-04 |
| 7 | Web UI | ✅ 完成 | 2026-03-04 |
| 8 | 漏洞修復（15 項） | ✅ 完成 | 2026-03-05 |
| 9 | 測試（80 個）+ README | ✅ 完成 | 2026-03-05 |

### 測試結果

```
tests/test_api.py          — 16 tests (健康檢查、任務CRUD、設定、去重、Dashboard、系統整合)
tests/test_dedup.py        — 9 tests  (標記/檢查/持久化/清除/統計)
tests/test_github.py       — 7 tests  (token管理/查詢建構/headers)
tests/test_linkedin.py     — 16 tests (URL清理/查詢建構/HTML解析/同義詞展開)
tests/test_models.py       — 10 tests (Candidate/SearchTask/ProcessedRecord)
tests/test_task_manager.py — 12 tests (CRUD/持久化/狀態重設/checkpoint)
──────────────────────────────────────
Total: 80 passed ✅
```

---

### Phase 1: 基礎建設 + 設定外部化 ✅

**目標**: 專案骨架、設定檔、資料模型

**產出檔案:**
- `config/default.yaml` — 所有可配置參數
- `config/skills_synonyms.yaml` — 300+ 技能同義詞映射
- `config/user_agents.txt` — 33 個 User-Agent
- `storage/models.py` — `Candidate`, `SearchTask`, `ProcessedRecord` dataclass
- `requirements.txt`
- `app.py` — Flask 應用入口

**來源:**
- 延遲參數: search-plan-executor.py `anti_scraping_delay()` 各處呼叫
- 超時設定: http_get(15s), page_load(30000ms), profile_read(25000ms)
- CAPTCHA 指標: `is_captcha_page()` 的 6 個關鍵字 → 擴充到 10 個
- GitHub 語言集: 44 個 → 擴充到 50+
- 技能同義詞: Python SKILL_SYNONYMS(50+) + JS SKILL_ALIASES(30+) → 合併去重

---

### Phase 2: 核心爬蟲模組 ✅

**目標**: 重構 search-plan-executor.py (832行) + profile-reader.py (527行)

**產出檔案:**
- `crawler/anti_detect.py`
- `crawler/linkedin.py`
- `crawler/github.py`
- `crawler/ocr.py`（全新）
- `crawler/profile_reader.py`
- `crawler/browser_pool.py`（全新）
- `crawler/dedup.py`（全新）
- `crawler/engine.py`

**來源對照:**

| 原始檔案 | 行數範圍 | 抽取到 |
|---------|---------|--------|
| search-plan-executor.py L34-106 | HTTP 工具、UA、SSL、延遲、CAPTCHA | anti_detect.py |
| search-plan-executor.py L238-318 | 技能同義詞、展開 | linkedin.py (從 YAML 載入) |
| search-plan-executor.py L320-356 | build_linkedin_query | linkedin.py |
| search-plan-executor.py L407-477 | LinkedIn URL 工具 | linkedin.py |
| search-plan-executor.py L483-750 | LinkedIn 4 層搜尋 | linkedin.py |
| search-plan-executor.py L112-230 | GitHub API 搜尋 | github.py |
| search-plan-executor.py L359-401 | GitHub 查詢建構 | github.py |
| profile-reader.py L40-114 | Stealth JS、人類模擬 | anti_detect.py |
| profile-reader.py L126-430 | ProfileReader class | profile_reader.py |
| profile-reader.py L435-504 | enrich_candidate_for_scoring | **刪除**（不接 AI）|

---

### Phase 3: Step1ne 系統 API 整合 ✅

**目標**: 可選連結 Step1ne 系統

**產出檔案:**
- `integration/step1ne_client.py`

**使用流程:**
1. Settings 頁設定 API 位址 → 測試連線
2. 新增任務時選「從系統匯入」→ 呼叫 `fetch_jobs()`
3. 用戶勾選職缺 → 自動帶入 search_primary / search_secondary
4. 爬蟲完成後，可選推回系統 (`push_candidates()`)

---

### Phase 4: Google Sheets 儲存層 ✅

**目標**: gspread 實現多客戶分離 CRUD

**產出檔案:**
- `storage/sheets_store.py`

**關鍵設計:**
- 每客戶一個工作表（自動建立）
- 獨立「已處理紀錄」分頁（去重）
- Flask 統一寫入（防多人衝突）
- 批次寫入（Google Sheets API 限制應對）

---

### Phase 5: 併發 + 排程 ✅

**目標**: 多職缺同時爬、定時執行

**產出檔案:**
- `scheduler/task_manager.py`
- `scheduler/worker.py`

**功能:**
- APScheduler 支援 once / daily / weekly / interval
- multiprocessing.Pool 控制併發
- 任務進度追蹤 → UI 即時顯示
- 斷點續爬（checkpoint 機制）

---

### Phase 6: REST API ✅

**目標**: 供前端和外部呼叫

**產出檔案:**
- `api/routes.py`

**端點數**: 15 個（候選人 4 + 任務 6 + 客戶 1 + 已處理 2 + 系統整合 2 + 內部 2）

---

### Phase 7: Web UI ✅

**目標**: 4 個頁面的數據儀表板

**產出檔案:**
- `web/views.py`
- `web/templates/base.html`
- `web/templates/dashboard.html`
- `web/templates/tasks.html`
- `web/templates/results.html`
- `web/templates/settings.html`
- `web/static/css/custom.css`
- `web/static/js/app.js`

**頁面:** Dashboard / Tasks / Results / Settings

---

### Phase 8: 漏洞修復 ✅

**目標**: 修復 15 個已識別的漏洞（15/15 已完成）

（見上方第 14 節詳細表格）

---

### Phase 9: 測試 + 文件 ✅

**單元測試（80 個，全部通過）:**
- `test_linkedin.py` — query builder、URL 清理、HTML 解析、同義詞展開（16 tests）
- `test_github.py` — token 管理、查詢建構、headers（7 tests）
- `test_dedup.py` — 去重邏輯、持久化、清除（9 tests）
- `test_models.py` — Candidate / SearchTask / ProcessedRecord（10 tests）
- `test_api.py` — REST API endpoints（16 tests）
- `test_task_manager.py` — 任務 CRUD、持久化、checkpoint（12 tests）

**文件:**
- `README.md` — 安裝指南（含 Tesseract-OCR）、設定說明、API 文件、專案結構

---

## 16. 製作流程

### 開發順序（建議嚴格遵循）

```
Phase 1: 基礎建設
  ↓ (可並行)
Phase 2: 核心爬蟲 ←──→ Phase 4: Google Sheets
  ↓                        ↓
Phase 3: Step1ne 整合      │
  ↓                        │
Phase 5: 併發排程 ←────────┘
  ↓
Phase 6: REST API
  ↓
Phase 7: Web UI
  ↓
Phase 8: 漏洞修復（貫穿 Phase 2-7，邊做邊修）
  ↓
Phase 9: 測試 + 文件
```

### 每個 Phase 的開發步驟

1. **閱讀來源** — 對照上方來源行數，理解原始邏輯
2. **建立檔案** — 建立模組 + `__init__.py`
3. **重構提取** — 從原始碼提取，重構為 class/function
4. **套用改進** — 套用漏洞修復（設定外部化、指數退避等）
5. **單元測試** — 測試核心邏輯
6. **整合測試** — 與其他模組整合

### 預估工作量

| Phase | 預估檔案數 | 依賴 |
|-------|----------|------|
| 1 | 5 | 無 |
| 2 | 8 | Phase 1 |
| 3 | 1 | Phase 1 |
| 4 | 1 | Phase 1 |
| 5 | 2 | Phase 2, 4 |
| 6 | 1 | Phase 2-5 |
| 7 | 8 | Phase 6 |
| 8 | - | 貫穿 2-7 |
| 9 | 2+ | 全部 |

---

## 17. 驗證方式

### 功能驗證清單

1. ✅ 啟動 Flask server (`python app.py`)
2. ✅ 開瀏覽器訪問 `localhost:5000`
3. ✅ Dashboard 顯示統計資料 + 圖表
4. ✅ Settings 設定 Step1ne API 位址 → 測試連線成功
5. ✅ Settings 設定 Google Sheets → 連線成功
6. ✅ 新增任務 → 選「從系統匯入」→ 載入職缺列表 → 勾選建立
7. ✅ 新增任務 → 選「手動輸入」→ 輸入技能 → 建立
8. ✅ 立即執行任務 → 進度條正常 → Google Sheets 出現結果
9. ✅ OCR 截圖提取正常（LinkedIn 登入牆場景）
10. ✅ Results 頁篩選、檢視候選人
11. ✅ 推送候選人到 Step1ne 系統
12. ✅ 同時建立 2-3 個不同客戶的任務 → 併發正常
13. ✅ 設定排程（每日/每週/間隔）→ 自動執行
14. ✅ 重啟 server → 排程任務自動恢復
15. ✅ 去重正常（重複候選人不重複寫入）

---

## 18. 來源檔案對照表

### 帶入新模組的檔案

| 來源 | 抽取到 | 原始行數 |
|------|--------|---------|
| `server/talent-sourcing/search-plan-executor.py` | linkedin.py, github.py, anti_detect.py, engine.py | 832 行 |
| `server/talent-sourcing/profile-reader.py` | profile_reader.py, anti_detect.py | 527 行 |
| `server/talent-sourcing/one-bot-pipeline.py` | worker.py, task_manager.py（架構參考） | 812 行 |
| `server/githubAnalysisService.js` | skills_synonyms.yaml（合併 SKILL_ALIASES） | 788 行 |
| `server/routes-api.js` | step1ne_client.py（API 端點參考） | 1000+ 行 |

### 不帶入的檔案

| 檔案 | 理由 |
|------|------|
| `candidate-scoring-system-v2.py` | AI 評分（不接 AI） |
| `claude-conclusion-generator.py` | Claude 結論生成 |
| `job-profile-analyzer.py` | 職缺分析 |
| `industry-migration-analyzer.py` | 產業遷移分析 |
| `talentSourceService.js` | Node.js 調度層（改用純 Python） |

---

## 19. 安裝與部署

### 環境需求

- Python 3.10+
- Tesseract-OCR (含中文語言包)
- Google Cloud Service Account (for Sheets API)
- 至少 1GB 可用記憶體（每個 Playwright browser 約 200MB）

### 安裝步驟

```bash
# 1. 建立虛擬環境
cd ~/Downloads/headhunter-crawler
python -m venv venv
source venv/bin/activate   # macOS/Linux
# venv\Scripts\activate    # Windows

# 2. 安裝依賴
pip install -r requirements.txt
playwright install chromium

# 3. 安裝 Tesseract-OCR
# macOS:
brew install tesseract tesseract-lang
# Ubuntu:
# sudo apt install tesseract-ocr tesseract-ocr-chi-tra

# 4. 設定 Google Sheets
# - 建立 Google Cloud 專案
# - 啟用 Google Sheets API
# - 建立 Service Account → 下載 JSON 金鑰
# - 將 JSON 放在專案根目錄命名為 credentials.json
# - 在 Google Sheets 分享給 Service Account email

# 5. 編輯設定
# 修改 config/default.yaml 中的:
# - google_sheets.spreadsheet_id
# - api_keys.github_tokens
# - step1ne.api_base_url (可選)

# 6. 啟動
python app.py
# → 訪問 http://localhost:5000
```
