#!/usr/bin/env bash
set -u

PROJECT_DIR="${IG_MONITOR_DIR:-/srv/ig-monitor}"
CONFIG_FILE="${IG_MONITOR_CONFIG:-${PROJECT_DIR}/config.yaml}"
STATE_DB="${IG_MONITOR_DB:-${PROJECT_DIR}/data/state.sqlite3}"
PYTHON_BIN="${IG_MONITOR_PYTHON:-${HOME}/miniconda3/envs/ig-monitor/bin/python}"
SERVICE_NAME="${IG_MONITOR_SERVICE:-ig-monitor.service}"
TIMER_NAME="${IG_MONITOR_TIMER:-ig-monitor.timer}"

pause_menu() {
  printf '\n'
  read -r -p "按 Enter 返回選單..." _
}

show_account_summary() {
  clear
  printf '%s\n' "========================================"
  printf '%s\n' " IG Monitor｜巡檢帳號摘要"
  printf '%s\n' "========================================"

  if [[ ! -x "${PYTHON_BIN}" ]]; then
    printf '找不到 Conda Python：%s\n' "${PYTHON_BIN}"
    printf '%s\n' "可設定：export IG_MONITOR_PYTHON=/正確路徑/python"
    pause_menu
    return
  fi
  if [[ ! -f "${CONFIG_FILE}" ]]; then
    printf '找不到設定檔：%s\n' "${CONFIG_FILE}"
    pause_menu
    return
  fi

  "${PYTHON_BIN}" - "${CONFIG_FILE}" "${STATE_DB}" <<'PY'
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

  if ! command -v systemctl >/dev/null 2>&1; then
    printf '%s\n' "找不到 systemctl，這個選項需在 Ubuntu systemd 主機執行。"
    pause_menu
    return
  fi

  printf 'Timer：%s\n' "${TIMER_NAME}"
  printf '啟用狀態：%s\n' "$(systemctl is-enabled "${TIMER_NAME}" 2>/dev/null || true)"
  printf '運作狀態：%s\n' "$(systemctl is-active "${TIMER_NAME}" 2>/dev/null || true)"
  printf '\n上次與下次執行：\n'
  systemctl list-timers --all "${TIMER_NAME}" --no-pager 2>/dev/null || \
    printf '%s\n' "尚未安裝或載入 ${TIMER_NAME}"

  printf '\n最近一次巡檢結果：\n'
  systemctl show "${SERVICE_NAME}" \
    -p Result -p ExecMainStatus -p ActiveState -p SubState --no-pager 2>/dev/null || true

  printf '\nTimer 設定：\n'
  systemctl cat "${TIMER_NAME}" --no-pager 2>/dev/null | \
    grep -E '^(OnCalendar|OnUnit|RandomizedDelaySec|AccuracySec|Persistent)=' || true

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
