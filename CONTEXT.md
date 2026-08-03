# IG Monitor Context

## Identity resolution

**Anonymous profile monitoring**: 不使用 Instagram 登入憑證，透過匿名檢視來源取得公開個人檔案狀態與可見媒體的既有巡檢流程。
_Avoid_: 公開 API 巡檢

**Authenticated Instagram enrichment**: 使用專用 Instagram 登入帳號與持久化工作階段，補充 Anonymous profile monitoring 無法提供的 Instagram 資料；本階段由 `instagrapi` 提供。
_Avoid_: Threads API、官方 Instagram API

**Threads integration**: 未來可選、與 Instagram 巡檢分離的 Threads 平台資料整合；只使用 Meta 官方 Threads API，不使用已封存的非官方 `threads-api` 套件。
_Avoid_: instagrapi threads-api

**Follower membership**: 某個 Instagram 帳號出現在受監控帳號 Followers 名單中的關係；方向為該帳號追蹤受監控帳號。
_Avoid_: 好友

**Following membership**: 某個 Instagram 帳號出現在受監控帳號 Following 名單中的關係；方向為受監控帳號追蹤該帳號。
_Avoid_: 好友

**Mutual follow**: 同一對帳號同時存在 Follower membership 與 Following membership 的雙向關係；共同名單只代表受監控帳號自身的 Mutual follow 集合。
_Avoid_: 共同好友、與登入帳號的共同名單、跨監控帳號交集

**Complete relationship snapshot**: 已成功讀取某個受監控帳號全部 Followers 或全部 Following 分頁的一次名單觀測；只有此種快照能成為下一次移除異動的比較基準。
_Avoid_: 名單快取、部分名單

**Incomplete relationship observation**: 因限流、登入失效、challenge、分頁錯誤或主動停止而未讀完全部分頁的名單觀測；不得覆蓋 Complete relationship snapshot，也不得單憑缺席產生移除事件。
_Avoid_: 空名單、完整快照

**Relationship change**: 兩份具充分證據的關係觀測之間，Follower membership、Following membership 或 Mutual follow 的加入或離開。
_Avoid_: 計數變化

**Instagram collector identity**: 專門供 Authenticated Instagram enrichment 使用的 Instagram 登入帳號；固定綁定單一 OCI 公網 IP、持久裝置指紋與工作階段，不與手機、瀏覽器或其他主機共用。
_Avoid_: 監控帳號、受監控帳號、小帳

**Monitored Instagram account**: 系統觀察其公開檔案、媒體與關係名單的目標 Instagram 帳號；不持有其登入憑證。
_Avoid_: 登入小帳、Instagram collector identity

**Collector observation period**: Instagram collector identity 首次在固定 OCI IP 建立持久工作階段後，至少 72 小時不擷取 Monitored Instagram account 的人工核准階段；期間每 24 小時至多一次工作階段健康檢查。
_Avoid_: 自動養號、模擬真人互動

**Collector active state**: Collector observation period 結束且經操作人員明確核准後，Instagram collector identity 才能進行 Authenticated Instagram enrichment 的狀態。
_Avoid_: 登入成功、養號完成

**Collector risk hold**: 發生 challenge、登入失效、限流或其他風控訊號後，停止所有 authenticated collection 且不得自動反覆登入的狀態；恢復需要人工處理並重新經過 Collector observation period。
_Avoid_: 自動重試、暫時錯誤

**Relationship tracking scope**: 單一 Monitored Instagram account 的 Followers 或 Following 成員數不超過 1,000 時，允許建立 Complete relationship snapshot 的安全擷取範圍；兩個方向分別判定。
_Avoid_: API 分頁上限

**Relationship scope exceeded**: Followers 或 Following 任一方向超過 1,000 人的明確狀態；該方向只記錄總數，不翻完整名單、不產生成員異動，也不視為空名單。
_Avoid_: Incomplete relationship observation、無 followers

**Relationship collection trigger**: Anonymous profile monitoring 確認公開帳號的 Followers 或 Following 總數改變後，為該帳號合併排入一次 Authenticated Instagram enrichment 關係名單工作的事件；私人或公開狀態不明的帳號不會觸發。
_Avoid_: 每 15 分鐘抓名單、即時關係異動

