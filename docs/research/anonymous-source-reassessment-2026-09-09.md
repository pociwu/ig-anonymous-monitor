# 匿名來源重新評估：2026-09-09

查核日期：2026-09-09（Asia/Taipei）。本輪讀取公開頁面、提供者自己的 API 文件與前端程式，並對免費網站做有限次普通匿名 HTTP 查詢；沒有註冊帳號、取得／使用金鑰、呼叫付費資料端點或調整正式設定。

## 判斷基準與本輪結論

依 [ADR 017](../adr/017-require-unattended-anonymous-source.md)，正式來源必須在 Ubuntu ARM64 上可無人值守運作，包含重新啟動與工作階段更新；人工解 CAPTCHA 後保留 cookie 不算通過。四類是 Posts、**目前 Stories**、Highlights 清單與專輯內文、Reels，不能用 Highlights 取代目前 Stories。

AnonyIG 先前桌面試抓成功，並未轉化為目標 Ubuntu 環境的通過證據。使用者最新 `a2f8f06` 測試仍為 `challenge_unresolved`／exit 2、沒有初始資料。此次改查替代來源，不再重跑同參數 AnonyIG 探測。[先前驗證的適用範圍](anonyig-adapter-validation.md)、[隔離探測規格](../anonyig-browser-probe.md)

**下一個免費來源優先探測 IGWatcher，Tracygram 作備選。** 兩者本機普通匿名 HTTP 都取得四類非空資料；IGWatcher 的字串 ID、Story 過期欄位與 Highlight 時間欄位，較接近現有資料模型需要。但仍有 owner／協作貼文、分頁及新鮮度問題，且尚未在 Ubuntu ARM64 驗證，不直接替換正式 adapter。

機器 API 的文件結論如下；「已定義」只表示有公開端點契約，**不是已實測成功或正式選用**。

| 候選 | Posts | 目前 Stories | Highlights 清單／內文 | Reels | 目前位置 |
| --- | --- | --- | --- | --- | --- |
| HikerAPI | 已定義 | 已定義 | 已定義／已定義 | 已定義 | 文件最完整，試用另有驗證條件 |
| RocketAPI（本輪新增） | 已定義 | 已定義 | 已定義／已定義 | 已定義 | 可排入試用；回傳契約與完整性尚待樣本驗證 |
| Scrape Creators | 已定義 | 官方目錄仍未找到 | 已定義／已定義 | 已定義 | 不符合單一來源四類需求 |

