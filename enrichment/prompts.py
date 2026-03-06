"""
Prompt 模板 — Perplexity API 分析指令

核心 prompt:
1. PROFILE_ANALYSIS_PROMPT: 分析 LinkedIn 頁面，提取結構化候選人資料
2. JOB_MATCH_PROMPT: 綜合職缺匹配評分（5 維度加權 + 公司 DNA + 職涯軌跡）
3. KEYWORD_GENERATION_PROMPT: 從職缺畫像 AI 生成搜尋關鍵字
4. JINA_TEXT_PARSE_PROMPT: 將純文字轉為結構化資料
"""

# ============================================================
# Prompt 1: LinkedIn 個人頁面分析 — 提取結構化候選人資料
# ============================================================
PROFILE_ANALYSIS_PROMPT = """你是專業獵頭顧問 AI。請分析以下 LinkedIn 個人頁面，提取候選人的完整資訊。

請訪問此 URL 並分析頁面內容：{url}

請以 JSON 格式回傳以下欄位（所有欄位都必須存在，無法取得的填空值）：

{{
  "name": "候選人全名",
  "current_position": "目前職稱",
  "company": "目前任職公司",
  "location": "所在地區（城市、國家）",
  "years_experience": 0,
  "work_history": [
    {{
      "company": "公司名稱",
      "title": "職稱",
      "duration": "起始年月-結束年月 或 至今",
      "description": "主要職責與成就（1-2句）"
    }}
  ],
  "education_details": [
    {{
      "school": "學校名稱",
      "degree": "學位（學士/碩士/博士）",
      "field": "主修科系",
      "year": "畢業年份"
    }}
  ],
  "skills": ["技能1", "技能2", "技能3"],
  "languages": ["語言1", "語言2"],
  "certifications": ["證照1", "證照2"],
  "summary": "2-3 句專業摘要，概述此人的核心能力與經驗亮點",
  "stability_indicators": {{
    "avg_tenure_months": 0,
    "job_changes": 0,
    "recent_gap_months": 0
  }},
  "education_level": "最高學歷（高中/大學/碩士/博士）",
  "industry_tags": ["此人經歷涉及的產業標籤"]
}}

注意事項：
- years_experience 請根據工作經歷推算總年數（整數）
- stability_indicators 中 avg_tenure_months 請根據各段工作時長計算平均月數
- job_changes 為總共換過幾份工作
- recent_gap_months 為最近一次工作間隔月數（0 表示目前在職無間隔）
- skills 請盡量完整列出，包含頁面上明確展示的和從工作經歷推斷的
- 如果無法訪問頁面或資訊不足，仍請回傳 JSON 結構，可取得的欄位填值，不確定的填空"""

