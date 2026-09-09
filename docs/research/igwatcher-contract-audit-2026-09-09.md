# IGWatcher 欄位契約查核：2026-09-09

後續決策：使用者已接受另行實作有明確限制的保守保存模式，見 [ADR 018](../adr/018-conservative-igwatcher-memberships.md) 及 [Ubuntu 隔離驗證](../igwatcher-validation.md)。這不改變下列歷史查核證據，也未把嚴格探針結果升級為完整或正式可用。

查核日期：2026-09-09（Asia/Taipei）。本輪先讀取提供者的公開靜態前端，對照使用者提供的 Ubuntu ARM64 `schema_version: 2` 探測摘要與本地資料路徑，再於 Windows 開發環境做恰好 4 次有上限的 NASA 資料 GET，只觀察欄位形狀。**前端如何顯示資料，不等於後端保證資料完整或歸屬正確。** 依 [ADR 017](../adr/017-require-unattended-anonymous-source.md)，本輪不改正式來源、不放寬驗收、不啟用媒體下載。

## 結論

目前沒有找到可直接修正 parser、就能解除三項阻礙的已證實欄位契約：

| 問題 | 靜態前端實際行為 | 本輪可下的結論 |
| --- | --- | --- |
| 輪播子項缺少 `id` | `postMedia` 將父貼文 ID 複製至每個畫面；輪播 deep-link 用父貼文加投影片位置定位。新樣本的 7 個子項確實只含 5 種媒體／標記欄位。 | 不只是本地漏讀 `id`，這 7 個回傳子項沒有其他 key 可作身分別名；網站仍可依序顯示它們。 |
| Posts／Reels owner 後綴不同 | 一般 Posts／Reels 映射未提供歸屬交叉驗證；`owner` 的使用位於獨立 Tagged 分頁。 | Tagged 的 owner 契約不能借用成一般 Posts／Reels 的協作證據。 |
| 精華宣告 10、實收 9 | `loadHighlightItems` 非空就停止；宣告數量只參與空結果處理，沒有數量相等檢查或續頁游標。 | 網站前端也會顯示這 9 筆；沒有「parser 漏用前端既有續頁」的證據。 |

