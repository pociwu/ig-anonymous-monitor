from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Callable

from flask import Flask, render_template_string
from waitress import serve


PAGE = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta http-equiv="refresh" content="30">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>IG Monitor</title>
<style>
body{font-family:system-ui,sans-serif;background:#101827;color:#e5e7eb;margin:0;padding:24px}main{max-width:1200px;margin:auto}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}.card,article{background:#1f2937;border-radius:10px;padding:16px}.value{font-size:1.5rem;font-weight:700}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px;border-bottom:1px solid #374151;vertical-align:top}code{word-break:break-all;color:#c4b5fd}.ok{color:#86efac}.bad{color:#fca5a5}.muted{color:#9ca3af}@media(max-width:700px){body{padding:12px}table{font-size:.85rem}}
</style></head><body><main>
<h1>IG Monitor</h1><p class="muted">唯讀儀表板 · 每 30 秒更新 · {{ data.generated_at }}</p>
<section class="grid">
<div class="card"><div>啟用帳號</div><div class="value">{{ data.summary.accounts }}</div></div>
<div class="card"><div>公開</div><div class="value">{{ data.summary.public }}</div></div>
<div class="card"><div>私人</div><div class="value">{{ data.summary.private }}</div></div>
<div class="card"><div>異常</div><div class="value">{{ data.summary.error }}</div></div>
<div class="card"><div>待下載媒體</div><div class="value">{{ data.summary.pending }}</div></div>
</section>
<h2>巡檢帳號</h2><article><table><thead><tr><th>帳號</th><th>狀態</th><th>Instagram Profile ID</th><th>有效網址</th><th>最後成功</th><th>媒體</th><th>錯誤</th></tr></thead><tbody>
{% for a in data.accounts %}<tr><td><strong>{{ a.label }}</strong><br><span class="muted">{{ a.username or '尚未取得' }}</span></td>
<td class="{{ 'bad' if a.fail_count >= 3 else 'ok' }}">{{ a.privacy }}<br>{{ a.fail_count }} 次失敗</td>
<td><code>{{ a.instagram_profile_id or '尚未建立' }}</code></td><td><code>{{ a.effective_url }}</code></td>
<td>{{ a.last_success_at or '尚未巡檢' }}</td><td>{{ a.downloaded }} 已下載<br>{{ a.pending }} 待處理</td><td>{{ a.last_error or '-' }}</td></tr>{% endfor %}
</tbody></table></article>
<h2>systemd</h2><section class="grid"><div class="card"><div>巡檢服務</div><div class="value">{{ data.services.monitor }}</div></div><div class="card"><div>排程器</div><div class="value">{{ data.services.timer }}</div></div><div class="card"><div>下次排程</div><div>{{ data.services.next_run }}</div></div></section>
</main></body></html>"""


def _systemctl_status(command: list[str]) -> str:
    try:
        output = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False).stdout.strip()
        return output or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def system_status() -> dict[str, str]:
    timer_rows = _systemctl_status(["systemctl", "list-timers", "--all", "ig-monitor.timer", "--no-pager"]).splitlines()
    next_run = timer_rows[-1] if len(timer_rows) > 1 else "unknown"
    return {
        "monitor": _systemctl_status(["systemctl", "is-active", "ig-monitor.service"]),
        "timer": _systemctl_status(["systemctl", "is-active", "ig-monitor.timer"]),
        "next_run": next_run,
    }


def dashboard_data(db_path: Path, status_provider: Callable[[], dict[str, str]] = system_status) -> dict[str, Any]:
    summary = {"accounts": 0, "public": 0, "private": 0, "error": 0, "pending": 0}
    accounts: list[dict[str, Any]] = []
    if db_path.is_file():
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute("""
                SELECT id,label,url,effective_url,instagram_profile_id,snapshot_json,fail_count,last_error,last_success_at
                FROM accounts WHERE enabled=1 ORDER BY id
            """).fetchall()
            summary["accounts"] = len(rows)
            for row in rows:
                snapshot = json.loads(row["snapshot_json"]) if row["snapshot_json"] else {}
                privacy = snapshot.get("privacy", "unknown")
                if privacy in ("public", "private"):
                    summary[privacy] += 1
                if int(row["fail_count"] or 0) >= 3:
                    summary["error"] += 1
                media = {item["status"]: item["count"] for item in connection.execute(
                    "SELECT status,COUNT(*) AS count FROM media WHERE account_id=? GROUP BY status", (row["id"],)
                )}
                pending = int(media.get("pending", 0)) + int(media.get("failed", 0))
                summary["pending"] += pending
                accounts.append({
                    "label": row["label"], "username": snapshot.get("username"), "privacy": privacy,
                    "instagram_profile_id": row["instagram_profile_id"],
                    "effective_url": row["effective_url"] or row["url"], "last_success_at": row["last_success_at"],
                    "fail_count": int(row["fail_count"] or 0), "last_error": row["last_error"],
                    "downloaded": int(media.get("downloaded", 0)), "pending": pending,
                })
        except sqlite3.Error:
            pass
        finally:
            connection.close()
    from datetime import UTC, datetime
    return {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "summary": summary,
            "accounts": accounts, "services": status_provider()}


def create_app(db_path: Path, status_provider: Callable[[], dict[str, str]] = system_status) -> Flask:
    app = Flask(__name__)

    @app.after_request
    def no_store(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return render_template_string(PAGE, data=dashboard_data(db_path, status_provider))

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="IG Monitor dashboard")
    parser.add_argument("--db", default="data/state.sqlite3")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8888)
    args = parser.parse_args()
    serve(create_app(Path(args.db).expanduser().resolve()), host=args.host, port=args.port, threads=4)


if __name__ == "__main__":
    main()
