# OCI 單筆瀏覽器媒體驗證

2026-09-28，以隔離 Chromium/Xvfb 容器驗證 `songjilay9453` 的一筆 Stories。
未掛載正式設定、資料庫或登入工作階段；未解除正式來源冷卻，未啟動批次下載。

## 結果

- search 與 stories 均 HTTP 200，成功契約成立，Stories 回傳 1 筆。
- 開啟播放器取得 video/mp4 HTTP 206；按網站 Download 後取得完整 HTTP 200。
- 存檔：OCI `/tmp/igw-download-evidence/igw-story.mp4`（暫存驗證檔，非正式媒體）。
- 長度與回應 Content-Length 相符：1,065,084 bytes；MP4 magic 正確。
- ffprobe：H.264 720×1280、AAC、5.130204 秒。
- 以無網路容器執行 `ffmpeg -v error -xerror -i /evidence/igw-story.mp4 -f null -`，完整解碼退出碼 0。
- SHA-256：`ef9468f46b5c2f9acd991b7232ad8e0abd927f4020ed4f8b08a44a12aa107f73`。

## 測試腳本問題

1. 網站 Stories 載入超過 6 秒可自動轉回 Posts。必須先觸發 Stories，等待隱藏分頁的卡片建立，再切回 Stories 並開啟卡片；不能只等待可見且名稱符合時間格式的按鈕。
2. 隔離路由誤擋網站 `/wp-json/igw/v1/media`。診斷須允許網站本身的媒體端點，同時維持請求上限與拒絕即停。
3. 播放器以 HTTP 206 讀取影片，僅接受 HTTP 200 的測試不會完成。此次使用網站 Download 取得完整檔，不將任意分段視為完整下載。

## 能與不能推論的事

此結果證明 OCI 的正常瀏覽器流程可保存這筆媒體，不證明既有 HTTP 下載器、舊 URL、其他帳號或各分類均可用，也不證明不會再次限流。

## 可選媒體傳輸（尚未正式啟用）

`browser.igwatcher_media_transport` 預設 `http`。`browser_proxy` 使用隔離 Chromium 的同源 fetch，將已驗證 CDN 網址交給網站 `/wp-json/igw/v1/media`；不載入網站查詢脚本、不刷新網址、不改媒體身分，不在 HTTP 失敗後自動切換通道。這是依網站 Download 傳輸行為整理的實作，與已驗證的完整 UI 流程不同，仍需獨立驗收。

維持單筆容量上限、完整 200 回應、MIME/magic 驗證、來源冷卻及逐檔退避；不接受 206 或重新導向作為成功。401/403/422/429 與驗證訊號仍停止來源；一般 404/格式錯誤只讓該檔失敗。`headless: false` 需要 Xvfb/顯示環境，並須設定可寫入的 `XDG_CONFIG_HOME`；本次不修改部署設定。

不得因本功能而自動清除現有來源冷卻。需到期後先驗證一筆真實佇列網址，確認完整存檔、去重與狀態一致後，才擴大下載。

離線整合：`tests/browser_media_offline_check.py` 已於 OCI `ig-anonymous-monitor:local` 的 Chromium 執行，容器 `--network none`，所有回應由測試 fixture 提供。驗證完整 200、冷卻零請求、206 拒收、串流容量上限、challenge/429 停止；沒有呼叫來源或寫入正式資料庫。

可規劃將一般分類解析失敗與媒體下載狀態分開；不能僅依一次成功取消來源 429、驗證要求或拒絕存取的保護。網站代理與現有 CDN 直連不同，正式支援仍需身分綁定、URL/重新導向驗證、容量限制、去重及資料庫整合測試。正式冷卻邏輯本次未修改。