依據為 [IGWatcher shortcode 前端](https://igwatcher.com/wp-json/igw/v1/shortcode-js-458.js) 與下節限定樣本，精確函式及適用範圍如下。它們沒有證明後端永遠不提供其他欄位，也沒有證明三項異常的上游原因。

## 既有 Ubuntu 證據與本地路徑

使用者此次摘要的 `observed_at` 為 `2026-09-08T22:39:07+00:00`，8 個請求均為 HTTP 200、JSON；結果仍為 `sample_incomplete`、`production_ready: false`、exit 2：

- 兩頁 Posts 共 24 筆，其中 6 個輪播父項、21 個子項；21 個子項都沒有 `id` key，而非 `null`、數字、格式錯誤或重複 ID。
- Posts 的 owner 後綴不符數為 11／24，Reels 為 13／24。兩類可能重疊，不能加總成 24 則不同的錯配媒體。
- 各集合與輪播子項的固定 owner／協作候選欄位均未觀測到；`fields: {}` 不等於所有可能欄位都不存在。
- 第一個精華專輯宣告 10 筆、實得 9 筆；只確認數量差異，不知道是哪一筆、為何不同，或是否受到上游可見性影響。
- 所有診斷的 `collection_error`、`truncated` 都是 false，沒有把診斷失敗或上限截斷誤當成欄位缺失。

本地 `run_probe` 直接取 JSON `data` 陣列，將原始項目及 `children` 交給品質檢查與被動診斷；這條路徑沒有執行網站的 `postMedia` 或移除子項 ID 的前端轉換。因此「本地先轉換後弄丟 `id`」不符合目前程式路徑；但缺少 Ubuntu 原始 payload，不能進一步定位是 IGWatcher 後端、省略欄位的上游，或其他原因。[探測程式](../../ig_monitor/igwatcher_probe.py)、[被動診斷](../../ig_monitor/igwatcher_probe_diagnostics.py)、[欄位定義與 Ubuntu 部署說明](../igwatcher-probe.md)

## 本輪限定回應形狀：恰好 4 次資料 GET

新樣本觀察時間為 `2026-09-08T22:58:20+00:00`，執行位置是 **Windows 開發環境，不是使用者的 Ubuntu 容器**。只查 Posts 首頁、Reels 首頁、Highlights 清單及其第一個已驗證格式的專輯內文，各一次，均 HTTP 200。沒有查第二頁、重試、重新整理快取、帶入 cookie／憑證、跟隨轉址或下載媒體。

本次資料查核限制為單一請求最多 20 秒、整輪最多 80 秒；每份回應最多 2 MiB，只接受未壓縮的 identity 內容；每個陣列最多 100 筆，每個輪播最多 20 個子項。輸出只含固定候選欄位名稱、型別、計數及相等比較，不保存原始 payload。

| 已知資料路徑 | 回應形狀觀察 |
| --- | --- |
| [Posts 首頁](https://igwatcher.com/wp-json/igw/v1/posts?username=nasa&limit=24) | 12 筆；`data`、`items`、`reels` 三個陣列內容完全相等。3 個輪播父項共 7 個子項，子項 key 的聯集只有 `is_video`、`image_url`、`video_url`、`thumbnail_url`、`tagged`，未知 key 計數為 0。 |
| [Reels 首頁](https://igwatcher.com/api/reels?username=nasa&limit=12) | 12 筆；`data`、`items`、`reels` 三個陣列內容完全相等。 |
| [Highlights 清單](https://igwatcher.com/wp-json/igw/v1/highlights?username=nasa) | 5 個專輯；`data` 與 `items` 完全相等。第一專輯宣告 10 筆。 |
| [Highlight 內文路徑](https://igwatcher.com/api/highlight-items) | 只帶前一步第一專輯的 ID 與 NASA username；實得 9 筆，`data` 與 `items` 完全相等。項目 key 聯集只有 `id`、`taken_at`、`image_url`、`video_url`、`thumbnail_url`、`tagged`。這裡不公開實際專輯 ID。 |

陣列相等比較是對完整解析內容進行，不只是比 ID；因此在**這次樣本**中，改讀 `items` 或 `reels` 不會找回額外的輪播欄位或第 10 筆精華，不能把別名陣列重複加總成較完整資料。

Posts／Reels 的已知父項欄位包含媒體 `id`、`shortcode`、類型、`children`、時間、媒體 URL、`tagged`、文案與統計。固定檢查的 owner、author、username 與 coauthor 候選均未出現；但每個 12 筆集合仍有 36 次未分類欄位出現，**不能聲稱所有可能的歸屬資訊都不存在**。這輪沒有檢查 `tagged` 人物內容或回傳任意未知欄位名稱／值。

兩種 feed envelope 均有 `has_more: true` 及 `nextMaxId`；本輪未續頁。兩種 highlight envelope 的已知欄位為 `status`、`code`、`data`、`items`、`total`，各另有 1 個未分類 top-level key。沒有觀測到固定候選 `has_more`、`more_available`、`nextMaxId`、`next_max_id`、`max_id`、`next_cursor`、`end_cursor`、`cursor`、`page_info`、`pagination`；但不能斷言那個未分類 key 絕不含分頁資訊。因此結論是**尚未找到有效續頁契約**，不是「已證明後端永遠沒有續頁」。

10→9 的相同差異在本次 Windows 樣本再次出現，但這不能追溯重現先前 Ubuntu 原始回應，更不能證明差異由快取、刪除或上游權限造成。這輪只保留固定白名單欄位／型別、計數及相等比較摘要，沒有保存原始 payload 或輸出媒體 URL、ID、文案、人物資料。

## 輪播：父項 ID 與畫面順序不是子項 ID

`postMedia`（本次讀取文字第 2029–2047 行）先建立包含父項 `id`、時間、文案等欄位的 `meta`；父項 ID 缺少時才使用父項 `shortcode`。若有 `children`，它逐項讀取 `is_video`／`video_url`／`image_url`／`tagged`，再合併相同的父項 `meta`，**沒有讀取 `c.id`、`c.pk` 或 `c.shortcode` 作為子項識別**。[前端 `postMedia`](https://igwatcher.com/wp-json/igw/v1/shortcode-js-458.js)

deep-link 接收分支（第 570–579 行）先在已載入貼文中找父項 ID，再把 `dl.slide` 傳給輪播 viewer；這是畫面定位，不是後端提供了子項的穩定媒體 ID。`mapReel`、`mapHlItems` 中存在 `pk` fallback，也只是那些映射的前端容錯，不能移植成「輪播子項一定有 `pk`」的保證。[前端 deep-link 與映射](https://igwatcher.com/wp-json/igw/v1/shortcode-js-458.js)

因此不能將父 ID 加索引冒充 Instagram 子項 ID、從可能變動的媒體 URL 猜 ID，或直接忽略這項品質檢查。是否願意另採「父貼文內的有序槽位」資料模型，是不同的設計決策，不是本次查核已授權的 parser 修正。

## 歸屬：Tagged 分頁不能證明協作關係

全檔搜尋 `owner`、`coauthor`、`collaborat` 後，與媒體 owner 物件有關的前端程式出現在 Tagged 點擊／deep-link 分支（第 577–579、1522–1529 行）：讀取 `p.owner` 的 username、full name、profile picture 與 verified，作為該 viewer 的作者顯示。這個分支取得的是獨立 Tagged 內容，而非本次探測的 Posts 或 Reels。[前端 Tagged 與 owner 顯示](https://igwatcher.com/wp-json/igw/v1/shortcode-js-458.js)

一般 Posts 開啟 viewer 的分支沒有相同的 owner 覆寫；`mapReel` 和 `mapReelsList` 也沒有把 owner／協作者傳入 viewer。全檔沒有找到 `coauthor` 或 `collaborat` 字樣。這是對本次讀取的單一前端檔案作出的有限否定，**不是對所有後端欄位或其他網站版本的否定**。[前端 Posts／Reels 映射](https://igwatcher.com/wp-json/igw/v1/shortcode-js-458.js)

`tagged` 被當作被標記的人物清單使用，輪播還會保留每個子項自身的標記。被標記的人不等於共同作者；不能據此自動接受 NASA 出現在另一 owner 的媒體中，也不能因網站畫面顯示 NASA 的頁面標頭就確認所有媒體歸 NASA。

## 精華：宣告數量不是前端完整性門檻

`loadHighlights`（第 1905–2026 行）以 `media_count`、其次 `count` 產生專輯的數量顯示與 `expectN`，讀取一組專輯陣列，沒有使用 Highlight 清單游標。`loadHighlightItems`（第 1866–1891 行）對同一專輯內文路徑取得陣列後，以這個條件停止：

```javascript
if (Array.isArray(items2) && items2.length) break;
```

`expectN` 只在空結果時判斷是否繼續嘗試。該函式沒有將已收到的非空陣列長度與宣告數量比較，也沒有讀取／傳回 `cursor`、`maxId`、`nextMaxId` 或 `has_more` 來補頁。後續 grid 直接對回傳項目逐項顯示。[前端精華載入與顯示](https://igwatcher.com/wp-json/igw/v1/shortcode-js-458.js)

原始碼另有空結果／錯誤時以 `fresh=1` 再查的流程；它是重新查詢同一專輯，不是續頁。**本輪沒有執行這個流程，也不將增加重試視為修復 10→9 的依據。** 前端註解關於快取或上游查詢的說法，不能證明目前差異一定是快取造成。

## 採用判斷與停止點

本輪靜態與限定資料查核已完成；不再重跑同參數 probe，不猜測新端點或追加 `fresh=1`。新的 7 個輪播子項明確沒有其他識別欄位，且改讀別名陣列不會補資料；現有證據不支持以 parser 修正解除品質阻擋。

目前仍應保留 `sample_incomplete`。若要採用 IGWatcher，需要提供者能給出實際存在、穩定且可驗證的子項身分、歸屬與精華完整性契約；或由使用者明確決定是否接受不同資料模型／來源限制。把「父貼文內的有序槽位」改當媒體身分，或接受無法確認的 owner 與精華差異，都屬新的取捨，不在本次查核範圍。另一個來源是否較適合，應另行評估，而不是本輪已自動切換。

## 靜態查核帳本與限制

| 動作 | 次數／結果 | 是否查詢媒體資料 |
| --- | --- | --- |
| web 抽取首頁 | 1 次，取得頁面內容；工具未提供原始 HTTP 狀態碼 | 否 |
| web 抽取已知 shortcode JS | 1 次，抽取器拒絕開啟；未取得來源 HTTP 狀態碼 | 否 |
| 限制環境的一般 HTTP 讀取 JS | 1 次，本機 socket 權限拒絕；未取得來源 HTTP 狀態碼 | 否 |
| 經核准的一般 HTTP 讀取同一 JS | 3 次，均為 HTTP 200；依序為內容讀取、截斷後定位、一次取得全部相關函式 | 否 |
| 限定 NASA 回應形狀查核 | 恰好 4 次資料 GET，均為 HTTP 200；Posts 首頁、Reels 首頁、Highlights 清單、第一專輯內文各 1 次 | 是，只有 JSON 中繼資料 |

靜態查核本身沒有發出資料 GET；另列的 4 次資料查核才是本輪全部媒體資料請求。兩部分均未操作搜尋表單、載入第三方媒體、重試資料端點、猜路由或執行前端程式。第一份完整 JS 工具輸出被截斷，因此用後續限定範圍的文字抽取補足；靜態 HTTP 200 不是判定媒體端點可用的依據。

已知來源 URL：[首頁](https://igwatcher.com/)、[shortcode JavaScript](https://igwatcher.com/wp-json/igw/v1/shortcode-js-458.js)。JS 內版本變數為 `IGW_VER = 426`，與路徑中的 `458` 不同，不能僅靠檔名當版本證據。本次解碼文字為 223,115 字元、2,781 行；其重新編為 UTF-8 的 SHA-256 為 `bd7d94eca24b38a01cbac7cdb1f3e118659662e30a0848d4d218d447a6840e25`。這是**解碼文字指紋，不是原始 HTTP 位元組雜湊**；行號只適用這次內容，來源之後可變動。

本輪未取得後端原始碼、正式資料 API schema、歷史 Ubuntu 原始回應或 Instagram 本身的同一媒體契約；沒有把 UI 容錯／註解推論成後端完整性保證。研究方法只補證據與結論，不修改現有收集器、測試、設定或部署檔。
