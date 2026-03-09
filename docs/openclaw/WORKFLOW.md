# OpenClaw 工作流程 SOP

## 概述

OpenClaw 是獨立的本地 AI 工具，負責對 Step1ne 系統中 `status='爬蟲初篩'` 的候選人進行深度 AI 分析（複篩）。

**定位**：Crawler（初篩）→ Step1ne（儲存）→ **OpenClaw（複篩）**

---

## 系統需求

- Python 3.10+
- 可存取 Step1ne Backend API（Zeabur 或本地 localhost:3001）
- AI 模型 API Key（Perplexity sonar-pro 或其他 LLM）

---

## 設定

### 環境變數

```bash
export OPENCLAW_API_KEY="your-openclaw-api-key"
export STEP1NE_API_URL="https://backendstep1ne.zeabur.app"
export PERPLEXITY_API_KEY="your-perplexity-key"
```

### 設定檔（config.yaml）

```yaml
step1ne:
  api_url: ${STEP1NE_API_URL}
  openclaw_key: ${OPENCLAW_API_KEY}
  batch_size: 20

ai:
  provider: perplexity
  model: sonar-pro
  scoring_model: sonar
  temperature: 0.1
  max_retries: 2
  timeout: 60

scoring:
  min_score_threshold: 25
  auto_status_update: true
  grade_thresholds:
    A: 85
    B: 70
    C: 55
    D: 0
```

---

## 完整工作流程

### Step 1: 拉取待分析候選人

```
GET /api/openclaw/pending?limit=50&job_id={optional}
Header: X-OpenClaw-Key: {api_key}
```

回傳所有 `status='爬蟲初篩'` 的候選人，包含：
- 基本資料（name, email, location, current_position, skills）
- Enrichment 資料（work_history, education_details）
- 初篩標籤（ai_match_result 中的 match_tags）
- 目標職缺 ID（target_job_id）

### Step 2: 取得職缺詳情

如果候選人有 `target_job_id`，需要取得對應職缺的完整資料：
- position_name, client_company
- job_description, talent_profile, company_profile
- key_skills, experience_required

可用 Step1ne 的職缺 API：
```
GET /api/jobs/{job_id}
```

### Step 3: AI 深度分析

對每位候選人執行 5 維度 AI 匹配評分：

1. **組裝 System Prompt**（per job 固定，見 PROMPTS.md）
   - 填入職缺資訊
   - 設定三道閘門 + 五維度評分規則

2. **組裝 User Prompt**（per candidate 變動）
   - 填入候選人完整資料（含 work_history, education_details）

3. **呼叫 AI**
   - model: sonar-pro（深度分析用）
   - temperature: 0.1（確保一致性）

4. **解析回應**
   - 提取 JSON（可能被 markdown code block 包裹）
   - 驗證 score 範圍（0-100）
   - 驗證 recommendation 值

5. **三門後處理**（見 SCORING-RULES.md）
   - 名字黑名單檢查
   - 分數合理性驗證
   - Grade 映射

### Step 4: 批量回寫結果

```
POST /api/openclaw/batch-update
Header: X-OpenClaw-Key: {api_key}
Body: { candidates: [...] }
```

每位候選人回寫：
- `ai_match_result`（JSONB）：完整 AI 分析結果
- `ai_score`（int）：0-100 綜合分數
- `ai_grade`（text）：A/B/C/D
- `ai_report`（text）：AI 分析報告
- `status`（text）：
  - A/B 級 → `'AI推薦'`（顧問應聯繫的人選）
  - C/D 級 → `'備選人才'`（不符合當前職缺，但保留在人才池，可能適合其他職缺）

### Step 5: 不符合候選人處理

`完成不符合` 的候選人（三個 match_tags 都不匹配）**不丟棄**：
- AI 應建議其他可能適合的職缺
- 在 ai_report 中說明為何不符合當前職缺
- 在 ai_match_result 中加入 `suggested_jobs` 欄位

---

## 錯誤處理

| 錯誤 | 處理方式 |
|------|----------|
| API 401 | 檢查 X-OpenClaw-Key 是否正確 |
| API 503 | 等待 30 秒後重試，最多 3 次 |
| AI 回應非 JSON | 嘗試從 markdown code block 提取 |
| AI 回應缺欄位 | 使用預設值填充，標記 `data_completeness: partial` |
| 單一候選人失敗 | 記錄錯誤，跳過繼續處理下一位 |
| 批量回寫部分失敗 | API 回傳 `results.errors[]`，記錄後重試失敗的 |

---

## 批次處理建議

- 每批最多 50 位候選人（API 限制 100）
- 按 `target_job_id` 分組處理（同職缺共用 system prompt，省 token）
- AI 呼叫間隔 1-2 秒（避免 rate limit）
- 每完成一批立即回寫（不要全部分析完才回寫）

---

## 日誌

建議記錄：
- 每次拉取的候選人數量
- 每位候選人的 AI 分析耗時
- AI 評分結果分布（A/B/C/D 各幾位）
- 錯誤率和重試次數
- Token 使用量（成本追蹤）
