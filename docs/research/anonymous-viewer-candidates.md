# 匿名 IG 來源候選：AnonyIG、StoriesIG、InstaNavigation

調查日期：2026-09-05（Asia/Taipei）。目的：不提供 IG 登入、不購買訂閱、完全無人值守，取得 Posts／Stories／Highlights／Reels。此輪只對公開品牌帳號 `nasa` 做少量普通搜尋，沒有完成 CAPTCHA、安裝擴充套件、聯絡服務商或改動正式監控。

## 結論

**優先驗證 AnonyIG。** 此輪在同一瀏覽器工作階段，兩個不同分頁都成功查到 NASA；第一個分頁的四類內容均實際出現非空結果，包含一個 Highlight 專輯的內部媒體，未要求 IG 登入或 CAPTCHA。這是可行性證據，尚不是 Docker、全新無 Cookie 工作階段、16 帳號／每 15 分鐘或長期成功率的驗收。[實測入口](https://anonyig.com/en2/)

不能把首頁載入、存在分類按鈕或宣稱免費視為抓取成功。下表的「實測」是瀏覽器 DOM 出現帳號資訊、媒體預覽與下載連結；未擷取原始 JSON，也未下載完整檔案驗證播放、雜湊或內容歸屬。

| 候選 | 本輪確認 | 尚未確認 | 建議 |
| --- | --- | --- | --- |
| AnonyIG | NASA 四類非空；第二個新分頁 NASA 搜尋成功；無 IG 登入或 CAPTCHA 互動 | Docker 可重現性、長期頻率、穩定媒體 ID、完整分頁、媒體實體下載 | 第一順位 |
| StoriesIG | NASA 個人檔案與 Posts 非空；公開程式有四類及分頁流程 | Stories 查詢已送出但未完成終態檢查；Highlights、Reels 與長期可靠性未實測 | 第二順位，勿視為完全獨立備援 |
| InstaNavigation `.com` | Web 讀取失敗，授權後的 shell 仍 DNS 解析失敗 | 全部功能 | 本輪不投入 |

來源：[AnonyIG](https://anonyig.com/en2/)、[StoriesIG](https://storiesig.info/en7jr/)、[InstaNavigation 本輪失敗目標](https://instanavigation.com/)。未將同名但不同網域網站自動當成 `.com` 的官方替代站。

## AnonyIG 實測紀錄

由 `https://anonyig.com/en/` 自動導向 `https://anonyig.com/en2/`，在網站搜尋列輸入 `nasa`。結果顯示 `@nasa`、名稱 NASA、連往 `instagram.com/nasa` 的帳號連結及個人檔案統計。未把個人檔案統計視為獨立驗證過的 IG 真實值。[來源頁面](https://anonyig.com/en2/)

| 類別 | 終態 | 可見證據 |
| --- | --- | --- |
| Posts | 確認非空 | 初始顯示 6 個媒體格，包含圖片與影片，附文案摘要、相對時間及下載連結。兩格同文案，可能來自同一輪播，但沒有原始父貼文 ID，**輪播歸組未驗證**。 |
| Stories | 確認非空 | 切換後顯示 3 格：2 個 `.mp4` 連結及 1 個圖片連結，時間為約 11–12 小時前。 |
| Highlights | 確認專輯及內部內容非空 | 顯示 Roman、Wallpapers、Artemis III、Artemis II、MoonTunes 共 5 個專輯；點 Roman 後顯示 6 個影片媒體格。只測此一專輯。 |
| Reels | 確認非空 | 顯示 5 項文案／時間；首項有實際預覽及下載連結，其餘尚受懶載入影響。沒有據此宣稱 5 項檔案均可下載。 |

以上均為 [AnonyIG 搜尋結果](https://anonyig.com/en2/) 的本輪 DOM 實測。Posts 與 Reels 出現同一影片，實作需保留多分類關聯，不能將下載 URL 當作永久分類身分。

第一次切 Stories 曾顯示 Google 全頁廣告；點一般的關閉廣告按鈕後正常顯示 Stories，未涉及驗證。網站廣告本身因此是瀏覽器互動需要處理的因素。第二個分頁重新由首頁輸入 `nasa`，再次取得帳號資訊及 6 個 Posts 格；此測試共用瀏覽器狀態，**不是獨立乾淨 Cookie／IP 的驗證**。[實測頁面](https://anonyig.com/en2/)

### 公開前端流程與限制

檢查版本：[AnonyIG 公開 app.js](https://anonyig.com/js/app.js?id=500c39611047f0043c00671cf568e665)。以下是程式靜態觀察，未將其當成穩定公開 API 契約：

- **確認**：有 profile、userInfo、stories、highlights、highlightStories、postsV2／posts 的請求邏輯，集中到 `/api/v1/instagram/`；主機可由 `workerHubDomain` 改寫。存在前端簽署請求掛鉤，不能由這些路徑推定直接無狀態 HTTP 能成功。
- **確認**：Highlight 清單使用使用者識別值，專輯內容使用專輯識別值；前端有使用者 ID、使用者名稱及專輯 ID 的概念。**未驗證**：實際回應中每筆媒體的永久 ID、作者 ID 與輪播父子 ID 是否完整、可靠、一致。
- **確認**：Posts／Reels 的追加載入使用 `page_info.has_next_page`、`end_cursor`；Reels 路徑還有 Posts 結果篩選。**未驗證**：後續頁能否成功、只存在 Reels 分頁而未出現在 Posts 的影片是否完整覆蓋，以及最大回溯量。
- **確認**：HTTP 422／429 分支會顯示 CAPTCHA 或 Turnstile；挑戰型別由回應決定。初始狀態不直接顯示挑戰，符合本輪無驗證成功的結果。這表示驗證可能依流量／環境觸發，不能保證永久免驗證。
- **確認**：介面會檢查回應的失敗旗標，HTTP 200 本身不等於觀察成功。下載連結帶有簽章與到期參數，**不宜當作永久去重鍵**。

上列觀察均來自[同一公開前端版本](https://anonyig.com/js/app.js?id=500c39611047f0043c00671cf568e665)。若來源要求互動，建議該輪記錄驗證／限流失敗、保留既有資料並結束；此為符合無人值守要求的設計建議，不是供應商保證。

AnonyIG 有[官方 API 介紹](https://anonyig.com/en/api/)，列出公開帳號及四類內容，但只提供聯絡取得存取方式，沒有公開免費額度、認證規格或服務保證。本輪沒有取得 API key，也沒有採購或聯絡。

## StoriesIG

首頁 `https://storiesig.info/en/` 自動導向 `https://storiesig.info/en7jr/`。普通 NASA 搜尋成功顯示帳號資訊與 6 個 Posts 格，內容順序及帳號統計與 AnonyIG 本輪相同，媒體下載主機則為 StoriesIG 自身。[實測頁面](https://storiesig.info/en7jr/)

其[公開 app.js](https://storiesig.info/js/app.js?id=8a971a7e7f10dce5d7b740e871e59d8d) 也有四分類、Highlight 專輯、相同形狀的分頁與 WorkerHub 呼叫，以及 422／429 導向 CAPTCHA／Turnstile 的處理。**推論**：兩站前端／依賴高度相似，故不宜先假定互為獨立故障域；本輪未證實兩站公司或後端身分相同。

Stories 查詢已點擊但未等到終態，四類全面驗證集中在 AnonyIG。頁面另出現安裝外部 helper 的廣告，未點選下載或安裝。[實測頁面](https://storiesig.info/en7jr/)

[官方 API 頁](https://storiesig.info/en/api-faq/) 同樣只引導聯絡服務商，沒有本輪可直接驗證的免費機器存取契約。

## 下一步驗收建議

建議 root 以 AnonyIG 作為首選候選，進入實作前的最小驗收：在目標 Docker／瀏覽器環境取得一組 NASA 原始回應 fixture，核對作者 ID、媒體 ID、輪播父子關係及 Highlight album ID，再測試一次分頁與實際媒體下載。僅在此驗收完成後，才能將「可在桌面普通查詢」提升為「符合本專案的自動收集來源」。

完整性、429／422、逾時與明確空資料應分開記錄；失敗不得清除舊資料，也不得據此宣布帳號沒有內容。16 帳號／每 15 分鐘的排程負載本輪沒有測試，需採用保守上限與退避，而非依首頁的無限制宣稱設定。

供後續檢查的結果分頁：IAB browser ID `1`、tab ID `1`，URL `https://anonyig.com/en2/`，目前位於 NASA Reels 結果，已標記 handoff 保留。此編號只供本次瀏覽器工作階段使用；重新載入前應先確認狀態。第二次成功搜尋位於同一 browser 的 tab ID `3`，未視為新認證環境。
