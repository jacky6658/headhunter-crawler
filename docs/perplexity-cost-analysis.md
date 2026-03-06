# Perplexity API 費用分析 — LinkedIn Profile 分析用途

> 更新日期: 2026-03-06

## 場景：分析一位 LinkedIn 候選人

- Prompt: ~500 tokens (指令 + URL)
- Response: ~800 tokens (技能、經歷、學歷結構化輸出)
- Search context: Low (只讀一個 URL)

## Perplexity 方案對比

| 方案 | 每次 Request 費 | Token 費 | 每位候選人成本 | 100 位 | 1000 位 |
|---|---|---|---|---|---|
| **Sonar** (最便宜) | $0.005/次 | Input $1/1M, Output $1/1M | ~$0.006 | ~$0.60 | ~$6 |
| **Sonar Pro** (更準確) | $0.006/次 | Input $3/1M, Output $15/1M | ~$0.02 | ~$2 | ~$20 |
| **Sonar Reasoning Pro** | $0.006/次 | Input $2/1M, Output $8/1M | ~$0.015 | ~$1.5 | ~$15 |
| **Sonar Deep Research** | $0.005/search | Input $2/1M, Output $8/1M + Reasoning $3/1M | ~$0.05+ | ~$5 | ~$50 |

### 其他工具價格
- **Search API** (純搜尋無合成): $5/1K requests = $0.005/次
- **Fetch URL** (Agent API): $0.0005/次
- **Embeddings**: pplx-embed-v1-0.6b $0.004/1M tokens

## Perplexity 免費額度

- 註冊送 **$5 免費額度** (約 800+ 次 Sonar 查詢)
- **無持續性免費方案** — 用完就要付費
- 消費版 perplexity.ai 有免費搜尋，但 API 沒有月免費額度

## 免費替代方案比較

| 方案 | 成本 | 免費額度 | 能讀 LinkedIn？ | 優劣 |
|---|---|---|---|---|
| **Jina Reader** | 免費 | 100 RPM, 10M tokens 試用 | 可以 | 最簡單，URL 前加 prefix 就好 |
| **Tavily** | 免費 1000次/月 | 1000 credits/月 | 搜尋+提取 | 有月免費額度 |
| **Brave Search API** | 已購買 | 已有 | 只有搜尋摘要 | 已整合在爬蟲裡 |
| **現有 OCR** | 免費 | 10次/小時 | 截圖辨識 | 慢、受 CAPTCHA 限制 |
| **Perplexica** (自架) | 完全免費 | 無限 | 需搭配 LLM | 需自己架設+維護 |

## 結論

- **最划算**: Jina Reader (完全免費, 直接可用)
- **最穩定付費**: Perplexity Sonar (~$6/1000人)
- **現有方案**: OCR (免費但慢且有限)

## 參考連結

- Perplexity Pricing: https://docs.perplexity.ai/docs/getting-started/pricing
- Jina Reader: https://jina.ai/reader/
- Tavily: https://docs.tavily.com/documentation/api-credits
- Perplexica (開源): https://github.com/ItzCrazyKns/Perplexica
