# HeadHunter Crawler - API 文檔

**版本**: 1.0.0
**Base URL**: `http://localhost:5000/api`

---

## 目錄

1. [健康檢查](#健康檢查)
2. [候選人 API](#候選人-api)
3. [任務管理 API](#任務管理-api)
4. [客戶 API](#客戶-api)
5. [評分系統 API](#評分系統-api)
6. [關鍵字生成 API](#關鍵字生成-api)
7. [LinkedIn OCR 分析 API](#linkedin-ocr-分析-api)
8. [GitHub 深度分析 API](#github-深度分析-api)
9. [去重快取 API](#去重快取-api)
10. [已處理紀錄 API](#已處理紀錄-api)
11. [Step1ne 系統整合 API](#step1ne-系統整合-api)
12. [Dashboard 統計 API](#dashboard-統計-api)
13. [設定 API](#設定-api)
14. [資料模型](#資料模型)
15. [錯誤處理](#錯誤處理)

---

## 健康檢查

### 健康狀態

```http
GET /api/health
```

**回應範例**：
```json
{
  "status": "ok",
  "timestamp": "2026-03-05T17:30:00.000000"
}
```

---

## 候選人 API

### 1. 列出候選人

```http
GET /api/candidates
```

**查詢參數**：

| 參數 | 類型 | 預設 | 說明 |
|------|------|------|------|
| `client` | string | — | 按客戶名稱篩選 |
| `job_title` | string | — | 按職缺名稱篩選 |
| `status` | string | — | 按狀態篩選（`new` / `imported` / `reviewed` / `skipped`） |
| `limit` | int | 50 | 每頁數量 |
| `offset` | int | 0 | 偏移量 |

**回應範例**：
```json
{
  "data": [
    {
      "id": "a1b2c3d4",
      "name": "John Doe",
      "source": "github",
      "github_url": "https://github.com/johndoe",
      "linkedin_url": "",
      "email": "john@example.com",
      "location": "Taiwan",
      "bio": "Full-stack developer",
      "company": "TechCorp",
      "title": "Senior Engineer",
      "skills": "Python, Go, React",
      "public_repos": 42,
      "followers": 150,
      "job_title": "Backend Engineer",
      "search_date": "2026-03-05",
      "status": "new",
      "score": 87,
      "grade": "A"
    }
  ],
  "total": 1
}
```

---

### 2. 取得候選人詳情

```http
GET /api/candidates/:id
```

**路徑參數**：

| 參數 | 說明 |
|------|------|
| `id` | 候選人 UUID |

**回應**：單一候選人物件（同上），找不到回傳 `404`。

---

### 3. 更新候選人狀態

```http
PATCH /api/candidates/:id
```

**請求 Body**：
```json
{
  "client_name": "TechCo",
  "status": "imported"
}
```

| 欄位 | 必填 | 說明 |
|------|------|------|
| `client_name` | 是 | 客戶名稱（用於定位工作表） |
| `status` | 是 | 新狀態值 |

---

### 4. 匯出候選人

```http
POST /api/candidates/export
```

**請求 Body**：
```json
{
  "client": "TechCo",
  "job_title": "Backend Engineer",
  "limit": 1000
}
```

---

## 任務管理 API

### 1. 列出所有任務

```http
GET /api/tasks
```

**回應範例**：
```json
[
  {
    "id": "task-uuid-123",
    "client_name": "TechCo",
    "job_title": "Backend Engineer",
    "primary_skills": ["Python", "Go"],
    "secondary_skills": ["Docker", "K8s"],
    "location": "Taiwan",
    "location_zh": "台灣",
    "pages": 5,
    "schedule_type": "once",
    "status": "completed",
    "progress": 100,
    "progress_detail": "完成",
    "last_run": "2026-03-05T10:00:00",
    "last_result_count": 23,
    "linkedin_count": 15,
    "github_count": 8,
    "ocr_count": 3,
    "auto_push": false,
    "step1ne_job_id": null
  }
]
```

---

### 2. 建立任務

```http
POST /api/tasks
```

**請求 Body**：
```json
{
  "client_name": "TechCo",
  "job_title": "Backend Engineer",
  "primary_skills": ["Python", "Go"],
  "secondary_skills": ["Docker", "K8s"],
  "location": "Taiwan",
  "location_zh": "台灣",
  "pages": 5,
  "schedule_type": "once",
  "schedule_time": "09:00",
  "schedule_interval_hours": 6,
  "schedule_weekdays": [0, 2, 4],
  "step1ne_job_id": 42,
  "auto_push": false,
  "run_now": true
}
```

**欄位說明**：

| 欄位 | 類型 | 必填 | 預設 | 說明 |
|------|------|------|------|------|
| `client_name` | string | 是 | — | 客戶名稱 |
| `job_title` | string | 是 | — | 職缺名稱 |
| `primary_skills` | string[] | 否 | `[]` | 主要技能（AND 邏輯） |
| `secondary_skills` | string[] | 否 | `[]` | 次要技能（OR 邏輯） |
| `location` | string | 否 | `"Taiwan"` | 地點（英文） |
| `location_zh` | string | 否 | 自動翻譯 | 地點（中文） |
| `pages` | int | 否 | `3` | 搜尋頁數 |
| `schedule_type` | string | 否 | `"once"` | 排程類型：`once` / `interval` / `daily` / `weekly` |
| `schedule_time` | string | 否 | `""` | 排程時間 `HH:MM`（daily/weekly 用） |
| `schedule_interval_hours` | int | 否 | `6` | 間隔小時數（interval 用） |
| `schedule_weekdays` | int[] | 否 | `[]` | 星期幾 0=Mon..6=Sun（weekly 用） |
| `step1ne_job_id` | int | 否 | `null` | Step1ne 系統職缺 ID |
| `auto_push` | bool | 否 | `false` | 完成後自動推送到 Step1ne |
| `run_now` | bool | 否 | `false` | 建立後立即執行 |

> **注意**：`schedule_type` 為 `once` 時會自動立即執行。

**回應範例**：
```json
{
  "id": "task-uuid-456",
  "task": { ... }
}
```

---

### 3. 更新任務

```http
PATCH /api/tasks/:id
```

**請求 Body**：任意可更新欄位。

---

### 4. 刪除任務

```http
DELETE /api/tasks/:id
```

---

### 5. 立即執行任務

```http
POST /api/tasks/:id/run
```

**回應範例**：
```json
{
  "success": true,
  "message": "任務已開始執行"
}
```

---

### 6. 查詢任務即時進度

```http
GET /api/tasks/:id/status
```

**回應範例**：
```json
{
  "id": "task-uuid-123",
  "status": "running",
  "progress": 45,
  "progress_detail": "LinkedIn 第 3/5 頁",
  "linkedin_count": 12,
  "github_count": 5,
  "ocr_count": 2
}
```

---

## 客戶 API

### 列出客戶

```http
GET /api/clients
```

**回應**：客戶名稱陣列。
```json
["TechCo", "FinBank", "StartupXYZ"]
```

---

## 評分系統 API

### 1. 對候選人評分

```http
POST /api/score/candidates
```

**請求 Body**：
```json
{
  "client_name": "TechCo",
  "job_title": "Backend Engineer",
  "primary_skills": ["Python", "Go"],
  "secondary_skills": ["Docker"],
  "candidate_ids": ["id1", "id2"]
}
```

| 欄位 | 必填 | 說明 |
|------|------|------|
| `client_name` | 是 | 客戶名稱 |
| `job_title` | 否 | 職缺名稱（用於建立 Job Profile） |
| `primary_skills` | 否 | 主要技能（若未提供，從任務或 job_title 自動推導） |
| `secondary_skills` | 否 | 次要技能 |
| `candidate_ids` | 否 | 指定評分的候選人 ID（若空，評分全部候選人） |

**回應範例**：
```json
{
  "scored": 2,
  "results": [
    {
      "id": "id1",
      "name": "John Doe",
      "score": 87,
      "grade": "A",
      "matched_skills": ["Python", "Go", "Docker"],
      "missing_critical": ["Kubernetes"]
    }
  ]
}
```

**評分等級**：

| 等級 | 分數範圍 |
|------|---------|
| A | 80-100 |
| B | 60-79 |
| C | 40-59 |
| D | 0-39 |

---

### 2. 取得評分細項

```http
GET /api/score/detail/:candidate_id
```

**回應範例**：
```json
{
  "score": 87,
  "grade": "A",
  "skill_matches": [
    { "skill": "Python", "matched": true, "weight": "primary" },
    { "skill": "Go", "matched": true, "weight": "primary" }
  ],
  "dimension_scores": {
    "skills": 45,
    "experience": 20,
    "activity": 12,
    "completeness": 10
  }
}
```

---

### 3. 取得 Job Profile

```http
GET /api/score/profile/:client_name/:job_title
```

---

### 4. 儲存自訂 Job Profile

```http
POST /api/score/profile
```

**請求 Body**：
```json
{
  "client_name": "TechCo",
  "job_title": "Backend Engineer",
  "profile": {
    "primary_skills": ["Python", "Go"],
    "secondary_skills": ["Docker", "K8s"],
    "weights": { "skills": 50, "experience": 25, "activity": 15, "completeness": 10 }
  }
}
```

---

## 關鍵字生成 API

### 1. 從職缺名稱生成搜尋關鍵字

```http
POST /api/keywords/generate
```

**請求 Body**：
```json
{
  "job_title": "Senior Full-Stack Engineer",
  "existing_skills": ["React"]
}
```

**回應範例**：
```json
{
  "primary_skills": ["JavaScript", "TypeScript", "React", "Node.js"],
  "secondary_skills": ["Docker", "AWS", "PostgreSQL"],
  "search_queries": ["Senior Full-Stack Engineer React Node.js"]
}
```

---

### 2. 取得所有已知技能列表

```http
GET /api/keywords/suggestions
```

**回應**：按字母排序的技能名稱陣列（供前端自動完成）。
```json
["AWS", "Angular", "C++", "Docker", "Go", "Java", "JavaScript", ...]
```

---

## LinkedIn OCR 分析 API

### 1. OCR 深度分析

對單一 LinkedIn 候選人截圖做 OCR 文字提取，可自動重新評分。

```http
POST /api/linkedin/ocr-analyze
```

**請求 Body**：
```json
{
  "candidate_id": "a1b2c3d4",
  "client_name": "TechCo",
  "linkedin_url": "https://linkedin.com/in/johndoe"
}
```

| 欄位 | 必填 | 說明 |
|------|------|------|
| `linkedin_url` | 是 | LinkedIn 個人檔案 URL |
| `candidate_id` | 否 | 候選人 ID（若提供則自動更新評分） |
| `client_name` | 否 | 客戶名稱（配合 candidate_id 使用） |

**回應範例**：
```json
{
  "success": true,
  "ocr_data": {
    "extracted_skills": ["Python", "Machine Learning", "TensorFlow"],
    "headline": "Senior ML Engineer at Google",
    "experience": "5+ years in AI/ML",
    "raw_text": "..."
  },
  "new_score": 92,
  "new_grade": "A",
  "previous_score": 45,
  "quota_remaining": 7
}
```

**配額限制**：每小時 10 次。超過回傳 `429`。

---

### 2. 查詢 OCR 配額

```http
GET /api/linkedin/ocr-quota
```

**回應範例**：
```json
{
  "quota_remaining": 7,
  "quota_total": 10
}
```

---

## GitHub 深度分析 API

### 深度分析 GitHub 用戶

```http
POST /api/github/analyze/:username
```

**路徑參數**：

| 參數 | 說明 |
|------|------|
| `username` | GitHub 用戶名 |

**回應範例**：
```json
{
  "username": "johndoe",
  "name": "John Doe",
  "bio": "Full-stack developer",
  "company": "TechCorp",
  "location": "Taipei",
  "email": "john@example.com",
  "public_repos": 42,
  "followers": 150,
  "top_languages": ["Python", "Go", "TypeScript"],
  "recent_activity": "2026-03-01",
  "notable_repos": [
    {
      "name": "awesome-project",
      "stars": 120,
      "language": "Python",
      "description": "A cool project"
    }
  ]
}
```

---

## 去重快取 API

### 1. 去重統計

```http
GET /api/dedup/stats
```

**回應範例**：
```json
{
  "total": 256,
  "by_source": { "linkedin": 180, "github": 76 },
  "cache_file": "data/dedup_cache.json"
}
```

---

### 2. 清除去重快取

```http
POST /api/dedup/clear
```

**請求 Body**（選填）：
```json
{
  "source": "linkedin"
}
```

> 不傳 `source` 則清除全部快取。

---

## 已處理紀錄 API

### 1. 列出已處理紀錄

```http
GET /api/processed
```

**回應**：已處理紀錄陣列（用於去重追蹤，記錄在 Google Sheets「去重」工作表）。

---

### 2. 更新已處理紀錄狀態

```http
PATCH /api/processed/:record_id
```

**請求 Body**：
```json
{
  "status": "imported",
  "system_id": 42
}
```

---

## Step1ne 系統整合 API

### 1. 從 Step1ne 拉取職缺

```http
GET /api/system/jobs
```

**回應範例**：
```json
{
  "connected": true,
  "jobs": [
    {
      "id": 1,
      "title": "Backend Engineer",
      "client_name": "TechCo",
      "job_status": "招募中",
      "skills": "Python, Go"
    }
  ]
}
```

> 連線失敗回傳 `503`：`{"error": "Step1ne 系統未連結", "connected": false}`

---

### 2. 測試 Step1ne 連線

```http
GET /api/system/test
```

**回應範例**：
```json
{
  "connected": true,
  "api_base": "http://localhost:3001",
  "job_count": 5
}
```

---

### 3. 推送候選人到 Step1ne

```http
POST /api/system/push
```

**請求 Body**：
```json
{
  "candidates": [
    {
      "name": "John Doe",
      "title": "Senior Engineer",
      "skills": ["Python", "Go"],
      "grade": "A",
      "score": 87,
      "source": "github",
      "email": "john@example.com",
      "linkedin_url": "https://linkedin.com/in/johndoe",
      "github_url": "https://github.com/johndoe",
      "location": "Taiwan",
      "company": "TechCorp",
      "bio": "Full-stack developer"
    }
  ],
  "min_grade": ""
}
```

| 欄位 | 必填 | 說明 |
|------|------|------|
| `candidates` | 是 | 候選人陣列（爬蟲原始格式） |
| `min_grade` | 否 | 最低等級篩選（`"A"` / `"B"` / `"C"` / `"D"` / `""`=全部） |

**回應範例**：
```json
{
  "success": true,
  "created_count": 1,
  "updated_count": 0,
  "failed_count": 0,
  "results": [
    { "name": "John Doe", "action": "created", "id": 42 }
  ]
}
```

> 此端點透過 Step1ne 的 `POST /api/crawler/import` 新版匯入端點運作，
> Step1ne 端會自動進行欄位映射（`title` → `current_position`、`skills[]` → `skills` 字串等）。

---

## Dashboard 統計 API

### Dashboard 總覽

```http
GET /api/dashboard/stats
```

**回應範例**：
```json
{
  "total_candidates": 256,
  "today_new": 12,
  "running_tasks": 1,
  "scheduled_tasks": 3,
  "clients": {
    "TechCo": 120,
    "FinBank": 80,
    "StartupXYZ": 56
  },
  "sources": {
    "linkedin": 180,
    "github": 70,
    "li+ocr": 6
  },
  "grades": {
    "A": 15,
    "B": 45,
    "C": 80,
    "D": 100,
    "": 16
  },
  "recent_runs": [
    {
      "task_id": "task-uuid-123",
      "client_name": "TechCo",
      "job_title": "Backend Engineer",
      "status": "completed",
      "last_run": "2026-03-05T10:00:00",
      "last_result_count": 23,
      "progress": 100
    }
  ]
}
```

---

## 設定 API

### 1. 讀取設定

```http
GET /api/settings
```

**回應範例**：
```json
{
  "step1ne": {
    "api_base_url": "http://localhost:3001",
    "auto_push": false
  },
  "crawler": {
    "headless": true,
    "max_pages": 10,
    "browser_pool_size": 3,
    "default_location": "Taiwan"
  },
  "anti_detect": {
    "request_delay": { "min": 2, "max": 5 },
    "page_delay": { "min": 3.0, "max": 6.0 }
  },
  "google_sheets": {
    "spreadsheet_id": "15X2NNK9...",
    "has_credentials": true
  },
  "api_keys": {
    "github_token_count": 1,
    "has_brave_key": true
  }
}
```

> **安全性**：API Key 完整值不會暴露，僅回傳數量/是否存在。

---

### 2. 更新設定

```http
POST /api/settings
```

**請求 Body**（部分更新，只傳需要改的區塊）：
```json
{
  "step1ne": {
    "api_base_url": "http://localhost:3001",
    "auto_push": true
  },
  "crawler": {
    "max_pages": 15,
    "headless": false
  }
}
```

> 更新後自動寫入 `config/default.yaml`。
> 若更新 `step1ne.api_base_url`，會自動重新初始化 Step1ne Client。

---

## 內部 API

### Worker 回報搜尋結果

```http
POST /api/internal/results
```

> 此端點供爬蟲 Worker 內部呼叫，將搜尋結果寫入 Google Sheets。

**請求 Body**：
```json
{
  "task_id": "task-uuid-123",
  "client_name": "TechCo",
  "candidates": [
    {
      "name": "John Doe",
      "source": "github",
      "github_url": "https://github.com/johndoe",
      "skills": ["Python", "Go"],
      "location": "Taiwan"
    }
  ]
}
```

---

## 資料模型

### Candidate（候選人）

| 欄位 | 類型 | 說明 |
|------|------|------|
| `id` | string | UUID |
| `name` | string | 姓名 |
| `source` | string | 來源：`"linkedin"` / `"github"` / `"li+ocr"` |
| `github_url` | string | GitHub 個人頁面 |
| `github_username` | string | GitHub 用戶名 |
| `linkedin_url` | string | LinkedIn 個人頁面 |
| `linkedin_username` | string | LinkedIn 用戶名 |
| `email` | string | Email |
| `location` | string | 地點 |
| `bio` | string | 簡介 |
| `company` | string | 公司 |
| `title` | string | 職稱 / Headline |
| `skills` | string[] | 技能列表 |
| `public_repos` | int | 公開 Repo 數（GitHub） |
| `followers` | int | 粉絲數（GitHub） |
| `recent_push` | string | 最近活躍（GitHub） |
| `top_repos` | string[] | 熱門 Repo（GitHub） |
| `client_name` | string | 搜尋的客戶名稱 |
| `job_title` | string | 搜尋的職缺名稱 |
| `task_id` | string | 關聯任務 ID |
| `search_date` | string | 搜尋日期 |
| `status` | string | 狀態：`"new"` / `"imported"` / `"reviewed"` / `"skipped"` |
| `created_at` | string | 建立時間 |
| `score` | int | 評分 0-100 |
| `grade` | string | 等級：`"A"` / `"B"` / `"C"` / `"D"` / `""`（未評分） |
| `score_detail` | string | 評分細項（JSON 字串） |

---

### SearchTask（搜尋任務）

| 欄位 | 類型 | 說明 |
|------|------|------|
| `id` | string | UUID |
| `client_name` | string | 客戶名稱 |
| `job_title` | string | 職缺名稱 |
| `primary_skills` | string[] | 主要技能（AND 邏輯） |
| `secondary_skills` | string[] | 次要技能（OR 邏輯） |
| `location` | string | 地點（英文） |
| `location_zh` | string | 地點（中文） |
| `pages` | int | 搜尋頁數 |
| `schedule_type` | string | `"once"` / `"interval"` / `"daily"` / `"weekly"` |
| `schedule_time` | string | 排程時間 `HH:MM` |
| `schedule_interval_hours` | int | 間隔小時數 |
| `schedule_weekdays` | int[] | 星期 0=Mon..6=Sun |
| `step1ne_job_id` | int? | Step1ne 系統職缺 ID |
| `auto_push` | bool | 完成後自動推送 |
| `status` | string | `"pending"` / `"running"` / `"completed"` / `"failed"` / `"paused"` |
| `progress` | int | 進度 0-100 |
| `progress_detail` | string | 進度描述 |
| `last_run` | string | 最後執行時間 |
| `last_result_count` | int | 最後結果數 |
| `linkedin_count` | int | LinkedIn 找到數 |
| `github_count` | int | GitHub 找到數 |
| `ocr_count` | int | OCR 補充數 |

---

## 錯誤處理

所有錯誤回傳統一格式：

```json
{
  "error": "錯誤描述"
}
```

**常見 HTTP 狀態碼**：

| 狀態碼 | 說明 |
|--------|------|
| `200` | 成功 |
| `201` | 建立成功（POST 新任務） |
| `400` | 請求參數錯誤 |
| `404` | 資源不存在 |
| `429` | 超過配額限制（OCR） |
| `503` | 依賴服務不可用（Google Sheets 未設定 / Step1ne 未連結） |

---

## 頁面路由（Web UI）

| 路徑 | 頁面 |
|------|------|
| `/` | Dashboard 總覽 |
| `/tasks` | 任務管理 |
| `/results` | 候選人結果 |
| `/settings` | 系統設定 |

---

## 與 Step1ne 系統的整合流程

```
                 ┌─────────────────────────┐
                 │   HeadHunter Crawler     │
                 │   (localhost:5000)       │
                 └──────┬──────────────────┘
                        │
            ┌───────────┼───────────┐
            │           │           │
     LinkedIn 搜尋  GitHub 搜尋   OCR 分析
            │           │           │
            └───────────┼───────────┘
                        ▼
              Google Sheets 儲存
                        │
         ┌──────────────┼──────────────┐
         │              │              │
    Web UI 推送    Auto-push      外部 API 呼叫
   POST /system/push  (任務完成後)   (OpenClaw 等)
         │              │              │
         └──────────────┼──────────────┘
                        ▼
           POST /api/crawler/import
                        │
                        ▼
              ┌─────────────────────┐
              │   Step1ne System    │
              │   (localhost:3001)  │
              └─────────────────────┘
```
