# 匿名來源遷移與隔離驗證

本次實作新增 AnonyIG 適配器、四類分類狀態、群組媒體關聯與 Dashboard。
正式 `schedule.media_download_enabled` 仍預設為 `false`；程式碼完成不代表來源驗收通過或正式下載已啟用。

## 目前來源限制

- AnonyIG 的普通網頁可回傳 Posts、Stories、Highlights 及專輯內容；媒體必須有可核對的作者資訊才接受。作者不符的項目可能是協作貼文，會保留不完整狀態，不直接認定污染。
- 實測 Reels 是 Posts 影片篩選，並非已確認的獨立 Reels 完整來源。Reels 頁保留來源分類，但持續標示完整性未驗證。
- Posts 分頁透過普通頁面捲動。跨巡檢可重播有界視窗，但直接以保存的來源游標跳回任意歷史頁尚未驗證；視窗不足時回報部分完成，不宣稱完成增量追蹤。
- 本開發主機沒有 Docker CLI；本機 Playwright 成功不等於正式 Docker 或長期無人值守驗收。

上述限制未解除前，不應開啟正式媒體記錄／下載。研究證據與實測結果見 `docs/research/anonyig-adapter-validation.md`。

## 已完成驗證（2026-09-06）

- 全套回歸 **294 項通過**，包含桌面／手機瀏覽器 QA、驗證腳本跨平台快取路徑、阻擋診斷與一次性瀏覽器對照測試；這些測試不連線匿名來源或正式帳號。
- 瀏覽器 QA 驗證四類切換、輪播／燈箱、精選專輯、舊版媒體、手機寬度與 JavaScript 錯誤，使用隔離資料庫及人工圖片／影片。示範截圖位於 `.pytest-tmp/anonymous-dashboard/`，不是正式媒體庫。
- 回歸包含相同媒體網址刷新、空基準後增量、作者證據保留、下載端驗證頁阻擋，以及跨工作程序的冷卻／恢復競態。
- 另外完成兩次全新本機 headless 來源查詢及一張記憶體 JPEG 解碼驗證；詳見[來源實測報告](research/anonyig-adapter-validation.md)。未完成 Docker、影片實體下載或長期排程驗證。
- 正式設定、資料庫、下載目錄及執行中的服務均未改動。本次未部署或啟動正式服務。
- 操作人員後續在 Ubuntu 啟動隔離驗證，於個人檔案階段收到來源阻擋訊息。第一版診斷已確認 `userInfo` HTTP 422；第二版將補充驗證類型與判讀依據，尚待 Ubuntu 重測。正式來源驗收仍未通過。

## 設定與資料續接

`browser.anonymous_source: anonyig` 選擇新來源；`legacy` 只供既有來源相容診斷。
帳號使用 `https://www.instagram.com/<username>/`，舊 `insta-stories-viewer.com` 帳號網址仍可讀取。
新網址用來識別 IG 帳號，不代表直接向 Instagram 登入抓取。

同步時以可確認的既有身分續接，保留帳號 ID、改名、媒體檔案與隔離狀態；遇同名歧義會回滾報錯。
沒有可靠父貼文／專輯資訊的舊媒體留在「舊版媒體」，不以相同文字或時間猜測歸組。
媒體去重只合併檔案，不合併貼文／分類／專輯的不同歸屬及順序。

四類首次範圍為 Posts／Reels 各 12 則、當下可取得的全部 Stories、全部可取得 Highlights 分批內容。
預設每類每輪最多 4 次分頁／專輯工作；未完成時保留狀態及可用續抓資訊。
正式記錄關閉時可更新觀測狀態，但不推進已保存媒體的基準或游標。

## Docker 隔離驗證

在 Ubuntu 已安裝 Docker Engine 與 Compose 外掛的前提下，建議另開測試目錄，不覆蓋正式 checkout 或設定：

```bash
git clone https://github.com/pociwu/ig-anonymous-monitor.git ig-anonyig-validation
cd ig-anonyig-validation
```

下列命令只使用 `compose.validation.yaml`，**不要與正式 `compose.yaml` 合併**。
它使用獨立命名卷、設定檔及 loopback 8889 埠，沒有正式 data/downloads、collector secrets 或 Telegram／Apify 設定。
`validation.example.yaml` 中的 `media_download_enabled: true` 僅作用於這個測試卷，每輪最多下載 8 個樣本檔案。

```bash
docker compose -f compose.validation.yaml build
docker compose -f compose.validation.yaml run --rm --no-deps validate
docker compose -f compose.validation.yaml up -d dashboard
```

