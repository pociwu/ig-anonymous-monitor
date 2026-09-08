# IGWatcher：一次性隔離 HTTP 驗證

## 目的與限制

這個工具在 Ubuntu ARM64 上，以全新程序對固定公開帳號 `nasa` 做有限度匿名 HTTP 查詢，
觀察 Profile、Posts、目前 Stories、Reels、Highlights 清單與一個專輯內文。
它補查本機初步研究尚未核對的 ID、擁有者、時間、媒體 URL 形狀與有限分頁，
不是正式抓取器，也不啟動 AnonyIG 或再次重試先前的挑戰。

IGWatcher 在 Windows 的單次初查曾取得四類非空資料，但不代表 Ubuntu 可用、
所有內容歸屬正確或長期無人值守可靠；背景與欄位缺口見
[2026-09-09 匿名來源重新評估](research/anonymous-source-reassessment-2026-09-09.md)。

即使退出碼為 `0`，也只代表這次有限樣本符合探測條件，**不等於正式來源驗收通過**。
目前 Stories 為空、時間欄位不足或協作貼文擁有者不同，都需要個別判讀，不能因 HTTP 200 就宣稱完整成功。
工具不寫入正式資料，不啟用媒體下載，也不改既有四分類頁面、舊資料或正式來源設定。

### Ubuntu ARM64 首輪回報

操作人員在 Ubuntu ARM64 的一次全新容器執行中，8 個請求均回 HTTP 200，
耗時 14.028 秒，取得 Stories 2 筆、Posts 兩頁合計 24 筆、Reels 兩頁合計 24 筆、
Highlights 5 個專輯，以及第一個專輯的 9 筆內文。
這提供了目標主機單次匿名取得四類樣本的證據，**不代表長期穩定、全歷史完整或可以啟用正式來源**。
結果仍是 `sample_incomplete`、`production_ready: false`。

首輪的品質計數需要區分：

- Posts 的 owner 後綴不一致合計 11 次，Reels 合計 13 次；兩分類可能有同一媒體，
  不能相加宣稱有 24 筆不同的異常內容，也沒有證據據此確認為協作貼文。
- 輪播相關的 21 次異常涉及子項 ID 問題；這是問題計數，**不是 21 篇父貼文都有問題**。
  同一問題也可能同時記入輪播及子項 ID 品質欄位，不能再重複相加。
  原摘要尚不足以拆分「缺少 ID、數字 ID、不合形狀或重複 ID」及受影響父貼文數。
- 第一個精華專輯的 `album_count_mismatch: 1` 表示發現一次數量不一致，
  **不是少一筆內文**。原摘要沒有輸出宣告數與實收數的差距，不能猜測缺了多少。

因此後續版本只增加這三類的被動診斷；既有品質門檻、請求數與停止規則不變，
不因 HTTP 全部成功便略過 owner、輪播或專輯數量問題。

## 隔離與請求邊界

- 使用獨立 `compose.igwatcher-probe.yaml`、`ig-monitor-igwatcher-probe` 專案名稱及
  `ig-anonymous-monitor:igwatcher-probe` 映像標籤。**不要與正式或 validation Compose 合併使用。**
- 只有一個 `probe` 服務；沒有資料卷、`.env`、設定檔、登入憑證、服務埠、依賴服務或重啟排程。
  不讀取／重設任何既有來源冷卻資料，不發送 Telegram，不呼叫 Apify 或其他付費來源。
- 容器使用 `pwuser`、唯讀檔案系統及臨時 `/tmp`，結束即移除容器。
  沿用現有 Dockerfile 與既有 `httpx` 依賴；基底雖含 Playwright，**本工具不啟動瀏覽器或 Xvfb**。
  不需要先做 Chromium 啟動測試或設定 Crashpad。
- 固定查詢 NASA；不接受其他帳號、任意 URL、登入、代理或權杖參數。
  請求限定 IGWatcher 的已知 HTTPS 網域與來源端點，回傳欄位不會被當成任意下一頁 URL。
- 不使用 Cookie、登入憑證或代理，不跟隨重新導向，不重試、不解 CAPTCHA，也不下載媒體。
- 最多 **8 個循序請求**：Profile、目前 Stories、Posts 首頁、Reels 首頁、Highlights 清單、
  最多一個專輯內文，以及 Posts／Reels 各最多一次有效 cursor 的續頁。沒有有效續頁依據便不追加請求，
  不遍歷全部專輯，也不查輪播子項詳情。
- 每次請求最多 20 秒，程式總時限 160 秒；要求 `Accept-Encoding: identity`，只接受未壓縮回應，
  不支援的 `Content-Encoding` 會在讀取 body 前停止，不嘗試解壓縮。每次 JSON 回應最多 2 MiB，
  單一集合最多檢查 100 筆，每個輪播最多檢查 20 個子項。
  非 200 HTTP、挑戰、錯誤回應、不合預期 JSON／schema 或不合法 cursor 都停止本輪後續來源請求；
  不以換端點、重新啟動或重試來蓋過失敗。
