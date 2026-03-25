# AI Agent 整合指南 — HeadHunter Crawler

> 本文件專為 AI Agent 設計。如果你是 AI，請閱讀以下內容來操作爬蟲系統和獵頭系統。

---

## 系統概述

你可以操作兩個系統：

| 系統 | 用途 | API 位址 |
|------|------|---------|
| **爬蟲系統** | 搜索 LinkedIn/GitHub 候選人 | `http://localhost:5001/api` |
| **Step1ne 獵頭系統** | 管理候選人、職缺 | `https://backendstep1ne.zeabur.app/api` |

> 注意：爬蟲系統需要在本地運行，Step1ne 系統在雲端。

---

## 核心工作流程

### 流程 1：從職缺出發，自動搜索候選人

```
步驟 1 → 取得職缺列表
步驟 2 → 選擇職缺，提取搜索關鍵字
步驟 3 → 建立爬蟲任務
步驟 4 → 啟動任務，等待完成
步驟 5 → 查看搜索結果
步驟 6 → 推送候選人到系統
```

### 流程 2：手動指定關鍵字搜索

```
步驟 1 → 直接建立任務（指定關鍵字）
步驟 2 → 啟動任務
步驟 3 → 查看結果 + 評分
步驟 4 → 推送到系統
```

---

## API 操作詳細說明

### 步驟 1：取得系統職缺

```bash
GET http://localhost:5001/api/system/jobs
```

回應範例：
```json
{
  "connected": true,
  "jobs": [
    {
      "id": 51,
      "position_name": "BIM工程師",
      "client_company": "XX營造",
      "key_skills": "BIM, Revit, AutoCAD",
      "job_status": "招募中",
      "search_primary": "BIM Revit",
      "search_secondary": "AutoCAD Navisworks"
    }
  ]
}
```

> 如果 `search_primary` 已有值，可直接用來建任務。若為空，用步驟 1b 生成。

### 步驟 1b：自動生成搜索關鍵字（可選）

```bash
POST http://localhost:5001/api/keywords/generate
Content-Type: application/json

{
  "job_title": "BIM工程師"
}
```

回應：
```json
{
  "primary": ["BIM", "Revit"],
  "secondary": ["AutoCAD", "Navisworks", "建築資訊模型"]
}
```

### 步驟 2：建立爬蟲任務

```bash
POST http://localhost:5001/api/tasks
Content-Type: application/json

{
  "client_name": "XX營造",
  "job_title": "BIM工程師",
  "primary_skills": ["BIM", "Revit"],
  "secondary_skills": ["AutoCAD", "Navisworks"],
  "location": "Taiwan",
  "pages": 3,
  "step1ne_job_id": 51,
  "auto_push": false
}
```

回應：
```json
{
  "task": {
    "id": "abc123-...",
    "status": "pending",
    "client_name": "XX營造",
    "job_title": "BIM工程師"
  }
}
```

> **重要欄位**：
> - `primary_skills`：主關鍵字，搜索時用 AND 邏輯（都要出現）
> - `secondary_skills`：次關鍵字，用 OR 邏輯（出現任一即可）
> - `step1ne_job_id`：對應 Step1ne 系統的職缺 ID，匯入時會自動關聯
> - `auto_push`：設 true 則爬完自動推送到系統
> - `pages`：搜索頁數，1-10，數字越大結果越多但越慢

### 步驟 3：啟動任務

```bash
POST http://localhost:5001/api/tasks/{task_id}/start
```

回應：
```json
{
  "message": "任務已啟動",
  "task_id": "abc123-..."
}
```

### 步驟 4：輪詢任務狀態（等待完成）

```bash
GET http://localhost:5001/api/tasks/{task_id}
```

回應：
```json
{
  "task": {
    "id": "abc123-...",
    "status": "running",
    "progress": 45,
    "progress_detail": "LinkedIn 第 2/3 頁",
    "linkedin_count": 12,
    "github_count": 5
  }
}
```

