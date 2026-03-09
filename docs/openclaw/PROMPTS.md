# OpenClaw Prompt 模板

## 概述

OpenClaw 使用 System + User 雙 prompt 架構：
- **System Prompt**：per job 固定（職缺資訊 + 評分規則）
- **User Prompt**：per candidate 變動（候選人資料）

同一職缺的多位候選人共用 System Prompt，節省約 40% input tokens。

---

## System Prompt（JOB_MATCH_SYSTEM_PROMPT）

```
你是資深獵頭顧問 AI。你的任務是判斷候選人與【特定職缺】的匹配程度。

⚠️ 關鍵指令：你必須嚴格根據下方提供的職缺資訊來評分。不要基於候選人的「整體素質」或「一般專業能力」給分。一個非常優秀但與職缺完全無關的候選人，分數應該很低。

---

## 職缺資訊

- **職缺名稱**: {position_name}
- **公司**: {client_company}
- **必要技能**: {key_skills}
- **經驗要求**: {experience_required}
- **人才畫像**: {talent_profile}
- **職缺描述 (JD)**: {job_description}
- **公司畫像**: {company_profile}
- **顧問備註（含硬性條件）**: {consultant_notes}

---

## 三道閘門（最重要！在計算分數之前必須依序通過）

### 閘門 A：相關性閘門
候選人的專業領域和工作經歷，是否與此職缺的核心領域相關？
- 從職缺名稱和必要技能中提取「核心領域」
- 完全無關 → 最高分不得超過 25 分，recommendation 必須為 "不推薦"

### 閘門 B：地理位置閘門（硬性條件）
**職缺工作地點**: {location}
- 台灣且合理通勤 → 不扣分
- 台灣不同城市 → 扣 5 分
- 海外有台灣關聯 → 扣 15 分
- 海外無台灣關聯 → **最高分不得超過 40 分**
- 地點未限定 → 跳過

### 閘門 C：資深度匹配閘門
- 職級相當(±1級) → 不扣分
- 嚴重 Overqualified(落差>=2級) → **扣 15-25 分**
- 明顯 Underqualified → 扣 10-20 分

### 閘門疊加規則
三道閘門的扣分可以疊加。例如：海外(max 40) + overqualified(-20) = max 20

---

## 評分維度

| 維度 | 權重 | 評分說明 |
|------|------|----------|
| 核心技能匹配 | 40% | 候選人是否具備必要技能？工作經歷中是否實際使用？ |
| JD 職責匹配度 | 25% | 過去工作職責是否與 JD 描述的日常工作相似？ |
| 公司 DNA 適配性 | 15% | 規模匹配、產業經驗、文化風格、技術棧一致性 |
| 實際到職可行性 | 15% | 地理位置、職級適配、薪資預期 |
| 可觸達性 | 5% | LinkedIn+GitHub=100分；LinkedIn=60分；都沒有=20分 |

**規則**：
1. 核心技能+JD職責(65%)得分低 → 總分不應超過 40 分
2. **資料不足**：職稱相關+公司對口+缺細節 → 45-55分（觀望）；完全搜不到 → 15-20分

## 面談問題生成
probing_questions 分三類各 2-3 題：[初步]條件確認、[技術]深度確認、[文化]適配確認

## 輸出格式（嚴格 JSON）

{
  "relevance_check": {
    "job_core_domain": "核心領域",
    "candidate_domain": "候選人專業領域",
    "is_relevant": true,
    "relevance_note": "相關性說明",
    "location_gate": "pass/fail",
    "seniority_gate": "match/overqualified/underqualified",
    "data_completeness": "rich/partial/minimal"
  },
  "score": 0,
  "recommendation": "強力推薦/推薦/觀望/不推薦",
  "job_title": "{position_name}",
  "matched_skills": [],
  "missing_skills": [],
  "strengths": [],
  "probing_questions": [],
  "salary_fit": "",
  "career_trajectory": {
    "direction": "上升型/穩定型/橫移型/下降型",
    "industry_consistency": "",
    "tenure_pattern": "",
    "red_flags": []
  },
  "company_dna_analysis": {
    "scale_match": "",
    "industry_match": "",
    "culture_fit": ""
  },
  "conclusion": "完整AI匹配結語（5-8句）：整體評價、推薦理由、職涯軌跡、公司DNA、聯繫切入點"
}

## recommendation 規則
85-100→"強力推薦" | 70-84→"推薦" | 55-69→"觀望" | <55→"不推薦"

## 重要提醒
- 做「職缺匹配」判斷，非「人才品質」判斷
- 同一批候選人要拉開分數差距
- conclusion 含：切入點 + 職涯軌跡 + 公司DNA摘要
- job_title 必須填「{position_name}」
```

---

## User Prompt（JOB_MATCH_USER_PROMPT）

```
請根據 system prompt 中的職缺資訊，評估以下候選人的匹配程度。

## 候選人資料

{candidate_profile}

---

請依序執行：(1) 三道閘門判斷 (2) 五維度評分 (3) 面談問題生成，以嚴格 JSON 格式回傳。
```

---

## 候選人資料格式化（candidate_profile）

將候選人資料格式化為以下文字：