- 摘要只保留固定分類、布林值、狀態、計數及平台／架構／觀測時間；不輸出原始回應、完整 URL、cursor、ID 值、
  Cookie、標頭、權杖、錯誤訊息文字或媒體檔案。

URL 欄位形狀合格不表示媒體可下載；ID 字串合格不表示已對照 Instagram 原始資料。
兩頁樣本沒有重複也不等於完整分頁；本工具不判定所有貼文／精華皆已取得。

## Ubuntu 執行

使用既有隔離 checkout。以下命令全部使用完整路徑，可從家目錄或 `/srv/ig-monitor` 執行。
先更新：

```bash
git -C /srv/ig-monitor/ig-anonyig-validation pull --ff-only
```

確認更新成功後，重建專用映像。`schema_version: 2` 的診斷新增 Python 輔助模組，
**不能只 pull 後沿用舊映像**；Compose 隔離與命令不需另加參數：

```bash
docker compose -f /srv/ig-monitor/ig-anonyig-validation/compose.igwatcher-probe.yaml build probe
```

確認建置成功後，執行一次，並立即記下退出碼：

```bash
docker compose -f /srv/ig-monitor/ig-anonyig-validation/compose.igwatcher-probe.yaml run --rm --no-deps probe
echo $?
```

請貼回含 `[IGWATCHER-PROBE]` 的完整最後一行與退出碼，不需要貼出整段建置進度。
`echo $?` 必須緊接 `run`，中間不要先執行其他命令。
本工具沒有自動排程或背景重試；先回傳第一輪結果再決定下一步，不連續重跑。

容器入口另設 180 秒 watchdog；收到 TERM 後最多再等 5 秒強制停止。
若沒有摘要而得到 `124`／`137`，外部時限可能已介入；這不是來源通過或內容不存在的判定。
其他沒有摘要的 Compose／啟動錯誤也請一併回傳，不以退出碼推測網站原因。

## 結果判讀

| `outcome` | 意義 | 退出碼 |
| --- | --- | --- |
| `sample_observed` | 本次所需分類樣本均非空，且未偵測到探測範圍內的資料品質問題；不代表可以上線 | `0` |
| `sample_incomplete` | 樣本為空、品質欄位有問題，或分頁資訊不足／cursor 未前進 | `2` |
| `stopped_http` | 收到非 200 HTTP，包括重新導向；沒有跟隨或重試 | `2` |
| `challenge_required` | 回應含已知挑戰訊號，已停止；不嘗試操作驗證 | `2` |
| `stopped_source` | JSON 含來源錯誤、提示或 `success: false`；HTTP 200 也可能是此結果 | `2` |
| `invalid_response`／`response_too_large` | JSON、schema、cursor、編碼或大小不符邊界，已停止 | `2` |
| `identity_mismatch`／`private_profile` | Profile 不是預期 NASA 身分，或標示私人帳號，已停止 | `2` |
| `network_timeout`／`network_error`／`deadline`／`request_budget` | 網路或本輪時限／請求數邊界，未取得足夠結果 | `2` |
| `runtime_error` | 本地執行環境或探測器非預期錯誤；不是來源內容驗收結果 | `3` |

應先看總結果與停止原因，再看已完成端點的計數。前段成功不會抹去後段的阻擋或不完整。
品質計數非零不一定立即終止：若回應契約仍成立，工具可繼續收集後續分類樣本，最後回報 `sample_incomplete`。
相反地，HTTP、挑戰、來源錯誤、schema、cursor 或網路失敗會立即停止後續請求，不以其他分類成功替代。
缺少資料的欄位不得當成 `0`、1970 年或「一定不是自己的內容」；擁有者不一致也不能未經驗證就當協作貼文通過。
本輪目的在找出需要確認的差異，不自行修補來源缺失或把異常媒體寫入 NASA 的資料庫。

### 摘要欄位與品質計數

摘要含 `schema_version`、固定 `source`／`target`、`platform`／`architecture`、UTC `observed_at`、
`elapsed_ms`、`request_count`、`events` 及 `outcome`。`production_ready` **永遠是 `false`**，
不會因一次成功而自行更改正式來源。

新版摘要使用 `schema_version: 2`，在集合事件補入最小 `diagnostics`，
分開呈現輪播子項 ID 原因、受影響父貼文數、owner 欄位形狀／一致性，以及精華數量關係。
只從同一次既有回應計算，不新增請求，也不回傳外部 owner 值、帳號名稱或原始欄位內容。
若更新後仍見 `schema_version: 1`，請先確認已重建並執行新版映像，不要連續重跑來源查詢。