**Relationship reconciliation**: 公開且位於 Relationship tracking scope 的帳號即使總數未變，也每 30 天隨機分散執行一次完整名單比較，以發現加入與離開互相抵銷的 Relationship change。
_Avoid_: 每月固定整點巡檢

**Relationship membership source**: `instagrapi` 登入工作階段回傳的 Followers／Following Instagram ID 與 username；匿名檢視來源不被視為成員名單來源。
_Avoid_: insta-stories-viewer Followers API

**Member profile enrichment**: 以 Relationship membership source 的 username 向匿名檢視來源查詢單一公開成員的詳細個人資料；只因新成員、username 改變或 Dashboard 人工點開而排程，不會遍歷整份名單。
_Avoid_: 名單巡檢、全名單補抓

**Member enrichment budget**: 全系統每個台北時區曆日最多執行 66 次 Member profile enrichment；工作間隔隨機 30 至 90 秒，超額工作保留至後續日期。
_Avoid_: 每帳號每日上限、佇列長度

**Relationship change digest**: 每個 Monitored Instagram account 在一次完整名單比較後產生至多一則 Telegram 彙總；分別呈現 Followers、Following 與 Mutual follow 的加入／離開數量，各分類最多列出 20 個 username，完整內容連結至 Dashboard。
_Avoid_: 每成員通知、基準名單新增通知

**Relationship baseline event**: 首份 Complete relationship snapshot 建立完成的通知；只報告基準規模與完成狀態，不將基準中的既有成員描述為 Relationship change。
_Avoid_: 首次新增名單

**Collector credentials**: 只存在 Ubuntu 主機 `.env` 的 Instagram collector identity username、password 與可選 TOTP secret；不得透過 Dashboard 輸入或保存於設定檔、資料庫、日誌、Telegram 或 GitHub。
_Avoid_: Dashboard 登入資料、session 檔

**Collector session**: 保存於獨立 `collector-secrets` 主機目錄的敏感 Instagram cookies、authorization data、裝置 UUID 與 client settings；只掛載至 Relationship worker，不屬於一般資料庫備份，也不得由其他服務下載或顯示。
_Avoid_: Collector credentials、API token

**Collector administration**: 只能在 Ubuntu CLI 執行的首次登入、challenge 處理、人工核准啟用及解除 Collector risk hold 操作。
_Avoid_: Dashboard 重新登入

**Current relationship state**: 每個 Monitored Instagram account 與名單成員目前是否存在 Follower membership、Following membership 及 Mutual follow 的單一持續更新狀態；不為每次巡檢複製整份名單。
_Avoid_: 關係快照歷史

**Relationship history event**: 保存 365 天的 Follower membership、Following membership 或 Mutual follow 加入／離開紀錄，包含首次與最後觀察時間及產生事件的完整巡檢。
_Avoid_: Telegram 訊息、計數變化

**Relationship run record**: 保存 90 天的一次關係巡檢執行摘要，包含觸發原因、分頁與成員數、完整性、超限狀態、錯誤及開始／結束時間。
_Avoid_: Complete relationship snapshot

**Relationship dashboard**: Monitored Instagram account 詳細頁中的 Followers、Following、共同名單與異動紀錄讀取介面；每頁 50 筆伺服器端分頁，提供搜尋、狀態篩選、成員詳細頁與資料完整性標示，不提供 Instagram 互動操作。
_Avoid_: Instagram 管理介面、完整名單單頁載入

**Collector-fatal signal**: challenge/checkpoint、登入或憑證失效、2FA、feedback block、`PleaseWaitFewMinutes`、429／rate limit、Sentry block、帳號停權或條款要求等會立即進入 Collector risk hold 的訊號。
_Avoid_: 一般擷取失敗、可重試錯誤

**Transient relationship failure**: timeout、DNS／連線中斷、回應截斷、JSON 解碼或 Instagram 5xx 等只使本次名單觀測不完整的失敗；不覆蓋基準、不產生移除，也不立即重試。
_Avoid_: Collector-fatal signal