> **輪詢策略**：每 10 秒查一次，直到 `status` 變為 `completed` 或 `failed`。
> - `pending` → 等待中
> - `running` → 執行中（看 progress 和 progress_detail）
> - `completed` → 完成（看 last_result_count）
> - `failed` → 失敗（看 error_message）

### 步驟 5：查看搜索結果

```bash
# 取得所有候選人
GET http://localhost:5001/api/candidates

# 篩選特定任務的候選人
GET http://localhost:5001/api/candidates?task_id={task_id}

# 篩選特定等級
GET http://localhost:5001/api/candidates?grade=A

# 篩選特定客戶
GET http://localhost:5001/api/candidates?client_name=XX營造
```

回應：
```json
{
  "total": 35,
  "items": [
    {
      "id": "cand-001",
      "name": "王小明",
      "title": "BIM Engineer",
      "company": "XX建設",
      "source": "linkedin",
      "linkedin_url": "https://linkedin.com/in/xxx",
      "skills": ["BIM", "Revit", "AutoCAD"],
      "score": 87,
      "grade": "A",
      "location": "Taipei, Taiwan"
    }
  ]
}
```

### 步驟 6：推送候選人到 Step1ne 系統

```bash
# 推送特定任務的所有候選人
POST http://localhost:5001/api/system/push
Content-Type: application/json

{
  "task_id": "abc123-...",
  "min_grade": "D"
}
```

或推送指定候選人：
```bash
POST http://localhost:5001/api/system/push
Content-Type: application/json

{
  "candidate_ids": ["cand-001", "cand-002", "cand-003"]
}
```

回應：
```json
{
  "success": true,
  "created_count": 15,
  "updated_count": 3,
  "failed_count": 0
}
```

> **去重機制**：系統會自動按候選人姓名去重。重複匯入同一人只會更新空欄位，不會新增重複記錄。

---

## Step1ne 系統 API（雲端）

以下 API 直接操作 Step1ne 獵頭系統，用於查看/更新已匯入的候選人。

### 查看系統中的候選人

```bash
GET https://backendstep1ne.zeabur.app/api/candidates?limit=100
```

回應：
```json
{
  "success": true,
  "data": [
    {
      "id": "123",
      "name": "王小明",
      "position": "BIM Engineer",
      "consultant": "Phoebe",
      "status": "未開始",
      "targetJobId": 51
    }
  ],
  "count": 832
}
```

### 更新候選人資料

```bash
PATCH https://backendstep1ne.zeabur.app/api/candidates/{id}
Content-Type: application/json

{
  "recruiter": "Phoebe",
  "skills": "BIM, Revit, AutoCAD",
  "years_experience": "5",
  "talent_level": "A",
  "notes": "AI 分析：此候選人具有 5 年 BIM 經驗..."
}
```

### 可更新的候選人欄位

| 欄位 | 說明 | 範例 |
|------|------|------|
| `recruiter` | 負責顧問 | "Phoebe" |
| `skills` | 技能（逗號分隔） | "BIM, Revit, AutoCAD" |
| `years_experience` | 年資 | "5" |
| `talent_level` | 等級 | "A" / "B" / "C" / "D" |
| `current_position` | 現職 | "Senior BIM Engineer" |
| `location` | 地點 | "Taipei" |
| `education` | 學歷 | "碩士" |
| `notes` | 備註 | "AI 評估摘要..." |
| `stability_score` | 穩定度分數 | "75" |
| `personality_type` | 人格特質 | "INTJ" |
| `job_changes` | 換工作次數 | "3" |
| `avg_tenure_months` | 平均任期（月） | "24" |
| `leaving_reason` | 離職原因 | "尋求更大挑戰" |
| `work_history` | 工作經歷（JSON） | `[{"company":"XX","title":"工程師","period":"2020-2023"}]` |
| `education_details` | 教育背景（JSON） | `[{"school":"台大","degree":"碩士","year":"2019"}]` |
| `status` | 狀態 | "未開始" / "已聯繫" / "面試中" / "錄取" / "婉拒" |