測試頁位於 [隔離 Dashboard](http://127.0.0.1:8889/)。未完整分類會讓單次驗證退出碼為 1；應讀取分類原因，不要改成忽略錯誤。
若 Ubuntu 是遠端主機，在自己的電腦另開終端機建立 SSH 通道（替換使用者與主機名稱），再以本機瀏覽器開啟上述網址：

```bash
ssh -N -L 8889:127.0.0.1:8889 <使用者>@<Ubuntu主機>
```

另行執行多輪、記錄開始／結束時間、各分類完整度與實際下載結果，才有連續無人值守證據。
來源冷卻時不發新的來源請求，也不把略過算成新失敗。

停止測試頁但保留測試資料：

```bash
docker compose -f compose.validation.yaml stop dashboard
```

驗收需涵蓋四類作者／識別碼、輪播保序、專輯內容、至少一次真實分頁、實體下載、乾淨工作階段及連續執行。
先排除未通過項目、完成來源驗收，再取得操作人員另行核准，才能修改正式下載開關；不能自動轉用付費來源。

## Ubuntu 阻擋診斷與重測

新版 AnonyIG API／可見驗證頁阻擋訊息會附上 `[ANONYIG-DIAG]` 與固定 JSON 欄位：
`source`、`endpoint`、`http_status`、`block_type`、`observed_at`（UTC）。
第二版新增 `verification_required`、`error_type`、`classification_basis`、`body_state`。
只保留本次工作階段第一個阻擋事件；診斷 JSON 不輸出完整 URL、查詢參數、帳號、請求標頭、Cookie、權杖或回應內容。
已知端點只記名稱；未識別的 API 路徑記為 `unknown_api`。

| `block_type` | 直接觀測到的訊號 |
| --- | --- |
| `http_unauthorized` | HTTP 401 |
| `http_forbidden` | HTTP 403 |
| `http_unprocessable` | HTTP 422 |
| `http_rate_limit` | HTTP 429 |
| `visible_challenge` | 可見驗證元件；`endpoint=page`、`http_status=null` |
| `source_cooldown` | 其他工作程序已建立冷卻；`endpoint=none`、`http_status=null` |
| `unknown_block` | 已標記阻擋但缺少可識別訊號；不推測 HTTP 狀態 |

`block_type` 只描述觀測證據，不把 HTTP 403 直接判定為 IP 封鎖；HTTP 422 本身也沒有跨網站通用的 CAPTCHA 意義。
新增分類使用本次已核對的 AnonyIG 前端契約，並明確標記證據來自何處，詳見[補充來源核對](research/anonyig-adapter-validation.md#阻擋分類的補充來源核對2026-09-05)。
這是有限欄位的操作診斷，不是開啟 HTTP debug／完整封包追蹤。
個人檔案或分類阻擋會在終端機與既有診斷 `.txt` 顯示此摘要，並沿用原 DB 冷卻與一次性通知。
既有冷卻／Telegram 事件不會因更新映像被重設或重送。
捲動、等待或檢查畫面期間若收到阻擋／其他工作程序建立冷卻，會在後續點擊或捲動前重新檢查，並保留原診斷。

新增欄位的判讀方式：

- `verification_required: true` 表示來源流程／可見元件要求人機驗證；`null` 表示尚無判定證據，不等於 `false`。
- `error_type` 只會是 `turnstile_required`、`captcha_required`、`rate_limited`、`unauthorized`、`forbidden` 或 `unclassified`；不輸出來源的自由文字錯誤訊息。
- `classification_basis: response_challenge` 表示回應含已確認的 Turnstile 挑戰結構；`frontend_http_status` 表示依已核對的 AnonyIG HTTP 處理流程判讀，不表示已讀到挑戰原文；另外有 `visible_widget` 與 `none`。
- `body_state` 為 `json`、`unreadable`、`timeout` 或 `not_applicable`。只有首個已收到的 HTTP 阻擋回應會用於分類；最多等待 0.5 秒，不主動增加來源請求，逾時後不反覆等待或改寫已輸出的結果。

422 的前端流程預設為 `captcha_required`；429 為 `rate_limited` 且會進入驗證流程。
若該回應直接提供有效的 Turnstile 挑戰結構，才細分為 `turnstile_required`。
只在記憶體讀取錯誤回應並產生固定分類，不將原文、`siteKey`、權杖、未知錯誤值放進日誌、診斷檔或回應歷史。

在既有的隔離測試 checkout 內更新、重建，再執行一次（不需要刪除測試卷）：

```bash
git pull --ff-only
docker compose -f compose.validation.yaml build
docker compose -f compose.validation.yaml run --rm --no-deps validate
```

若輸出只有「冷卻中／下次允許」，表示尚未發出新的來源請求；等到列出的時間後再執行最後一行。
`validate` 是單次工作，退出後不會自動在冷卻結束時重跑。
請回傳含 `[ANONYIG-DIAG]` 的完整錯誤行；舊的通用錯誤無法事後補出 HTTP 狀態。
本更新補強診斷與收到阻擋後的停止檢查，不代表已解除 Ubuntu 的來源阻擋，不改動正式下載設定。

### 已確認 Turnstile 挑戰後的單次對照

2026-09-06 的 Ubuntu 回報已確認 `postsV2` HTTP 422，且為
`turnstile_required / response_challenge / json`。這是直接挑戰證據，但尚不知道是否能由網站自行恢復。
操作人員已另外核准[同機瀏覽器一次性隔離對照](anonyig-browser-probe.md)：
使用獨立 `compose.browser-probe.yaml`，不登入、不點驗證、不改既有抓取器或冷卻資料。
請先等原冷卻結束，再依該文件執行；不要與本頁的 `validate` 同時跑。

## 回歸測試

```bash
python -m pytest
```

單元測試只使用臨時資料庫與已去除臨時網址／憑證的回應 fixture，不登入 IG、不傳 Telegram，也不啟動正式服務。

包含選用瀏覽器 QA 的 PowerShell 指令（需已安裝 Playwright Chromium）：

```powershell
$env:IG_MONITOR_BROWSER_TESTS = '1'
python -m pytest -ra
```