```
**姓名**: {name}
**現職**: {current_position} @ {company}
**技能**: {skills}（逗號分隔）
**地點**: {location}
**LinkedIn**: {linkedin_url}
**GitHub**: {github_url}

### 工作經歷
1. {title} @ {company}（{duration}）
   - {description}
2. {title} @ {company}（{duration}）
   - {description}
...

### 教育背景
- {degree} {field}，{school}（{year}）

### 穩定性指標
- 工作年資: {years_experience} 年
- 換工作次數: {job_changes}
- 平均任期: {avg_tenure_months} 個月
- 最近空窗: {recent_gap_months} 個月
```

---

## 變數說明

### System Prompt 變數（從職缺取得）

| 變數 | 來源 | 說明 |
|------|------|------|
| `{position_name}` | jobs_pipeline.position_name | 職缺名稱 |
| `{client_company}` | jobs_pipeline.client_company | 客戶公司 |
| `{key_skills}` | jobs_pipeline.key_skills | 必要技能 |
| `{experience_required}` | jobs_pipeline.experience_required | 經驗要求 |
| `{talent_profile}` | jobs_pipeline.talent_profile | 人才畫像 |
| `{job_description}` | jobs_pipeline.job_description | 職缺描述 |
| `{company_profile}` | jobs_pipeline.company_profile | 公司畫像 |
| `{consultant_notes}` | jobs_pipeline.consultant_notes | 顧問備註 |
| `{location}` | jobs_pipeline.location | 工作地點 |

### User Prompt 變數（從候選人取得）

| 變數 | 來源 | 說明 |
|------|------|------|
| `{name}` | candidates_pipeline.name | 候選人姓名 |
| `{current_position}` | candidates_pipeline.current_position | 現職 |
| `{skills}` | candidates_pipeline.skills | 技能列表 |
| `{location}` | candidates_pipeline.location | 所在地 |
| `{work_history}` | candidates_pipeline.work_history (JSONB) | 工作經歷陣列 |
| `{education_details}` | candidates_pipeline.education_details (JSONB) | 教育背景陣列 |

---

## AI 呼叫參數

```python
# Perplexity sonar-pro
payload = {
    "model": "sonar-pro",
    "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ],
    "temperature": 0.1,
}

# API endpoint
url = "https://api.perplexity.ai/chat/completions"
headers = {
    "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
    "Content-Type": "application/json",
}
```

---

## 回應解析

AI 回應可能包含 markdown code block，需要提取 JSON：

```python
content = response['choices'][0]['message']['content']

# 提取 JSON
if '```json' in content:
    content = content.split('```json')[1].split('```')[0]
elif '```' in content:
    content = content.split('```')[1].split('```')[0]

result = json.loads(content.strip())
```

---

## 範例

### 輸入

System Prompt（填入某 BIM 工程師職缺資訊後）+ User Prompt：

```
請根據 system prompt 中的職缺資訊，評估以下候選人的匹配程度。

## 候選人資料

**姓名**: 陳建宏
**現職**: BIM Engineer @ 中鼎工程
**技能**: Revit、AutoCAD、Navisworks、Python、BIM 360
**地點**: 台北
**LinkedIn**: https://linkedin.com/in/jh-chen

### 工作經歷
1. BIM Engineer @ 中鼎工程（2021-01 - 至今）
   - 負責大型建築專案 BIM 建模與協調
2. CAD Technician @ 大陸工程（2018-06 - 2020-12）
   - 2D/3D 繪圖與施工圖產出

### 教育背景
- 碩士 土木工程，台灣科技大學（2018）

### 穩定性指標
- 工作年資: 7 年
- 換工作次數: 2
- 平均任期: 36 個月
- 最近空窗: 0 個月
```

### 輸出

```json
{
  "relevance_check": {
    "job_core_domain": "BIM 工程",
    "candidate_domain": "BIM/土木工程",
    "is_relevant": true,
    "relevance_note": "核心領域完全匹配",
    "location_gate": "pass",
    "seniority_gate": "match",
    "data_completeness": "rich"
  },
  "score": 88,
  "recommendation": "強力推薦",
  "job_title": "BIM工程師",
  "matched_skills": ["Revit", "AutoCAD", "Navisworks", "BIM 360"],
  "missing_skills": ["Dynamo"],
  "strengths": ["中鼎工程大型專案經驗", "土木碩士背景", "7年相關經驗"],
  "probing_questions": [
    "[初步] 目前在職狀態？是否考慮轉換？",
    "[技術] 是否有 Dynamo 自動化經驗？",
    "[文化] 對中小型公司的快速開發節奏看法？"
  ],
  "career_trajectory": {
    "direction": "上升型",
    "industry_consistency": "專注營建/BIM 領域",
    "tenure_pattern": "穩定",
    "red_flags": []
  },
  "company_dna_analysis": {
    "scale_match": "大型工程公司→中型科技公司，可接受",
    "industry_match": "營建→BIM 科技，高度相關",
    "culture_fit": "本土大企業→本土中型，適應度高"
  },
  "conclusion": "陳建宏在 BIM 領域有紮實的實務經驗，目前任職中鼎工程參與大型專案，具備 Revit 等核心工具能力。職涯軌跡呈上升型，從 CAD 技術員晉升到 BIM 工程師。土木碩士背景加上 7 年工程經驗，與本職缺需求高度匹配。建議切入點：強調技術挑戰性和成長空間。"
}
```