**Target relationship ineligible**: Monitored Instagram account 不存在、轉為私人或公開狀態不明，因而停止該目標關係工作的狀態；不代表 Instagram collector identity 失效。
_Avoid_: Collector risk hold

**Relationship work budget**: 全系統每個台北時區曆日最多執行 6 個 `instagrapi` 關係名單工作；工作全域單工，相鄰開始時間至少間隔 4 小時，同一目標的多次觸發合併且超額工作跨日保留。
_Avoid_: Member enrichment budget、每帳號上限

**Relationship baseline rollout**: Collector canary 成功後，依 Dashboard 卡片順序且受 Relationship work budget 約束，分批為其餘公開且位於 Relationship tracking scope 的目標建立首份基準；16 個目標至少跨三個台北曆日完成，且不得繞過預算立即全抓。
_Avoid_: 批次初始化、首次異動

**Collector canary**: Collector observation period 完成並人工核准後，以一個人工指定的低量公開帳號進行 7 天關係巡檢驗證的上線閘門；期間任何 Collector-fatal signal 都會進入 risk hold，且其他目標不得開始基準。
_Avoid_: Collector observation period、全量試跑

**Canonical Instagram Profile ID**: Monitored Instagram account 唯一保存的穩定 Instagram ID；可由 `instagrapi` 或 Apify 提供，但兩者必須交叉驗證，來源不同不代表存在多個 ID。
_Avoid_: instagrapi ID、Apify ID

**Identity conflict**: `instagrapi` 與既有 Canonical Instagram Profile ID 回傳不同值的狀態；不得自動覆蓋，必須保存診斷並通知操作人員。
_Avoid_: Username change、ID 更新

**Relationship worker**: 唯一可讀取 Collector credentials 與 Collector session、執行 `instagrapi` 關係工作的單工 Docker 服務；負責 collector 生命週期與 Relationship work budget，不執行 Playwright。
_Avoid_: monitor、Member enrichment worker

**Member enrichment worker**: 不持有 Instagram 登入憑證或 session，只使用 Playwright 與匿名檢視來源執行 Member profile enrichment 並落實 Member enrichment budget 的 Docker 服務。
_Avoid_: Relationship worker、匿名巡檢主程序

**Collector state notification**: Instagram collector identity 進入觀察期、等待核准、Active、risk hold 或重新觀察，以及 Identity conflict 或長時間工作積壓時產生的一次性 Telegram 通知；只含狀態、分類、時間、積壓數與建議 CLI，不含登入身分或 session 資料。
_Avoid_: 每輪健康通知、完整例外通知

**Relationship member profile**: Member profile enrichment 保存的公開個人資料與頭像；不包含 Posts、Stories、Highlights、Reels 或其他媒體下載，也不使該成員成為 Monitored Instagram account。
_Avoid_: 完整監控帳號、成員媒體

**Member promotion**: 操作人員將 Relationship member profile 明確加入正式監控清單的動作；受 16 個 Monitored Instagram account 上限、網址驗證、既有排程與媒體去重規則約束。
_Avoid_: 自動監控、點開成員

**Frozen relationship state**: Monitored Instagram account 轉為私人或公開狀態不明後保留的最後 Complete relationship snapshot；不將成員視為離開，且在重新公開後首次完整比較前不更新基準。
_Avoid_: 空名單、移除全部成員

**Private-interval net change**: 帳號重新公開後，與 Frozen relationship state 比較得出的加入與離開集合；只表示異動發生於兩份完整快照之間，不表示確切事件時間。
_Avoid_: 即時關係異動

**Relationship collection pace**: 單一 Relationship worker 工作以每頁最多 200 人讀取，API 分頁間隨機等待 10 至 20 秒，Followers 與 Following 方向間隨機冷卻 2 至 5 分鐘，全程不平行且每方向最多讀取 1,000 人。
_Avoid_: Relationship work budget、帳號間隔

**Relationship monitoring removal**: Monitored Instagram account 被移除時取消或安全停止其關係與成員補充工作，但保留最後狀態與歷史且不產生全員離開事件；相同 Canonical Instagram Profile ID 重新加入時接回歷史並建立新基準。
_Avoid_: 刪除關係資料、全員取消追蹤

