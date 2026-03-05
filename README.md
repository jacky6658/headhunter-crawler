# HeadHunter Crawler

獨立自動化人才爬蟲工具。透過 Flask Web UI 設定搜尋條件與排程，從 LinkedIn 和 GitHub 搜尋候選人，結果存入 Google Sheets，並可一鍵匯入 Step1ne 獵頭系統。

## 功能

- **LinkedIn 4 層搜尋**：Playwright → Google → Bing → Brave API，自動備援切換
- **GitHub API 搜尋**：多 token 輪換，避免 rate limit
- **OCR 支援**：LinkedIn 截圖文字提取、履歷圖片辨識、簡易 CAPTCHA 解決
- **Google Sheets 儲存**：每客戶一個工作表，獨立去重紀錄
- **排程管理**：APScheduler 支援每日/每週/間隔/一次性任務
- **併發執行**：多任務同時爬取不同職缺
- **反偵測**：30+ UA 輪換、代理支援、指數退避、playwright-stealth
- **Web UI**：Dashboard 總覽、任務管理、結果瀏覽、系統日誌、設定頁面
- **Step1ne 系統整合（可選）**：
  - 從系統 API 匯入職缺
  - 爬蟲儀表板一鍵批量匯入候選人到系統
  - 任務完成後自動推送（auto-push）
  - OpenClaw 等外部工具可直接呼叫 API 匯入
- **AI Agent 整合**：提供完整 REST API + OpenAPI 規格，任何 AI 工具可直接呼叫

---

## 快速安裝（3 步驟）

### 1. Clone + 安裝

```bash
git clone https://github.com/jacky6658/headhunter-crawler.git
cd headhunter-crawler

python -m venv venv
source venv/bin/activate    # macOS/Linux
# venv\Scripts\activate     # Windows

pip install -r requirements.txt
playwright install chromium
```

### 2. Tesseract OCR（可選）

**macOS:**
```bash
brew install tesseract tesseract-lang
```

**Ubuntu/Debian:**
```bash
sudo apt install tesseract-ocr tesseract-ocr-chi-tra
```

### 3. 啟動

```bash
python app.py
```

開啟瀏覽器訪問 `http://localhost:5000`

**背景執行（關掉終端機也不會停）：**
```bash
nohup python3 app.py > logs/server.log 2>&1 &
```

停止背景服務：
```bash
pkill -f "python3 app.py"
```

> **API Key、credentials.json、Google Sheets 設定已在倉庫中，clone 下來直接能用。**

### 共用資源連結