### 查看職缺列表

```bash
GET https://backendstep1ne.zeabur.app/api/jobs
```

### 查看職缺詳情（含完整 JD）

```bash
GET https://backendstep1ne.zeabur.app/api/jobs/{id}
```

---

## 直接匯入候選人到系統（跳過爬蟲）

如果 AI 自己找到了候選人資料，可以直接匯入：

```bash
POST https://backendstep1ne.zeabur.app/api/crawler/import
Content-Type: application/json

{
  "candidates": [
    {
      "name": "王小明",
      "title": "Senior BIM Engineer",
      "company": "XX建設",
      "email": "wang@example.com",
      "linkedin_url": "https://linkedin.com/in/xxx",
      "skills": ["BIM", "Revit", "AutoCAD"],
      "location": "Taipei",
      "grade": "A",
      "score": 87,
      "source": "linkedin",
      "step1ne_job_id": 51
    }
  ],
  "actor": "AI-Agent"
}
```

---

## 智能策略建議

### 搜索無結果時的處理

當任務完成但 `last_result_count == 0` 時，建議：

1. **擴展關鍵字**：使用同義詞（例："BIM" → "建築資訊模型"、"VDC"）
2. **放寬條件**：增加 `pages`、減少 `primary_skills`
3. **換搜索角度**：嘗試相關職位（例："BIM工程師" → "結構工程師 Revit"）
4. **更新任務**：`PUT /api/tasks/{id}` 修改關鍵字後重新啟動

### 評分參考

| 等級 | 分數範圍 | 含義 |
|------|---------|------|
| A | 80-100 | 高度匹配，優先聯繫 |
| B | 60-79 | 中等匹配，值得評估 |
| C | 40-59 | 部分匹配，備選 |
| D | 0-39 | 低匹配，可跳過 |

### 指派顧問建議

匯入後，應根據職缺類型指派對應的顧問（recruiter）：
```bash
PATCH https://backendstep1ne.zeabur.app/api/candidates/{id}
Content-Type: application/json

{ "recruiter": "顧問名稱" }
```

---

## 完整自動化範例

以下是 AI 完整執行一次搜索的流程：

```
1. GET  localhost:5001/api/system/jobs
   → 找到: id=51, "BIM工程師", search_primary="BIM Revit"

2. POST localhost:5001/api/tasks
   → { client_name: "XX營造", job_title: "BIM工程師",
       primary_skills: ["BIM","Revit"], step1ne_job_id: 51 }
   → 回傳 task_id

3. POST localhost:5001/api/tasks/{task_id}/start
   → 任務開始執行

4. GET  localhost:5001/api/tasks/{task_id}  (每 10 秒)
   → 等到 status == "completed", last_result_count == 25

5. GET  localhost:5001/api/candidates?task_id={task_id}
   → 取得 25 位候選人

6. POST localhost:5001/api/system/push
   → { task_id: task_id, min_grade: "C" }
   → 推送 A/B/C 級候選人到系統

7. 完成！回報：「已搜索 BIM工程師，找到 25 位候選人，
   其中 18 位已匯入系統（A 級 5 位、B 級 8 位、C 級 5 位）」
```

---

## 注意事項

- 所有 POST/PUT/PATCH 請求都需要 `Content-Type: application/json`
- 爬蟲系統 (`localhost:5001`) 必須在本地運行才能使用
- Step1ne 系統 (`backendstep1ne.zeabur.app`) 是雲端服務，隨時可用
- 任務執行時間取決於 `pages` 數量，通常 3 頁約需 2-5 分鐘
- 候選人姓名是去重的唯一依據，同名候選人只會更新不會重複建立