**Authenticated enrichment global switch**: 新版部署後預設關閉且只能由 Ubuntu CLI／設定檔啟用的總閘門；關閉時 Relationship worker 不登入或呼叫 Instagram，並優先於所有帳號級設定。
_Avoid_: Collector active state、Dashboard 開關

**Account relationship tracking switch**: 每個 Monitored Instagram account 是否參與關係工作的獨立設定；預設開啟但受 Authenticated enrichment global switch 與 collector 狀態約束，關閉時保留歷史，再開啟時建立新基準。
_Avoid_: 移除監控帳號、全域 collector 開關

**Directional relationship refresh**: 只有 Followers 或 Following 總數改變時，只重新取得該方向 Complete relationship snapshot，並使用另一方向最近完整基準重算 Mutual follow；首次基準、30 天校正、重新公開與人工診斷才強制更新雙向。
_Avoid_: 每次雙向抓取、部分共同名單

## Authenticated post monitoring

**Post monitoring phase**: 第一階段穩定運作至少 30 天後，才可啟動的公開帳號貼文補充巡檢；它有獨立的 Canary、核准與暫停狀態。_Avoid_: Relationship monitoring、匿名媒體巡檢

**Feed post**: 公開 Instagram 帳號個人頁動態中，以 Media PK 識別的一則照片、影片或輪播貼文；一般動態查詢自然回傳的 Reel 也屬此範圍。_Avoid_: Story、Highlight、專用 Reels 掃描

**Carousel post**: 單一 Feed post 內按固定順序排列的多個照片或影片項目；子項目不是獨立貼文。_Avoid_: 相同時間的多則貼文

**Post baseline**: 一般公開監控帳號首次固定抽取的最近 1 至 6 則 Feed post；抽取數量只決定一次並保存，不代表完整歷史。_Avoid_: Full post backfill

**Full post backfill target**: 全系統唯一獲准在私人轉公開時完整回補 Feed post 的 Monitored Instagram account；資格綁定 Canonical Instagram Profile ID，預設為 `sin_9311`。_Avoid_: 依 username 永久綁定、所有公開帳號完整回補

**Full post backfill**: 對 Full post backfill target 從最新貼文開始、分批且可續傳地巡檢，直到分頁游標耗盡；中途轉私人時保留進度。_Avoid_: 單次無上限下載、歷史 Reels 專用回補

**Post canary**: Collector active 且第一階段穩定至少 30 天後，以 `chaiyi_lili.cos` 進行七天、每日最多一個貼文工作的獨立驗證期。_Avoid_: Collector canary、可略過的試跑

**Authenticated work budget**: Relationship 與 Post 工作共用的全域限制：Asia/Taipei 每日最多六個工作，任兩個工作開始至少相隔四小時。_Avoid_: 各功能各自擁有六個工作額度

**Post work budget**: Authenticated work budget 的子額度，Asia/Taipei 每日最多兩個 Post 工作；Canary 期間最多一個。_Avoid_: 額外於全域六個工作的獨立額度

**Post reconciliation**: 一般公開帳號每 30 天檢視最近六則 Feed post 的低優先工作，用來發現貼文刪除與新增互相抵銷而總數不變的情況。_Avoid_: 完整歷史掃描、每次匿名巡檢都執行

**Post media item**: Feed post 的一個有序媒體項目；單圖或單影片貼文有一個，Carousel post 可有多個。_Avoid_: Post record

**Natural Reel inclusion**: 一般 Feed post 查詢自然回傳 Reel 時接受並分類為 Reels，但不呼叫專用 Reel 端點。_Avoid_: Reels 全量掃描

**Possibly unavailable post**: 曾存在的貼文在一次具權威性的完整掃描中缺席，尚未由至少 24 小時後的另一個完整掃描確認。_Avoid_: 已確認刪除

**Confirmed unavailable post**: Possibly unavailable post 在至少 24 小時後的另一個完整掃描仍缺席；資料與媒體保留，不自動刪除。_Avoid_: Instagram 已證實刪除

**Post download pause**: 磁碟剩餘少於 5 GB 或 10% 時暫停新媒體下載，但仍保存已取得的貼文中繼資料與續傳進度。_Avoid_: Collector risk hold、自動刪檔

