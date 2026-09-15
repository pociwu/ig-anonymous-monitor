# IGWatcher 下載失敗診斷

`來源 HTTP 回應不成功` 與 `來源請求逾時` 是不同路徑，不能僅憑失敗數量調高等待時間。

非 200 的一般媒體回應現在附帶 `http_status=404` 等實際狀態碼；下載佇列的失敗警告另含 `[IGWATCHER-MEDIA-DIAG]`、`category`、`kind` 與 `elapsed_ms`（該項目本輪處理至失敗的毫秒數，不是佇列等待時間）。

媒體下載允許最多 3 次 301／302／303／307／308 跳轉，每次都重新檢查 HTTPS、無帳密、標準連接埠及 cdninstagram.com／fbcdn.net 媒體網域（含子網域）。相對 Location 依當前網址解析；缺失、不合法或不受信任的目的地不會被請求。來源 API 不跟隨跳轉。

整條跳轉鏈仍受原本的總逾時限制，每一跳清除 Cookie，使用固定來源 Referer，不攜帶上一跳網址。401／403／422／429 與驗證回應仍走原本的來源暫停流程；最終回應仍須通過容量、MIME 與媒體檔頭檢查。不調整排程、不清除資料，也不保證跳轉後必定能取得檔案。

診斷不輸出媒體 URL、查詢參數、回應本文、Cookie 或 Location；分類及媒體類型使用固定允許值。這不是其他套件或完整應用程式日誌的全面脫敏功能。

Ubuntu 正式部署更新後，等排程完成一輪，再取得診斷：

```bash
cd /srv/ig-monitor
docker compose logs --since 2h --no-color monitor 2>&1 |
  grep -F '[IGWATCHER-MEDIA-DIAG]' |
  tail -n 30
```

只貼回篩選結果。3xx 可辨識重新導向；404／410 表示該次請求的資源不可取得；5xx 表示伺服器錯誤。單憑狀態碼仍不能斷定網址過期、內容永久刪除或來源故障的確切原因。逾時及連線失敗沒有 HTTP 狀態碼，不應偽造為 0 或 200。

## 觸發來源暫停的診斷

`[IGWATCHER-BLOCK-DIAG]` 僅記錄實際收到拒絕或驗證訊號的回應，不會在冷卻跳過請求時產生。欄位：

- `http_status`：實際狀態碼，含 HTTP 200 但本文要求驗證的情況。
- `phase=api`／`media`：來源 API 或媒體 CDN；頭像也屬媒體。
- `endpoint`：固定 API 名稱（profile、stories、posts、reels、highlights、highlight_items）或 `cdn`，不是原始網址。
- `redirects`：收到該回應前已跟隨的跳轉次數；0 為首個請求，1 表示跳轉一次後遭阻擋。
- `signal=http_status`／`challenge`：HTTP 拒絕狀態，或本文含驗證訊號。不是對網站拒絕原因的推測。

主監控 API 警告附當前帳號標籤。媒體佇列警告另有 `operation=queue`、分類與檔案類型；頭像有 `operation=avatar` 與帳號鍵值。成員補充 worker 另記 `operation=member_profile` 與正在查詢的成員 username（不是被監控的上層帳號）；若為成員頭像，可能同時看到以成員 ID 記錄的 avatar 訊息。一次阻擋可能同時出現在日誌、冷卻事件或診斷檔，不能按標記出現次數計算網路請求次數。

```bash
cd /srv/ig-monitor
docker compose logs --since 24h --no-color monitor member-enrichment-worker 2>&1 |
  grep -F '[IGWATCHER-BLOCK-DIAG]' |
  tail -n 30
```

這次只補診斷，仍維持來源全域冷卻、不再送出後續請求、不把 blocked 項目當成成功或普通下載失敗。中途停止的帳號仍可能沒有完整下載摘要；僅憑已完成帳號的「失敗 0」不能推論後續下載正常。既有冷卻紀錄沒有上述欄位，只會在更新後實際允許的排程遇到新阻擋時產生；不要為取得日誌清除冷卻或另外啟動巡檢。
