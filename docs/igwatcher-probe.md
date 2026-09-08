# IGWatcher：一次性隔離 HTTP 驗證

## 目的與限制

這個工具在 Ubuntu ARM64 上，以全新程序對固定公開帳號 `nasa` 做有限度匿名 HTTP 查詢，
觀察 Profile、Posts、目前 Stories、Reels、Highlights 清單與一個專輯內文。
它補查本機初步研究尚未核對的 ID、擁有者、時間、媒體 URL 形狀與有限分頁，
不是正式抓取器，也不啟動 AnonyIG 或再次重試先前的挑戰。

IGWatcher 在 Windows 的單次初查曾取得四類非空資料，但不代表 Ubuntu 可用、
所有內容歸屬正確或長期無人值守可靠；背景與欄位缺口見
[2026-09-09 匿名來源重新評估](research/anonymous-source-reassessment-2026-09-09.md)。

即使退出碼為 `0`，也只代表這次有限樣本符合探測條件，**不等於正式來源驗收通過**。
目前 Stories 為空、時間欄位不足或協作貼文擁有者不同，都需要個別判讀，不能因 HTTP 200 就宣稱完整成功。
工具不寫入正式資料，不啟用媒體下載，也不改既有四分類頁面、舊資料或正式來源設定。

## 隔離與請求邊界

- 使用獨立 `compose.igwatcher-probe.yaml`、`ig-monitor-igwatcher-probe` 專案名稱及
  `ig-anonymous-monitor:igwatcher-probe` 映像標籤。**不要與正式或 validation Compose 合併使用。**
- 只有一個 `probe` 服務；沒有資料卷、`.env`、設定檔、登入憑證、服務埠、依賴服務或重啟排程。
  不讀取／重設任何既有來源冷卻資料，不發送 Telegram，不呼叫 Apify 或其他付費來源。
- 容器使用 `pwuser`、唯讀檔案系統及臨時 `/tmp`，結束即移除容器。
  沿用現有 Dockerfile 與既有 `httpx` 依賴；基底雖含 Playwright，**本工具不啟動瀏覽器或 Xvfb**。
  不需要先做 Chromium 啟動測試或設定 Crashpad。
- 固定查詢 NASA；不接受其他帳號、任意 URL、登入、代理或權杖參數。
  請求限定 IGWatcher 的已知 HTTPS 網域與來源端點，回傳欄位不會被當成任意下一頁 URL。
- 不使用 Cookie、登入憑證或代理，不跟隨重新導向，不重試、不解 CAPTCHA，也不下載媒體。
- 最多 **8 個循序請求**：Profile、目前 Stories、Posts 首頁、Reels 首頁、Highlights 清單、
  最多一個專輯內文，以及 Posts／Reels 各最多一次有效 cursor 的續頁。沒有有效續頁依據便不追加請求，
  不遍歷全部專輯，也不查輪播子項詳情。
- 每次請求最多 20 秒，程式總時限 160 秒；要求 `Accept-Encoding: identity`，只接受未壓縮回應，
  不支援的 `Content-Encoding` 會在讀取 body 前停止，不嘗試解壓縮。每次 JSON 回應最多 2 MiB，
  單一集合最多檢查 100 筆，每個輪播最多檢查 20 個子項。
  非 200 HTTP、挑戰、錯誤回應、不合預期 JSON／schema 或不合法 cursor 都停止本輪後續來源請求；
  不以換端點、重新啟動或重試來蓋過失敗。
- 摘要只保留固定分類、布林值、狀態、計數及平台／架構／觀測時間；不輸出原始回應、完整 URL、cursor、ID 值、
  Cookie、標頭、權杖、錯誤訊息文字或媒體檔案。

URL 欄位形狀合格不表示媒體可下載；ID 字串合格不表示已對照 Instagram 原始資料。
兩頁樣本沒有重複也不等於完整分頁；本工具不判定所有貼文／精華皆已取得。

## Ubuntu 執行

