# IGWatcher 冷卻與降低請求量

冷卻儲存在共用資料庫，不因主程式重啟而消失。期限內主監控整輪跳過來源，成員補充 worker 也檢查相同來源狀態；每個媒體請求與跳轉仍檢查 guard。已送出的請求不能撤回。獨立驗證環境若使用另一份資料庫，無法共享正式環境冷卻，勿同時執行探針或驗證排程。

冷卻到期表示允許下次排程嘗試，不等於網站已恢復。IGWatcher 必須整輪無帳號、分類或媒體下載錯誤，才會宣告來源恢復並清除退避次數。只成功一個帳號不能清除其他失敗。再次遭拒絕時保留既有 30、60、120、240 分鐘遞增冷卻；期限內不送出來源請求。單純逾時仍不是來源限流的證據。

降低負載可先手動調整正式 config.yaml 的既有鍵值（合併至原區塊，不覆蓋帳號或其餘設定）：

```yaml
browser:
  max_pages_per_collection: 1
schedule:
  interval_minutes: 60
  account_delay_min_seconds: 30
  account_delay_max_seconds: 60
  media_limit_per_account: 10
```

這是本地保守的起始設定，不是 IGWatcher 公告的安全配額，也不能保證不被限流。代價是資料更新與歷史下載較慢；分類內請求及媒體請求並未因此增加逐筆延遲。max_pages_per_collection 限制每類分頁／精選批次，不是整輪 HTTP 請求上限；媒體跳轉也會增加請求。

短期要大幅減少流量，可用既有 `schedule.media_download_enabled: false` 暫停新媒體記錄與下載，但仍會執行來源分類觀測，不是完全零請求。若要完全停止正式主監控，使用 `docker compose stop -t 300 monitor`，不要刪除資料；另外確認成員補充與獨立驗證服務是否仍有工作。

本次修改不更動 Ubuntu 實際設定、不取消冷卻、不縮短既有等待時間，也不將失敗當成空資料。