## Monitoring dashboard

**Monitoring dashboard endpoint**: 供操作人員檢視巡檢帳號、摘要與排程狀態的 HTTP 服務端點；服務直接監聽 `0.0.0.0:8888`。

**Dashboard access**: 監控儀表板不使用帳密驗證；存取邊界僅由 Tailscale ACL 負責，不額外設定 Ubuntu 主機防火牆規則。

**Dashboard transport**: 監控儀表板目前以 Tainet 網路上的直接 HTTP 提供服務，不經 HTTPS 反向代理。

**Read-only dashboard**: 不會改動帳號設定、巡檢程序或 systemd 的監控儀表板；初版僅呈現帳號摘要、媒體統計與排程狀態，並每 30 秒重新取得資料。

**Dashboard application**: 以 Flask 建立、由獨立 systemd service 執行的 Read-only dashboard。

**Dashboard service**: 名為 `ig-monitor-dashboard.service` 的常駐 systemd service；開機啟動、異常自動重啟，並與巡檢 timer 分離。

**Public project repository**: `pociwu/ig-anonymous-monitor`；對外公開程式碼與部署範本，不包含 `.env`、實際 `config.yaml`、SQLite 狀態或下載媒體。

**Continuous integration**: GitHub Actions 在 `main` 與 Pull Request 使用 Python 3.12 執行 pytest；測試未通過的變更不得合併到 `main`。

## Media identity

**Duplicate media**: 同一 Instagram 帳號內，內容與構圖高度一致、差異主要來自解析度或壓縮的兩筆或多筆照片／影片。裁切、浮水印、文字覆蓋或重新編輯版視為不同媒體；不同 Instagram 帳號的相同內容也不視為重複。

**Canonical media**: 一組 Duplicate media 中畫質最高、被保留作為顯示與檔案來源的那一筆媒體；若後續取得更高畫質版本，Canonical media 可被替換。

**Media quality rank**: Canonical media 的品質排序。照片依像素總數、檔案大小排序；影片依像素總數、位元率、檔案大小排序；完全相同時保留最早下載版本。

**Duplicate record**: 已確認與 Canonical media 相同、但為避免日後重新下載而保留的不可見媒體紀錄；不會再次存檔、發送附件或出現在儀表板。

**Media collection**: 帳號媒體依來源分成 Posts、Stories 與 Highlights；每個來源再分為照片與影片。相同 Canonical media 可保留多個來源關聯，但只保存與顯示一份內容。

**Instagram Profile ID**: Instagram 帳號的穩定識別碼；巡檢程式會為每個帳號保存此值，並在使用者名稱改變後用它查回目前的名稱。

_Avoid_: 帳號名稱、Apify ID

**Instagram username**: 使用者可變更的 Instagram 帳號名稱，也是目前監控網址所使用的識別。

**Identity resolution**: 首次建立帳號資料時保存 Instagram Profile ID；之後僅在一般巡檢無法定位帳號或名稱不一致時，以該 ID 向 Apify 查詢目前 Instagram username 的流程。

**Username change**: Identity resolution 確認 username 改變後，自動切換監控目標至新網址並以 Telegram 回報舊、新名稱的事件。

**Effective monitoring URL**: 資料庫中目前應使用的巡檢網址。它可因 username change 自動更新，並優先於 `config.yaml` 的初始網址。

**Apify monthly usage cycle**: 由 Apify 帳戶 API 回傳的用量週期；所有 5 美元預算判斷均以此週期計算，而非曆月。

**Usage guard**: 呼叫 Apify 前檢查本地已記錄用量與 Apify 帳戶限制的雙重保護；達到 5 美元後禁止新的查詢並通知 Telegram。

**Budget-exhausted notification**: 每一個 Apify monthly usage cycle 最多發送一次的 Telegram 通知；用以告知名稱反查已因 5 美元上限暫停。

**Apify API token**: 只存在部署主機 `.env` 的 `APIFY_API_TOKEN` 機密值；不得寫入 `config.yaml`、資料庫、日誌或 Telegram 通知。