使用既有隔離 checkout。以下命令全部使用完整路徑，可從家目錄或 `/srv/ig-monitor` 執行。
先更新：

```bash
git -C /srv/ig-monitor/ig-anonyig-validation pull --ff-only
```

確認更新成功後，重建專用映像。這次新增 Python 模組，**不能只 pull 後沿用舊映像**：

```bash
docker compose -f /srv/ig-monitor/ig-anonyig-validation/compose.igwatcher-probe.yaml build probe
```

確認建置成功後，執行一次，並立即記下退出碼：

```bash
docker compose -f /srv/ig-monitor/ig-anonyig-validation/compose.igwatcher-probe.yaml run --rm --no-deps probe
echo $?
```

請貼回含 `[IGWATCHER-PROBE]` 的完整最後一行與退出碼，不需要貼出整段建置進度。
`echo $?` 必須緊接 `run`，中間不要先執行其他命令。
本工具沒有自動排程或背景重試；先回傳第一輪結果再決定下一步，不連續重跑。

容器入口另設 180 秒 watchdog；收到 TERM 後最多再等 5 秒強制停止。
若沒有摘要而得到 `124`／`137`，外部時限可能已介入；這不是來源通過或內容不存在的判定。
其他沒有摘要的 Compose／啟動錯誤也請一併回傳，不以退出碼推測網站原因。

## 結果判讀

| `outcome` | 意義 | 退出碼 |
| --- | --- | --- |
| `sample_observed` | 本次所需分類樣本均非空，且未偵測到探測範圍內的資料品質問題；不代表可以上線 | `0` |
| `sample_incomplete` | 樣本為空、品質欄位有問題，或分頁資訊不足／cursor 未前進 | `2` |
| `stopped_http` | 收到非 200 HTTP，包括重新導向；沒有跟隨或重試 | `2` |
| `challenge_required` | 回應含已知挑戰訊號，已停止；不嘗試操作驗證 | `2` |
| `stopped_source` | JSON 含來源錯誤、提示或 `success: false`；HTTP 200 也可能是此結果 | `2` |
| `invalid_response`／`response_too_large` | JSON、schema、cursor、編碼或大小不符邊界，已停止 | `2` |
| `identity_mismatch`／`private_profile` | Profile 不是預期 NASA 身分，或標示私人帳號，已停止 | `2` |
| `network_timeout`／`network_error`／`deadline`／`request_budget` | 網路或本輪時限／請求數邊界，未取得足夠結果 | `2` |
| `runtime_error` | 本地執行環境或探測器非預期錯誤；不是來源內容驗收結果 | `3` |

應先看總結果與停止原因，再看已完成端點的計數。前段成功不會抹去後段的阻擋或不完整。
品質計數非零不一定立即終止：若回應契約仍成立，工具可繼續收集後續分類樣本，最後回報 `sample_incomplete`。
相反地，HTTP、挑戰、來源錯誤、schema、cursor 或網路失敗會立即停止後續請求，不以其他分類成功替代。
缺少資料的欄位不得當成 `0`、1970 年或「一定不是自己的內容」；擁有者不一致也不能未經驗證就當協作貼文通過。
本輪目的在找出需要確認的差異，不自行修補來源缺失或把異常媒體寫入 NASA 的資料庫。

### 摘要欄位與品質計數

摘要含 `schema_version`、固定 `source`／`target`、`platform`／`architecture`、UTC `observed_at`、
`elapsed_ms`、`request_count`、`events` 及 `outcome`。`production_ready` **永遠是 `false`**，
不會因一次成功而自行更改正式來源。

每個事件的 `endpoint`／`page` 指出本輪查詢位置；`http_status: null` 表示沒有取得狀態，
`body_state` 區分尚未讀取、JSON、不合法 JSON、過大或不支援編碼。
`state: observed` 只表示取得可檢查的非空資料，**不保證品質通過**；`empty_unverified` 表示空樣本尚未驗證為正常空集合。
Profile 的 `identity_match` 核對 username、ID 與可選 pk；集合事件另外包含 `count` 及 `quality`。
若中途停止，最後一個事件的 `state` 會記錄停止原因，之前的計數仍保留。

