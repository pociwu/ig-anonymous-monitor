# AnonyIG 適配器：第一手契約與隔離本機驗證

日期：2026-09-05（Asia/Taipei）。此報告描述本次實際觀察，不把網站宣傳或 DOM 卡片數當成完整收集契約。

## 結論

新適配器已能在全新、無 IG 登入的本機 headless Playwright 工作階段取得結構化帳號資料、貼文、Stories、Highlight 專輯與其內容，保留輪播父子關係，並取得網站 Reels 視圖使用的影片子集。已在記憶體完成一張作者證據符合查詢帳號的圖片下載及解碼驗證。

**尚未通過正式來源驗收。** 本機沒有可呼叫的 Docker CLI，因此以下不是 Docker 驗收；獨立 Reels 完整性、超出有界視窗的跨巡檢直接游標恢復、協作貼文共同作者證據及長期排程可靠性亦未通過。正式媒體新增／下載不得因本報告自動啟用。

## 隔離與執行方式

- `Get-Command docker -ErrorAction SilentlyContinue` 沒有結果。未安裝 Docker，也未假定存在可用的遠端 Docker 環境。
- 執行器：專案 `.pytest-tmp/venv/Scripts/python.exe`，已安裝 Playwright Chromium 1228。
- 每次公開查詢都以新的 headless browser 與 context 執行；沒有重用使用者的桌面瀏覽器 Cookie。只查詢公開品牌帳號 NASA，沒有 IG 登入、解 CAPTCHA、發送 Telegram、啟動正式 monitor、購買 API，或讀取正式 config、`.env`、DB。
- 初次沙箱連線遇到 `ERR_NETWORK_ACCESS_DENIED`；經工具核准公開來源網路存取後成功。此環境限制與來源本身驗證／限流分開紀錄。
- 兩次實際 adapter smoke 都成功回傳相同四類狀態；第二次在 2026-09-05 07:22 UTC 執行，命令耗時約 8.49 秒。這是兩次短期、獨立乾淨 Cookie 的可重現性測試，不是 15 分鐘排程、16 帳號負載、Docker 或長時間穩定性測試。

重現命令（不載入應用設定）：

```powershell
.pytest-tmp/venv/Scripts/python.exe scripts/probe_anonyig.py --adapter-smoke
.pytest-tmp/venv/Scripts/python.exe scripts/probe_anonyig.py --adapter-smoke --download-sample
.pytest-tmp/venv/Scripts/python.exe -m pytest tests/test_anonyig.py -q
```

`--download-sample` 只下載一張已通過作者欄位核對的圖片到記憶體，使用 Pillow 解碼／驗證；它不會寫入正式媒體庫。預設 smoke 的分頁／專輯預算刻意設為 1，僅供小量可重現檢查。

## 第一手來源