依據：[HikerAPI OpenAPI](https://api.hikerapi.com/openapi.json)、[RocketAPI User 目錄](https://docs.rocketapi.io/category/user-%EF%B8%8F/)、[RocketAPI Highlight 內容](https://docs.rocketapi.io/api/instagram/highlight/get_stories/)、[Scrape Creators 目錄](https://docs.scrapecreators.com/)。

## 免費網站來源：先做 Ubuntu 隔離探測

本次免費來源查詢在 **Windows 本機**進行。瀏覽器控制介面回報沒有可用瀏覽器，因此下面全是一般不帶登入憑證的 HTTP 觀察，**沒有 UI／瀏覽器成功證據**。請求沒有提供 Cookie／登入憑證、沒有解 CAPTCHA、沒有重試或下載媒體，也沒有操作正式監控程序。免費資料 GET 共 **13 次：Tracygram 6 次、IGWatcher 6 次、PeekIG 1 次**；首頁與靜態前端檔案的唯讀檢查另計，不算資料 GET。

### Tracygram

從 [Tracygram 自己的 viewer 前端](https://tracygram.com/viewer) 確認六個查詢路徑，使用公開帳號 NASA 各查一次，共 **6 次資料 GET**，均回 HTTP `200`、`success: true`：

| 用途 | 前端使用的路徑 | 本機單次觀察 |
| --- | --- | --- |
| Profile | `/api/user?username=...` | 回傳 username 為 `nasa` |
| 目前 Stories | `/api/stories?username=...` | 2 筆 |
| Posts | `/api/posts?username=...&cursor=...` | 首頁 9 筆 |
| Reels | `/api/reels?username=...&cursor=...` | 首頁 12 筆 |
| Highlights 清單 | `/api/highlights?username=...` | 5 個專輯 |
| Highlight 內文 | `/api/highlight-items?highlight_id=...` | 第一個專輯 `18195781759377100` 回 9 筆；首筆類型為 video |

這比只有首頁四分類標籤更有用，但仍存在影響資料正確性的缺口：

- 首筆 Post 的 `taken_at` 為 `0`，不能當成真的發布時間或寫為 1970 年。
- 首筆 Reel 的 ID 是 JSON 數字；超過 JavaScript 安全整數範圍的媒體 ID，必須從原始回應及跨端點樣本確認是否已失真，不能事後轉成字串就視為修好。
- 首筆 Highlight 內文只有 `id`／`type`／`url`／`thumbnail`，缺發布時間；專輯與項目的 owner 來源關係尚未驗證。
- 只確認 URL 欄位存在，沒有下載任何檔案；尚未驗證 Stories 新鮮度／過期時間、完整分頁、有效空集合、工作階段更新或服務端快取。

Tracygram 保留為備選。若伺服器端點傳出不精確 ID，或無法可靠區分時間／擁有者，HTTP 成功也不算通過。不能僅靠更換 parser 補出不存在的資料。[Tracygram 前端](https://tracygram.com/viewer)

### PeekIG

[官方前端程式](https://peekig.com/js/app.js) 可見 profile／stories／posts／highlights／highlight 查詢路徑；此輪 Reels 能力尚未確認。對 NASA 恰好做一次 profile 查詢，**25 秒逾時，沒有收到 HTTP status**。這只表示當次本機請求沒有完成，不等於整個網站停機，也不是另一輪重試的理由。

### IGWatcher

已讀取 [首頁](https://igwatcher.com/) 與 [官方 shortcode 前端程式](https://igwatcher.com/wp-json/igw/v1/shortcode-js-458.js)，依前端路徑共做 **6 次資料 GET**，各類均回 HTTP `200` 及結構化非空資料：

| 用途 | 普通匿名查詢路徑 | 本機單次觀察 |
| --- | --- | --- |
| Profile | `/wp-json/igw/v1/search?username=nasa` | `data.user.username: nasa`、ID／pk、統計與私人帳號欄位；本輪只記錄 ID 存在，未輸出其值 |
| 目前 Stories | `/wp-json/igw/v1/stories?username=nasa` | 2 筆 |
| Posts | `/wp-json/igw/v1/posts?username=nasa&limit=24` | 12 筆，不因要求 24 而宣稱已取得 24 或全部 |
| Reels | `/api/reels?username=nasa&limit=12` | 12 筆 |
| Highlights 清單 | `/wp-json/igw/v1/highlights?username=nasa` | 5 個專輯 |
| Highlight 內文 | `/api/highlight-items?highlight_id=highlight%3A18195781759377100&user=nasa` | 第一個專輯 9 筆 |

這些是 HTTP／資料形狀觀察，**沒有套用 AnonyIG 的 `contract_valid` 驗證器**。回應中的 `data`／`items` 或 Posts 中的 `reels` 陣列可能只是別名，沒有重複加總為額外媒體。

比 Tracygram 更有用的欄位與尚待確認處：

- 首筆 Story ID 為字串 `3981779955892387138_528817151`，`taken_at: 1788885145`；`expiring_at` 欄位存在，但本輪摘要沒有保留其值，尚不能據此證明目前有效或計算到期時間。
- 首筆 Post ID 為字串 `3967213292204992434_528817151`；有 `shortcode`／`product_type`／`media_type`／`children`／`taken_at_timestamp`。時間欄位的值未記錄，**不能寫成「沒有時間」或「時間已驗證正確」**。
- 首筆 Reel ID 為字串 `3979136276912553964_377359921`，ID 中 owner 後綴與既有 NASA 身分資料 `528817151` 不同。可能與協作貼文有關，但本輪沒有證明；下一階段須按 owner／協作來源驗證，不能靜默歸入 NASA。未另外查詢該帳號。
- 第一個 Highlight ID 為 `highlight:18195781759377100`；首筆內文 ID `3975089864169193671_528817151`，`taken_at: 1788087653`，並有 `image_url`／`thumbnail_url`／`video_url`／`tagged` 欄位。URL 只查存在，沒有下載檔案。
- 與 Tracygram 觀察到首筆 Post 的媒體 ID 主體相同（一站帶 owner 後綴），第一個 Highlight 專輯與首項也相同，但 Reel 的數字表示不同：Tracygram 為 `3979136276912554000`，IGWatcher 為 `3979136276912553964`。這**符合可能的數字精度問題**，但沒有保存兩者 Reel shortcode 作同一內容比對，不能把截斷當成已證實事實，也不能反推兩站一定共用後端。

因此 IGWatcher 優先進入 Ubuntu ARM64 隔離探測；**不是已選定正式來源，也不是已證明可持續無人值守**。後續須從新程序／無既存 session 測試，並檢查完整分頁、有效空集合、四類新鮮度、ID 精度與擁有者關係。本輪免費資料查詢到此結束，不增加重試。[IGWatcher 官方前端](https://igwatcher.com/wp-json/igw/v1/shortcode-js-458.js)

## HikerAPI：重新確認四類，但不是免條件免費來源

提供者明確說明：部署者使用 `x-access-key`，不需提供 IG 憑證，服務端處理 IG 工作階段管理。這支持「把 IG 工作階段交給提供者」的介面設計，但自動更新、可用率仍是提供者說明，沒有本專案的連續巡檢證據。[介面介紹](https://hikerapi.com/instagram-api)、[工作階段說明](https://hikerapi.com/instagram-private-api)

目前公開 OpenAPI 版本仍為 `1.8.1`。最低樣本路徑可以是：

| 內容 | GET 端點 | 需注意 |
| --- | --- | --- |
| Profile | `/v1/user/by/username` | 保存數字 ID 為字串 |
| Posts | `/v1/user/medias/chunk` | `end_cursor` 分頁 |
| 目前 Stories | `/v2/user/stories` | `user_id`；**每 call 扣 2 request 單位** |
| Highlights 清單 | `/v2/user/highlights` | **每 call 扣 2 單位**；使用 `page_id`，`amount` 已忽略 |
| Highlight 內文 | `/v2/highlight/by/id` | 每專輯額外查詢；不要使用已 deprecated 的 V1 |
| Reels | `/v2/user/clips` | `page_id` 分頁 |

Stories／Highlights 的 by-username 版本各扣 3 單位，HTTP 呼叫數不能直接當作計費單位；其餘上表端點沒有同樣的額外 `x-reqs-cost` 標示，仍應核對帳戶實際扣量。[OpenAPI](https://api.hikerapi.com/openapi.json)

### 試用與付款條件

- 介紹頁宣稱 100 requests、無信用卡試用；**當前公開註冊元件明寫試用須 Telegram verification**，且有註冊用 Turnstile 流程。因此不能承諾建立帳號就立即有可用額度。[介紹頁](https://hikerapi.com/instagram-api)、[註冊頁](https://hikerapi.com/registration)、[2026-09-09 註冊元件](https://hikerapi.com/assets/js/Registration-3fQ1JR42.js)
- 註冊元件來自註冊頁載入的 `index-DCb1K9f6.js` 所引用資源；以唯讀 HTTP 取得文字，未執行註冊、讀取 token 或提交驗證。一次性的服務商開戶驗證與「每輪監控需要人工驗證」不同，但是否願意開戶仍由使用者決定。
- 方案介紹表列 Standard USD 1／千、Business USD 0.69／千、Ultra USD 0.60／千，對應 `Top-up from` USD 100／299／599。[官方方案表](https://hikerapi.com/instagram-api)
- Pricing FAQ 說明可儲值任意金額、達餘額門檻後手動選擇 tier，解鎖後費率不因餘額下降而失效。因此上表應理解為**列出的費率解鎖門檻**，不能逕稱整個服務硬性最低付款 USD 100。較低儲值的初始費率、實際最小付款與結帳條件未驗證。[計費 FAQ](https://hikerapi.com/pricing)
- 預付餘額不過期、不是月費；`200`／`400`／`403`／`404` 可能計費，`410`／`50x` 不計費。失敗不能一律當零成本。[計費 FAQ](https://hikerapi.com/pricing)

2026-09-05 的 [成本模型](anonymous-machine-api-candidates.md#負載比較基準) 仍只是比較假設，未獲批准作為正式排程。沒有必要在試用前就購買大額 tier。

## RocketAPI：新發現的完整端點候選

官方文件列出 `https://v1.rocketapi.io/` 與 `Authorization: Token ...`；公開範例只要求服務商金鑰與查詢 ID／username，沒有客戶 IG 登入參數。這是機器介面的證據，不是本專案已驗證工作階段更新的證據。[接入契約](https://docs.rocketapi.io/api/)、[Profile 使用範例](https://docs.rocketapi.io/)

| 內容 | POST 端點 | 官方參數／限制 |
| --- | --- | --- |
| Profile | `/instagram/user/get_web_profile_info` | `username` |
| Posts | `/instagram/user/get_media` | `id`；`count` 最多 12；輸入 `max_id`，從回應 `next_max_id` 續頁 |
| 目前 Stories | `/instagram/user/get_stories` | `ids`，每次最多 4 個 user IDs |
| Highlights 清單 | `/instagram/user/get_highlights` | `id`；此頁沒有列出分頁參數 |
| Highlight 內文 | `/instagram/highlight/get_stories` | `ids`，每次最多 4 個 Highlight IDs |
| Reels | `/instagram/user/get_clips` | `id`；`count` 最多 12；`max_id` 分頁；文件說包含 IGTV |

逐項來源：[Posts](https://docs.rocketapi.io/api/instagram/user/get_media/)、[目前 Stories](https://docs.rocketapi.io/api/instagram/user/get_stories/)、[Highlights 清單](https://docs.rocketapi.io/api/instagram/user/get_highlights/)、[Highlight 內文](https://docs.rocketapi.io/api/instagram/highlight/get_stories/)、[Reels](https://docs.rocketapi.io/api/instagram/user/get_clips/)。

### 試用與價格

官方首頁及註冊頁標示 **100 次免費 API requests、無需信用卡**。註冊頁顯示 email／password 欄位；本輪沒有註冊，無法確認後續 email 驗證、風控或試用是否實際核發，不能保證無其他步驟。[官方價格與試用](https://rocketapi.io/)、[註冊頁](https://rocketapi.io/dashboard/signup/)

正式方案是月費，不是長期免費：Mini **EUR 49／50,000 requests**、Startup EUR 99／100,000、Business EUR 299／500,000、Enterprise EUR 499／1,000,000。官方 FAQ 把 `200` **及 `404`** 列為計費回應；不要因 API overview 簡稱錯誤不計費便假定 `404` 免費。[價目及計費 FAQ](https://rocketapi.io/)、[API overview](https://docs.rocketapi.io/api/)

預設限制 32 requests/second；usage headers 約每分鐘更新，亦有 `/usage`。批次 Stories 雖可同時放 4 個帳號，**本輪尚未核實是否按一次 HTTP 請求、每帳號或其他單位扣量**，不把批次能力直接換算成保證省四倍費用。[接入與用量](https://docs.rocketapi.io/api/)、[Stories 批次上限](https://docs.rocketapi.io/api/instagram/user/get_stories/)

### 尚未通過的關卡

- 本次文字抽取在 response example 區塊只得到 `Loading...`；沒有取得並驗證真實資料 schema、owner ID、Story 過期時間或媒體可下載性。
- Highlights 清單沒有公開 cursor 參數，不代表無限量完整回傳；需用有多個專輯的帳號查核總數與截斷行為。
- Reels 文件包含 IGTV，不能把「影片」一律標為 Reels；需依真實媒體類型／產品欄位確認。
- 尚未驗證私人／不存在帳號、成功空集合、限流、扣量、Ubuntu ARM64 冷啟動與長時間重複巡檢。亦未證實與其他候選的後端故障來源彼此獨立。

因此 RocketAPI 可進入**有額度上限的試用評估**，但目前不應直接換入正式 adapter。

## Scrape Creators：仍缺目前 Stories 契約

重新檢查官方 Instagram 目錄，仍列 Posts、Reels、Story Highlights、Highlights Details，未找到目前 Stories 的公開端點。這是「公開文件未確認」，不是證明提供者永遠不能支援。[官方目錄](https://docs.scrapecreators.com/)

Posts 包括 Reels；獨立 Reels 端點目前明說不含置頂項目且文案需另查詳情。Highlights 清單／內文各有端點，但不能拿已保存的精華冒充目前限時動態。[Posts](https://docs.scrapecreators.com/v2/instagram/user/posts/)、[Reels 限制](https://docs.scrapecreators.com/v1/instagram/user/reels/)、[Highlights 清單](https://docs.scrapecreators.com/v1/instagram/user/highlights/)、[Highlights 內文](https://docs.scrapecreators.com/v1/instagram/user/highlight/detail/)

試用仍標示 100 credits、無信用卡；正式預付包為 USD 47／25,000 credits、USD 497／500,000 credits。獎勵點數有完成條件，不計入保證試用額度。[官方價格](https://scrapecreators.com/)

當前註冊介面顯示 Google／Microsoft 登入；官方 changelog 2026-01-11 記載曾因濫用停用 email/password 登入。此次未建立帳號，不能承諾純 email 註冊。[註冊介面](https://app.scrapecreators.com/)、[官方 changelog](https://scrapecreators.com/changelog)

目前不推薦作為唯一四類來源；若之後接受部分來源或提供者正式公布 Stories 契約，才重新評估。

## 下一步的採用門檻

1. Tracygram／IGWatcher 的本機有限度普通公開帳號初查已完成；下一步先做 **IGWatcher 的 Ubuntu ARM64 新程序／無既存工作階段隔離驗證**，同時補驗 ID、owner、時間、新鮮度及分頁缺口。這些關卡未通過，不改正式 adapter；Tracygram 保留為備選。
2. 機器 API 須先由使用者選定提供者並同意其開戶／試用條件；接受「研究替代來源」不等於授權新帳號、付款、訂閱或使用既有正式金鑰。
3. 已取得授權的試用，先固定單一公開帳號、總呼叫與計費單位上限，分別驗證四類、owner、輪播順序、精華專輯關係、兩頁去重、有效空集合與錯誤區分；不自動補買額度。
4. 單次樣本通過後，再確認容器冷啟動、無人工工作階段更新與連續巡檢；依實際扣量設計各類更新頻率。通過前保持既有 UI、舊資料與正式來源設定不動。

這份報告補充而非覆寫 [2026-09-05 機器 API 查核](anonymous-machine-api-candidates.md)：新增 RocketAPI 與當前註冊／費率門檻細節，沒有把文件能力改寫為實測成功。
