# AI 深度分析 JSON 格式規範

## 品質標準（對照 #1890 Zedd pai）

以下欄位全部必填，不達標不准匯入。

## 完整格式

```json
{
  "version": "1.0",
  "analyzed_at": "2026-03-24T14:00:00+08:00",
  "analyzed_by": "Lobster",

  "candidate_evaluation": {
    "career_curve": {
      "summary": "從 LINE 到 Google，技術深度持續提升，走 IC 路線",
      "pattern": "穩定上升 | 探索期 | 頻繁轉換 | 管理轉型",
      "details": [
        {
          "company": "Google",
          "industry": "科技",
          "title": "Senior Backend Engineer",
          "duration": "3y+",
          "move_reason": "目前在職"
        }
      ]
    },

    "personality": {
      "type": "技術導向的深度思考者",
      "top3_strengths": ["系統設計能力強", "大規模系統經驗", "學習能力快"],
      "weaknesses": ["管理經驗不足"],
      "evidence": "在 Google 負責搜尋廣告核心系統重構"
    },

    "role_positioning": {
      "actual_role": "Senior Backend Engineer",
      "spectrum_position": "Junior | Mid | Senior | Staff | Lead | Manager",
      "best_fit": ["Staff Engineer", "Tech Lead"],
      "not_fit": ["Engineering Manager", "Frontend"]
    },

    "salary_estimate": {
      "actual_years": 8,
      "current_level": "Senior",
      "current_estimate": "250萬/年",
      "expected_range": "280-320萬/年",
      "risks": ["Google RSU 可能是留任關鍵"]
    }
  },

  "job_matchings": [
    {
      "job_id": 42,
      "job_title": "Senior Backend Engineer",
      "company": "某新創",
      "match_score": 88,
      "verdict": "強烈推薦 | 推薦 | 條件式 | 待確認 | 不推薦",
      "company_analysis": "高速成長期新創，技術導向文化",

      "must_have": [
        {"condition": "3+ 年後端經驗", "actual": "8 年", "result": "pass"},
        {"condition": "Go 或 Python", "actual": "兩者皆精通", "result": "pass"},
        {"condition": "分散式系統經驗", "actual": "Google 搜尋廣告系統", "result": "pass"}
      ],
      "nice_to_have": [
        {"condition": "Kubernetes 經驗", "actual": "有", "result": "pass"},
        {"condition": "帶人經驗", "actual": "無正式管理經驗", "result": "warning"}
      ],

      "strongest_match": "大規模分散式系統設計經驗完全匹配",
      "main_gap": "無正式管理經驗，但職缺不要求",
      "hard_block": "無",
      "salary_fit": "預算範圍內"
    }
  ],

  "recommendation": {
    "summary_table": [
      {"job_id": 42, "job_title": "Senior BE", "company": "某新創", "score": 88, "verdict": "強烈推薦", "priority": 1}
    ],
    "first_call_job_id": 42,
    "first_call_reason": "技術棧完全匹配，Google 背景加分",
    "overall_pushability": "高 | 中 | 低",
    "pushability_detail": "目前在職但對新創有興趣",
    "fallback_note": "若此職缺不合適，可推薦職缺 #58"
  }
}
```

## 最低要求（抽查清單）

| 區塊 | 必須 |
|------|------|
| career_curve.summary | 不能只貼職稱，要有分析 |
| career_curve.details | 每段要有 move_reason |
| personality.top3_strengths | 至少 3 個具體內容 |
| personality.evidence | 引用履歷事實 |
| role_positioning | actual_role + best_fit + not_fit |
| salary_estimate | actual_years + current_estimate + risks |
| must_have | 至少 3 條，每條有 condition/actual/result |
| nice_to_have | 至少 2 條 |
| salary_fit | 具體分析，不是只寫「ok」 |
| recommendation | overall_pushability 有理由 + fallback_note |

## result 值

- `pass` — 完全符合
- `warning` — 部分符合或需確認
- `fail` — 不符合