- [AnonyIG 入口](https://anonyig.com/en/) 重新導向[實測頁面](https://anonyig.com/en2/)。輸入框 placeholder 是 `@username or link`；使用普通表單 Enter 搜尋及 Posts／Stories／Highlights／Reels 按鈕。
- [公開前端版本](https://anonyig.com/js/app.js?id=500c39611047f0043c00671cf568e665)：Posts V2 請求含 `username`、`maxId`；前端將 `page_info.end_cursor` 作為後續 `maxId`。網站自己完成正常請求簽署，本適配器沒有重製簽署、注入驗證憑證或呼叫未驗證的 API。
- 真實網頁網路回應的路徑是 `/api/v1/instagram/userInfo`、`postsV2`、`stories`、`highlights`、`highlightStories`。程式只接受 AnonyIG 自身或其子網域下的這些結構化回應，不泛掃廣告、DOM 媒體網址或任意 JSON。

前端程式的靜態觀察不是公開受保證的 API；解析器以本次真實回應 fixture 為依據，形狀缺失／矛盾會回報失敗或未完成。

## 真實回應契約

| 類別 | 本次結構及證據 | 本次取得 |
| --- | --- | --- |
| 個人檔案 | `result[0].user`，`id`／`pk`、`username`、明確 `is_private`、整數統計及頭像資源 | 唯一 NASA 帳號，ID 與 PK 一致 |
| Posts | `result.edges[].node`；`id`、`taken_at_timestamp`、`owner.username`、`GraphImage/GraphVideo/GraphSidecar`；`page_info` 終態與游標 | 首頁 12 個邏輯貼文，另一次真實分頁再取得 12 個不同 ID |
| 輪播 | 父貼文 `id`；`edge_sidecar_to_children.edges[].node.id` 及陣列次序；子項目没有獨立 owner 時沿用已核對父貼文證據 | fixture 含 2 張、3 張等真實輪播；不以文案／時間猜測分組 |
| Stories | `result[]`；`pk`、`taken_at`、`user.id`／`username`、`video_versions` 或 `image_versions2.candidates` | 3 個項目 |
| Highlights | `result[]`，各項 `id`、`title`、`user.id`／`username`；點唯一專輯名稱後取得 `highlightStories.result[]` | 5 個專輯；Roman 回應實際 9 個影片，DOM 當時只顯示 6 格 |
| Reels 視圖 | 點 Reels 沒有新 API 請求；公開程式使用 `Qv($v(postsResult))`，追加載入仍呼叫 `getPosts`；本版本沒有 `product_type` 字串 | 初始 12 Posts 中 5 個影片符合畫面 Reels 的 5 格；不是獨立 clips 契約 |

Posts 的 `owner` 在本次 fixture 只有 username，不能宣稱每項都提供作者 ID。Stories／Highlights 的 item 與 album 提供 user ID，會與 profile ID 核對。ID 相同而 username 衝突或其反向矛盾都不接受。

NASA feed 中確實有 `owner.username` 與查詢帳號不同的貼文。這**可能是協作內容，不代表已證實污染**；但本次回應没有共同作者證據，適配器不將它們歸入 NASA，分類保留 partial。這個保守選擇也意味著「原網站看得到」不等於適配器已完整收集。

## 實際 adapter smoke 與下載

設定每分類最多 1 頁／1 個專輯，結果為：

| 分類 | 保存候選數（子媒體） | 狀態 |
| --- | ---: | --- |
| Posts | 10 | partial；存在未證實的共同作者項目，首次範圍未完整完成 |
| Reels | 2 | partial；作者限制與獨立完整性未驗證 |
| Stories | 3 | media，complete=true |
| Highlights | 9 | partial，error=null；已完成 Roman，其他專輯待後續批次 |

兩個分類中的同一影片具有相同 canonical media key，但保留各自 membership。Highlight album ID 與媒體 ID 分開；同一媒體可以屬於不同專輯。

真實下載主機為 `media.anonyig.com`。一張圖片下載並通過 Pillow `verify()`：

- HTTP 回應 Content-Type：`image/jpeg`。
- 大小：192,132 bytes；解碼尺寸：1440 × 810。
- SHA-256：`ea5ce193b0f8fbfc5b91c660a29d8a16a39d309eff383f3d64a79dc0537ae7bd`。
- 不保存其臨時簽署網址，也沒有把圖像落入正式目錄；未聲稱完成影片下載／播放驗證或重啟後檔案持久性測試。

## Fixture 與自動測試

`tests/fixtures/anonyig/` 的 6 份 JSON 來自本次 Playwright 真實網路回應，不是舊報告重建或虛構成功資料。經最小化：只保留解析契約欄位；移除 viewer、關係／旁觀者／貼紙等資訊；作者與媒體 ID 做一致替換；username、文案與 URL 做去識別化；所有媒體 URL 改為保留用 `.test` 網址，不含原始簽署參數。

36 個適配器測試通過，涵蓋明確空集合與失敗區別、帳號與媒體作者矛盾、輪播完整性及順序、9 個 Highlight 子項目、同一 canonical 媒體的多分類／多專輯、真實第二頁、首次 partial 範圍不擴張、增量不因置頂舊貼文提前停止、已完成空基準後出現超過 12 則新貼文不再套用首次上限、被阻擋時保留已取得 feed、各類獨立失敗、全域 source guard 及 profile-only ID。

視窗內已知貼文與首次批次已保存的貼文仍回傳已核對候選，以刷新可能過期的簽署媒體網址；同時保留原有媒體鍵、父貼文 ID、位置和增量邊界，不再計入初次 12 則範圍。回歸測試使用同一個兩張輪播，確認新網址會刷新、順序不變、連續觀察不累加重複項目。超出當前有界視窗的舊候選仍受下述續抓限制約束。

Highlights 分批測試使用明確測試替身：第一輪 5 專輯／預算 4，只記錄 4 個成功專輯 ID；第二輪只抓第五個，完成後清除游標；下一個新巡檢會重新檢查專輯。這個兩輪 fixture 測試不是五專輯全部真實下載驗收。

## 未完成驗收項與實作邊界

1. Docker 不可用；正式部署映像、獨立測試 DB／下載目錄及目標容器瀏覽器尚未實測。
2. Reels 是來源自己對 Posts 的影片篩選。程式明確回報「來源 Reels 是已載貼文影片篩選，獨立完整性未驗證」，不建立 Reels 完整 baseline，不把一般影片類型冒充 IG 權威 product type。
3. 有界 Posts／Reels 視窗透過普通頁面滾動取得。游標保存 baseline 的固定 scope 與已選媒體 ID，避免重複保存或每次再多收 12 則；但沒有經驗證的直接 cursor 跳轉，因此超出設定視窗的續抓尚不能保證前進。遇此情況保持 partial，不假稱完成。因 Reels 始終未通過完整性驗收，其長期 baseline 到增量更新流程也未驗收。
4. 共同作者缺少證據的貼文、重複／無法唯一點擊的 Highlight 專輯名稱，不猜測歸屬；保留失敗／partial 以待後續來源契約補足。
5. 两次本機 fresh headless 成功不等於無驗證長期運行保證。422／429／403 或可見驗證 widget 會停止本輪後續來源請求，並交由 root 的持久化冷卻與告警處理。
6. 尚未完成影片二進位驗證、所有精選完整實測、完整 Docker 排程週期、正式帳號來源品質驗證。這些都是啟用前的真實缺口。

本輪沒有繞過上述邊界、購買另一個來源或啟用正式媒體新增／下載。

## 阻擋分類的補充來源核對（2026-09-05）

操作人員的 Ubuntu 診斷回報 `userInfo` HTTP 422。本輪只重新讀取[同版本公開前端](https://anonyig.com/js/app.js?id=500c39611047f0043c00671cf568e665)，未查詢任何帳號。
取得的 JavaScript 共 787,699 字元，UTF-8 文字 SHA-256：`251a74d292799a0b2fcdd463af561bf83bb80c332ac664cb490e182b84583685`。

一般初始查詢的 HTTP 422／429 分支會呼叫 `showCaptcha`。
`extractTurnstileChallenge` 只接受物件形式的 `response.data.challenge`，且要求 `type` 精確為 `turnstile`、`siteKey` 為非空字串；符合時顯示 Turnstile，否則走一般 CAPTCHA 分支。
來源也使用自由文字 `error_message`，但它不作為本程式可直接輸出的安全白名單。

因此新增分類會分開標記「前端 HTTP 流程推論」與「回應含已知挑戰結構」。前者不是 Ubuntu 該次回應原文的證據；後者也不輸出 `siteKey` 或其他回應值。
目前仍未取得操作人員 Ubuntu 上的實際挑戰分類，亦未完成或繞過任何驗證。

第二版診斷的挑戰物件、異常 JSON、延遲與競態測試均為離線合成輸入，用來驗證上述前端契約、固定白名單與逾時凍結；不是 Ubuntu HTTP 422 原始回應的 fixture。
