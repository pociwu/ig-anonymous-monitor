# 無 IG 登入之機器 API 候選查核

查核日期：2026-09-05（Asia/Taipei）。本次只讀取提供者自己的公開網站、API 文件、OpenAPI 與價目；未註冊帳號、使用既有金鑰、啟動付費請求或購買服務。

## 結論

**HikerAPI 是這次三家查核中，唯一能從公開 API 定義確認 Profile、Posts、目前 Stories、Highlights 清單／內容、Reels 都有明確端點的候選。** 這是文件層級的可行性，尚無本專案的真實媒體樣本與連續巡檢驗證。Scrape Creators 缺乏目前 Stories 的已公布端點；Apify 自家 Instagram Scraper 的 Stories 文件互相不一致，Highlights 內容則未找到支援，均不能直接宣稱完整替代。

「無 IG 登入」是**部署者不用提供 IG 帳號、密碼或登入 cookie**；三者仍需服務商帳號與服務 API key/token。接受匿名替代來源並不自動代表接受付費、建立新外部帳號或修改既有付款設定。

| 候選 | Posts | 目前 Stories | Highlights | Reels | 目前判斷 |
| --- | --- | --- | --- | --- | --- |
| HikerAPI | 已定義 | 已定義 | 清單與單專輯已定義 | 已定義 | 優先進行有額度上限的樣本驗證 |
| Scrape Creators | 已定義 | 官方目錄未列出 | 清單與單專輯已定義 | 已定義，但列明置頂／文案限制 | 可作部分來源；未滿足四類 |
| Apify 自家 `apify/instagram-scraper` | 已定義 | input enum 有 `stories`，README 卻未說明 | 只有專輯數的證據，未見內容模式 | 已定義 | 必須先解決文件差異與 Highlights 缺口 |