每個事件的 `endpoint`／`page` 指出本輪查詢位置；`http_status: null` 表示沒有取得狀態，
`body_state` 區分尚未讀取、JSON、不合法 JSON、過大或不支援編碼。
`state: observed` 只表示取得可檢查的非空資料，**不保證品質通過**；`empty_unverified` 表示空樣本尚未驗證為正常空集合。
Profile 的 `identity_match` 核對 username、ID 與可選 pk；集合事件另外包含 `count` 及 `quality`。
若中途停止，最後一個事件的 `state` 會記錄停止原因，之前的計數仍保留。

| `quality` 欄位 | 判讀界線 |
| --- | --- |
| `invalid_ids`／`numeric_ids` | ID 不符預期字串形狀，或來源以 JSON 數字傳回；數字計數本身不證明已失真，也不能轉字串就當驗證完成 |
| `duplicate_ids` | 同一端點內，含本次續頁，媒體 ID 主體重複；不把 Posts 與 Reels 的合法重疊混算 |
| `owner_suffix_mismatch`／`owner_suffix_unknown` | 媒體 ID 後綴不同於 NASA ID，或無可確認後綴；這是字串一致性檢查，不是作者／協作或專輯歸屬證明，零計數也不是所有權驗收 |
| `timestamp_missing`／`timestamp_invalid`／`timestamp_future` | 發布時間缺少、不合有效秒數範圍，或超過本次檢查時間 5 分鐘；不會補造時間 |
| `expiry_missing`／`expiry_invalid`／`expired_stories` | 目前 Story 的過期欄位缺少、先後／期間不合理，或已過期；Highlights 不是目前 Stories，不套用此過期檢查 |
| `media_url_missing`／`media_url_invalid` | 缺少來源媒體欄位，或不符 HTTPS URL 基本形狀；只有縮圖不算來源媒體，也沒有真的連線下載驗證 |
| `reel_type_unconfirmed` | Reels 未同時標示 `product_type: clips`、影片類型與 `is_video: true`，不能只因是影片就認定分類正確 |
| `media_type_unconfirmed` | Posts／Reels 缺媒體類型、類型值未知，或類型與影片旗標矛盾 |
| `carousel_invalid` | 輪播陣列、宣告數量、子項 ID／重複或子項數上限有問題；仍未對照 Instagram 原始排序 |
| `child_ids_invalid`／`child_owner_mismatch`／`child_owner_unknown` | 輪播子項 ID 缺失／重複，或子項 owner 後綴不符／無法判定；不能只因父貼文符合便接受全部子項 |
| `album_count_mismatch` | 精華專輯宣告數量缺少／不合法，或已測第一個專輯的項目數不符；不能用一個專輯樣本推論全部完整 |

品質計數可以重疊，子項問題也可累加，因此不能將各欄相加當成異常媒體總數。
Posts／Reels 的 `pagination` 為 `available`、`exhausted` 或 `unavailable`；
前兩者是來源提供的布林／cursor 形狀，不是全歷史完整性證明。
第二頁的 `cursor_advanced` 只檢查下一個 cursor 是否不同於輸入（包含來源回報結束），
仍須搭配重複 ID 與品質計數判讀，不是新內容或可靠續頁的單獨證據。

### schema 2：被動診斷明細

每個已取得有效集合的事件新增 `diagnostics`；它只補充既有 `quality`，不參與放寬品質門檻。
即使 owner 欄位與 NASA 相符，也不會抵銷原本的後綴不一致，或讓 `sample_incomplete` 變成通過。
診斷函式非預期失敗時，`collection_error` 會是 `true`；原有品質結果、總結果及請求順序仍保留，
此時不能把缺少的診斷或零計數解讀為沒有問題。
`truncated: true` 表示有內容因形狀或檢查上限而未完整檢查；不是資料已完整的訊號。

#### 輪播子項 ID

`diagnostics.carousel` 只出現在 Posts／Reels 集合事件。

| 欄位 | 意義 |
| --- | --- |
| `parents` | 本事件檢查的輪播父項目數 |
| `id_affected_parents` | 有子項 ID 問題或非物件子項的父項目數；每個父項目只計一次，不等於問題次數 |
| `children_checked`／`non_object_children` | 已檢查子項位置總數，包含非物件項；後者另列其中不是物件的數量 |
| `uninspected_parents` | `children` 不是清單或超過 20 個子項而未展開檢查的父項目數；同時標記 `truncated`，不能視為子項沒有問題 |
| `id_states` | 對物件子項的 ID，互斥計入 `missing`、`null`、`numeric`、`invalid_string`、`invalid_type` 或 `valid_string`；非物件子項不進此分組 |
| `duplicate_ids` | 同一輪播內重複的有效 ID 主體次數，是額外計數，可能同時存在於 `valid_string`；不能與互斥狀態相加當成子項總數 |
| `ownership` | 輪播子項層級的 owner 形狀與一致性計數，規則同下節，不沿用父貼文結果替子項背書 |

