# ADR 001：以 Apify 進行帳號身分反查與成本上限

- 狀態：已接受
- 日期：2026-07-27

## 背景

Instagram username 可變更；巡檢網址若仍使用舊名稱，可能無法繼續定位原帳號。系統需要保存穩定的帳號識別，並限制外部查詢服務的月支出不超過 5 美元。

## 決策

1. 固定使用官方 Apify Actor `apify/instagram-profile-scraper`。
2. 首次建立資料時，對每個啟用帳號保存 Instagram Profile ID。
3. 後續僅在一般巡檢無法定位帳號，或名稱不一致時，才以保存的 Profile ID 反查目前 username。
   反查確認更名後，自動切換監控目標至新網址，並以 Telegram 回報舊、新名稱。
   資料庫的有效監控網址優先於 `config.yaml` 的初始網址。
4. 以 Apify 的 `monthlyUsageCycle` 作為預算週期。
5. 程式在呼叫前採用本地用量帳本檢查，且在 Apify 帳戶設定每月 5 美元上限；任一保護層判定已達上限時，不再發出查詢並以 Telegram 通知。
   同一用量週期的上限通知只發送一次。

## 後果

- 正常巡檢不會持續產生 Apify 查詢成本。
- 更名後可用保存的穩定 ID 找回新 username，並更新監控網址。
- 達到上限時，名稱反查會暫停至下一個 Apify 用量週期。
