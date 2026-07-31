# Ubuntu Docker 遷移指南

以下命令假設專案位於 `/srv/ig-monitor`，帳號為目前登入的 Ubuntu 使用者。既有的
`config.yaml`、`.env`、SQLite、照片及影片都會沿用，不需要重新建立監控資料。

## 1. 安裝 Docker Engine 與 Compose

如果主機已有可正常執行的 `docker compose version`，可跳到下一節。

```bash
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker version
sudo docker compose version
```

讓目前使用者之後可以直接使用管理選單：

```bash
sudo usermod -aG docker "$USER"
```

完成首次部署後登出再登入，讓 `docker` 群組權限生效。在此之前的命令均可加上
`sudo` 執行。

## 2. 停止舊 systemd 排程並備份

```bash
cd /srv/ig-monitor

sudo systemctl disable --now ig-monitor.timer
sudo systemctl stop ig-monitor.service
sudo systemctl disable --now ig-monitor-dashboard.service

mkdir -p data/backups
cp -a data/state.sqlite3 \
  "data/backups/state-before-docker-$(date +%Y%m%d-%H%M%S).sqlite3"
```

停止舊服務後才啟動容器，可避免 systemd 與 Docker 同時巡檢同一批帳號。

## 3. 更新專案並準備權限

```bash
cd /srv/ig-monitor
git pull --ff-only origin main

mkdir -p data downloads
printf '\nPUID=%s\nPGID=%s\n' "$(id -u)" "$(id -g)" >> .env
sudo chown -R "$(id -u):$(id -g)" data downloads
sudo chown "$(id -u):$(id -g)" config.yaml
chmod 600 config.yaml
chmod 600 .env
```

Docker 內部仍使用 `/srv/ig-monitor`，因此 SQLite 既有的媒體絕對路徑仍然有效。
Telegram 與 Apify 金鑰只從 `.env` 注入，不會進入映像。

## 4. 建置與啟動

```bash
cd /srv/ig-monitor

sudo docker compose config
sudo docker compose build --pull

sudo docker compose run --rm --no-deps monitor \
  python -m ig_monitor --config /srv/ig-monitor/config.yaml --check

sudo docker compose up -d
sudo docker compose ps
sudo docker compose logs --tail=100 monitor
curl --fail http://127.0.0.1:8888/healthz
```

服務內容：

- `monitor`：啟動後立即巡檢，完成後依 `config.yaml` 的
  `schedule.interval_minutes` 等待下一輪。
- `dashboard`：監聽 `0.0.0.0:8888`，健康檢查路徑為 `/healthz`。
- Dashboard 首頁可驗證、新增及移除監控帳號，因此 `config.yaml` 必須可由
  `PUID`/`PGID` 指定的使用者寫入。
- 最多可監控 16 個帳號；拖曳首頁卡片後，顯示與巡檢順序會寫回 `config.yaml`。
- 兩個容器共用 `data/` 與 `downloads/`。
- Chromium、ffmpeg 及 ffprobe 已包含在映像，不再依賴 Miniconda。

## 5. 日常操作

```bash
cd /srv/ig-monitor

docker compose ps
docker compose logs -f monitor
docker compose logs -f dashboard
docker compose restart monitor
docker compose restart dashboard
```

手動巡檢一次：

```bash
docker compose run --rm --no-deps monitor \
  python -m ig_monitor --config /srv/ig-monitor/config.yaml
```

測試 Telegram：

```bash
docker compose run --rm --no-deps monitor \
  python -m ig_monitor --config /srv/ig-monitor/config.yaml --send-test
```

更新版本：

```bash
cd /srv/ig-monitor
git pull --ff-only origin main
docker compose build --pull
docker compose up -d
docker compose ps
```

重新安裝家目錄選單：

```bash
cp /srv/ig-monitor/deploy/ig-monitor-menu.sh ~/ig-monitor-menu.sh
chmod +x ~/ig-monitor-menu.sh
~/ig-monitor-menu.sh
```

## 回復舊 systemd 版本

容器遷移不會改寫或刪除資料。需要暫時回復時：

```bash
cd /srv/ig-monitor
docker compose down
sudo systemctl enable --now ig-monitor.timer
sudo systemctl enable --now ig-monitor-dashboard.service
```
