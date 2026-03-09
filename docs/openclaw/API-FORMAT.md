# OpenClaw API 格式規格

## 認證

所有請求需要在 Header 中帶入 API Key：

```
X-OpenClaw-Key: {your-api-key}
```

API Key 在 Step1ne Backend 環境變數 `OPENCLAW_API_KEY` 中設定。

---

## GET /api/openclaw/pending

取得所有 `status='爬蟲初篩'` 的候選人。

### 請求

```bash
curl -X GET "https://backendstep1ne.zeabur.app/api/openclaw/pending?limit=50&job_id=123" \
  -H "X-OpenClaw-Key: your-api-key"
```

### Query Parameters

| 參數 | 類型 | 預設 | 說明 |
|------|------|------|------|
| `limit` | int | 50 | 每頁筆數（最大 200）|
| `offset` | int | 0 | 跳過筆數（分頁用）|
| `job_id` | int | - | 篩選特定職缺的候選人 |

### 回應 (200)

```json
{
  "success": true,
  "data": [
    {
      "id": 123,
      "name": "王小明",
      "email": "wang@example.com",
      "phone": "0912345678",
      "location": "台北",
      "current_position": "Senior Software Engineer",
      "skills": "Python、Java、React",
      "talent_level": "B",
      "source": "爬蟲匯入",
      "linkedin_url": "https://linkedin.com/in/xiaoming",
      "github_url": "https://github.com/xiaoming",
      "work_history": [
        {
          "company": "Google",
          "title": "Software Engineer",
          "duration": "2020-01 - 2023-06",
          "description": "Backend development with Java and Python"
        }
      ],
      "education_details": [
        {
          "school": "台灣大學",
          "degree": "碩士",
          "field": "資訊工程",
          "year": "2020"
        }
      ],
      "ai_match_result": {
        "match_tags": {
          "skill_match": ["Python", "Java"],
          "title_match": true,
          "experience_match": ["Software Engineer @ Google"]
        }
      },
      "ai_score": null,
      "ai_grade": null,
      "ai_report": null,
      "score": 75,
      "score_detail": "{...}",
      "notes": "Crawler 評分: 75 (B)",
      "target_job_id": 45,
      "status": "爬蟲初篩",
      "recruiter": "待指派",
      "created_at": "2025-03-09T10:30:00Z",
      "updated_at": "2025-03-09T10:30:00Z"
    }
  ],
  "pagination": {
    "total": 150,
    "limit": 50,
    "offset": 0,
    "has_more": true
  }
}
```

### 錯誤回應

```json
// 401 Unauthorized
{ "success": false, "error": "Unauthorized: invalid or missing X-OpenClaw-Key header" }

// 500 Server Error
{ "success": false, "error": "Connection refused" }
```

---

## POST /api/openclaw/batch-update

批量回寫 AI 分析結果。

### 請求

```bash
curl -X POST "https://backendstep1ne.zeabur.app/api/openclaw/batch-update" \
  -H "X-OpenClaw-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "candidates": [
      {
        "id": 123,
        "ai_match_result": {
          "match_tags": {
            "skill_match": ["Python", "Java"],
            "title_match": true,
            "experience_match": ["Software Engineer @ Google"]
          },
          "relevance_check": {
            "job_core_domain": "後端工程",
            "candidate_domain": "軟體工程",
            "is_relevant": true,
            "relevance_note": "核心領域高度相關",
            "location_gate": "pass",
            "seniority_gate": "match",
            "data_completeness": "rich"
          },
          "score": 82,
          "recommendation": "推薦",
          "matched_skills": ["Python", "Java", "React"],
          "missing_skills": ["Kubernetes"],
          "strengths": ["Google 工作經歷", "全端能力"],
          "probing_questions": [
            "[初步] 目前在職狀態？",
            "[技術] K8s 經驗如何？",
            "[文化] 對新創環境的看法？"
          ],
          "career_trajectory": {
            "direction": "上升型",
            "industry_consistency": "專注軟體業",
            "tenure_pattern": "穩定（平均 3 年）",
            "red_flags": []
          },
          "company_dna_analysis": {
            "scale_match": "大廠→中型公司，可接受",
            "industry_match": "同為軟體業",
            "culture_fit": "外商→本土科技，需確認"
          },
          "conclusion": "王小明具備紮實的後端開發經驗..."
        },
        "ai_score": 82,
        "ai_grade": "B",
        "ai_report": "AI 匹配分析報告...",
        "status": "AI推薦"
      }
    ]
  }'
```

### 可更新欄位

| 欄位 | 類型 | 說明 |
|------|------|------|
| `id` | int | **必填** — 候選人 ID |
| `ai_match_result` | object/string | AI 分析完整結果（JSONB）|
| `ai_score` | int | 0-100 綜合分數 |
| `ai_grade` | string | A/B/C/D |
| `ai_report` | string | AI 分析報告文字 |
| `ai_recommendation` | string | 強力推薦/推薦/觀望/不推薦 |
| `status` | string | 更新狀態（如 'AI推薦'）|
| `talent_level` | string | 人才等級 |
| `notes` | string | 附加備註 |

### 回應 (200)

```json
{
  "success": true,
  "results": {
    "updated": 48,
    "failed": 2,
    "errors": [
      { "id": 999, "error": "Candidate not found" },
      { "id": null, "error": "Missing candidate id" }
    ]
  }
}
```

### 限制

- 每批最多 **100** 位候選人
- `ai_match_result` 如果是 object 會自動轉為 JSON 字串存入 JSONB 欄位
- 每次更新會自動在 `progress_tracking` 中記錄一筆事件

### 回寫的 progress_tracking 格式

每次 batch-update 會自動附加：

```json
{
  "event": "AI分析完成",
  "by": "OpenClaw",
  "at": "2025-03-09T15:30:00.000Z",
  "note": "AI評等: B"
}
```

---

## 狀態流轉

```
爬蟲初篩 → (OpenClaw 分析) → AI推薦   (A/B 級) → 顧問聯繫
爬蟲初篩 → (OpenClaw 分析) → 備選人才  (C/D 級) → 留在人才池，可能適合其他職缺
```

status 設定邏輯：
- A 級（85+）→ `'AI推薦'`（顧問應優先聯繫）
- B 級（70-84）→ `'AI推薦'`（顧問安排聯繫）
- C 級（55-69）→ `'備選人才'`（觀望，可能適合其他職缺）
- D 級（<55）→ `'備選人才'`（不符合當前職缺，但保留在人才池）