| `quality` 欄位 | 判讀界線 |
| --- | --- |
| `invalid_ids`／`numeric_ids` | ID 不符預期字串形狀，或來源以 JSON 數字傳回；數字計數本身不證明已失真，也不能轉字串就當驗證完成 |
| `duplicate_ids` | 同一端點內，含本次續頁，媒體 ID 主體重複；不把 Posts 與 Reels 的合法重疊混算 |
| `owner_suffix_mismatch`／`owner_suffix_unknown` | 媒體 ID 後綴不同於 NASA ID，或無可確認後綴；這是字串一致性檢查，不是作者／協作或專輯歸屬證明，零計數也不是所有權驗收 |
| `timestamp_missing`／`timestamp_invalid`／`timestamp_future` | 發布時間缺少、不合有效秒數範圍，或超過本次檢查時間 5 分鐘；不會補造時間 |
| `expiry_missing`／`expiry_invalid`／`expired_stories` | 目前 Story 的過期欄位缺少、先後／期間不合理，或已過期；Highlights 不是目前 Stories，不套用此過期檢查 |
| `media_url_missing`／`media_url_invalid` | 缺少來源媒體欄位，或不符 HTTPS URL 基本形狀；只有縮圖不算來源媒體，也沒有真的連線下載驗證 |
| `reel_type_unconfirmed` | Reels 未同時標示 `product_type: clips`、影片類型與 `is_video: true`，不能只因是影片就認定分類正確 |
| `media_type_unconfirmed` | Posts／Reels 缺媒體類型、類型值未知，或類型與影片旗標矛盾 |
| `carousel_invalid` | 輪播陣列、宣告數量、子項 ID／重複或子項數上限有問題；仍未對照 Instagram 原始排序 |
| `child_ids_invalid`／`child_owner_mismatch`／`child_owner_unknown` | 輪播子項 ID 缺失／重複，或子項 owner 後綴不符／無法判定；不能只因父貼文符合便接受全部子項 |
| `album_count_mismatch` | 精華專輯宣告數量缺少／不合法，或已測第一個專輯的項目數不符；不能用一個專輯樣本推論全部完整 |

品質計數可以重疊，子項問題也可累加，因此不能將各欄相加當成異常媒體總數。
Posts／Reels 的 `pagination` 為 `available`、`exhausted` 或 `unavailable`；
前兩者是來源提供的布林／cursor 形狀，不是全歷史完整性證明。
第二頁的 `cursor_advanced` 只檢查下一個 cursor 是否不同於輸入（包含來源回報結束），
仍須搭配重複 ID 與品質計數判讀，不是新內容或可靠續頁的單獨證據。

## 開發驗證範圍

2026-09-09 在 Windows 開發過程以本工具做了一輪真實 HTTP 查詢：8 個請求均取得 200，
Stories 2 筆、Posts 與 Reels 各兩頁且每頁 12 筆、Highlights 5 個專輯，第一個專輯內文 9 筆。
結果為 `sample_incomplete`：有 owner 後綴不一致、輪播子項品質與精華宣告數量差異，
不是來源拒絕，也不是資料已驗收。此輪沒有下載媒體、重試或保存原始回應；
後續加嚴的類型與子項檢查使用離線合成回應驗證，沒有為此連續重跑來源。

部署回歸測試靜態檢查 Compose 隔離、環境變數、程序入口、時限、套件包含與操作命令。
探測器測試使用合成回應，不需連線 IGWatcher：

```powershell
python -m pytest tests/test_igwatcher_probe.py tests/test_igwatcher_probe_deployment.py -ra
```

Windows 開發環境沒有 Docker CLI；這些靜態／離線測試**不能代替 Ubuntu ARM64 容器實測**。
操作人員上述一次性執行的結果，才是此次目標部署環境的來源證據。
