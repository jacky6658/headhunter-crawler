# 通知規則

## TG 群組通知

- **群組**：https://t.me/c/3231629634/1247
- **Bot Token**：8342445243:AAErYaMxOSO7p5cZUwYLBsRJqiOkH73nqSc
- **Chat ID**：-1003231629634
- **Thread ID**：1247

## 觸發條件

| match_score | 動作 |
|-------------|------|
| >= 60 | 即時通知群組 @behe10 @jackyyuqi |
| < 60 | 靜默存入人才庫，不通知 |

## 通知訊息格式

```
🔔 新人選推薦 — 請顧問盡快聯繫 @behe10 @jackyyuqi

📋 職缺：#42 Senior Backend Engineer @ 某新創
👤 人選：#1890 Zedd pai
📊 匹配分數：90/100（強烈推薦）
🏷 評級：A+
📍 地點：台北
💼 現職：Senior Engineer @ Circle
📝 摘要：9年資深軟體工程師，Web3/Blockchain專家

✅ 資料：PDF ✅ | AI分析 ✅ | 經歷 ✅

👉 指派顧問：Phoebe
```

## 彙總報告（每輪 Phase C 結束後）

```
📊 Phase C 完成報告 — 2026-03-25 14:30

✅ 本輪處理：15 人
🔔 推薦聯繫（≥60分）：3 人
📁 存入人才庫（<60分）：12 人

推薦名單：
1. #1890 Zedd pai | SRE @ 宇泰華 | 90分 | Phoebe
2. #3003 Hsin Yi Chao | Java Dev | 77分 | Phoebe
3. #3767 Tzu-Wei Huang | Data Engineer | 55分 | Phoebe
```

## 顧問指派規則

| 客戶公司 | 顧問 |
|---------|------|
| 築楽國際 | Jacky |
| 其他所有 | Phoebe |

## 執行方式

```bash
# Phase C 完成後自動執行
cd /Users/user/clawd/headhunter-crawler
python3 scripts/notify_consultant.py --batch

# 單人測試
python3 scripts/notify_consultant.py --candidate-id 1890

# 指定多人
python3 scripts/notify_consultant.py --batch --ids 1890,3767
```
