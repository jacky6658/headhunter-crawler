# Headhunter Crawler — 逐步重現指南

> 本文件記錄 v3 + v4 的所有程式碼改動，讓另一個 AI 可以在新的程式碼基底上重現相同的修改。
> 基底版本：commit `4da03de` (feat: 接入 AI 全流程 Pipeline)
> 目標版本：commit `103d0a7` (feat: v4 token 優化)

---

## 目錄

1. [修改總覽](#1-修改總覽)
2. [v3 修改步驟](#2-v3-修改步驟)
3. [v4 修改步驟 (P0~P3)](#3-v4-修改步驟)
4. [驗證方法](#4-驗證方法)

---

## 1. 修改總覽

### v3 改動（commit 7468f03）
| 檔案 | 改動內容 |
|------|----------|
| `scheduler/task_manager.py` | 排程器 misfire_grace_time 修復 + 漏跑偵測 |
| `enrichment/contextual_scorer.py` | 三道閘門後處理 + 候選人名稱黑名單 + grade 欄位 |
| `enrichment/prompts.py` | JOB_MATCH_PROMPT 加入三道閘門指令 + location 欄位 + 資料不足處理 |
| `crawler/engine.py` | Phase 1.5 相關性篩選 + 多維度搜尋參數傳遞 |

### v4 改動（commit 103d0a7）
| 檔案 | P0 | P1 | P2 | P3 | 改動內容 |
|------|:--:|:--:|:--:|:--:|----------|
| `enrichment/perplexity_client.py` | | ✅ | | ✅ | model_override + system_prompt 參數 |
| `enrichment/contextual_scorer.py` | ✅ | ✅ | | ✅ | should_ai_score() + scoring_model + prompt 拆分 |
| `enrichment/prompts.py` | | | | ✅ | 拆分 SYSTEM + USER prompt |
| `enrichment/profile_enricher.py` | | | ✅ | | enrichment 快取（JSON file + TTL） |
| `crawler/engine.py` | ✅ | | | | 預篩呼叫 + skipped 計數 |
| `config/default.yaml` | | ✅ | ✅ | | scoring_model + cache 設定 |

---

## 2. v3 修改步驟

### 步驟 2.1：修改 `scheduler/task_manager.py`

#### 2.1.1 在 `_schedule_task()` 方法中加入排程參數

找到 `self._scheduler.add_job(...)` 呼叫（在 `_schedule_task` 方法裡），加入三個參數：

```python
# 修改前：
self._scheduler.add_job(
    self._execute_task,
    trigger=trigger,
    id=task.id,
    args=[task.id],
    replace_existing=True,
)
logger.info(f"排程: {task.id} ({task.schedule_type})")

# 修改後：
self._scheduler.add_job(
    self._execute_task,
    trigger=trigger,
    id=task.id,
    args=[task.id],
    replace_existing=True,
    misfire_grace_time=3600,  # v3: 允許最多 1 小時的延遲（原本只有 1 秒）
    coalesce=True,           # v3: 錯過多次只補跑一次，不會重複執行
    max_instances=1,         # 同一任務最多同時跑 1 個（避免重複執行）
)
logger.info(f"排程: {task.id} ({task.schedule_type}) misfire_grace=3600s")
```

#### 2.1.2 在 `start()` 方法中加入漏跑偵測

找到 `start()` 方法中 for loop 恢復排程任務的區塊：

```python
# 修改前：
scheduled_count = 0
for task_id, task in self.tasks.items():
    if task.schedule_type != 'once':
        self._schedule_task(task)
        scheduled_count += 1

logger.info(f"排程器已啟動，{len(self.tasks)} 個任務，{scheduled_count} 個定期排程已恢復")

# 修改後：
scheduled_count = 0
missed_tasks = []
for task_id, task in self.tasks.items():
    if task.schedule_type != 'once':
        self._schedule_task(task)
        scheduled_count += 1

        # v3: 檢查是否有「今天該跑但沒跑」的任務
        if task.schedule_type == 'daily' and task.last_run:
            try:
                last = datetime.strptime(task.last_run, '%Y-%m-%d %H:%M:%S')
                now = datetime.now()
                hours_since = (now - last).total_seconds() / 3600
                if hours_since > 25:  # 超過 25 小時沒跑 = 至少錯過一天
                    missed_tasks.append((task_id, task.job_title, f"{hours_since:.0f}h ago"))
            except (ValueError, TypeError):
                pass

logger.info(f"排程器已啟動，{len(self.tasks)} 個任務，{scheduled_count} 個定期排程已恢復")
if missed_tasks:
    for tid, title, ago in missed_tasks:
        logger.warning(f"⚠️ 定期任務可能漏跑: [{tid}] {title} (上次執行: {ago})")
```

---

### 步驟 2.2：修改 `enrichment/prompts.py` — 三道閘門

#### 2.2.1 更新 JOB_MATCH_PROMPT

在 `JOB_MATCH_PROMPT` 中做以下改動：

**A. 加入 `{location}` 欄位**

在「顧問備註」後加入地點欄位：
```python
# 加在 "**顧問備註（含硬性條件）**: {consultant_notes}" 之後：
```

**B. 將「相關性閘門」改為「三道閘門」**

把原本只有一道閘門（相關性）的段落，替換為三道閘門完整版。具體內容見目前 `prompts.py` 中 `JOB_MATCH_PROMPT` 的 `## 🚨 第二步：三道閘門` 區塊（line 212-244）。

關鍵改動：
- 閘門 A（相關性）：不相關 → max 25 分
- 閘門 B（地理位置）：海外無台灣關聯 → max 40 分
- 閘門 C（資深度）：overqualified → 扣 15-25 分
- 閘門疊加規則

**C. 更新評分維度權重**

```
核心技能匹配: 45% → 40%
JD 職責匹配度: 25% → 25%（不變）
公司 DNA 適配性: 15% → 15%（不變）
舊「可觸達性」10% + 舊「活躍信號」5% → 新「實際到職可行性」15% + 新「可觸達性」5%
```

**D. 在 relevance_check JSON 格式中新增三個欄位**

```json
"relevance_check": {
    "job_core_domain": "...",
    "candidate_domain": "...",
    "is_relevant": true,
    "relevance_note": "...",
    "location_gate": "pass/fail",          // 新增
    "seniority_gate": "match/overqualified/underqualified",  // 新增
    "data_completeness": "rich/partial/minimal"  // 新增
}
```

**E. 加入資料不足處理規則**

在評分維度後加入：
```
2. **資料不足處理規則**：
   - 職稱相關+公司對口+缺細節 → 45-55分（觀望）
   - 職稱相關+CPA/Big4標記+缺年資 → 50-60分
   - 完全搜不到 → 15-20分
```

---

### 步驟 2.3：修改 `enrichment/contextual_scorer.py` — 三道閘門後處理

#### 2.3.1 加入候選人名稱黑名單

在 class `ContextualScorer` 內（`_ai_score` 方法之前）加入：

```python
# v3: 無效候選人名稱黑名單 — 爬蟲有時會抓到 LinkedIn 登入頁或廣告
INVALID_NAME_PATTERNS = [
    'LinkedIn', '登入', '註冊', 'Sign in', 'Sign up', 'Log in',
    'HRnetGroup', '加入 LinkedIn', '同意並加入', 'Join LinkedIn',
    'People also viewed', '其他人也看了', 'LinkedIn Premium',
]
```

#### 2.3.2 在 `_ai_score()` 開頭加入名稱黑名單檢查

```python
def _ai_score(self, enriched: dict, job: dict) -> dict:
    """用 Perplexity AI 做深度評分"""
    try:
        # v3: 過濾無效候選人名稱
        candidate_name = enriched.get('name', '')
        for pattern in self.INVALID_NAME_PATTERNS:
            if pattern.lower() in candidate_name.lower():
                logger.info(f"跳過無效候選人: {candidate_name} (匹配黑名單: {pattern})")
                return {
                    'ai_match_result': {
                        'score': 0, 'grade': 'C', 'recommendation': '不推薦',
                        'job_id': job.get('id'), 'job_title': job.get('position_name', ''),
                        'conclusion': f'無效候選人：名稱「{candidate_name}」為 LinkedIn 系統頁面，非真實候選人',
                        'relevance_check': {'is_relevant': False, 'relevance_note': '非真實候選人'},
                        'evaluated_by': 'Crawler-enricher-v4',
                    },
                    'talent_level': 'C',
                    'report': f'無效候選人: {candidate_name}',
                    'success': True,
                }
        # ... 原有的 _ai_score 邏輯繼續 ...
```

#### 2.3.3 改寫三道閘門後處理

在 `_ai_score()` 中，把原本只有閘門 A 的後處理改為三道閘門：

```python
# 原本只有：
# if relevance_check.get('is_relevant') is False:
#     if raw_score > 25:
#         raw['score'] = min(raw_score, 25)
#         raw['recommendation'] = '不推薦'

# 改為三道閘門版本：
relevance_check = raw.get('relevance_check', {})
if not isinstance(relevance_check, dict):
    relevance_check = {}

raw_score = int(raw.get('score', 50))
capped = False
cap_reasons = []

# 閘門 A: 相關性 — 不相關則 max 25
if relevance_check.get('is_relevant') is False:
    if raw_score > 25:
        logger.info(f"閘門A(相關性): {enriched.get('name', '?')} 不相關, {raw_score} → 25")
        raw_score = 25
        capped = True
        cap_reasons.append('相關性不符')

# 閘門 B: 地理位置 — 海外無台灣關聯 max 40
location_gate = relevance_check.get('location_gate', '')
if 'fail' in str(location_gate).lower():
    if raw_score > 40:
        logger.info(f"閘門B(地點): {enriched.get('name', '?')} 地點不符, {raw_score} → 40")
        raw_score = min(raw_score, 40)
        capped = True
        cap_reasons.append('工作地點不符')

# 閘門 C: Overqualified — 嚴重超資格扣 15-25 分
seniority_gate = relevance_check.get('seniority_gate', '')
if 'overqualified' in str(seniority_gate).lower():
    penalty = 20
    logger.info(f"閘門C(職級): {enriched.get('name', '?')} overqualified, {raw_score} → {raw_score - penalty}")
    raw_score = max(0, raw_score - penalty)
    cap_reasons.append('段位過高(overqualified)')

if capped or cap_reasons:
    raw['score'] = raw_score
    if raw_score < 40:
        raw['recommendation'] = '不推薦'
    elif raw_score < 55:
        raw['recommendation'] = '不推薦'
```

#### 2.3.4 在 `_build_result()` 的 ai_match_result 中加入 `grade` 欄位

```python
ai_match_result = {
    'score': score,
    'grade': grade,  # v3: 明確寫入 grade 到 ai_match_result（之前漏掉）
    'recommendation': recommendation,
    # ... 其他欄位 ...
}
```

---

### 步驟 2.4：修改 `crawler/engine.py` — Phase 1.5 相關性篩選

#### 2.4.1 新增 `_filter_by_relevance()` 方法

在 `SearchEngine` class 中新增完整的 `_filter_by_relevance()` 方法。這個方法：

1. 從 `self.task` 的 `job_title` + `primary_skills` + `secondary_skills` 建立關鍵字集合
2. 對每個候選人的 `title`/`bio`/`skills` 做文字匹配
3. 定義 `UNRELATED_PATTERNS`（sales/HR/legal/nurse 等）
4. 邏輯：
   - 沒有文字資訊 → 保留
   - 有關鍵字命中 → 保留
   - 無命中 + 明確不相關職稱 → 過濾
   - 無命中 + 不確定 → 保留

完整程式碼見目前 `engine.py` 第 257-375 行。

#### 2.4.2 在 `execute()` 中呼叫 Phase 1.5

在 Phase 1（搜尋）和 Phase 2（enrichment）之間插入：

```python
# ═══ Phase 1.5: 相關性篩選 ═══
if candidates:
    before_count = len(candidates)
    candidates = self._filter_by_relevance(candidates)
    filtered_count = before_count - len(candidates)
    if filtered_count > 0:
        logger.info(f"[Phase 1.5] 相關性篩選: {before_count} → {len(candidates)} "
                   f"(過濾 {filtered_count} 位不相關候選人)")
    else:
        logger.info(f"[Phase 1.5] 相關性篩選: 全部 {len(candidates)} 位通過")
```

#### 2.4.3 搜尋參數傳遞

在 `execute()` 中呼叫 `search_with_fallback()` 時，加入三個新參數：

```python
linkedin_result = self.linkedin_searcher.search_with_fallback(
    # ... 原有參數 ...
    title_variants=self.task.title_variants,        # 新增
    target_companies=self.task.target_companies,    # 新增
    exclusion_keywords=self.task.exclusion_keywords, # 新增
)
```

---

## 3. v4 修改步驟

### 步驟 3.1 [P1+P3]：修改 `enrichment/perplexity_client.py`

#### 3.1.1 `analyze_profile()` 新增兩個參數

```python
# 修改前：
def analyze_profile(self, linkedin_url: str, prompt: str) -> dict:

# 修改後：
def analyze_profile(self, linkedin_url: str, prompt: str,
                    model_override: str = None,
                    system_prompt: str = None) -> dict:
```

在 docstring 中加入參數說明：
```python
"""
Args:
    linkedin_url: LinkedIn 個人頁面 URL
    prompt: 分析指令 prompt（已填入 URL）
    model_override: 覆蓋預設模型（如 'sonar' 用於評分以節省成本）
    system_prompt: 自訂 system prompt（用於拆分 prompt 場景）
"""
```

#### 3.1.2 方法開頭加入 model 和 system prompt 處理

```python
if not self.api_key:
    return {'error': 'Perplexity API key 未設定', 'success': False}

# v4: 支援 model 覆蓋（P1: scoring 用 sonar 省成本）
active_model = model_override or self.model

# v4: 支援自訂 system prompt（P3: 拆分 job context 到 system）
sys_content = system_prompt or '你是專業獵頭顧問 AI。請根據指令分析候選人資訊，以嚴格 JSON 格式回傳。不要包含任何 markdown 標記或額外文字。'
```

#### 3.1.3 payload 使用 active_model 和 sys_content

```python
payload = {
    'model': active_model,         # 改：self.model → active_model
    'messages': [
        {
            'role': 'system',
            'content': sys_content,  # 改：硬編碼字串 → sys_content
        },
        {
            'role': 'user',
            'content': prompt,
        }
    ],
    'temperature': 0.1,
    'max_tokens': 4000,
    'return_citations': self.return_citations,
}
```

#### 3.1.4 更新 log 行

```python
# 修改前：
logger.info(f"Perplexity API 呼叫 (嘗試 {attempt + 1}/{self.max_retries + 1}): {linkedin_url}")

# 修改後：
logger.info(f"Perplexity API 呼叫 (嘗試 {attempt + 1}/{self.max_retries + 1}, model={active_model}): {linkedin_url}")
```

#### 3.1.5 更新使用量追蹤

```python
# 追蹤使用量（用實際使用的 model 計算成本）
usage = data.get('usage', {})
self._track_usage(usage, model_used=active_model)  # 加入 model_used 參數
```

#### 3.1.6 更新 result 的 _usage

```python
result['_usage'] = {
    'input_tokens': usage.get('prompt_tokens', 0),
    'output_tokens': usage.get('completion_tokens', 0),
    'model': active_model,                                    # self.model → active_model
    'cost': self._estimate_cost(usage, model_used=active_model),  # 加入 model_used
}
```

#### 3.1.7 修改 `_track_usage()` 和 `_estimate_cost()`

```python
# 修改前：
def _track_usage(self, usage: dict):
    self._usage['estimated_cost'] += self._estimate_cost(usage)

def _estimate_cost(self, usage: dict) -> float:
    pricing = PRICING.get(self.model, PRICING['sonar'])

# 修改後：
def _track_usage(self, usage: dict, model_used: str = None):
    self._usage['estimated_cost'] += self._estimate_cost(usage, model_used)

def _estimate_cost(self, usage: dict, model_used: str = None) -> float:
    """估算單次呼叫費用（v4: 支援指定模型計價）"""
    model = model_used or self.model
    pricing = PRICING.get(model, PRICING['sonar'])
```

---

### 步驟 3.2 [P3]：修改 `enrichment/prompts.py` — 拆分 prompt

#### 3.2.1 在 `PROFILE_ANALYSIS_PROMPT` 之後、`JOB_MATCH_PROMPT` 之前，新增兩個常數

新增 `JOB_MATCH_SYSTEM_PROMPT` 和 `JOB_MATCH_USER_PROMPT`。

**⚠️ 重要注意事項**：`JOB_MATCH_SYSTEM_PROMPT` 裡的 JSON 範例使用 `{{{{` 和 `}}}}` 代替 `{` 和 `}`，因為此模板會透過 `.format()` 呼叫填入 `{position_name}` 等變數，所以所有非變數的大括號都需要雙重轉義。

```python
# ============================================================
# Prompt 2: 綜合職缺匹配評分 — v4 拆分版（system + user）
# ============================================================
# v4: 拆分為 SYSTEM（job context + 規則，per job 固定）+ USER（candidate，per person 變動）
# 省 ~40% input tokens：同一職缺的候選人共用 system prompt

JOB_MATCH_SYSTEM_PROMPT = """你是資深獵頭顧問 AI。你的任務是判斷候選人與【特定職缺】的匹配程度。

⚠️ 關鍵指令：你必須嚴格根據下方提供的職缺資訊來評分。不要基於候選人的「整體素質」或「一般專業能力」給分。一個非常優秀但與職缺完全無關的候選人，分數應該很低。

---

## 🎯 職缺資訊

- **職缺名稱**: {position_name}
- **公司**: {client_company}
- **必要技能**: {key_skills}
- **經驗要求**: {experience_required}
- **人才畫像**: {talent_profile}
- **職缺描述 (JD)**: {job_description}
- **公司畫像**: {company_profile}
- **顧問備註（含硬性條件）**: {consultant_notes}

---

## 🚨 三道閘門（最重要！在計算分數之前必須依序通過）

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
- 嚴重 Overqualified(落差≥2級) → **扣 15-25 分**
- 明顯 Underqualified → 扣 10-20 分

### ⚠️ 閘門疊加規則
三道閘門的扣分可以疊加。例如：海外(max 40) + overqualified(-20) = max 20

---

## 📊 評分維度

| 維度 | 權重 | 評分說明 |
|------|------|----------|
| 核心技能匹配 | 40% | 候選人是否具備必要技能？工作經歷中是否實際使用？ |
| JD 職責匹配度 | 25% | 過去工作職責是否與 JD 描述的日常工作相似？ |
| 公司 DNA 適配性 | 15% | 規模匹配、產業經驗、文化風格、技術棧一致性 |
| 實際到職可行性 | 15% | 地理位置、職級適配、薪資預期 |
| 可觸達性 | 5% | LinkedIn+GitHub=100分；LinkedIn=60分；都沒有=20分 |

**規則**：
1. 核心技能+JD職責(65%)得分低 → 總分不應超過 40 分
2. **資料不足**：職稱相關+公司對口+缺細節 → 45-55分（觀望）；職稱相關+CPA/Big4等標記+缺年資 → 50-60分；完全搜不到 → 15-20分

## 面談問題生成
probing_questions 分三類各 2-3 題：[初步]條件確認、[技術]深度確認、[文化]適配確認

## 輸出格式（嚴格 JSON）

{{{{
  "relevance_check": {{{{
    "job_core_domain": "核心領域",
    "candidate_domain": "候選人專業領域",
    "is_relevant": true,
    "relevance_note": "相關性說明",
    "location_gate": "pass/fail",
    "seniority_gate": "match/overqualified/underqualified",
    "data_completeness": "rich/partial/minimal"
  }}}},
  "score": 0,
  "recommendation": "強力推薦/推薦/觀望/不推薦",
  "job_title": "{position_name}",
  "matched_skills": [],
  "missing_skills": [],
  "strengths": [],
  "probing_questions": [],
  "salary_fit": "",
  "career_trajectory": {{{{
    "direction": "上升型/穩定型/橫移型/下降型",
    "industry_consistency": "",
    "tenure_pattern": "",
    "red_flags": []
  }}}},
  "company_dna_analysis": {{{{
    "scale_match": "",
    "industry_match": "",
    "culture_fit": ""
  }}}},
  "conclusion": "完整AI匹配結語（5-8句）：整體評價、推薦理由、職涯軌跡、公司DNA、聯繫切入點"
}}}}

## recommendation 規則
85-100→"強力推薦" | 70-84→"推薦" | 55-69→"觀望" | <55→"不推薦"

## 重要提醒
- 做「職缺匹配」判斷，非「人才品質」判斷
- 同一批候選人要拉開分數差距
- conclusion 含：切入點 + 職涯軌跡 + 公司DNA摘要
- job_title 必須填「{position_name}」"""

JOB_MATCH_USER_PROMPT = """請根據 system prompt 中的職缺資訊，評估以下候選人的匹配程度。

## 候選人資料

{candidate_profile}

---

請依序執行：(1) 三道閘門判斷 (2) 五維度評分 (3) 面談問題生成，以嚴格 JSON 格式回傳。"""
```

#### 3.2.2 保留原始 JOB_MATCH_PROMPT

在上面兩個新常數之後，加一行註解然後保留原來的 `JOB_MATCH_PROMPT`：

```python
# v3 相容：保留原始單一 prompt（供 recommend_jobs 等場景使用）
JOB_MATCH_PROMPT = """...(原始完整版 prompt，不動)..."""
```

---

### 步驟 3.3 [P0+P1+P3]：修改 `enrichment/contextual_scorer.py`

#### 3.3.1 更新 import

```python
# 修改前：
from .prompts import JOB_MATCH_PROMPT, ANALYSIS_REPORT_TEMPLATE

# 修改後：
from .prompts import JOB_MATCH_PROMPT, JOB_MATCH_SYSTEM_PROMPT, JOB_MATCH_USER_PROMPT, ANALYSIS_REPORT_TEMPLATE
```

#### 3.3.2 新增 UNRELATED_TITLE_PATTERNS class 變數

在 `ContextualScorer` class 定義開頭（`__init__` 之前）加入：

```python
# P0: 明確不相關的職稱模式（用於預篩，避免浪費 AI API call）
UNRELATED_TITLE_PATTERNS = [
    r'\b(sales|salesperson|業務|銷售)\b',
    r'\b(marketing|行銷)\b',
    r'\b(hr|human resources|人資|人力資源)\b',
    r'\b(legal|法務|律師|lawyer|attorney)\b',
    r'\b(nurse|護理|護士|醫師|doctor|physician)\b',
    r'\b(teacher|教師|教授|professor)\b',
    r'\b(driver|司機|駕駛)\b',
    r'\b(chef|廚師|cook)\b',
    r'\b(receptionist|前台|櫃台)\b',
]
```

#### 3.3.3 `__init__()` 新增兩行

在 `self.perplexity = ...` 之後加入：

```python
# v4 P1: scoring 用較便宜的模型（sonar），enrichment 維持 sonar-pro
self.scoring_model = config.get('perplexity', {}).get('scoring_model', 'sonar')
```

在 `self._all_jobs_cache = None` 之後加入：

```python
self._job_system_cache = {}  # v4 P3: 快取 job system prompt
```

#### 3.3.4 新增 `should_ai_score()` 方法

在 `recommend_jobs()` 方法之後加入完整方法：

```python
def should_ai_score(self, enriched: dict, job: dict) -> bool:
    """
    P0: 預篩 — 判斷候選人是否值得花 API call 做 AI 評分

    回傳 True = 需要 AI 評分, False = 跳過（用 rule-based 評分）

    跳過條件（同時滿足才跳過）：
    1. quick_skill_match 分數 = 0（完全無技能重疊）
    2. 候選人有足夠已知資訊（≥3 個技能），代表我們有足夠信心判斷不相關
    或
    1. 職稱明確不相關（sales/HR/legal 等）且 quick_score < 10
    """
    import re

    # 提取候選人技能
    candidate_skills = self._extract_skills(enriched)
    quick_score = self._quick_skill_match(candidate_skills, job)

    # 條件 1: quick_score = 0 且有足夠已知技能
    if quick_score == 0 and len(candidate_skills) >= 3:
        name = enriched.get('name', '?')
        logger.info(f"P0 預篩跳過: {name} (quick_score=0, 有 {len(candidate_skills)} 個已知技能但完全不匹配)")
        return False

    # 條件 2: 職稱明確不相關 + quick_score 很低
    if quick_score < 10:
        title = (enriched.get('current_position') or enriched.get('title', '')).lower()
        if title:
            for pattern in self.UNRELATED_TITLE_PATTERNS:
                if re.search(pattern, title, re.IGNORECASE):
                    name = enriched.get('name', '?')
                    logger.info(f"P0 預篩跳過: {name} (職稱不相關: {title[:40]}, quick_score={quick_score})")
                    return False

    return True
```

#### 3.3.5 改寫 `_ai_score()` 的 prompt 組裝部分

把原本的單一 prompt 呼叫替換為拆分版：

```python
# ====== 刪除這段 ======
# prompt = JOB_MATCH_PROMPT.format(
#     candidate_profile=candidate_profile,
#     position_name=job.get('position_name', ''),
#     ...
# )
# raw = self.perplexity.analyze_profile('', prompt)

# ====== 替換為 ======
# v4 P3: 拆分 prompt — system（job context）+ user（candidate）
job_id = job.get('id') or job.get('jobId') or job.get('job_id')

if job_id and job_id not in self._job_system_cache:
    self._job_system_cache[job_id] = JOB_MATCH_SYSTEM_PROMPT.format(
        position_name=job.get('position_name', ''),
        client_company=job.get('client_company', ''),
        talent_profile=job.get('talent_profile', '（未提供）'),
        job_description=job.get('job_description', '（未提供）'),
        company_profile=job.get('company_profile', '（未提供）'),
        consultant_notes=job.get('consultant_notes', '（無）'),
        key_skills=job.get('key_skills', ''),
        experience_required=job.get('experience_required', ''),
        location=job.get('location', '（未指定）'),
    )
    logger.info(f"v4 P3: job system prompt cached for job_id={job_id}")

system_prompt = self._job_system_cache.get(job_id)
user_prompt = JOB_MATCH_USER_PROMPT.format(candidate_profile=candidate_profile)

# 速率控制：等待適當時間後再呼叫 API
self.rate_limiter.wait()
# v4 P1: scoring 用 sonar（便宜）; P3: 拆分 system/user prompt
raw = self.perplexity.analyze_profile(
    '', user_prompt,
    model_override=self.scoring_model,
    system_prompt=system_prompt,
)
```

#### 3.3.6 更新 `evaluated_by`

```python
# 修改前：
'evaluated_by': 'Crawler-enricher-v2',
# 或
'evaluated_by': 'Crawler-enricher-v3',

# 修改後：
'evaluated_by': 'Crawler-enricher-v4',
```

#### 3.3.7 更新 `clear_cache()`

```python
def clear_cache(self):
    """清除職缺快取"""
    self._jobs_cache.clear()
    self._all_jobs_cache = None
    self._job_system_cache.clear()  # v4: 新增
```

---

### 步驟 3.4 [P2]：修改 `enrichment/profile_enricher.py` — enrichment 快取

#### 3.4.1 新增 import

在檔案頂部加入：

```python
import json
import os
import threading
```

#### 3.4.2 `__init__()` 新增快取初始化

在 `self.rate_limiter = ...` 之後加入：

```python
# v4 P2: Enrichment 快取（跨職缺不重複 enrich 同一人）
cache_cfg = config.get('cache', {})
self._cache_file = cache_cfg.get('file', 'data/enrichment_cache.json')
self._cache_ttl_days = cache_cfg.get('ttl_days', 7)
self._enrichment_cache = self._load_cache()
self._cache_lock = threading.Lock()
```

在 `_stats` dict 中加入：

```python
'cache_hits': 0,
```

#### 3.4.3 `enrich_candidate()` 加入快取查詢

在 `linkedin_url = candidate.get('linkedin_url', '')` 和 URL 驗證之後、provider loop 之前加入：

```python
# v4 P2: 檢查快取 — 同一 LinkedIn URL 不重複 enrich
cached = self._get_cached(linkedin_url)
if cached:
    self._stats['cache_hits'] = self._stats.get('cache_hits', 0) + 1
    logger.info(f"enrichment cache hit: {candidate.get('name', '?')} ({linkedin_url[:50]}...)")
    return cached
```

#### 3.4.4 每個 provider 成功後寫入快取

在每個 provider 的 `return result` 之前加入一行：

```python
self._set_cached(linkedin_url, result)  # v4 P2: 寫入快取
```

共三處（linkedin、perplexity、jina provider 的 success path）。

#### 3.4.5 `enrich_batch()` 結束時持久化

在 `enrich_batch()` 的 ThreadPoolExecutor 結束後加入：

```python
# v4 P2: 批量完成後持久化快取
self._save_cache()

cache_hits = self._stats.get('cache_hits', 0)
logger.info(f"批量深度分析完成: {total} 位, "
             f"成功 {self._stats['success']}, 失敗 {self._stats['failed']}, "
             f"快取命中 {cache_hits}")
```

#### 3.4.6 新增四個快取方法

在 `_calc_stability_score()` 之後新增：

```python
# ── v4 P2: Enrichment 快取方法 ──────────────────────────────

def _load_cache(self) -> dict:
    """從磁碟載入 enrichment 快取"""
    if not os.path.exists(self._cache_file):
        logger.info(f"enrichment cache 檔案不存在，建立空快取: {self._cache_file}")
        return {}
    try:
        with open(self._cache_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        logger.info(f"enrichment cache 載入: {len(data)} 筆快取")
        return data
    except Exception as e:
        logger.warning(f"enrichment cache 載入失敗: {e}")
        return {}

def _save_cache(self):
    """持久化 enrichment 快取到磁碟"""
    with self._cache_lock:
        try:
            os.makedirs(os.path.dirname(self._cache_file) or '.', exist_ok=True)
            with open(self._cache_file, 'w', encoding='utf-8') as f:
                json.dump(self._enrichment_cache, f, ensure_ascii=False, indent=None)
            logger.info(f"enrichment cache 已儲存: {len(self._enrichment_cache)} 筆")
        except Exception as e:
            logger.error(f"enrichment cache 儲存失敗: {e}")

def _get_cached(self, linkedin_url: str) -> Optional[dict]:
    """查詢快取（含 TTL 檢查）"""
    if not linkedin_url:
        return None

    entry = self._enrichment_cache.get(linkedin_url)
    if not entry:
        return None

    # TTL 檢查
    try:
        cached_at = datetime.fromisoformat(entry.get('cached_at', ''))
        age_days = (datetime.now() - cached_at).days
        if age_days > self._cache_ttl_days:
            logger.info(f"enrichment cache 過期: {linkedin_url[:50]}... ({age_days} days old)")
            with self._cache_lock:
                self._enrichment_cache.pop(linkedin_url, None)
            return None
    except (ValueError, TypeError):
        # cached_at 格式不對，當作過期
        with self._cache_lock:
            self._enrichment_cache.pop(linkedin_url, None)
        return None

    return entry.get('result')

def _set_cached(self, linkedin_url: str, result: dict):
    """寫入快取"""
    if not linkedin_url:
        return
    with self._cache_lock:
        # 移除 _enrichment_raw 以減少快取大小（原始 API 回應通常很大）
        cache_result = {k: v for k, v in result.items() if k != '_enrichment_raw'}
        self._enrichment_cache[linkedin_url] = {
            'cached_at': datetime.now().isoformat(),
            'result': cache_result,
        }
```

#### 3.4.7 更新 `get_stats()`

```python
def get_stats(self) -> dict:
    """回傳使用統計"""
    stats = {**self._stats}
    stats['enrichment_cache_size'] = len(self._enrichment_cache)  # 新增
    # ... 其他不變 ...
```

---

### 步驟 3.5 [P0]：修改 `crawler/engine.py` — 預篩呼叫

#### 3.5.1 在 `_ai_score_candidates()` 中加入預篩

```python
def _ai_score_candidates(self, candidates):
    """
    用 ContextualScorer 做 AI 5 維度匹配評分

    v4 P0: 加入預篩 — quick_skill_match + title 檢查，跳過明顯不相關的候選人
    """
    job_id = self.task.step1ne_job_id
    scored_ai = 0
    scored_fallback = 0
    skipped_prefilter = 0  # v4 P0: 新增計數器

    for i, candidate in enumerate(candidates):
        try:
            if self.on_progress:
                self.on_progress(i + 1, len(candidates), i + 1,
                                 f"AI 評分 ({candidate.name})")

            c_dict = candidate.to_dict()

            # v4 P0: 預篩 — 跳過明顯不相關的候選人，省 API call
            if self.job_context and not self.contextual_scorer.should_ai_score(c_dict, self.job_context):
                self._fallback_keyword_score(candidate)
                skipped_prefilter += 1
                continue

            # ... 原有的 AI 評分邏輯不變 ...
```

更新最後的 log 行：

```python
# 修改前：
logger.info(f"Phase 3 完成: AI 評分 {scored_ai} 位, fallback {scored_fallback} 位")

# 修改後：
logger.info(f"Phase 3 完成: AI 評分 {scored_ai} 位, 預篩跳過 {skipped_prefilter} 位, fallback {scored_fallback} 位")
```

---

### 步驟 3.6 [P1+P2]：修改 `config/default.yaml`

#### 3.6.1 在 `enrichment.perplexity` 下加入 `scoring_model`

```yaml
enrichment:
  perplexity:
    model: sonar-pro
    scoring_model: sonar    # v4 P1: scoring 用較便宜的模型
    timeout: 60
    # ... 其他不變 ...
```

#### 3.6.2 在 `enrichment` 下加入 `cache` 區塊

```yaml
enrichment:
  # ... perplexity, provider_priority 等設定 ...
  cache:                    # v4 P2: enrichment 結果快取
    file: data/enrichment_cache.json
    ttl_days: 7
  # ... scoring 等其他設定 ...
```

---

## 4. 驗證方法

### 4.1 啟動伺服器

```bash
cd /Users/jackychen/Downloads/headhunter-crawler
source venv/bin/activate
python app.py
```

### 4.2 檢查日誌確認

啟動後應看到：
```
排程: xxx (daily) misfire_grace=3600s
enrichment cache 檔案不存在，建立空快取: data/enrichment_cache.json
```

### 4.3 API 驗證

```bash
# 確認 enrichment stats 有新欄位
curl http://localhost:5050/api/enrich/stats | python -m json.tool
# 應包含 cache_hits, enrichment_cache_size
```

### 4.4 程式碼驗證（Python REPL）

```python
# 測試 prompt 格式化
from enrichment.prompts import JOB_MATCH_SYSTEM_PROMPT, JOB_MATCH_USER_PROMPT
sys = JOB_MATCH_SYSTEM_PROMPT.format(
    position_name='Test', client_company='Co', key_skills='Python',
    experience_required='3y', talent_profile='x', job_description='y',
    company_profile='z', consultant_notes='n', location='Taipei'
)
print(len(sys))  # 應為 ~2200+ 字元，不含任何未替換的 {xxx}

user = JOB_MATCH_USER_PROMPT.format(candidate_profile='Some candidate')
print(len(user))  # 應為 ~130+ 字元
```

### 4.5 預篩邏輯測試

```python
# 建立 scorer 並測試 should_ai_score
from enrichment.contextual_scorer import ContextualScorer
scorer = ContextualScorer({'perplexity': {'scoring_model': 'sonar'}})

# 不相關候選人 → 應回傳 False
result = scorer.should_ai_score(
    {'name': 'A', 'current_position': 'Sales Director', 'skills': 'CRM, 銷售, 業務開發'},
    {'key_skills': 'BIM, Revit, AutoCAD'}
)
print(result)  # False

# 相關候選人 → 應回傳 True
result = scorer.should_ai_score(
    {'name': 'B', 'current_position': 'BIM Engineer', 'skills': 'Revit, AutoCAD, BIM'},
    {'key_skills': 'BIM, Revit, AutoCAD'}
)
print(result)  # True
```
