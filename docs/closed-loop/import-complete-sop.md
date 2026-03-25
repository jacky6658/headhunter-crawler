# import-complete API SOP — 2026-03-24 起生效

## 端點
```
POST https://api-hr.step1ne.com/api/ai-agent/candidates/import-complete
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

## 規則
1. `require_complete: true` → 缺 PDF、target_job_id 或 talent_level 直接拒絕
2. PDF 用 base64 放在 JSON 裡，不用另外呼叫上傳 API
3. ai_analysis 跟 candidate 一起送
4. `candidate_written: false` → DB 完全沒動，可修正後重試
5. 去重：LinkedIn URL > Email > Name，已存在會 UPDATE

## Payload 範本
```json
{
  "candidate": {
    "name": "陳小明",
    "email": "chen@example.com",
    "linkedin_url": "https://linkedin.com/in/chen",
    "current_position": "Senior Engineer",
    "current_company": "Google",
    "skills": "Python、Go、PostgreSQL",
    "years_experience": "8",
    "location": "Taipei",
    "target_job_id": 42,
    "talent_level": "A",
    "status": "未開始",
    "recruiter": "待指派",
    "work_history": [
      {"company": "Google", "title": "Senior Engineer", "from": "2022-01", "to": "now", "description": "負責搜尋廣告後端"}
    ],
    "education_details": [
      {"school": "台灣大學", "degree": "碩士", "major": "資訊工程", "graduation": "2019"}
    ],
    "ai_match_result": {
      "grade": "A", "score": 88,
      "summary": "8年後端經驗，Google背景，技術棧高度匹配"
    },
    "notes": "LinkedIn主動開發"
  },
  "resume_pdf": {
    "base64": "<PDF base64>",
    "filename": "LinkedIn_陳小明.pdf",
    "format": "auto"
  },
  "ai_analysis": {
    "version": "1.0",
    "analyzed_at": "2026-03-24T14:00:00+08:00",
    "analyzed_by": "Lobster",
    "candidate_evaluation": { "..." : "見 ai-analysis-format.md" },
    "job_matchings": [{ "..." : "見 ai-analysis-format.md" }],
    "recommendation": { "..." : "見 ai-analysis-format.md" }
  },
  "actor": "Lobster",
  "require_complete": true
}
```

## 廢棄的舊流程（不要再用）
- ❌ POST /api/candidates（建檔）
- ❌ POST /api/candidates/:id/resume-parse（上傳 PDF）
- ❌ PUT /api/ai-agent/candidates/:id/ai-analysis（寫 AI 分析）

全部用 import-complete 一次完成。
