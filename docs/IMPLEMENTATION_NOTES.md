# Headhunter Crawler — Implementation Notes

> Last updated: 2026-03-08
> Version: v4 (token optimization)
> For handoff to next AI assistant

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Architecture & Pipeline](#architecture--pipeline)
3. [Version History (v1~v4)](#version-history)
4. [v3 Changes (Search Strategy + Scoring)](#v3-changes)
5. [v4 Changes (Token Optimization)](#v4-changes)
6. [File-by-File Reference](#file-by-file-reference)
7. [Configuration](#configuration)
8. [Known Issues & Pending Tasks](#known-issues--pending-tasks)
9. [Git Status & Deployment](#git-status--deployment)

---

## System Overview

A Flask-based headhunter crawler system that:
1. Accepts search tasks via REST API (or the React frontend)
2. Scrapes LinkedIn + GitHub for candidate profiles
3. Enriches candidates with Perplexity Sonar API (LinkedIn deep analysis)
4. Scores candidates against job descriptions using AI (5-dimension weighted scoring)
5. Pushes results to Step1ne CRM system
6. Supports scheduled (daily/weekly/interval) and one-off tasks

**Tech Stack**: Python 3.11, Flask, APScheduler, Playwright (headless Chrome), Perplexity Sonar API, Jina Reader, Google Sheets API, Step1ne REST API

**Server**: Runs on `0.0.0.0:5050` (configurable in `config/default.yaml`)

---

## Architecture & Pipeline

```
Phase 0: Pull Job Context from Step1ne + AI keyword generation
    ↓
Phase 1: LinkedIn search (Google/Brave/Bing) + GitHub search → merge & dedup
    ↓
Phase 1.5: Relevance filtering (rule-based, no API cost)
    ↓
Phase 2: ProfileEnricher — LinkedIn API → Perplexity sonar-pro → Jina Reader (fallback chain)
    ↓
Phase 3: ContextualScorer — AI 5-dimension scoring (Perplexity sonar) with pre-filtering
    ↓
Phase 4: Sort → Save to local JSON + Google Sheets → Auto-push to Step1ne
```

### Key Components

| Module | File | Purpose |
|--------|------|---------|
| API Server | `app.py`, `api/routes.py` | Flask REST API + frontend serving |
| Search Engine | `crawler/engine.py` | Main pipeline orchestrator (Phase 1-4) |
| LinkedIn Search | `crawler/linkedin.py` | Multi-engine LinkedIn scraping |
| GitHub Search | `crawler/github.py` | GitHub user search |
| Anti-Detection | `crawler/anti_detect.py` | Rate limiting, proxy, user-agent rotation |
| Deduplication | `crawler/dedup.py` | Cross-run LinkedIn/GitHub dedup cache |
| Profile Enricher | `enrichment/profile_enricher.py` | LinkedIn deep analysis (3-provider fallback) |
| Perplexity Client | `enrichment/perplexity_client.py` | Perplexity Sonar API wrapper |
| Contextual Scorer | `enrichment/contextual_scorer.py` | AI scoring with job context |
| Prompt Templates | `enrichment/prompts.py` | All prompt templates for Perplexity |
| Rate Limiter | `enrichment/rate_limiter.py` | Shared API rate limiter |
| Jina Reader | `enrichment/jina_reader.py` | Free fallback page reader |
| Step1ne Client | `integration/step1ne_client.py` | Step1ne CRM API integration |
| Task Manager | `scheduler/task_manager.py` | APScheduler task scheduling + execution |
| Scoring Engine | `scoring/engine.py` | Rule-based keyword scoring (fallback) |
| Data Models | `storage/models.py` | Candidate + SearchTask dataclasses |
| Google Sheets | `storage/sheets_store.py` | Google Sheets result writer |

---

## Version History

| Version | Commit | Key Changes |
|---------|--------|-------------|
| v1-v2 | `4da03de` | Initial pipeline, Phase 0-3, Perplexity integration |
| v3 | `7468f03` | Search strategy upgrade, 3-gate scoring, scheduler fix |
| v4 | `103d0a7` | Token optimization (P0-P3), ~60-75% cost reduction |

---

## v3 Changes

### Commit: `7468f03` — Search Strategy + AI Scoring v2 + Scheduler Fix

#### 1. Three-Gate Scoring System (contextual_scorer.py)
Added three mandatory gates BEFORE AI scoring to prevent inflated scores:

- **Gate A (Relevance)**: Is the candidate's field related to the job? If unrelated → max 25 points
- **Gate B (Location)**: Is the candidate in the right geography? Overseas with no Taiwan connection → max 40 points
- **Gate C (Seniority)**: Partner applying for Engineer role? Overqualified → -15 to -25 points
- **Stacking rule**: Gates stack. e.g., overseas (max 40) + overqualified (-20) = max 20

#### 2. Candidate Name Blacklist (contextual_scorer.py)
`INVALID_NAME_PATTERNS` list filters out non-person entries like "LinkedIn", "Sign in", "HRnetGroup", etc. Returns score 0 with `is_relevant: False`.

#### 3. Scheduler Fix (scheduler/task_manager.py)
- `misfire_grace_time`: Changed from 1s (default) to **3600s**
  - Problem: Tasks scheduled at 09:00 would be skipped if server was busy at 09:00:01
  - Fix: Allow up to 1 hour late execution
- Added `coalesce=True`: If multiple misfires, only run once
- Added `max_instances=1`: Prevent duplicate concurrent runs
- Added missed-task detection on startup (logs warning if daily task hasn't run in 25+ hours)

#### 4. Multi-Dimensional Search (crawler/linkedin.py, engine.py)
- `title_variants`: Search with multiple job title variations (e.g., "BIM Engineer", "BIM Coordinator", "VDC Engineer")
- `target_companies`: Targeted company search (e.g., competitors, same-industry companies)
- `exclusion_keywords`: Filter out noise (e.g., "student", "intern", "sales")
- Phase 0 AI keyword generation now produces all these fields from job descriptions

#### 5. Grade Field Fix
`ai_match_result` now explicitly includes `grade` field (S/A+/A/B/C), not just `score`.

#### 6. Phase 1.5 Relevance Filtering (engine.py)
Rule-based pre-filter after search, before enrichment:
- Builds keyword set from job_title + primary_skills + secondary_skills
- Checks candidate title/bio/skills for keyword overlap
- Removes obviously unrelated candidates (sales/HR/legal/nurse/teacher with no skill match)
- Keeps candidates with insufficient info (lets enrichment decide)

---

## v4 Changes

### Commit: `103d0a7` — Token Optimization (P0~P3)

**Context**: Each job costs ~$11.4 USD (109 candidates x 2 Perplexity API calls). 47 jobs = ~$536 total. Four optimizations target 60-75% cost reduction.

---

### P0: Pre-filter Before AI Scoring (Save 30-50% scoring API calls)

**Files**: `enrichment/contextual_scorer.py`, `crawler/engine.py`

**New method**: `ContextualScorer.should_ai_score(enriched, job) -> bool`

Logic:
1. Calls existing `_quick_skill_match()` for local keyword comparison (0-100 score)
2. If `quick_score == 0` AND candidate has >= 3 known skills → **skip AI** (we have enough info to know they're irrelevant)
3. If `quick_score < 10` AND title matches `UNRELATED_TITLE_PATTERNS` (sales/HR/legal/nurse/teacher/driver/chef/receptionist) → **skip AI**
4. Otherwise → proceed with AI scoring

**In engine.py** `_ai_score_candidates()`:
```python
if self.job_context and not self.contextual_scorer.should_ai_score(c_dict, self.job_context):
    self._fallback_keyword_score(candidate)  # Use rule-based scoring instead
    skipped_prefilter += 1
    continue
```

Skipped candidates still appear in results with rule-based scores (not deleted).

**Class variable** `UNRELATED_TITLE_PATTERNS`:
```python
UNRELATED_TITLE_PATTERNS = [
    r'\b(sales|salesperson|...)\b',
    r'\b(marketing|...)\b',
    r'\b(hr|human resources|...)\b',
    r'\b(legal|...)\b',
    r'\b(nurse|...)\b',
    r'\b(teacher|...)\b',
    r'\b(driver|...)\b',
    r'\b(chef|...)\b',
    r'\b(receptionist|...)\b',
]
```

---

### P1: Scoring Uses Cheaper Model (Save ~80% output token cost)

**Files**: `enrichment/perplexity_client.py`, `enrichment/contextual_scorer.py`, `config/default.yaml`

**Rationale**:
- Enrichment (Phase 2) needs `sonar-pro` — requires web search to find LinkedIn data ($3/$15 per M tokens in/out)
- Scoring (Phase 3) is pure reasoning — `sonar` suffices ($1/$1 per M tokens in/out)
- Scoring output cost drops from $15/M to $1/M (93% savings)

**Changes**:

1. `perplexity_client.py` — `analyze_profile()` now accepts `model_override` parameter:
```python
def analyze_profile(self, linkedin_url, prompt, model_override=None, system_prompt=None):
    active_model = model_override or self.model
    payload = {'model': active_model, ...}
```

2. `contextual_scorer.py` — reads scoring model from config:
```python
self.scoring_model = config.get('perplexity', {}).get('scoring_model', 'sonar')
```

3. Scoring calls use `sonar`:
```python
raw = self.perplexity.analyze_profile('', user_prompt,
    model_override=self.scoring_model, ...)
```

4. `config/default.yaml`:
```yaml
enrichment:
  perplexity:
    model: sonar-pro          # enrichment (needs search)
    scoring_model: sonar      # scoring (pure reasoning)
```

5. Cost tracking updated — `_track_usage()` and `_estimate_cost()` accept `model_used` parameter for accurate per-call cost calculation.

---

### P2: Enrichment Result Cache (Save cross-job duplicate API calls)

**File**: `enrichment/profile_enricher.py`

**Problem**: When running multiple jobs, the same LinkedIn profile may be enriched multiple times (e.g., a BIM engineer appears in both BIM Engineer and BIM Manager job searches).

**Solution**: File-based JSON cache keyed by LinkedIn URL with TTL.

**Cache lifecycle**:
1. `__init__()`: Load cache from `data/enrichment_cache.json`
2. `enrich_candidate()`: Check cache before calling any provider
3. On successful enrich: Write to cache (strips `_enrichment_raw` to save space)
4. `enrich_batch()`: Save cache to disk after batch completes

**Cache format**:
```json
{
  "https://linkedin.com/in/some-profile": {
    "cached_at": "2026-03-08T10:30:00",
    "result": { ... enrichment result without _enrichment_raw ... }
  }
}
```

**TTL**: Default 7 days (configurable in `config/default.yaml`)

**Thread safety**: Uses `threading.Lock()` for all cache mutations.

**New methods**:
- `_load_cache()` — Load from JSON file on startup
- `_save_cache()` — Persist to disk (called after batch)
- `_get_cached(url)` — Lookup with TTL check; expired entries are removed
- `_set_cached(url, result)` — Write to cache, strips `_enrichment_raw`

**Stats**: `get_stats()` now includes `cache_hits` and `enrichment_cache_size`.

**Config**:
```yaml
enrichment:
  cache:
    file: data/enrichment_cache.json
    ttl_days: 7
```

---

### P3: Split JOB_MATCH_PROMPT into System + User (Save ~40% input tokens)

**Files**: `enrichment/prompts.py`, `enrichment/contextual_scorer.py`, `enrichment/perplexity_client.py`

**Problem**: The full `JOB_MATCH_PROMPT` (~2,500 tokens) is sent for every candidate. Job-specific context (position_name, key_skills, JD, etc.) is identical across all candidates for the same job but gets resent each time.

**Solution**: Split into:
- `JOB_MATCH_SYSTEM_PROMPT` (~1,800 tokens): Role + job context + three gates + scoring dimensions + output format. Cached per job.
- `JOB_MATCH_USER_PROMPT` (~500 tokens): Only candidate_profile + brief instruction. Changes per candidate.

**Important**: The system prompt template uses `{{{{` and `}}}}` for literal braces in `.format()` calls (Python double-escaping for JSON examples in the prompt).

**Changes**:

1. `prompts.py` — Two new constants:
```python
JOB_MATCH_SYSTEM_PROMPT = """...(role + job fields + gates + scoring + output format)..."""
JOB_MATCH_USER_PROMPT = """...(just {candidate_profile} + brief instruction)..."""
```
Original `JOB_MATCH_PROMPT` preserved for backward compatibility (`recommend_jobs()` still uses single-prompt format).

2. `perplexity_client.py` — `analyze_profile()` now accepts `system_prompt`:
```python
def analyze_profile(self, linkedin_url, prompt, model_override=None, system_prompt=None):
    sys_content = system_prompt or 'default system prompt...'
    payload = {
        'messages': [
            {'role': 'system', 'content': sys_content},
            {'role': 'user', 'content': prompt},
        ], ...
    }
```

3. `contextual_scorer.py` — Caches system prompt per job:
```python
self._job_system_cache = {}  # {job_id: formatted_system_prompt}

# In _ai_score():
if job_id not in self._job_system_cache:
    self._job_system_cache[job_id] = JOB_MATCH_SYSTEM_PROMPT.format(
        position_name=..., key_skills=..., ...
    )
system_prompt = self._job_system_cache.get(job_id)
user_prompt = JOB_MATCH_USER_PROMPT.format(candidate_profile=candidate_profile)
```

4. `clear_cache()` updated to also clear `_job_system_cache`.

---

## File-by-File Reference

### `enrichment/perplexity_client.py`
- Perplexity Sonar API wrapper
- Supports per-call model override (`model_override` parameter)
- Supports custom system prompt (`system_prompt` parameter)
- Pricing table for sonar, sonar-pro, sonar-reasoning-pro
- Usage tracking with per-model cost calculation
- Retry logic with exponential backoff on 429
- JSON extraction from mixed content (pure JSON, markdown code blocks, raw text)

### `enrichment/contextual_scorer.py`
- AI scoring engine using Perplexity
- v4 additions:
  - `should_ai_score()` — Pre-filter method (P0)
  - `UNRELATED_TITLE_PATTERNS` — Regex patterns for irrelevant titles
  - `scoring_model` config — Defaults to 'sonar' (P1)
  - `_job_system_cache` — Per-job system prompt cache (P3)
  - Split prompt calling in `_ai_score()` (P3)
- Three-gate post-processing (v3): relevance cap, location cap, overqualified penalty
- `evaluated_by`: `'Crawler-enricher-v4'`
- Name blacklist filtering (`INVALID_NAME_PATTERNS`)
- Grade mapping: 85-100=S, 80-84=A+, 70-79=A, 55-69=B, 0-54=C
- `recommend_jobs()` — Multi-job matching (still uses original `JOB_MATCH_PROMPT`)

### `enrichment/prompts.py`
Five prompt templates:
1. `PROFILE_ANALYSIS_PROMPT` — LinkedIn profile analysis (used by Phase 2 enrichment)
2. `JOB_MATCH_SYSTEM_PROMPT` (v4) — Job context + scoring rules (system message)
3. `JOB_MATCH_USER_PROMPT` (v4) — Candidate data only (user message)
4. `JOB_MATCH_PROMPT` (legacy) — Combined version for backward compat (`recommend_jobs()`)
5. `KEYWORD_GENERATION_PROMPT` — Phase 0 AI keyword generation
6. `JINA_TEXT_PARSE_PROMPT` — Parse Jina raw text into structured data
7. `ANALYSIS_REPORT_TEMPLATE` — Human-readable report template

### `enrichment/profile_enricher.py`
- 3-provider fallback: LinkedIn API → Perplexity → Jina Reader
- v4 additions:
  - File-based enrichment cache (`_enrichment_cache`)
  - Cache methods: `_load_cache()`, `_save_cache()`, `_get_cached()`, `_set_cached()`
  - Thread-safe with `threading.Lock()`
  - Cache strips `_enrichment_raw` to save space
  - Stats include `cache_hits` and `enrichment_cache_size`
- `_normalize_enrichment()` — Converts raw API response to Step1ne candidate format
- `_calc_stability_score()` — 0-100 stability score from tenure/changes/gaps

### `crawler/engine.py`
- Main pipeline orchestrator
- Phase 1: LinkedIn + GitHub search → merge & dedup
- Phase 1.5: Relevance filtering (rule-based)
- Phase 2: ProfileEnricher batch enrichment
- Phase 3: AI scoring with pre-filter (v4 P0)
  - `should_ai_score()` check before each API call
  - `skipped_prefilter` counter in log
- Phase 4: Sort by ai_score/score, save results
- `_fallback_keyword_score()` — Rule-based scoring fallback

### `scheduler/task_manager.py`
- APScheduler integration for daily/weekly/interval tasks
- `misfire_grace_time=3600` (v3 fix)
- `coalesce=True`, `max_instances=1`
- Missed task detection on startup
- Phase 0: `_pull_job_context()` + `_generate_ai_keywords()`
- Auto-push to Step1ne on task completion
- Local JSON result storage in `data/results/{task_id}.json`

### `config/default.yaml`
Key sections:
```yaml
enrichment:
  perplexity:
    model: sonar-pro          # enrichment (Phase 2)
    scoring_model: sonar      # scoring (Phase 3)
    timeout: 60
    max_retries: 2
    search_context_size: high
  cache:
    file: data/enrichment_cache.json
    ttl_days: 7
  provider_priority: [linkedin, perplexity, jina]

step1ne:
  api_base_url: https://backendstep1ne.zeabur.app
  auto_push: false

server:
  port: 5050
```

---

## Configuration

### API Keys (in config/default.yaml)
- `api_keys.perplexity_api_key` — Perplexity Sonar API
- `api_keys.brave_api_key` — Brave Search API
- `api_keys.github_tokens` — GitHub API tokens (array)
- `enrichment.perplexity.api_key` — Same Perplexity key (duplicated for enrichment module)

### Step1ne Integration
- `step1ne.api_base_url`: `https://backendstep1ne.zeabur.app`
- `step1ne.auto_push`: Set to `true` to auto-push candidates after task completion
- Each task can also have individual `auto_push` flag

### Perplexity Model Selection
- `enrichment.perplexity.model`: Used for enrichment (Phase 2) — needs web search, use `sonar-pro`
- `enrichment.perplexity.scoring_model`: Used for scoring (Phase 3) — pure reasoning, use `sonar`

### Perplexity Pricing Reference (per 1M tokens)
| Model | Input | Output | Per Request |
|-------|-------|--------|-------------|
| sonar | $1.0 | $1.0 | $0.005 |
| sonar-pro | $3.0 | $15.0 | $0.006 |
| sonar-reasoning-pro | $2.0 | $8.0 | $0.006 |

---

## Known Issues & Pending Tasks

### High Priority
1. **Git push pending** — 2 commits not yet pushed to `origin/main` (`https://github.com/jacky6658/headhunter-crawler.git`):
   - `7468f03` — v3 search strategy + scoring + scheduler fix
   - `103d0a7` — v4 token optimization
   - Requires GitHub credentials setup (PAT, SSH key, or `gh` CLI)

2. **Step1ne API `/api/jobs` incomplete response** — GET `/api/jobs` doesn't return `title_variants`, `target_companies`, `exclusion_keywords` fields. These are only available from individual job detail endpoint.

### Medium Priority
3. **Remaining crawler jobs** — The following jobs still need to be run:
   - Job 173: Finance Supervisor (English Platinum)
   - Jobs 54,50,49,48,47,46: Chuanle Tech (6 jobs)
   - Jobs 53,52,51: Yitong Digital (3 jobs)
   - Jobs 45,44,40,38,30,29,21,20,19,18,16,15: Gamania (12 active)
   - Others: Meide(9,6), Shipeng BIM(8), Luzhun(7), Youfu(171), Zhibang(10), AIJob(5,4,3,2)

4. **v4 scoring quality validation** — After downgrading scoring model from `sonar-pro` to `sonar`, need to spot-check Top 10 candidates to ensure quality hasn't degraded significantly.

### Low Priority
5. **Enrichment cache cleanup** — No automatic cleanup of expired cache entries on startup (only checked on read). Consider periodic cleanup.
6. **LinkedIn API client** — The `linkedin_client.py` module exists but LinkedIn API credentials are not configured (`username`/`password` empty in config). Currently all enrichment goes through Perplexity.

---

## Git Status & Deployment

### Branch: `main`
### Remote: `https://github.com/jacky6658/headhunter-crawler.git`

### Recent Commits (newest first):
```
103d0a7 feat: v4 token optimization (pre-filter/model downgrade/cache/prompt split)
7468f03 feat: search strategy upgrade + AI scoring v2 + scheduler fix
4da03de feat: AI full pipeline — Phase 0~3 deep analysis + scoring
83ec646 Update config with new API keys and credentials path
4bee202 Fix SSL auto-fallback, Sheets quota, Settings key display
```

### How to Run
```bash
cd /Users/jackychen/Downloads/headhunter-crawler
source venv/bin/activate
python app.py  # Starts on port 5050
```

Or use the `.claude/launch.json` configuration:
```json
{
  "version": "0.0.1",
  "configurations": [
    {
      "name": "crawler",
      "runtimeExecutable": "python",
      "runtimeArgs": ["app.py"],
      "port": 5050
    }
  ]
}
```

### Key API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/tasks` | List all tasks |
| POST | `/api/tasks` | Create new task |
| POST | `/api/tasks/<id>/run` | Run task immediately |
| GET | `/api/tasks/<id>/status` | Task status + progress |
| GET | `/api/results/<id>` | Get task results |
| GET | `/api/enrich/stats` | Enrichment stats (cache hits, token usage) |
| POST | `/api/push/<task_id>` | Push results to Step1ne |
| GET | `/api/jobs` | List Step1ne jobs |
| GET | `/api/csv/<task_id>` | Export results as CSV |

---

## Cost Estimation (v4)

Per job (assuming 109 candidates):
| Phase | Model | Est. Cost | Notes |
|-------|-------|-----------|-------|
| Phase 0 (keywords) | sonar | ~$0.04 | 1 call |
| Phase 2 (enrichment) | sonar-pro | ~$5.78 | 109 calls |
| Phase 3 (scoring) | sonar | ~$1.50 | ~70 calls (after P0 filter) |
| **Total per job** | | **~$7.32** | **Was ~$11.4 pre-v4** |

Estimated savings: **~36% per job** from P0+P1 alone. With P2 cache across multiple jobs, savings increase to 60-75%.
