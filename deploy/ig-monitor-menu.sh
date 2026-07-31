#!/usr/bin/env bash
set -u

PROJECT_DIR="${IG_MONITOR_DIR:-/srv/ig-monitor}"
CONFIG_FILE="${IG_MONITOR_CONFIG:-${PROJECT_DIR}/config.yaml}"
STATE_DB="${IG_MONITOR_DB:-${PROJECT_DIR}/data/state.sqlite3}"
COMPOSE_FILE="${IG_MONITOR_COMPOSE:-${PROJECT_DIR}/compose.yaml}"
CONTAINER_CONFIG="/srv/ig-monitor/config.yaml"
CONTAINER_DB="/srv/ig-monitor/data/state.sqlite3"

compose() {
  docker compose --project-directory "${PROJECT_DIR}" -f "${COMPOSE_FILE}" "$@"
}

pause_menu() {
  printf '\n'
  read -r -p "按 Enter 返回選單..." _
}

show_account_summary() {
  clear
  printf '%s\n' "========================================"
  printf '%s\n' " IG Monitor｜巡檢帳號摘要"
  printf '%s\n' "========================================"

  if ! docker compose version >/dev/null 2>&1; then
    printf '%s\n' "找不到 docker compose，請先完成 Docker 部署。"
    pause_menu
    return
  fi
  if [[ ! -f "${CONFIG_FILE}" ]]; then
    printf '找不到設定檔：%s\n' "${CONFIG_FILE}"
    pause_menu
    return
  fi

  if ! compose exec -T dashboard true >/dev/null 2>&1; then
    printf '%s\n' "dashboard 容器未執行，請先執行：docker compose up -d"
    pause_menu
    return
  fi

  compose exec -T dashboard python - "${CONTAINER_CONFIG}" "${CONTAINER_DB}" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

import yaml

config_path = Path(sys.argv[1])
db_path = Path(sys.argv[2])
config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
accounts = [item for item in config.get("accounts", []) if item.get("enabled", True)]

print(f"設定檔：{config_path}")
print(f"啟用帳號：{len(accounts)}")
print()

connection = None
if db_path.is_file():
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        print(f"無法讀取狀態資料庫：{exc}")

privacy_labels = {"private": "私人", "public": "公開", "unknown": "未知"}
totals = {"public": 0, "private": 0, "unknown": 0, "error": 0, "pending": 0}

for index, item in enumerate(accounts, 1):
    url = str(item.get("url", ""))
    key = url.rstrip("/").rsplit("/", 1)[-1]
    label = str(item.get("label") or key)
    print(f"[{index}] {label}")
    print(f"    網址：{url}")

    row = None
    if connection is not None:
        try:
            row = connection.execute(
                "SELECT id,snapshot_json,fail_count,last_error,last_success_at,instagram_profile_id,effective_url FROM accounts WHERE url=? OR account_key=?",
                (url, key),
            ).fetchone()
        except sqlite3.Error as exc:
            print(f"    資料庫錯誤：{exc}")

    if row is not None:
        print(f"    Instagram Profile ID: {row['instagram_profile_id'] or '尚未建立'}")
        print(f"    Effective URL: {row['effective_url'] or url}")

    if row is None or not row["snapshot_json"]:
        print("    狀態：尚無成功巡檢資料")
        print()
        totals["unknown"] += 1
        continue

    snapshot = json.loads(row["snapshot_json"])
    privacy = snapshot.get("privacy", "unknown")
    totals[privacy if privacy in totals else "unknown"] += 1
    if int(row["fail_count"] or 0) >= 3:
        totals["error"] += 1

    print(f"    帳號狀態：{privacy_labels.get(privacy, privacy)}")
    print(f"    使用者名稱：{snapshot.get('username') or '（無）'}")
    print(f"    顯示名稱：{snapshot.get('display_name') or '（無）'}")
    print(f"    發文篇數：{snapshot.get('posts', 0)}")
    print(f"    跟隨者：{snapshot.get('followers', 0)}")
    print(f"    追蹤者：{snapshot.get('following', 0)}")
    bio = str(snapshot.get("bio") or "（無）").replace("\n", " / ")
    print(f"    自介：{bio}")
    print(f"    最後成功：{row['last_success_at'] or '（無）'}")
    print(f"    連續失敗：{row['fail_count'] or 0}")
    if row["last_error"]:
        print(f"    最近錯誤：{row['last_error']}")

    media = {"downloaded": 0, "pending": 0, "failed": 0}
    try:
        for media_row in connection.execute(
            "SELECT status,COUNT(*) AS count FROM media WHERE account_id=? GROUP BY status", (row["id"],)
        ):
            media[media_row["status"]] = media_row["count"]
    except sqlite3.Error:
        pass
    pending = int(media.get("pending", 0)) + int(media.get("failed", 0))
    totals["pending"] += pending
    print(f"    已下載媒體：{media.get('downloaded', 0)}")
    print(f"    尚待下載：{pending}")
    print()

if connection is not None:
    connection.close()

print("----------------------------------------")
print(f"摘要：公開 {totals['public']}｜私人 {totals['private']}｜未知 {totals['unknown']}｜異常 {totals['error']}")
print(f"尚待下載媒體：{totals['pending']}")
PY

  pause_menu
}

show_schedule() {
  clear
  printf '%s\n' "========================================"
  printf '%s\n' " IG Monitor｜排程時間"
  printf '%s\n' "========================================"
  printf '目前時間：%s\n\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"

  if ! docker compose version >/dev/null 2>&1; then
    printf '%s\n' "找不到 docker compose，請先完成 Docker 部署。"
    pause_menu
    return
  fi

  printf '%s\n' "容器狀態："
  compose ps

  printf '\n排程與最近一次巡檢：\n'
  compose exec -T dashboard python - "${CONTAINER_CONFIG}" "${CONTAINER_DB}" <<'PY'
import sqlite3
import sys
from pathlib import Path

import yaml

config = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8")) or {}
interval = int(config.get("schedule", {}).get("interval_minutes", 15))
print(f"巡檢間隔：每次完成後 {interval} 分鐘")

db_path = Path(sys.argv[2])
if not db_path.is_file():
    print("最近巡檢：尚無資料庫")
    raise SystemExit

connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
row = connection.execute(
    "SELECT started_at,finished_at,status,detail FROM runs ORDER BY id DESC LIMIT 1"
).fetchone()
connection.close()
if row:
    print(f"最近開始：{row[0] or '（無）'}")
    print(f"最近完成：{row[1] or '執行中'}")
    print(f"最近結果：{row[2] or '（無）'}")
    print(f"詳細資訊：{row[3] or '（無）'}")
else:
    print("最近巡檢：尚無紀錄")
PY

  pause_menu
}

while true; do
  clear
  printf '%s\n' "========================================"
  printf '%s\n' " IG Monitor 管理選單"
  printf '%s\n' "========================================"
  printf '%s\n' " 1. 巡檢帳號／摘要內容"
  printf '%s\n' " 2. 排程時間"
  printf '%s\n' " 0. 離開"
  printf '%s\n' "----------------------------------------"
  read -r -p "請輸入選項 [0-2]：" choice

  case "${choice}" in
    1) show_account_summary ;;
    2) show_schedule ;;
    0) printf '%s\n' "已離開。"; exit 0 ;;
    *) printf '%s\n' "選項錯誤，請輸入 0、1 或 2。"; sleep 1 ;;
  esac
done
