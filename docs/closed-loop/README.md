# 閉環系統 — 完整文件索引

## 架構

```
一個職缺一條龍：Phase A → Phase B → Phase C → 通知顧問 → 下一個職缺
```

所有 Phase 由 hr-yuqi 執行，mty-yuqi 負責品質監控。

## 文件

| 檔案 | 說明 |
|------|------|
| [閉環執行提示詞.md](../閉環執行提示詞.md) | 龍蝦主提示詞（Phase A/B/C 完整流程） |
| [import-complete-sop.md](./import-complete-sop.md) | import-complete API 使用說明 |
| [ai-analysis-format.md](./ai-analysis-format.md) | AI 深度分析 JSON 格式規範 |
| [notify-rules.md](./notify-rules.md) | 通知規則（誰收到、什麼條件觸發） |

## 腳本

| 腳本 | Phase | 說明 |
|------|-------|------|
| `scripts/daily_closed_loop.py` | A | 搜尋 + A層篩選 + 去重 + 匯入 |
| `scripts/phase_b_pdf_download.py` | B | 批次 LinkedIn PDF 下載（JS click） |
| `scripts/linkedin_pdf_download.py` | B | PDF 下載核心邏輯（被 phase_b 引用） |
| `scripts/phase_c_ai_analysis.py` | C | AI 分析寫入（格式鎖死） |
| `scripts/notify_consultant.py` | C+ | Phase C 完成後通知顧問（TG 群組） |
| `scripts/verify_pipeline.py` | 驗證 | 每日驗證閉環結果完整性 |

## 每日排程

| 時間 | 誰 | 做什麼 |
|------|-----|--------|
| 06:00 | hr-yuqi | Phase A — 搜尋+篩選+匯入 |
| Phase A 完成後 | hr-yuqi | Phase B — LinkedIn PDF 下載 |
| Phase B 完成後 | hr-yuqi | Phase C — AI 分析 + 寫入 |
| Phase C 完成後 | hr-yuqi | 執行 notify_consultant.py 通知群組 |
| 12:00 / 16:00 | mty-yuqi | 品質監控 — 檢查缺 PDF 補漏 |

## Phase C 分數規則

| 分數 | 動作 |
|------|------|
| >= 60 | 完整 AI 分析 + must_ask 10 題 + 通知 TG 群組 |
| 30-59 | AI 分析，不產生 must_ask，不通知 |
| < 30 | 基本分析，放人才庫 |

## 智慧職缺重配（Phase C）

如果候選人跟原始職缺 score < 40：
1. 自動掃全部 22 個招募中職缺
2. 用 key_skills + title + experience 比對
3. 找到 score >= 60 的就改 target_job_id
4. 找到多個就挑最高分的
5. 全部 < 40 才放人才庫

例：搜 Java Developer 進來一個 BD → 原職缺 score 15 → 自動匹配到 #238 BD主管 score 72 → 改 target_job_id + 通知顧問

## 通知規則

- match_score >= 60 → 即時通知 TG 群組 @behe10 @jackyyuqi
- match_score < 60 → 靜默存入人才庫
- 每輪跑完發彙總報告

## API 端點

| 用途 | 端點 |
|------|------|
| 一次匯入（新） | POST /api/ai-agent/candidates/import-complete |
| 讀人選 | GET /api/ai-agent/candidates/:id/full-profile |
| 寫 AI 分析 | PUT /api/ai-agent/candidates/:id/ai-analysis |
| 上傳 PDF | POST /api/candidates/:id/resume-parse |
| 爬蟲鎖定 | PUT /api/system-config/crawl_lock_{job_id} |
