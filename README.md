# IG Anonymous Monitor

## Apify identity resolution and monthly cap

Add the secret to `/srv/ig-monitor/.env`:

```env
APIFY_API_TOKEN=your_Apify_token
```

Then enable the non-secret configuration:

```yaml
apify:
  enabled: true
  actor_id: apify/instagram-profile-scraper
  monthly_cap_usd: 5.0
  request_reservation_usd: 0.01
```

The first successful inspection saves an Instagram Profile ID for each enabled account. Later, the service calls Apify only when the anonymous viewer cannot locate an account, then resolves a changed username and updates the database effective URL. The monthly cap cannot exceed 5 USD; the service checks Apify's usage cycle, records conservative local reservations, and sends only one budget-exhausted Telegram alert per cycle.

Run tests in the Conda environment with `conda run -n ig-monitor python -m pytest`. Existing environments created before the test dependency was added need `conda install -n ig-monitor pytest` once.

## Read-only monitoring dashboard

The dashboard runs as a separate Flask/Waitress service on `0.0.0.0:8888`. It has no application login and is intended to be reachable only through the configured Tailscale ACL.

```bash
conda run -n ig-monitor python -m pip install -e '.[test]'
sudo cp deploy/ig-monitor-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ig-monitor-dashboard.service
```

Open `http://<tailscale-hostname-or-ip>:8888/`. The page is read-only and refreshes every 30 seconds.

以 Playwright 等待 `insta-stories-viewer.com` 的動態內容，監控最多 10 個 IG 頁面。程式保存完整快照、透過 Telegram 通知變化，並在帳號公開時分批下載網站能提供的最高畫質照片與影片。

## 已實作功能

- 私人、公開、未知三態；Stories 與 Publications 都必須進入明確終止狀態。
- 每次最長等待 45 秒，失敗後以全新頁面重試一次。
- 初次成功載入通知一次；程式或主機重啟不重複通知。
- 監控使用者名稱、顯示名稱、發文數、跟隨者、追蹤者、自介、大頭貼及公開狀態。
- 大頭貼以內容雜湊比較，變更通知附上變更前與變更後圖片。
- Telegram 待送佇列、分階段續傳及失敗重試。
- 媒體摘要送出後，可把本輪新下載的照片與影片直接傳到 Telegram；預設每帳號每輪最多 10 個附件。
- 公開後同步大頭貼、貼文、輪播、Stories，以及頁面實際提供的 Highlights。
- 同時檢查 DOM 與背景 JSON 回應，依原始／下載來源、播放器來源、`srcset`、縮圖順序選擇品質。
- 每帳號每輪最多下載 50 個檔案，最新內容優先，使用媒體識別碼與 SHA-256 去重。
- 連續 3 次失敗才通知，恢復成功時另行通知。
- 本機保存去廣告 HTML、截圖及錯誤資訊，每帳號保留最近 10 次。
- SQLite 每日安全備份並保留 7 份。
- 台灣時間 09:00 可選的每日存活摘要。
- 程序鎖避免排程重疊；帳號之間隨機等待 10～20 秒。

## Ubuntu + Miniconda 安裝

以下假設專案位於 `/srv/ig-monitor`，請把路徑和使用者名稱改成實際值。

```bash
cd /srv/ig-monitor
conda env create -f environment.yml
conda run -n ig-monitor python -m pip install -e .
mkdir -p data/ms-playwright
PLAYWRIGHT_BROWSERS_PATH=/srv/ig-monitor/data/ms-playwright \
  conda run -n ig-monitor playwright install chromium
sudo /home/YOUR_USER/miniconda3/envs/ig-monitor/bin/playwright install-deps chromium
```

如果 Miniconda 不在 `/home/YOUR_USER/miniconda3`，用以下命令取得正確 Python 路徑：

```bash
conda run -n ig-monitor which python
```

## 設定

```bash
cp config.example.yaml config.yaml
cp .env.example .env
chmod 600 .env
```

編輯 `config.yaml` 加入最多 10 個網址。一般設定放在 YAML；Telegram Bot Token、Chat ID 和可選的 Topic ID 只放在 `.env`。

下載目錄預設結構：

```text
downloads/
└── sin_9311/
    ├── avatar/
    ├── posts/
    ├── stories/
    └── highlights/
```

## 安裝前測試

只載入與解析，不寫入 SQLite、不發通知、不保存媒體：

```bash
PLAYWRIGHT_BROWSERS_PATH=/srv/ig-monitor/data/ms-playwright \
  conda run -n ig-monitor python -m ig_monitor --config config.yaml --check
```

只測試 Telegram：

```bash
conda run -n ig-monitor python -m ig_monitor --config config.yaml --send-test
```

正式手動執行一次：

```bash
conda run -n ig-monitor python -m ig_monitor --config config.yaml
```

重設單一帳號基準；已下載媒體不會刪除：

```bash
conda run -n ig-monitor python -m ig_monitor --config config.yaml --reset-account sin_9311
```

## systemd 排程

先編輯 `deploy/ig-monitor.service`，替換：

- `YOUR_USER`
- 專案的 `WorkingDirectory` 與 `--config` 路徑
- Miniconda 環境的 Python 絕對路徑

然後安裝：

```bash
sudo cp deploy/ig-monitor.service /etc/systemd/system/
sudo cp deploy/ig-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ig-monitor.timer
systemctl list-timers ig-monitor.timer
```

查看執行紀錄：

```bash
journalctl -u ig-monitor.service -n 100 --no-pager
tail -f data/monitor.log
```

## 家目錄管理選單

把互動式選單安裝到 Ubuntu 使用者家目錄：

```bash
cp /srv/ig-monitor/deploy/ig-monitor-menu.sh ~/ig-monitor-menu.sh
chmod +x ~/ig-monitor-menu.sh
~/ig-monitor-menu.sh
```

選單功能：

- `1. 巡檢帳號／摘要內容`：顯示啟用帳號、最新個人資料、公開狀態、錯誤次數及媒體下載數量。
- `2. 排程時間`：顯示 timer 是否啟用、上次與下次執行時間、最近巡檢結果。
- `0. 離開`。

預設專案位置是 `/srv/ig-monitor`，Conda Python 是 `~/miniconda3/envs/ig-monitor/bin/python`。若路徑不同，可在 `~/.profile` 設定：

```bash
export IG_MONITOR_DIR=/srv/ig-monitor
export IG_MONITOR_PYTHON=/home/ubuntu/miniconda3/envs/ig-monitor/bin/python
```

停用每日摘要時，把 `config.yaml` 改為：

```yaml
heartbeat:
  enabled: false
  time: "09:00"
  timezone: Asia/Taipei
```

若要調整新媒體是否傳到 Telegram，使用：

```yaml
telegram:
  enabled: true
  retry_limit_per_run: 20
  send_new_media: true
  max_new_media_attachments: 10
```

`send_new_media: false` 只發數量摘要；`max_new_media_attachments` 控制每個帳號每輪最多傳送幾個新照片或影片。超過上限的檔案仍會完整保存到本機下載目錄。

## 網站載入判定

`DOMContentLoaded` 或 `networkidle` 都不被當作完成條件。程式會實際切換兩個頁籤，並要求每個頁籤連續兩次得到相同終止狀態：私人、有效媒體容器、或明確無內容。只載入個人資料、媒體容器仍隱藏時會判為未知，不覆蓋舊快照。

## 測試

```bash
python -m unittest discover -s tests -v
```
