# 匿名來源遷移與隔離驗證

本次實作新增 AnonyIG 適配器、四類分類狀態、群組媒體關聯與 Dashboard。
正式 `schedule.media_download_enabled` 仍預設為 `false`；程式碼完成不代表來源驗收通過或正式下載已啟用。

## 目前來源限制

- AnonyIG 的普通網頁可回傳 Posts、Stories、Highlights 及專輯內容；媒體必須有可核對的作者資訊才接受。作者不符的項目可能是協作貼文，會保留不完整狀態，不直接認定污染。
- 實測 Reels 是 Posts 影片篩選，並非已確認的獨立 Reels 完整來源。Reels 頁保留來源分類，但持續標示完整性未驗證。
- Posts 分頁透過普通頁面捲動。跨巡檢可重播有界視窗，但直接以保存的來源游標跳回任意歷史頁尚未驗證；視窗不足時回報部分完成，不宣稱完成增量追蹤。
- 本開發主機沒有 Docker CLI；本機 Playwright 成功不等於正式 Docker 或長期無人值守驗收。

上述限制未解除前，不應開啟正式媒體記錄／下載。研究證據與實測結果見 `docs/research/anonyig-adapter-validation.md`。

## 已完成驗證（2026-09-05）

- 全套回歸 **177 項通過**，包含桌面／手機瀏覽器 QA 與驗證腳本跨平台快取路徑測試；這些測試不連線匿名來源或正式帳號。
- 瀏覽器 QA 驗證四類切換、輪播／燈箱、精選專輯、舊版媒體、手機寬度與 JavaScript 錯誤，使用隔離資料庫及人工圖片／影片。示範截圖位於 `.pytest-tmp/anonymous-dashboard/`，不是正式媒體庫。
- 回歸包含相同媒體網址刷新、空基準後增量、作者證據保留、下載端驗證頁阻擋，以及跨工作程序的冷卻／恢復競態。
- 另外完成兩次全新本機 headless 來源查詢及一張記憶體 JPEG 解碼驗證；詳見[來源實測報告](research/anonyig-adapter-validation.md)。未完成 Docker、影片實體下載或長期排程驗證。
- 正式設定、資料庫、下載目錄及執行中的服務均未改動。本次未部署或啟動正式服務。

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
