from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Callable

from flask import Flask, abort, render_template_string, send_file, url_for
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


CARD_PAGE = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta http-equiv="refresh" content="30">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>IG Monitor</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{font-family:system-ui,-apple-system,sans-serif;background:#0b1120;color:#e5e7eb;margin:0;padding:24px}main{max-width:1200px;margin:auto}a{color:inherit;text-decoration:none}.summary,.accounts{display:grid;gap:14px}.summary{grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin-bottom:28px}.accounts{grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}.metric,.account-card,.service{background:#172033;border:1px solid #27344d;border-radius:16px}.metric{padding:16px}.metric strong{display:block;font-size:1.65rem;margin-top:4px}.account-card{padding:18px;transition:.18s transform,.18s border-color}.account-card:hover{transform:translateY(-3px);border-color:#8b5cf6}.identity{display:flex;gap:14px;align-items:center}.avatar{width:72px;height:72px;border-radius:50%;object-fit:cover;background:#27344d;border:2px solid #475569}.avatar-fallback{display:grid;place-items:center;font-size:1.5rem;font-weight:700}.name{font-size:1.15rem;font-weight:750}.handle,.muted{color:#94a3b8}.facts{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:16px 0}.fact{background:#0f172a;border-radius:10px;padding:9px;text-align:center}.fact strong{display:block}.row{display:flex;justify-content:space-between;gap:12px;margin-top:8px}.value{overflow-wrap:anywhere;text-align:right}.ok{color:#86efac}.bad{color:#fca5a5}.services{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.service{padding:16px}@media(max-width:600px){body{padding:14px}.accounts{grid-template-columns:1fr}}\n+</style></head><body><main>
<h1>IG Monitor</h1><p class="muted">唯讀儀表板 · 每 30 秒更新 · {{ data.generated_at }}</p>
<section class="summary">
<div class="metric">啟用帳號<strong>{{ data.summary.accounts }}</strong></div>
<div class="metric">公開帳號<strong>{{ data.summary.public }}</strong></div>
<div class="metric">私人帳號<strong>{{ data.summary.private }}</strong></div>
<div class="metric">異常帳號<strong>{{ data.summary.error }}</strong></div>
<div class="metric">待處理媒體<strong>{{ data.summary.pending }}</strong></div>
</section>
<h2>巡檢帳號</h2>
<section class="accounts">
{% for a in data.accounts %}
<a class="account-card" href="{{ url_for('account_detail', account_id=a.id) }}">
  <div class="identity">
    {% if a.has_avatar %}<img class="avatar" src="{{ url_for('avatar_asset', account_id=a.id) }}" alt="{{ a.label }}">
    {% else %}<div class="avatar avatar-fallback">{{ (a.username or a.label or '?')[0]|upper }}</div>{% endif %}
    <div><div class="name">{{ a.display_name or a.label }}</div><div class="handle">@{{ a.username or a.label }}</div></div>
  </div>
  <div class="facts">
    <div class="fact"><strong>{{ a.posts }}</strong>發文</div>
    <div class="fact"><strong>{{ a.followers }}</strong>跟隨者</div>
    <div class="fact"><strong>{{ a.following }}</strong>追蹤中</div>
  </div>
  <div class="row"><span>狀態</span><span class="{{ 'bad' if a.fail_count >= 3 else 'ok' }}">{{ a.privacy }} / {{ a.fail_count }} 次失敗</span></div>
  <div class="row"><span>Profile ID</span><span class="value">{{ a.instagram_profile_id or '尚未建立' }}</span></div>
  <div class="row"><span>媒體</span><span>{{ a.downloaded }} 已下載 / {{ a.pending }} 待處理</span></div>
</a>
{% else %}<p class="muted">尚無巡檢帳號資料。</p>{% endfor %}
</section>
<h2>systemd</h2><section class="services">
<div class="service">巡檢服務：<strong>{{ data.services.monitor }}</strong></div>
<div class="service">排程器：<strong>{{ data.services.timer }}</strong></div>
<div class="service">下次排程：<span>{{ data.services.next_run }}</span></div>
</section>
</main></body></html>"""


DETAIL_PAGE = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ account.display_name or account.label }} · IG Monitor</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{font-family:system-ui,-apple-system,sans-serif;background:#0b1120;color:#e5e7eb;margin:0;padding:24px}main{max-width:1200px;margin:auto}a{color:#c4b5fd;text-decoration:none}.profile{display:flex;gap:18px;align-items:center;background:#172033;border:1px solid #27344d;border-radius:16px;padding:20px}.avatar{width:96px;height:96px;border-radius:50%;object-fit:cover;background:#27344d}.avatar-fallback{display:grid;place-items:center;font-size:2rem;font-weight:700}.muted{color:#94a3b8}.stats{display:flex;gap:18px;flex-wrap:wrap;margin-top:10px}.stats strong{display:block;font-size:1.25rem}.meta{background:#172033;border-radius:12px;padding:16px;margin:16px 0;overflow-wrap:anywhere}.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.tabs button{border:1px solid #334155;background:#172033;color:#cbd5e1;border-radius:999px;padding:9px 14px;cursor:pointer}.tabs button.active{background:#7c3aed;border-color:#8b5cf6;color:white}.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}.media{background:#172033;border-radius:14px;overflow:hidden;border:1px solid #27344d}.media[hidden]{display:none}.media img,.media video{width:100%;aspect-ratio:1/1;display:block;object-fit:cover;background:#020617}.caption{padding:10px;font-size:.85rem;color:#94a3b8}@media(max-width:600px){body{padding:14px}.profile{align-items:flex-start}.avatar{width:72px;height:72px}.gallery{grid-template-columns:repeat(2,minmax(0,1fr))}}\n+</style></head><body><main>
<p><a href="{{ url_for('index') }}">← 返回帳號列表</a></p>
<section class="profile">
{% if account.has_avatar %}<img class="avatar" src="{{ url_for('avatar_asset', account_id=account.id) }}" alt="{{ account.label }}">
{% else %}<div class="avatar avatar-fallback">{{ (account.username or account.label or '?')[0]|upper }}</div>{% endif %}
<div><h1>{{ account.display_name or account.label }}</h1><div class="muted">@{{ account.username or account.label }}</div>
<div class="stats"><span><strong>{{ account.posts }}</strong>發文</span><span><strong>{{ account.followers }}</strong>跟隨者</span><span><strong>{{ account.following }}</strong>追蹤中</span></div></div>
</section>
<section class="meta"><div>Instagram Profile ID：{{ account.instagram_profile_id or '尚未建立' }}</div><div>有效網址：{{ account.effective_url }}</div>{% if account.bio %}<p>{{ account.bio }}</p>{% endif %}</section>
<h2>照片與影片</h2>
<nav class="tabs source-tabs">
<button class="active" data-source="posts">貼文 {{ counts.posts.all }}</button>
<button data-source="stories">Stories {{ counts.stories.all }}</button>
<button data-source="highlights">Highlights {{ counts.highlights.all }}</button>
</nav>
<nav class="tabs kind-tabs">
<button class="active" data-kind="all">全部</button>
<button data-kind="image">照片</button>
<button data-kind="video">影片</button>
</nav>
<section class="gallery">
{% for item in media %}<article class="media" data-sources="{{ item.categories|join(' ') }}" data-kind="{{ item.kind }}">
{% if item.kind == 'video' %}<video controls preload="metadata" src="{{ url_for('media_asset', media_id=item.id) }}"></video>
{% else %}<a href="{{ url_for('media_asset', media_id=item.id) }}" target="_blank"><img loading="lazy" src="{{ url_for('media_asset', media_id=item.id) }}" alt="IG photo"></a>{% endif %}
<div class="caption">{{ item.categories|join(' · ') }}{% if item.published_at %} · {{ item.published_at }}{% endif %}</div>
</article>
{% else %}<p class="muted">目前沒有已下載的照片或影片。</p>{% endfor %}
</section>
<script>
let selectedSource='posts',selectedKind='all';
function filterMedia(){
 document.querySelectorAll('.media').forEach(el=>{
  const sourceMatch=el.dataset.sources.split(' ').includes(selectedSource);
  const kindMatch=selectedKind==='all'||el.dataset.kind===selectedKind;
  el.hidden=!(sourceMatch&&kindMatch);
 });
 const c={{ counts|tojson }}[selectedSource];
 document.querySelector('[data-kind="all"]').textContent=`全部 ${c.all}`;
 document.querySelector('[data-kind="image"]').textContent=`照片 ${c.image}`;
 document.querySelector('[data-kind="video"]').textContent=`影片 ${c.video}`;
}
document.querySelectorAll('[data-source]').forEach(button=>button.addEventListener('click',()=>{
 selectedSource=button.dataset.source;
 document.querySelectorAll('[data-source]').forEach(x=>x.classList.toggle('active',x===button));
 filterMedia();
}));
document.querySelectorAll('[data-kind]').forEach(button=>button.addEventListener('click',()=>{
 selectedKind=button.dataset.kind;
 document.querySelectorAll('[data-kind]').forEach(x=>x.classList.toggle('active',x===button));
 filterMedia();
}));
filterMedia();
</script>
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
                    "id": row["id"], "label": row["label"], "username": snapshot.get("username"),
                    "display_name": snapshot.get("display_name"), "privacy": privacy,
                    "posts": snapshot.get("posts", 0), "followers": snapshot.get("followers", 0),
                    "following": snapshot.get("following", 0),
                    "has_avatar": bool(snapshot.get("avatar_path") and Path(snapshot["avatar_path"]).is_file()),
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


def account_detail_data(
    db_path: Path, account_id: int
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, dict[str, int]]]:
    if not db_path.is_file():
        return None, [], _empty_collection_counts()
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("""
            SELECT id,label,url,effective_url,instagram_profile_id,snapshot_json
            FROM accounts WHERE id=? AND enabled=1
        """, (account_id,)).fetchone()
        if row is None:
            return None, [], _empty_collection_counts()
        snapshot = json.loads(row["snapshot_json"]) if row["snapshot_json"] else {}
        account = {
            "id": row["id"], "label": row["label"], "username": snapshot.get("username"),
            "display_name": snapshot.get("display_name"), "posts": snapshot.get("posts", 0),
            "followers": snapshot.get("followers", 0), "following": snapshot.get("following", 0),
            "bio": snapshot.get("bio"), "instagram_profile_id": row["instagram_profile_id"],
            "effective_url": row["effective_url"] or row["url"],
            "has_avatar": bool(snapshot.get("avatar_path") and Path(snapshot["avatar_path"]).is_file()),
        }
        media_rows = connection.execute("""
            SELECT m.id,m.kind,m.published_at,m.local_path,GROUP_CONCAT(ms.category) AS categories
            FROM media m JOIN media_sources ms ON ms.media_id=m.id
            WHERE m.account_id=? AND m.status='downloaded' AND m.duplicate_of_id IS NULL
              AND m.local_path IS NOT NULL
            GROUP BY m.id,m.kind,m.published_at,m.local_path,m.downloaded_at
            ORDER BY COALESCE(m.published_at,m.downloaded_at) DESC,m.id DESC
        """, (account_id,)).fetchall()
        media = []
        counts = _empty_collection_counts()
        for item in media_rows:
            if not Path(item["local_path"]).is_file():
                continue
            categories = sorted({_collection_name(value) for value in (item["categories"] or "").split(",")})
            media.append({
                "id": item["id"], "kind": item["kind"], "published_at": item["published_at"],
                "categories": categories,
            })
            for category in categories:
                counts[category]["all"] += 1
                counts[category][item["kind"]] += 1
        return account, media, counts
    finally:
        connection.close()


def _empty_collection_counts() -> dict[str, dict[str, int]]:
    return {
        name: {"all": 0, "image": 0, "video": 0}
        for name in ("posts", "stories", "highlights")
    }


def _collection_name(value: str) -> str:
    lowered = value.strip().lower()
    if "highlight" in lowered:
        return "highlights"
    if "stor" in lowered:
        return "stories"
    return "posts"


def _avatar_path(db_path: Path, account_id: int) -> Path | None:
    if not db_path.is_file():
        return None
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT snapshot_json FROM accounts WHERE id=? AND enabled=1", (account_id,)).fetchone()
        snapshot = json.loads(row[0]) if row and row[0] else {}
        path = Path(snapshot["avatar_path"]) if snapshot.get("avatar_path") else None
        return path if path and path.is_file() else None
    finally:
        connection.close()


def _media_path(db_path: Path, media_id: int) -> Path | None:
    if not db_path.is_file():
        return None
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = connection.execute("""
            SELECT local_path FROM media WHERE id=? AND status='downloaded' AND local_path IS NOT NULL
        """, (media_id,)).fetchone()
        path = Path(row[0]) if row else None
        return path if path and path.is_file() else None
    finally:
        connection.close()


def create_app(db_path: Path, status_provider: Callable[[], dict[str, str]] = system_status) -> Flask:
    app = Flask(__name__)

    @app.after_request
    def no_store(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return render_template_string(CARD_PAGE, data=dashboard_data(db_path, status_provider))

    @app.get("/account/<int:account_id>")
    def account_detail(account_id: int):
        account, media, counts = account_detail_data(db_path, account_id)
        if account is None:
            abort(404)
        return render_template_string(DETAIL_PAGE, account=account, media=media, counts=counts)

    @app.get("/account/<int:account_id>/avatar")
    def avatar_asset(account_id: int):
        path = _avatar_path(db_path, account_id)
        if path is None:
            abort(404)
        return send_file(path, conditional=True)

    @app.get("/media/<int:media_id>")
    def media_asset(media_id: int):
        path = _media_path(db_path, media_id)
        if path is None:
            abort(404)
        return send_file(path, conditional=True)

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
