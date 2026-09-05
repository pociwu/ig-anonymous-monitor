# 其他匿名檢視來源的實測

日期：2026-09-05。使用一般瀏覽器流程做少量公開品牌帳號查詢，沒有使用 Instagram 登入憑證。這是單次環境觀測，不代表部署主機長期可用性。

| 來源 | 實測結果 | 對遷移的意義 |
| --- | --- | --- |
| [InstaView：instaview.app](https://instaview.app/) | 搜尋索引列出四類內容，但瀏覽器開啟首頁回報 `ERR_CONNECTION_CLOSED`。 | 目前無法在此環境驗證查詢能力；不能以索引中的功能介紹當成接入成功。 |
| [InstaView：getinstaview.com](https://www.getinstaview.com/) | 搜尋索引仍有產品介紹，實際首頁顯示 `Deployment Paused`。 | 本次不列為可用接入候選。這是與 instaview.app 不同的網域。 |
| [Imginn](https://imginn.com/) | 首頁 Cloudflare 安全檢查自動完成，接著顯示觀看簡短廣告可取得 12 小時存取權的介面。依網站流程觀看廣告並關閉後，搜尋 `nasa` 成功，帳號頁有頭像、簡介、followers/following 及非空貼文。 | 有實際資料，但廣告存取流程與重複安全檢查增加無人值守接入的依賴。四類完整覆蓋尚未驗證。 |

## Imginn 的已觀察欄位與邊界

- [搜尋結果](https://imginn.com/search/nasa/)中存在精確 `@nasa` 項目，連至 [NASA 帳號頁](https://imginn.com/nasa/)。帳號頁呈現 `NASA`、`@nasa`、followers/following 數量與自介。
- 帳號頁的內容導覽為 Posts、Stories、Reels、Tagged；本次頁面未提供獨立 Highlights 分類。
- Posts 出現多筆圖片及影片下載連結、貼文 shortcode 路徑、說明、相對時間與 More 按鈕。沒有下載二進位媒體或嘗試讀完歷史分頁。
- 正常點擊 Stories 時先出現可關閉的插頁廣告，關閉後導向 [Stories 頁](https://imginn.com/stories/nasa/)，再次進入 Cloudflare 安全檢查，隨後顯示必須操作的「驗證您是人類」核取方塊。本次沒有操作驗證，Stories 的實際內容未能確認。單次首頁的自動通過不代表後續請求永遠不需驗證。

網站官方[首頁](https://imginn.com/)也列出匿名 Posts、Stories、Reels 瀏覽功能；上述判斷以本次實際操作為主要證據，沒有將行銷宣稱視為完整 API 契約。
