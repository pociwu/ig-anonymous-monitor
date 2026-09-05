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
cd /srv/ig-monitor/ig-anonyig-validation
git pull --ff-only
docker compose -f compose.browser-probe.yaml build probe
docker compose -f compose.browser-probe.yaml run --rm --no-deps probe
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
`null` 表示沒有可用的可見性檢查結果。它不是「驗證成功」或「必須人工」的判決。
`initial_data` 欄位只記錄是否曾取得兩種資料契約；若後續被阻擋，仍以 `outcome` 為準。
執行摘要另外保留 `headless`、平台／架構白名單與只含數字的瀏覽器版本；
環境失敗以固定 `runtime_stage`／`runtime_kind` 分類，不輸出原始例外或系統路徑。

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

單元測試與選用瀏覽器 QA 使用合成資料。真 Chromium QA 會攔截所有網路請求，
以本地 fixture 回應模擬自動恢復、持續挑戰、早發慢回應與請求上限；不連 AnonyIG 或 Cloudflare。

```powershell
$env:IG_MONITOR_BROWSER_TESTS = '1'
python -m pytest tests/test_browser_probe.py tests/test_browser_probe_deployment.py tests/test_browser_probe_browser.py -ra
```

Windows 開發主機的離線 Chromium 測試不等於 Ubuntu ARM64 + Xvfb 實測。
實際 Docker 啟動與網站是否自行恢復，仍由上述 Ubuntu 一次性測試判定。