# ============================================================
# Prompt 2: 綜合職缺匹配評分 — 5 維度加權
# ============================================================
JOB_MATCH_PROMPT = """你是資深獵頭顧問 AI。根據以下候選人資料與職缺資訊，進行深度匹配評估。

## 候選人資料
{candidate_profile}

## 職缺資訊
- **職缺名稱**: {position_name}
- **公司**: {client_company}
- **人才畫像**: {talent_profile}
- **職缺描述 (JD)**: {job_description}
- **公司畫像**: {company_profile}
- **顧問備註（含硬性條件）**: {consultant_notes}
- **必要技能**: {key_skills}
- **經驗要求**: {experience_required}

## 評分維度（必須根據各畫像做真實判斷，不要只做關鍵字比對）

| 維度 | 權重 | 評分說明 |
|------|------|----------|
| 人才畫像符合度 | 40% | 候選人技能與經歷是否吻合人才畫像描述的理想人選特質。硬性條件（年齡/證件/語言）若不符直接 0-20 分 |
| JD 職責匹配度 | 30% | 候選人技能是否覆蓋 JD 中的核心工作職責，越核心越高分 |
| 公司 DNA 適配性 | 15% | 根據候選人【工作經歷中的公司背景】與客戶公司畫像做以下比對：(1) 公司規模匹配（大廠→大廠 ✅ / 大廠→新創 ⚠️）(2) 產業經驗匹配（同產業 ✅ / 跨產業但相關 ⚠️ / 完全無關 ❌）(3) 文化風格推斷（外商→外商 ✅ / 外商→傳產 ❌ / 新創→大企業 ⚠️）(4) 技術棧一致性（從候選人過去公司的技術背景推斷是否與客戶公司技術棧相容） |
| 可觸達性 | 10% | 有 LinkedIn URL = 60分；有 LinkedIn + GitHub = 100分；都沒有 = 20分 |
| 活躍信號 | 5% | 有 GitHub 且近期活躍 = 100分；有 GitHub 無近期活動 = 70分；無 GitHub = 50分 |

## 面談問題生成指令
在 probing_questions 中依以下分類生成面談問題（每類 2-3 題）：
1. **[初步]** 基本條件確認：在職狀態、薪資期望、到職時間、硬性條件是否符合
2. **[技術]** 技術深度確認：根據 JD 核心職責和缺失技能生成
3. **[文化]** 文化適配確認：根據公司畫像和人才畫像特質要求生成

## 輸出格式（必須是嚴格 JSON）

{{
  "score": 0,
  "recommendation": "強力推薦",
  "job_title": "{position_name}",
  "matched_skills": ["符合的技能1", "符合的技能2"],
  "missing_skills": ["缺少或待確認的技能/條件"],
  "strengths": [
    "根據人才畫像分析的第一個優勢",
    "根據JD分析的第二個優勢",
    "根據公司畫像分析的第三個優勢"
  ],
  "probing_questions": [
    "[初步] 目前是否在職、是否 Open to Work？",
    "[初步] 期望薪資範圍與最快到職時間？",
    "[技術] 具體技術深度確認問題",
    "[技術] 缺失技能確認問題",
    "[文化] 工作環境適配問題"
  ],
  "salary_fit": "根據市場行情和職缺薪資範圍的適配說明",
  "career_trajectory": {{
    "direction": "上升型/穩定型/橫移型/下降型",
    "industry_consistency": "專注單一產業 / 相關產業橫跨 / 跨界",
    "tenure_pattern": "穩定 / 遞減 / 不規則",
    "red_flags": ["紅旗警示（如頻繁跳槽、長期空窗等），無則空陣列"]
  }},
  "company_dna_analysis": {{
    "scale_match": "候選人過往公司規模 vs 客戶公司規模的匹配說明",
    "industry_match": "產業經驗匹配分析",
    "culture_fit": "文化風格推斷（外商/本土/新創/傳產等）"
  }},
  "conclusion": "完整的AI匹配結語（5-8句）：包含整體評價、核心推薦理由、職涯軌跡分析（晉升方向+穩定度）、公司DNA匹配摘要、建議聯繫方式與切入點。這段文字會直接顯示在候選人卡片的AI報告區域。"
}}

## recommendation 對應規則
- score 85-100 → "強力推薦"
- score 70-84  → "推薦"
- score 55-69  → "觀望"
- score < 55   → "不推薦"

## 重要
- 做真實判斷，不要只做關鍵字 overlap
- 同一批候選人要拉開分數差距
- conclusion 必須包含：(1) 具體切入點，讓顧問知道怎麼聯繫 (2) 職涯軌跡分析結論 (3) 公司DNA匹配摘要
- 硬性條件（顧問備註中提到的）若不符合要明確標記
- career_trajectory 必須根據工作經歷的時間軸做實質分析，不要只重複資料"""

