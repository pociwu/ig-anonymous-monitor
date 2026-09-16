# IGWatcher 分類擷取與媒體下載診斷

## 分類擷取失敗與 Posts 退避

`[IGWATCHER-COLLECTION-DIAG]` 記錄分類擷取的失敗與 Posts 退避略過狀態，附帳號標籤、分類及固定錯誤說明。新增欄位：

- `code`：錯誤或略過原因的固定代碼，見下表。
- `attempted=True`／`False`：本輪是否實際嘗試該分類。退避略過為 `False`，不算新的失敗。
- `next_retry_at`：保存在資料庫中的 Posts 最早重試時間（UTC），沒有期限時為 `none`。其他分類不會因此啟用 Posts 退避。

| 代碼 | 意義 |
| --- | --- |
| `posts_gated` | 來源暫時無法提供貼文，不代表貼文為空。 |
| `fetch_failed` | 來源回報抓取失敗，不代表內容為空。 |
| `source_error` | 來源回報錯誤或未支援的狀態提示。 |
| `invalid_response` | 成功狀態、欄位、媒體項目或分頁格式不符合預期。 |
| `http_error` | 一般 HTTP 回應不成功；明確驗證／限流訊號仍進入來源共用冷卻。 |
| `request_timeout` | 來源請求逾時，本輪不立即重試。 |
| `connection_failed` | 來源連線失敗，本輪不立即重試。 |
| `collection_backoff` | Posts 仍在退避中，本輪沒有送出 Posts API 請求。 |

Posts 每次實際失敗依序等待 30、60、120、240 分鐘，上限 240 分鐘；期限與失敗計數保存在資料庫，重啟後仍有效。到期只允許下一次正常排程嘗試，不保證在該時間執行或恢復。退避略過不更新失敗計數、最後實際嘗試時間、期限、游標或通知；Profile、其他分類與其他帳號仍可更新，既有媒體佇列仍可向 CDN 下載。來源共用冷卻仍具有優先權，完整行為見 [冷卻與降低請求量](igwatcher-cooldown.md)。

分類診斷使用固定代碼與經整理的錯誤說明，不輸出來源原始錯誤本文或媒體 URL。更新後等待正常排程，再取得分類診斷：

```bash
cd /srv/ig-monitor
docker compose logs --since 24h --no-color monitor 2>&1 |
  grep -F '[IGWATCHER-COLLECTION-DIAG]' |
  tail -n 30
```

只貼回上述標記的篩選結果。`attempted=False` 表示退避略過，不能把這些列加總為實際失敗次數或 HTTP 請求數；一次分類嘗試也可能涉及多頁 HTTP 請求。若來源共用冷卻已使整輪跳過，沒有新的分類診斷不代表 Posts 已恢復。

已通知且尚未恢復的舊事故，不會因程式更新或錯誤原因改變而立即重發 Telegram；新原因可先從正常排程的日誌確認。新 Posts 失敗通知會顯示實際失敗次數及最早重試時間，舊事件若沒有 `next_retry_at` 則保留原格式。無需清空資料、重建 schema、重設來源冷卻或額外啟動巡檢取得日誌。

升級後既有 `fail_count` 不歸零；舊 schema 新增的 `next_retry_at` 起初為 `NULL`，首次允許的正常排程實際嘗試 Posts 且失敗後才會產生期限。因此既有失敗次數已達 3 次以上的帳號，下次實際失敗會直接等待 240 分鐘。

## 媒體下載失敗

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

阻擋仍維持來源全域冷卻、不再送出後續請求、不把 blocked 項目當成成功或普通下載失敗。中途停止的帳號仍可能沒有完整下載摘要；僅憑已完成帳號的「失敗 0」不能推論後續下載正常。既有冷卻紀錄沒有上述欄位，只會在更新後實際允許的排程遇到新阻擋時產生；不要為取得日誌清除冷卻或另外啟動巡檢。