| 資源 | 連結 |
|------|------|
| **履歷池 Google Sheets** | [開啟](https://docs.google.com/spreadsheets/d/15X2NNK9bSmSl-GfCmfO2q8lS2wAinR9fNMZr4vqrMug) |
| **Step1ne 系統（線上）** | https://backendstep1ne.zeabur.app |

---

## 設定說明

設定檔 `config/default.yaml`，也可在 Web UI → Settings 修改：

| 區塊 | 說明 | 是否必要 |
|------|------|---------|
| `google_sheets.spreadsheet_id` | Google Sheets ID | 是 |
| `google_sheets.credentials_file` | Service Account 金鑰 | 是 |
| `api_keys.github_tokens` | GitHub API Token（支援多個） | 是（GitHub 搜尋） |
| `api_keys.brave_api_key` | Brave Search API Key | 否 |
| `step1ne.api_base_url` | Step1ne 系統 API 位址 | 否（獨立爬蟲不需要） |
| `step1ne.auto_push` | 任務完成自動推送到 Step1ne | 否 |

### 新建 Google Sheets Service Account（僅首次）

1. 建立 [Google Cloud 專案](https://console.cloud.google.com/)
2. 啟用 Google Sheets API + Google Drive API
3. 建立 Service Account → 下載 JSON 金鑰
4. 將 JSON 檔案放到專案根目錄，命名為 `credentials.json`
5. 到 Google Sheets 文件 → 共用 → 加入 Service Account 的 email

---

## 使用流程

1. **Settings** — 設定 Google Sheets、API Keys、爬蟲參數
2. **Tasks** — 新增搜尋任務（手動輸入或從 Step1ne 系統匯入職缺）
3. 點擊「立即執行」或設定排程
4. **Dashboard** — 監控執行進度
5. **Results** — 瀏覽、篩選、匯出候選人
6. **Logs** — 查看系統日誌、篩選錯誤、即時更新

### 匯入候選人到 Step1ne 系統

**方式 1 — UI 手動匯入：**
Step1ne 儀表板 → 爬蟲整合 → 評分總覽 → 勾選候選人 → 點「匯入到系統」

**方式 2 — API 匯入（OpenClaw / 外部工具）：**
```bash
curl -X POST http://localhost:3001/api/crawler/import \
  -H 'Content-Type: application/json' \
  -d '{
    "candidates": [
      {"name":"John","title":"Engineer","skills":["Python","Go"],"grade":"A","source":"github"}
    ],
    "actor": "OpenClaw"
  }'
```

**方式 3 — 自動推送：**
`config/default.yaml` 設定 `step1ne.auto_push: true`，任務完成自動推送。

---

## 專案結構

```
headhunter-crawler/
├── app.py                    # Flask 入口
├── config/
│   ├── default.yaml          # 設定檔（含 API Key）
│   ├── skills_synonyms.yaml  # 技能同義詞（300+）
│   └── user_agents.txt       # UA 列表（30+）
├── crawler/
│   ├── engine.py             # 搜尋引擎主控
│   ├── linkedin.py           # LinkedIn 4 層搜尋
│   ├── github.py             # GitHub API 搜尋
│   ├── ocr.py                # OCR 模組
│   ├── profile_reader.py     # 個人檔案讀取
│   ├── anti_detect.py        # 反偵測工具
│   ├── browser_pool.py       # 瀏覽器池管理
│   └── dedup.py              # 去重快取
├── scoring/
│   └── candidate_scorer.py   # 候選人評分引擎
├── storage/
│   ├── models.py             # 資料模型
│   └── sheets_store.py       # Google Sheets CRUD
├── integration/
│   └── step1ne_client.py     # Step1ne API 客戶端
├── scheduler/
│   ├── task_manager.py       # 排程管理（含 auto-push）
│   └── worker.py             # 爬蟲 Worker
├── api/
│   └── routes.py             # REST API
├── web/
│   ├── views.py              # 頁面路由
│   ├── templates/            # HTML 模板
│   └── static/               # CSS + JS
├── credentials.json          # Google Sheets 憑證
├── data/                     # 執行資料（自動建立，不進版控）
└── logs/                     # 日誌（自動建立，不進版控）
```

## REST API

| 端點 | 方法 | 說明 |
|------|------|------|
| `/api/health` | GET | 健康檢查 |
| `/api/candidates` | GET | 候選人列表（支援篩選） |
| `/api/candidates/:id` | GET | 候選人詳情 |
| `/api/candidates/:id` | PATCH | 更新狀態 |
| `/api/tasks` | GET | 任務列表 |
| `/api/tasks` | POST | 建立任務 |
| `/api/tasks/:id` | PATCH | 更新任務 |
| `/api/tasks/:id` | DELETE | 刪除任務 |
| `/api/tasks/:id/run` | POST | 立即執行 |
| `/api/tasks/:id/status` | GET | 即時進度 |
| `/api/clients` | GET | 客戶列表 |
| `/api/settings` | GET/POST | 讀取/更新設定 |
| `/api/system/jobs` | GET | 從 Step1ne 拉取職缺 |
| `/api/system/push` | POST | 推送候選人到 Step1ne |
| `/api/dashboard/stats` | GET | Dashboard 統計 |
| `/api/score/candidates` | POST | 評分候選人 |
| `/api/score/detail/:id` | GET | 評分細項 |
| `/api/logs` | GET | 系統日誌（支援等級篩選、搜尋） |
| `/api/logs/clear` | POST | 清空日誌 |

## AI Agent 整合

如果你要讓 AI（Ollama、OpenWebUI、Dify 等）自動操作爬蟲系統，請參考：

- **[AI 整合指南](docs/AI-INTEGRATION.md)** — 給 AI 閱讀的完整操作文檔
- **[OpenAPI 規格](docs/openapi.yaml)** — 標準 API 規格，可匯入任何 AI 工具
- **[API 文檔](docs/API.md)** — 完整 API 文檔

> 只要把 `docs/AI-INTEGRATION.md` 的內容貼到 AI 的系統提示詞（System Prompt），AI 就知道怎麼操作爬蟲系統。

## 測試

```bash
pip install pytest
pytest tests/ -v
```

## 注意事項

- LinkedIn 搜尋依賴 Google/Bing/Brave 搜尋引擎的公開索引，不直接登入 LinkedIn
- GitHub API 有 rate limit，建議配置多個 token
- 請遵守各平台的使用條款，合理設定爬取間隔
- Google Sheets API 寫入限制約 60 次/分鐘，系統已採用批次寫入優化
- `data/` 和 `logs/` 目錄會在首次執行時自動建立