# ============================================================
# Prompt 3: AI 搜尋關鍵字生成 — 從職缺畫像提取爬蟲關鍵字
# ============================================================
KEYWORD_GENERATION_PROMPT = """你是資深獵頭顧問 AI。根據以下職缺完整畫像，生成最適合用於 LinkedIn / GitHub 搜尋的關鍵字組合。

## 職缺資訊
- **職缺名稱**: {position_name}
- **公司**: {client_company}
- **人才畫像**: {talent_profile}
- **職缺描述 (JD)**: {job_description}
- **公司畫像**: {company_profile}
- **必要技能**: {key_skills}
- **顧問已設定的主要關鍵字**: {search_primary}
- **顧問已設定的次要關鍵字**: {search_secondary}

## 任務
分析以上所有資訊，生成搜尋關鍵字。策略：
1. **保留顧問手動設定的關鍵字**（如果有的話），在此基礎上補充
2. **從 JD 核心職責提取**：找出 JD 中最能區分合格/不合格候選人的技能關鍵字
3. **從人才畫像提取**：畫像中描述的理想技術棧、工具、方法論
4. **考慮同義詞和替代名稱**：例如 "React" 也可能寫為 "ReactJS"，"K8s" = "Kubernetes"
5. **區分核心 vs 加分**：primary_skills 是 AND 邏輯（必備），secondary_skills 是 OR 邏輯（加分）

## 輸出格式（必須是嚴格 JSON）

{{
  "primary_skills": ["核心必備技能1", "核心必備技能2", "核心必備技能3"],
  "secondary_skills": ["加分技能1", "加分技能2", "加分技能3", "加分技能4"],
  "title_keywords": ["搜尋用職稱關鍵字1", "職稱關鍵字2"],
  "search_queries": [
    "組合搜尋語句1（用於 Google/LinkedIn）",
    "組合搜尋語句2"
  ],
  "reasoning": "1-2 句說明為什麼選擇這些關鍵字"
}}

## 注意
- primary_skills 最多 5 個，挑最核心的
- secondary_skills 最多 8 個
- title_keywords 是職稱搜尋用，例如 "BIM Engineer"、"BIM 工程師"、"Building Information Modeling"
- search_queries 是完整的搜尋語句，會直接用於 Google 搜尋
- 關鍵字以英文為主（LinkedIn/GitHub 搜尋用），但可以包含中文職稱"""

# ============================================================
# Prompt 4: Jina 文字解析 — 將純文字轉為結構化資料
# ============================================================
JINA_TEXT_PARSE_PROMPT = """你是專業獵頭顧問 AI。以下是從 LinkedIn 個人頁面提取的純文字內容。
請分析這段文字，提取候選人的結構化資訊。

## LinkedIn 頁面文字內容
{raw_text}

請以 JSON 格式回傳（與標準 LinkedIn 分析相同的格式）：

{{
  "name": "候選人全名",
  "current_position": "目前職稱",
  "company": "目前任職公司",
  "location": "所在地區",
  "years_experience": 0,
  "work_history": [
    {{
      "company": "公司名稱",
      "title": "職稱",
      "duration": "起始-結束",
      "description": "主要職責（1-2句）"
    }}
  ],
  "education_details": [
    {{
      "school": "學校名稱",
      "degree": "學位",
      "field": "科系",
      "year": "畢業年份"
    }}
  ],
  "skills": ["技能1", "技能2"],
  "languages": ["語言1"],
  "certifications": [],
  "summary": "2-3 句專業摘要",
  "stability_indicators": {{
    "avg_tenure_months": 0,
    "job_changes": 0,
    "recent_gap_months": 0
  }},
  "education_level": "最高學歷",
  "industry_tags": ["產業標籤"]
}}

如果文字內容不足以判斷某欄位，請填入合理預設值或空值。"""

# ============================================================
# 人選分析報告模板 — 寫入 notes 欄位
# ============================================================
ANALYSIS_REPORT_TEMPLATE = """【AI評分 {score}分 / {grade}】{date}
分析來源：{source} | LinkedIn 深度分析

📌 配對職位：{position_name}（{client_company}）

👤 候選人概要：
- 現職：{current_position} @ {company}
- 年資：{years_experience} 年（{job_changes} 間公司，平均任期 {avg_tenure_months} 個月）
- 學歷：{education}
- 技能：{skills_summary}

✅ 優勢：
{strengths}

⚠️ 待確認：
{missing_items}

📊 穩定性分析：
- 穩定度評分：{stability_score}/100
- 跳槽頻率：{job_changes} 次 / {years_experience} 年
- 最近待業：{recent_gap_months} 個月

🎤 建議面談問題：
{probing_questions}

🎯 職缺推薦：
{job_recommendations}

💡 顧問建議：
{conclusion}

---"""
