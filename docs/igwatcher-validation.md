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

## 啟動時出現唯讀瀏覽器目錄錯誤

`84ec0fd` 在建立 `Monitor` 時仍無條件建立瀏覽器目錄；範本省略 `browser.browsers_path`，其預設因設定檔位置而解析為 `/validation-config/data/ms-playwright`，在唯讀容器中會得到 `OSError: [Errno 30] Read-only file system`。此錯誤發生於來源請求前，不是 IGWatcher 的驗證／限流結果，也不是工作目錄或其他專案的部署問題。

修正後，HTTP-only IGWatcher 不建立瀏覽器目錄；AnonyIG／Legacy 保留原本的初始化。仍建立隔離資料、媒體及診斷目錄，不改 `read_only: true` 或資料卷。重新執行本頁第一次執行的 `pull`、`build validate`、`run` 即可；僅重新 `run` 舊映像不會取得修正，不需要清除 volume。

回歸測試以實際範本經 `load_config` 載入，在檔案系統邊界模擬設定掛載的 EROFS，再執行真正的 `Monitor` 初始化；另驗證 AnonyIG／Legacy 仍建立瀏覽器目錄。這補上先前只檢查 YAML 明示路徑、而未測試隱含相對路徑的缺口，不代表已在開發機執行 Ubuntu Docker。

## HTTP 200 後出現個人檔案計數錯誤

`9d6d48c` 及先前版本只接受直接欄位 `media_count`／`follower_count`／`following_count`。2026-09-09 Ubuntu 回報來源 HTTP 200 後，開發機單次讀取 [NASA search 回應](https://igwatcher.com/wp-json/igw/v1/search?username=nasa)，確認貼文數位於 `data.user.edge_owner_to_timeline_media.count`，沒有 `media_count`；追蹤者與追蹤數則同時具有直接欄位與相同數值的巢狀欄位。因此先前的計數錯誤是欄位對應漏項，不是這次回應缺少貼文總數。

修正後接受以下已確認位置，並保留原直接欄位的相容性：

| 計數 | `data.user` 內的直接欄位 | `data.user` 內的巢狀欄位 |
| --- | --- | --- |
| 貼文數 | `media_count` | `edge_owner_to_timeline_media.count` |
| 追蹤者數 | `follower_count` | `edge_followed_by.count` |
| 追蹤數 | `following_count` | `edge_follow.count` |

每項至少一處存在；所有出現的對應欄位都必須是非負整數，且雙處存在時必須相等。真正的 `0` 可接受，缺值、`null`、布林值、浮點數、數字字串、縮寫計數或矛盾數值都會停止，不會任選一個值或補成零。錯誤只附上固定欄位名稱，不輸出來源內容。

先前嚴格探針的 profile 階段只檢查身分與隱私，沒有檢驗計數；因此探針成功未涵蓋這項契約。新增測試使用現場欄位結構、人工身分與計數，重現原錯誤；並涵蓋 profile-only、正常收集及既有資料不被錯誤計數覆蓋。修正後開發機以真正 adapter 單次讀取 NASA 個人檔案已成功；未在這次檢查下載媒體。Ubuntu 請重新執行本頁的 `pull`、`build validate`、`run`，不需要更改設定或清除資料卷。

## 已知契約與未驗證部分

2026-09-09 較早的開發機 profile 補查曾收到 HTTP 403 並停止；後續上述計數診斷與修正驗證收到 HTTP 200。這只證明當次個人檔案回應可解析，不保證匿名來源持續可用。本地離線測試使用人工 HTTP／媒體資料及真實暫存 SQLite，**未在開發機執行 Ubuntu ARM64 Docker 或證明新 adapter 的現場下載成功**。先前 Ubuntu 探針成功只證明當時端點可回資料。

目前 profile 解析明確要求 `data.user` 中字串 `id`、相符 `username`、布林 `is_private`、上表的三項明確計數、字串 `full_name`／`biography`／`profile_pic_url`；`pk` 若存在必須與 `id` 相同。缺欄位或欄位互相矛盾時會明確失敗，不填假 0。

媒體僅允許 HTTPS `cdninstagram.com`、`fbcdn.net` 及其子網域、預設或 443 埠，不跟隨重新導向，不接受任意代理網址。這是保守允許清單，不代表已確認 IGWatcher 所有回應都使用這些主機；其他網址會拒收並保留部分完成狀態。每份 JSON 上限 2 MiB、媒體 100 MiB；JSON 請求至多 20 秒、媒體至多 30 秒（亦受設定的較短期限限制），每類頁數有上限，本範本另有整輪 600 秒外層期限。來源變更契約需要另外查證，不會自動放寬。

請回傳第一次執行的最末段日誌及退出碼即可，不必貼完整建置過程、原始 JSON、媒體網址或憑證。隔離驗證完成後再另行決定是否正式啟用。