上表來源：[HikerAPI OpenAPI](https://api.hikerapi.com/openapi.json)、[Scrape Creators 官方目錄](https://docs.scrapecreators.com/)、[Apify README](https://apify.com/apify/instagram-scraper)、[Apify input schema](https://apify.com/apify/instagram-scraper/input-schema)。

## 負載比較基準

只為比較成本，先採：16 個帳號、每 15 分鐘一輪、每天 96 輪；每帳號每輪 1 次 Profile + Posts／Stories／Highlights 清單／Reels 各 1 次首頁。

- `16 × 96 = 1,536` 帳號巡檢／日。
- `1,536 × 5 = 7,680` HTTP 呼叫／日；30 日為 `230,400` 次。
- 此模型不是已批准的排程演算法，不代表所有內容每輪必查；也不是完整歷史抓取成本。分頁、重試、單一 Highlight 內文、單篇詳情、媒體檔案下載均另外計算。
- 不能把 HTTP 呼叫數、服務商計費單位、回傳媒體數混為一談。以下各家分開處理。

## 1. HikerAPI

### 接入與內容

使用 `https://api.hikerapi.com`，HTTP header `x-access-key`。提供者說明不需客戶的 IG 帳號，IG 工作階段由服務端管理；預設限制為 15 requests/second。這些是提供者的規格說明，並非本次測得的可用率。[接入介紹](https://hikerapi.com/instagram-api)、[工作階段說明](https://hikerapi.com/instagram-private-api)

下表依本次取得的 OpenAPI `1.8.1`。表中的 1 單位是一般 request 費率估算；多倍扣量以 API 的 `x-reqs-cost` 為準，帳戶實際扣量仍須小樣本核對。[OpenAPI 定義](https://api.hikerapi.com/openapi.json)

| 用途 | GET 端點 | 查詢／分頁 | 計費 request 單位／call |
| --- | --- | --- | --- |
| Profile | `/v1/user/by/username`；或 `/v1/user/by/id` | `username`；或 `id` | 1 |
| Posts/feed | `/v1/user/medias/chunk` | `user_id`, `end_cursor`；回傳 `[media_list, next_cursor]` | 1 |
| 目前 Stories | `/v2/user/stories` | `user_id`；此端點未列 cursor | **2** |
| Highlights 清單 | `/v2/user/highlights` | `user_id`, `page_id`，用回傳 `next_page_id` 翻頁 | **2／頁** |
| Highlight 內容 | `/v2/highlight/by/id` | `id`；此端點未列 cursor | 1，額外計算 |
| Reels | `/v2/user/clips` | `user_id`, `page_id`，用回傳 `next_page_id` 翻頁 | 1／頁 |

補充 API 契約事實：

- V1 `User` schema 提供 `pk`、`username`、`is_private`、`media_count`、`follower_count`、`following_count`、簡介與頭像；除 `pk` 外多個欄位可以缺失或為 null，不能把未回傳數字寫成 0。
- V2 profile 原始範例有 `pk`／`id`／`strong_id__`。採字串保存 ID；V2 相關端點有 `safe_int` 可將大整數轉為字串。
- Highlights V2 的 `amount` 已棄用且被忽略，必須使用 `page_id`。V1 Highlights 最多只回首頁，不能當完整專輯清單。
- 使用 `/v2/user/stories/by/username` 或 `/v2/user/highlights/by/username` 會各扣 **3** 單位；先解析並保存 profile ID 可使用上表的 2 單位版本。
- Feed 和 Reels 可能重疊；是否同一媒體以及 owner 是否相符，要以真實回傳 ID／owner 及跨端點樣本驗證。

上述 schema／分頁／扣量來源：[OpenAPI 定義](https://api.hikerapi.com/openapi.json)。

### 價格與模型

公開價目說明：預付、無月費，餘額不過期；`200`、`400`、`403`、`404` 均可能計費，`410 Gone` 與 `50x` 不計費。不能只依 HTTP 2xx 次數估帳。[定價及計費 FAQ](https://hikerapi.com/pricing)

提供者介紹頁列下列費率與儲值門檻；正式採用前須在帳戶實際 Pricing 畫面核對解鎖條件。免費試用標示 100 requests，但註冊頁另註明需 Telegram verification，因此不能假定免驗證即有試用額度。[方案表](https://hikerapi.com/instagram-api)、[註冊條件](https://hikerapi.com/registration)

| 方案 | 每 1,000 計費 request | 介紹頁列儲值起點 | 本基準每日消耗 | 30 日消耗 |
| --- | --- | --- | --- | --- |
| Standard | USD 1.00 | USD 100 | USD 10.752 | USD 322.56 |
| Business | USD 0.69 | USD 299 | USD 7.41888 | USD 222.57 |
| Ultra | USD 0.60 | USD 599 | USD 6.4512 | USD 193.54 |

計算是 `1,536 × (1 + 1 + 2 + 2 + 1) = 10,752` 計費 request／日，30 日 `322,560` 單位；**仍只有 7,680 HTTP 呼叫／日**。上表為消耗價值，不是月繳訂閱費，也未計 Highlight 內文與其他追加工作。若每帳號每輪再打開 1 個 Highlight，以 1 單位／call 估算，額外增加 `1,536` 單位／日，Standard 下約 USD 46.08／30 日。

未決：未使用金鑰呼叫，尚未驗證各類成功空集合、私人／不存在帳號、Stories 目前性與過期時間、原始媒體可下載性、同帳號多頁完整性、每次扣量及長時間穩定性。服務 API 文件存在不能替代這些驗證。

## 2. Scrape Creators

以 `https://api.scrapecreators.com` 與 `x-api-key` 接入。下面各頁的公開契約均標示 1 credit／request，回應帶 `credits_charged`／`credits_remaining`；查詢欄位未要求 IG 密碼或 session cookie。[Profile API](https://docs.scrapecreators.com/v1/instagram/profile/)

| 用途 | GET 端點／欄位 | 已確認限制 |
| --- | --- | --- |
| Profile | `/v1/instagram/profile`, `handle` | 範例含 `data.user.id`, `is_private`, `edge_followed_by.count`, `edge_follow.count`, `edge_owner_to_timeline_media.count`, `highlight_reel_count` |
| Posts | `/v2/instagram/user/posts`, `handle`, `next_max_id` | 包含 Reels；媒體有 `pk`／`id`、發布時間及媒體 URL |
| Reels | `/v1/instagram/user/reels`, `user_id` 或 `handle`, `max_id` | 文件明說不含 pinned reels；此端點文案為空，需另叫單篇詳情取得 |
| Highlights 清單 | `/v1/instagram/user/highlights`, `user_id` 或 `handle` | 回 `highlights[].id/title/owner.id`；公開 query schema 未列 cursor，不能宣稱多頁完整 |
| Highlight 內容 | `/v1/instagram/user/highlight/detail`, `id` | 回 `items[]`、各媒體 ID、發布時間、`media_count`、`media_ids`；每個專輯額外一請求 |
| 目前 Stories | 未找到公開契約 | `Story Highlights` 是已保存專輯，不足以證明能拿目前 24 小時 Stories |

逐項來源：[Profile](https://docs.scrapecreators.com/v1/instagram/profile/)、[Posts](https://docs.scrapecreators.com/v2/instagram/user/posts/)、[Reels](https://docs.scrapecreators.com/v1/instagram/user/reels/)、[Highlights 清單](https://docs.scrapecreators.com/v1/instagram/user/highlights/)、[Highlight 詳情](https://docs.scrapecreators.com/v1/instagram/user/highlight/detail/)、[完整官方目錄](https://docs.scrapecreators.com/)。

目前網站列：100 credits 免費、USD 47／25,000 credits（USD 1.88／千）、USD 497／500,000 credits（精算 USD 0.994／千），預付點數不過期；額外回饋／活動 credits 需完成條件，未納入正常免費額度。[官方價格](https://scrapecreators.com/)

如果只算可查的 Profile + Posts + Reels + Highlights 清單，為 `1,536 × 4 = 6,144` credits／日；兩包價的折算消耗分別約 USD 346.52 與 USD 183.21／30 日，**尚缺目前 Stories，因此不能當四類完整方案報價**。精華內容、Reels 補文案、分頁另計。

部分端點可用 `cache_max_age`，但允許值最短為 `1d`，快取命中才扣 0 credits。這不是 15 分鐘新鮮度保障，也不能預先把所有巡檢算免費；未支持此參數的端點會忽略它。[快取契約](https://docs.scrapecreators.com/caching/)

未決：Stories 是否有未刊登或新服務、Highlights 多頁、錯誤狀態是否扣點，以及所有樣本／連續巡檢表現。沒有這些資料時不選作唯一正式來源。

## 3. Apify 自家 Instagram Scraper

此處只評估 **Maintained by Apify** 的 `apify/instagram-scraper`；不能把商店所有社群 Actor 的能力歸到 Apify 自家 Actor。[Actor 首頁](https://apify.com/apify/instagram-scraper)

- API 使用 Apify account token；Python 介面為 `client.actor('apify/instagram-scraper').call(run_input=...)`，完成後讀 `default_dataset_id`。Token 是 Apify 服務憑證；input 沒有要求 IG cookie。[官方 API 範例](https://apify.com/apify/instagram-scraper/api/python)
- `resultsType` 明確含 `posts`、`reels`、`details` 等；用 `directUrls` 指定帳號、`resultsLimit` 控制回傳數、`onlyPostsNewerThan` 做日期條件。公開 input 未提供供呼叫端續查的 source cursor，抓取頁數由 Actor 內部處理；dataset 分頁不等於 IG 來源 cursor。[輸入契約](https://apify.com/apify/instagram-scraper/input-schema)
- `details` 範例有 `id`、`followersCount`、`followsCount`、`postsCount`、`highlightReelCount`、`private`；媒體結果有 `id`、`shortCode`、`ownerId`。`highlightReelCount` 只證明精華數量，並不等於取得專輯內容。[輸出樣本](https://apify.com/apify/instagram-scraper)
- **文件差異**：最新 input schema 的 enum 包含 `stories`，README 的內容模式列表、URL 對照與輸出範例卻沒有 Stories。未試跑前，只能標記候選能力待驗證。README 與 input 皆未找到 `highlights` 模式。[輸入契約](https://apify.com/apify/instagram-scraper/input-schema)、[README](https://apify.com/apify/instagram-scraper)

此 Actor 按寫入 dataset 的 result 計費，Free／Starter／Scale／Business 分別 USD 2.70／2.30／1.90／1.50 每千筆，Actor 頁明列 platform usage included。不是每 HTTP 呼叫，也不是每查一個帳號固定一點。[Actor 價格](https://apify.com/apify/instagram-scraper/pricing)

平台 Free 每月 USD 5 額度；Starter USD 19／月並含 USD 19 使用額，超出另計，未用額度不遞延。[平台價格與 FAQ](https://apify.com/pricing)

因此 `7,680 calls/day` 不能換算成固定費用。若僅 Profile 每帳號每輪都成功寫一筆，已是 `1,536 results/day`，按無折扣價約 USD 4.15／日；Posts／Reels 實際回傳多筆會追加，Stories／Highlights 仍未確認完整。方案費含抵用額的部分也不能重複加總。

未決：Stories enum 是否為實際可用正式功能、Highlights 內容是否另有自家 Actor、空結果／錯誤是否產生可計費 dataset row、跨次抓取是否可重用來源 cursor。文件明列的 Posts／Reels／Profile 可作補充候選，尚無完整四類替代證據。

## 建議後續驗證

若免費匿名網站不能通過現有環境的真實試抓，先讓使用者看見 **HikerAPI 的憑證取得條件與上述負載費用**，再決定是否使用付費來源；本報告不授權購買或新建外部帳號。

拿到已授權的試用／服務金鑰後，以一個確實有四類內容的公開帳號進行固定小額驗證：對照 owner ID、各媒體 ID、內容種類、發布／過期時間、分頁停止條件、精華專輯與內文數、空集合及 API 失敗區別；記錄帳戶扣量。只有樣本通過，再評估在原本 15 分鐘排程下的查詢頻率與額度上限。
