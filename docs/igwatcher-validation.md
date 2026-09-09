# IGWatcher 保守保存版：Ubuntu 隔離驗證

此版本實作 [ADR 018](adr/018-conservative-igwatcher-memberships.md) 的已接受取捨，不表示來源已通過完整性或正式上線驗收。預設來源仍為 AnonyIG，預設媒體下載仍關閉；只有本頁的獨立範本選用 IGWatcher 並允許每輪最多 8 筆媒體下載。不會使用 Instagram 登入、人工驗證碼、代理或付費服務。

## 三項問題如何處理

| 問題 | 保存與顯示規則 |
| --- | --- |
| 輪播沒有子項目 ID | 父貼文、當次觀測版本及位置產生明確的本地 ID，`source_media_id` 留空。相同回應重跑不重複建卡；順序、URL 或宣告數變動建立另一版觀測，歷史可展開。URL 只是區分觀測，不是媒體身分證據。下載後用 SHA-256 共用完全相同檔案，待確認內容不做感知相似合併，包括批次去重維護。 |
| 無法證明作者／共同作者 | 所有 IGWatcher 媒體先進入「歸屬待確認」，保留原四類篩選，不混入一般相簿、預覽或一般媒體統計。頁面標示來源、查詢帳號及不代表已確認作者；不發送媒體附件、不自動轉正，也不等同於既有跨帳號污染隔離。 |
| 精華宣告 10、回傳 9 | 保存可接納項目，分開顯示「來源宣告、本次回傳、本地已保存」。歷次項目取聯集，缺席不刪除；相同項目改序不重複計數。後續變成 9/9 仍保留先前缺口觀測與完整性未確認。 |

「本次回傳」是來源陣列長度，不是成功下載數；格式不合約或超出下載上限的項目不會被冒充為已保存。Posts／Reels 只收最近設定範圍（預設各 12 則邏輯貼文，包含輪播所有可接納子項），精華依有限批次續查，沒有完整 IG 歷史保證。關閉下載時只保存個人檔案、集合及群組觀測資訊，不新增媒體候選或推進下載續傳；原有檔案保留。

## 第一次執行

以下三步只操作 `/srv/ig-monitor/ig-anonyig-validation` 的程式及獨立 Docker Compose 專案，不要與正式 `compose.yaml` 合併，也不要覆蓋正式 `config.yaml`：

```bash
git -C /srv/ig-monitor/ig-anonyig-validation pull --ff-only &&
docker compose -f /srv/ig-monitor/ig-anonyig-validation/compose.igwatcher-validation.yaml build validate &&
docker compose -f /srv/ig-monitor/ig-anonyig-validation/compose.igwatcher-validation.yaml run --rm --no-deps validate
echo $?
```

獨立專案：`ig-monitor-igwatcher-validation`；映像：`ig-anonymous-monitor:igwatcher-validation`；資料存在命名 volume `igwatcher-validation-state` 所對應的 Compose 專案 volume，與 AnonyIG 驗證資料分離。容器內只掛入範本設定及隔離資料，不掛入 `.env`、collector secrets、正式資料或媒體。Telegram／心跳／Apify／登入補充全部關閉；沒有背景排程，單次執行有 600 秒外層期限。

查看頁面：

```bash
docker compose -f /srv/ig-monitor/ig-anonyig-validation/compose.igwatcher-validation.yaml up -d dashboard
```

儀表板只發布於 Ubuntu `127.0.0.1:8890`。若由另一台電腦開啟，可用既有 SSH 連線建立 `-L 8890:127.0.0.1:8890` 通道，再開啟 `http://127.0.0.1:8890`。進入 NASA 詳細頁後選「歸屬待確認」；一般四類計數為 0 並不表示沒下載。

## 第二次檢查

不必重新建置，用相同 `run --rm --no-deps validate` 再執行一次。檢查：

1. 已保存相同檔案不重複出卡；尚未下載項目可能繼續保存，因此第二輪新增不一定為 0。
2. Posts 輪播可依當次順序瀏覽；若來源改序或更換 URL，舊版本仍可展開。簽名 URL 變動可能需要再次下載後才能確認完全相同內容。
3. Highlights 顯示來源宣告／本次回傳／本地已保存三個不同數字。少回傳不刪除舊項目，不撤銷歷史缺口。
4. 沒有媒體附件、正式資料或正式設定變動。

若出現來源驗證／限流／拒絕存取，該輪後續來源與下載請求立即停止，沿用來源冷卻機制；不要連續重跑或把失敗改當空集合。退出 `0` 只代表本輪保守保存流程沒有操作錯誤，不是 `production_ready=true`；退出 `1` 檢查分類／下載錯誤日誌，`124` 表示外層期限到達。原 `[IGWATCHER-PROBE]` 嚴格探針未修改，仍可能報 `sample_incomplete`／退出 `2`。

## 已知契約與未驗證部分

2026-09-09 開發機補查 profile 收到 HTTP 403 後已停止，沒有重試或下載現場媒體。本地測試使用人工 HTTP／媒體資料及真實暫存 SQLite，**未在開發機執行 Ubuntu ARM64 Docker 或證明新 adapter 的現場下載成功**。先前 Ubuntu 探針成功只證明當時端點可回資料。

目前 profile 解析明確要求 `data.user` 中字串 `id`、相符 `username`、布林 `is_private`、非負整數 `media_count`／`follower_count`／`following_count`、字串 `full_name`／`biography`／`profile_pic_url`；`pk` 若存在必須與 `id` 相同。這套完整欄位映射尚待 Ubuntu 實際回應驗證，缺欄位時會明確失敗，不填假 0。

媒體僅允許 HTTPS `cdninstagram.com`、`fbcdn.net` 及其子網域、預設或 443 埠，不跟隨重新導向，不接受任意代理網址。這是保守允許清單，不代表已確認 IGWatcher 所有回應都使用這些主機；其他網址會拒收並保留部分完成狀態。每份 JSON 上限 2 MiB、媒體 100 MiB；JSON 請求至多 20 秒、媒體至多 30 秒（亦受設定的較短期限限制），每類頁數有上限，本範本另有整輪 600 秒外層期限。來源變更契約需要另外查證，不會自動放寬。

請回傳第一次執行的最末段日誌及退出碼即可，不必貼完整建置過程、原始 JSON、媒體網址或憑證。隔離驗證完成後再另行決定是否正式啟用。