例如多個子項缺少 ID，可能都屬於同一父貼文；應以 `id_affected_parents` 判讀受影響父項目數，
以 `id_states` 判讀原因。這不是補造子項 ID，也尚未確認原始輪播順序。
每個集合最多檢查 100 個項目；超出既有邊界不會額外抓取或放寬解析。

#### owner 欄位形狀與一致性

`diagnostics.ownership` 記錄集合項目，`diagnostics.carousel.ownership` 記錄已檢查的子項，
各有 `items_checked`、`fields`、`containers`、`collaboration_fields`。
只觀察預先列出的欄位；`fields: {}` 表示沒有觀測到這些白名單欄位，**不代表回應完全沒有任何作者資訊**。

| 欄位 | 記錄內容與限制 |
| --- | --- |
| `fields` | 只允許 `owner.id`、`owner.pk`、`user.id`、`user.pk`、`owner_id`、`user_id`；僅出現實際觀測到的路徑，不輸出來源自訂鍵或值 |
| 每條路徑的形狀計數 | `present`、`null`、`numeric`、`invalid`、`valid_string`；`present` 是總出現數，不能再與其子分類相加 |
| 每條路徑的一致性計數 | `target_match`／`target_mismatch` 比對固定 NASA ID；`suffix_match`／`suffix_mismatch`／`suffix_unknown` 比對該媒體 ID 的後綴，只對符合嚴格正整數字串形狀的 owner ID 比較 |
| `containers` | 只記錄 `owner`／`user` 是 `object`、`null` 或 `other`，不列其內容 |
| `collaboration_fields` | 只記錄 `coauthor_producers`、`invited_coauthor_producers`、`collaborators` 是 `array`、`object`、`null` 或 `other`；不解析成員、不輸出帳號或 ID，也不認定邀請已獲接受 |

數字型 owner ID 只計為 `numeric`，不強制轉成字串或參與一致性比較。
各路徑可能描述同一項目，同一項目也可能同時貢獻目標及後綴比較，不能跨欄位加總為不同作者數。
協作欄位只是候選欄位的形狀觀察，**不是已驗證的 IGWatcher 協作契約或協作貼文證據**。
摘要不保存或輸出外部 owner／協作者的原值、任意來源鍵、URL 或錯誤訊息。

#### 精華宣告數與實收數

第一個專輯內文事件另有 `diagnostics.album_comparison`：

- `declared_state`／`received_state` 為 `integer`、`missing`、`null`、`invalid_type` 或 `out_of_range`。
- `declared_count`／`received_count` 只輸出 0 至 1,000,000 內的整數，其他狀態為 `null`；
  缺少、無效、超界或 `relation: unknown` **都不等於零筆**。
- `relation` 為 `equal`、`declared_more`、`declared_less` 或 `unknown`，分別表示兩數相等、宣告較多、
  宣告較少或無法比較。這比較的是來源宣告與這次實收，不是 Instagram 原始總數的真值。

這些明細可區分「宣告數大於實收數」與「沒有有效宣告數」等不同情況，但不會下載其他專輯來補數，
也不會自行解釋為刪除、快取、隱藏或抓取遺漏；原因仍需後續證據。

## 開發驗證範圍

2026-09-09 在 Windows 開發過程以本工具做了一輪真實 HTTP 查詢：8 個請求均取得 200，
Stories 2 筆、Posts 與 Reels 各兩頁且每頁 12 筆、Highlights 5 個專輯，第一個專輯內文 9 筆。
結果為 `sample_incomplete`：有 owner 後綴不一致、輪播子項品質與精華宣告數量差異，
不是來源拒絕，也不是資料已驗收。此輪沒有下載媒體、重試或保存原始回應；
後續加嚴的類型與子項檢查使用離線合成回應驗證，沒有為此連續重跑來源。

部署回歸測試靜態檢查 Compose 隔離、環境變數、程序入口、時限、套件包含與操作命令。
探測器與被動診斷測試使用合成回應，不需連線 IGWatcher：

```powershell
python -m pytest tests/test_igwatcher_probe.py tests/test_igwatcher_probe_diagnostics.py tests/test_igwatcher_probe_deployment.py -ra
```

Windows 開發環境沒有 Docker CLI；這些靜態／離線測試**不能代替 Ubuntu ARM64 容器實測**。
操作人員已提供前述第一輪目標主機結果；新增被動診斷本身仍須由更新映像後的一次性回報確認，
不能將開發環境的合成測試當成新版已在 Ubuntu 驗證完成。
