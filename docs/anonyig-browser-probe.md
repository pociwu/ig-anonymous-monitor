# AnonyIG 同機瀏覽器對照：一次性隔離測試

## 目的與判讀邊界

2026-09-06 操作人員在 Ubuntu 回報 `postsV2` HTTP 422，且診斷為
`turnstile_required / response_challenge / json`。這確認來源回應含已知 Turnstile 挑戰，
不代表驗證已完成，也不能單憑此紀錄認定 IP、ARM64 或瀏覽器是觸發原因。

本工具只回答：在相同 Ubuntu 主機，以全新瀏覽器工作階段查詢一次公開 NASA 帳號後，
網站是否能在限時內自行取得初始個人資料與貼文回應？

Turnstile 有非互動與隱形模式；可見元件也不一定代表需要人工點擊。
因此工具只觀察回應與元件是否可見，不根據畫面猜測驗證結果。
參考 [Cloudflare 元件模式](https://developers.cloudflare.com/turnstile/concepts/widget/)。

這是操作人員明確批准的獨立診斷，不修改正式抓取器「遇阻擋即停止」的規則。
即使一次取得資料，也不代表媒體作者已逐筆核對、四類內容完整、影片可下載、長期運作可靠或正式來源驗收通過。

## 隔離方式

- 使用獨立 `compose.browser-probe.yaml`、專案名稱及映像標籤；不與正式或 validation Compose 合併。
- 不掛載任何正式／驗證資料卷、設定檔、`.env`、登入憑證或媒體目錄；不對外開啟服務埠。
- 容器使用 `pwuser`、唯讀檔案系統與臨時 `/tmp`；結束後移除容器，不保存 cookies 或瀏覽器工作階段。
  `XDG_CONFIG_HOME=/tmp` 讓 Chromium 的預設設定與 Crashpad 目錄也位於可寫的臨時掛載內。
- 沿用現有 Playwright 1.61 系列與 Docker 基底，預設以 **headed Chromium + Xvfb** 執行。
  Xvfb 是容器內的虛擬螢幕，不需要 Ubuntu 桌面，也不提供人工操作驗證畫面。
  [Playwright 的 Linux headed 執行說明](https://playwright.dev/python/docs/ci#running-headed)。
- 沒有偽造 User-Agent、stealth、代理輪替、驗證服務、權杖注入或持久化登入。
  Service Worker 會停用，讓診斷的請求上限可受控；這仍是受自動化控制的測試瀏覽器，不能等同真人瀏覽器對照。
  locale 與 viewport 沿用原驗證器的 `en-US`、1440 × 1000。
- 只開一次首頁、填一次 `nasa`、提交一次搜尋，之後不捲動、不點分類、不重新整理，也不點擊驗證元件。
- 不直接重播 API。網站 JavaScript 若自行發出後續請求，照樣計數；最多放行 8 個來源 Instagram API 請求，第 9 個攔截並停止。
  收到 HTTP 401／403／429 提早停止；422 則留在限時被動觀察，沒有程式主動重試。
- 不輸出回應原文、完整 URL、查詢參數、標頭、Cookie、`siteKey`、權杖、HAR、截圖或媒體。
  瀏覽器正常載頁可能載入圖片／資源，但工具不建立媒體庫或保存下載檔。

## Ubuntu 執行

先等既有來源冷卻結束，且不要與 `validate` 或其他 AnonyIG 測試同時執行。
這個工具刻意不讀取、重設或刪除既有冷卻資料；不能靠換 Compose 檔來清除冷卻。

在既有隔離 checkout：

```bash
git -C /srv/ig-monitor/ig-anonyig-validation pull --ff-only
```

確認更新成功後重建測試映像；建置成功且冷卻結束後，執行一次探測：

```bash
docker compose -f /srv/ig-monitor/ig-anonyig-validation/compose.browser-probe.yaml build probe
docker compose -f /srv/ig-monitor/ig-anonyig-validation/compose.browser-probe.yaml run --rm --no-deps probe
echo $?
```

預設提交搜尋後觀察 30 秒；即使先取得成功回應，也繼續觀察到時窗結束，避免漏掉稍後的阻擋。
程式含啟動與頁面操作的總時限為 75 秒，容器入口另有 90 秒 watchdog，終止失效時最多再等 5 秒強制結束。
它不會排程、不會自動再跑，也不會接著執行另一個瀏覽器模式。

請回傳包含 `[ANONYIG-BROWSER-PROBE]` 的完整最後一行，以及退出碼：

```bash
echo $?
```

`echo $?` 要緊接在 `docker compose ... run` 後執行，才是那次測試的退出碼。
若沒有標記而回傳 `124`／`137`，代表外部時限已介入，不能當成來源驗證結果；請一併回傳啟動錯誤。

### 2026-09-09 啟動修正紀錄

操作人員在 Ubuntu 22.04 ARM64 的原測試容器回報 `runtime_stage=launch`、
`request_count=0`。同一映像的無導覽啟動檢查進一步重現
`chrome_crashpad_handler: --database is required`、`SIGTRAP`，退出碼為 `1`。
只新增 `-e XDG_CONFIG_HOME=/tmp`，其他設定與啟動命令不變後，回報
`BROWSER_LAUNCH_OK`、退出碼 `0`。這是操作人員提供的同機啟動對照，不是 Windows 測試模擬的結果。

Crashpad 使用預設設定目錄，不直接沿用 Playwright 的 `--user-data-dir` 暫存 profile；
因此只有 profile 位於 `/tmp`，仍不足以讓唯讀容器完成啟動。
參考 [Chromium 崩潰報告路徑](https://raw.githubusercontent.com/chromium/chromium/main/chrome/common/chrome_paths.cc)
與 [Linux 預設設定目錄](https://chromium.googlesource.com/chromium/src/+/main/chrome/common/chrome_paths_linux.cc)。
Compose 現已固定加入這個環境變數，保留 `pwuser`、`read_only` 與原有 `/tmp` tmpfs；
沒有修改 `HOME`、停用 Crashpad、升級瀏覽器或變更正式抓取器。

`0e0e9b8` 的啟動修正只有 Compose 設定需要更新，因此當時已建置的測試映像不需重建。
**後續新增的資源／JavaScript 診斷包含 Python 程式變更，必須依上方命令重建映像。**
以下使用完整路徑，避免重新登入後在家目錄或 `/srv/ig-monitor` 執行而找不到檔案：

```bash
git -C /srv/ig-monitor/ig-anonyig-validation pull --ff-only
```

確認更新成功後，可先檢查新版 Compose 的啟動設定；這段不開啟網站，且不再需要手動傳入 `-e`：

```bash
docker compose -f /srv/ig-monitor/ig-anonyig-validation/compose.browser-probe.yaml run --rm --no-deps -T \
  --entrypoint timeout probe --signal=TERM --kill-after=5s 30s \
  xvfb-run -a python - <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(channel="chromium", headless=False, timeout=15000)
    print("BROWSER_LAUNCH_OK", flush=True)
    browser.close()
PY
echo $?
```

啟動成功不代表來源驗收通過。等既有來源冷卻結束且沒有其他 AnonyIG 測試執行時，
再執行一次完整探測並回傳最後的摘要與退出碼：

```bash
docker compose -f /srv/ig-monitor/ig-anonyig-validation/compose.browser-probe.yaml run --rm --no-deps probe
echo $?
```

## 結果

| `outcome` | 意義 | 退出碼 |
| --- | --- | --- |
| `initial_data` | 本次取得符合既有解析契約的個人資料與初始貼文回應，未觀測到未解決的阻擋 | 0 |
| `recovered_after_challenge` | 曾取得有效 Turnstile 挑戰，在該 HTTP 回應標頭到達後，網站自行發出的同端點新請求成功，且初始資料契約成立 | 0 |
| `challenge_unresolved` | 觀測到挑戰／422，但在本次時窗內未取得充分恢復證據；不等於一定需要人工或永遠不可恢復 | 2 |
| `inconclusive` | 沒有足夠的初始資料契約或恢復證據；HTTP 200 本身不算成功 | 2 |
| `stopped_http` | 收到 HTTP 401／403／429，已停止 | 2 |
| `request_budget` | 網站後續請求觸及本次上限，已停止 | 2 |
| `runtime_error` | 瀏覽器、頁面操作或執行時限等診斷環境失敗；不是來源內容驗收結果 | 3 |

事件順序以**請求開始與阻擋回應標頭到達的關係**判斷，不能把阻擋前已發出的慢速 200 當成之後恢復。
`recovered_after_challenge` 表示觀測到資料恢復，不是直接證明 Cloudflare 內部權杖驗證成功；工具沒有讀取或驗證權杖。
`userInfo` 的成功也不能替代被阻擋的 `postsV2`；其他端點未解決的阻擋不得被蓋過。
`visible_challenge: true` 只表示本次曾看到可見元件，不表示結束時仍可見；
`false` 只表示既有主頁 iframe selector 的首個匹配項未被觀測為可見；後續檢查失敗也可能保留這個值，
不能解讀為「沒有驗證元件」或「不用驗證」。`null` 表示沒有可用的可見性檢查結果。
這些值都不是「驗證成功」或「必須人工」的判決。
`initial_data` 欄位只記錄是否曾取得兩種資料契約；若後續被阻擋，仍以 `outcome` 為準。
執行摘要另外保留 `headless`、平台／架構白名單與只含數字的瀏覽器版本；
環境失敗以固定 `runtime_stage`／`runtime_kind` 分類，不輸出原始例外或系統路徑。

### 資源與 JavaScript 最小診斷

2026-09-09 的同機完整探測已能啟動 Chromium，但 `userInfo`、`postsV2`、`posts`
各回傳 `422 + turnstile`；30 秒內沒有初始資料，結果為 `challenge_unresolved`、退出碼 `2`。
操作人員核准補上以下被動診斷，以區分資源未取得、JavaScript 錯誤或仍未恢復的情況；
這不是已確認根因，也不會解決或操作驗證。

同一個 `[ANONYIG-BROWSER-PROBE]` JSON 現在包含 `diagnostics`：
若更新後摘要仍沒有此欄位，請先確認已重建並執行新版測試映像；不要連續重跑來源查詢。

| 欄位 | 意義與限制 |
| --- | --- |
| `active` | 是否曾成功註冊診斷監聽；正常收尾後仍為 `true`。若為 `false`，零計數不代表沒有錯誤 |
| `collection_error` | 診斷註冊、事件讀取或停止時發生錯誤，資料可能不完整；不覆寫原有來源結果，也不略過瀏覽器清理 |
| `resources.turnstile_script` | 官方 `challenges.cloudflare.com/turnstile/v0/api.js` 載入紀錄，忽略查詢參數 |
| `resources.challenge_resource` | 同一驗證網域下其他 `/turnstile/`、`/cdn-cgi/challenge-platform/` 資源紀錄 |
| `resources.source_script` | AnonyIG 網域及其子網域的 script 資源，不追蹤其他來源的腳本 |
| `js_errors.uncaught` | 整個 context 內頁面的未捕捉 JavaScript 錯誤，僅依標準錯誤名稱計數；未知名稱歸為 `other` |
| `js_errors.console_errors`／`console_warnings` | console error／warning 的數量，不讀訊息文字、參數或位置；不能直接歸因於 Turnstile |
| `turnstile_api_seen` | 主頁是否曾觀測到 `turnstile.render` 是函式；`true` 不等於呼叫過或驗證成功，`false` 表示至少一次檢查成功但從未看到，`null` 表示沒有可用檢查結果 |
| `truncated` | 計數或狀態種類超出上限；此時數字只能當下限，不是完整事件數 |

每個資源分類只聚合 `requested`、`responses`、`finished`、`failed`、`http_statuses`、`failure_kinds`。
計數最多 99，HTTP 狀態保留最多 8 種；網路失敗只分為 `dns`、`tls`、`timeout`、`blocked`、
`aborted`、`connection`、`other`。資源 HTTP 200 或 `finished` 只表示收到回應／下載結束，
不保證腳本成功執行；HTTP 錯誤也可能觸發 `finished`。
參考 [Playwright 請求事件語意](https://playwright.dev/python/docs/api/class-request)
及 [Cloudflare 官方腳本與 API](https://developers.cloudflare.com/turnstile/get-started/client-side-rendering/)。

診斷在導覽前註冊，涵蓋 context 中頁面的資源及錯誤，並在關閉 context 前停止計數，
避免把關閉瀏覽器造成的中斷算成載入失敗。未捕捉錯誤與 console 計數並不涵蓋所有 worker 或被網站自行捕捉的例外。
主頁 API 存在性只取得布林值，每次檢查最多 0.2 秒且受原觀察截止時間限制；
不呼叫 `render`、`ready`、`getResponse`，也不包裝回呼或修改驗證狀態。

若腳本未請求、下載失敗或有 JS 錯誤，這些欄位提供後續排查方向，而非單獨的根因判決。
沒有錯誤紀錄也不能證明驗證成功。`runtime_stage=null`／`runtime_kind=null` 只代表探測流程未拋出執行錯誤，
不代表網站 JavaScript 無錯誤。來源可用性仍以原 `outcome` 與初始資料契約為準。

不保存或輸出資源 URL、查詢參數、訊息文字、stack、標頭、回應內容、cookies、siteKey 或權杖；
不新增網路請求、不改 Service Worker 設定、不延長觀察、不點擊驗證，也不改正式來源的停止規則。

### 需要進一步對照時

先回報第一輪結果，等冷卻結束且確認需要再測，才手動執行 headless 模式；不要連續批次測試。
兩模式使用相同一次查詢與被動觀察規則，避免把原抓取器的立即停止行為和瀏覽器模式混為同一差異。

```bash
docker compose -f compose.browser-probe.yaml run --rm --no-deps probe --headless --observe-seconds 30
```

`--observe-seconds` 只接受 1 至 45 秒。兩模式均固定 `channel="chromium"`，
因此使用同一完整 Chromium；headless 採新模式，不切換成原抓取器預設的 headless shell。
相較於原 `validate`，瀏覽器模式與「被動等待」等條件都不同，不能由一次成功直接歸因。
即使本工具兩種模式結果不同，也仍需同機重現，不能單次推論 IP 被封鎖。
參考 [Playwright 新 headless 模式](https://playwright.dev/python/docs/browsers#chromium-new-headless-mode)。

## 開發驗證

2026-09-06：34 項探測器單元測試、4 項部署契約測試與 8 項真 Chromium 離線案例通過；
包含本工具的全專案回歸共 294 項通過。彈出視窗案例先重現未關閉的錯誤，再驗證整個 context 收尾修正。

2026-09-09：新增 Crashpad 設定的靜態回歸檢查；補入 Compose 設定前，部署契約測試
2 項失敗、3 項通過，補入後 5 項全數通過。含離線 Chromium QA 的全專案回歸共 295 項通過。

同日新增最小資源／JavaScript 診斷：71 項事件邊界測試、6 項新的真 Chromium 全網路攔截案例，
以及接線、布林採樣截止時間與診斷失敗不干擾來源結果的回歸測試通過；全專案共 379 項通過。
舊版在真瀏覽器案例先因缺少 `diagnostics` 欄位失敗，再驗證新增觀測；
診斷 start／stop 故障注入也先重現覆蓋來源結果，再修正為獨立 `collection_error`。
合成腳本的成功下載、網路失敗、HTTP 錯誤、執行錯誤、未請求及隱藏 iframe 錯誤均有覆蓋。
這些案例驗證診斷能力與不干擾性，不模擬或宣稱解決真實 Turnstile。

單元測試與選用瀏覽器 QA 使用合成資料。真 Chromium QA 會攔截所有網路請求，
以本地 fixture 回應模擬自動恢復、持續挑戰、早發慢回應與請求上限；不連 AnonyIG 或 Cloudflare。

```powershell
$env:IG_MONITOR_BROWSER_TESTS = '1'
python -m pytest tests/test_browser_probe.py tests/test_browser_probe_deployment.py tests/test_browser_probe_browser.py tests/test_browser_probe_diagnostics.py tests/test_browser_probe_diagnostics_browser.py -ra
```

Windows 開發主機的離線 Chromium 測試不等於 Ubuntu ARM64 + Xvfb 實測。
本次設定的 Ubuntu ARM64 無導覽啟動已由操作人員確認；完整探測與網站是否自行恢復，
仍須由 Ubuntu 一次性來源測試判定。部署契約測試只鎖定設定與隔離限制，不代替實際 Docker 啟動。
