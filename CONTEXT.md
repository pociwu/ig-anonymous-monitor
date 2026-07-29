# IG Monitor Context

## Identity resolution

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
