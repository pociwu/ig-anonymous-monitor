from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from flask import Flask, abort, redirect, render_template_string, request, send_file, url_for

from .account_registry import AccountRegistry, AccountValidator
from .config import load_config
from .dedup import delete_quarantined_media, restore_quarantined_media


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
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>IG Monitor</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{font-family:system-ui,-apple-system,sans-serif;background:#0b1120;color:#e5e7eb;margin:0;padding:24px}main{max-width:1200px;margin:auto}a{color:inherit;text-decoration:none}.summary,.accounts{display:grid;gap:14px}.summary{grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin-bottom:28px}.accounts{grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}.metric,.account-card,.service,.manage{background:#172033;border:1px solid #27344d;border-radius:16px}.metric{padding:16px}.metric strong{display:block;font-size:1.65rem;margin-top:4px}.manage{padding:16px;margin:0 0 28px}.manage form{display:grid;grid-template-columns:minmax(260px,2fr) minmax(140px,1fr) auto;gap:10px}.manage input,.manage button{border:1px solid #334155;border-radius:10px;padding:11px 12px;font:inherit}.manage input{background:#0f172a;color:#e5e7eb}.manage button{background:#7c3aed;color:white;cursor:pointer}.error{color:#fecaca;background:#7f1d1d;padding:10px;border-radius:10px}.account-card{padding:18px;transition:.18s transform,.18s border-color}.account-card[draggable=true]{cursor:grab}.account-card.dragging{opacity:.45;transform:scale(.98)}.drag-handle{color:#94a3b8;text-align:right;font-size:.82rem;margin:-5px 0 8px;user-select:none}.account-card>a{display:block}.account-card:hover{transform:translateY(-3px);border-color:#8b5cf6}.remove-form{margin-top:14px;padding-top:12px;border-top:1px solid #334155}.remove-form button{width:100%;border:1px solid #7f1d1d;border-radius:9px;padding:9px;background:#3f1721;color:#fecaca;cursor:pointer}.identity{display:flex;gap:14px;align-items:center}.avatar{width:72px;height:72px;border-radius:50%;object-fit:cover;background:#27344d;border:2px solid #475569}.avatar-fallback{display:grid;place-items:center;font-size:1.5rem;font-weight:700}.name{font-size:1.15rem;font-weight:750}.handle,.muted{color:#94a3b8}.facts{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:16px 0}.fact{background:#0f172a;border-radius:10px;padding:9px;text-align:center}.fact strong{display:block}.delta{font-size:.68em;margin-left:.2em}.delta-up{color:#4ade80}.delta-down{color:#fb7185}.row{display:flex;justify-content:space-between;gap:12px;margin-top:8px}.value{overflow-wrap:anywhere;text-align:right}.ok{color:#86efac}.bad{color:#fca5a5}.services{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.service{padding:16px}@media(max-width:700px){body{padding:14px}.accounts{grid-template-columns:1fr}.manage form{grid-template-columns:1fr}}</style></head><body><main>
<h1>IG Monitor</h1><p class="muted">監控管理頁面 · {{ data.generated_at }}</p>
<section class="summary">
<div class="metric">啟用帳號<strong>{{ data.summary.accounts }}</strong></div>
<div class="metric">公開帳號<strong>{{ data.summary.public }}</strong></div>
<div class="metric">私人帳號<strong>{{ data.summary.private }}</strong></div>
<div class="metric">異常帳號<strong>{{ data.summary.error }}</strong></div>
<div class="metric">待處理媒體<strong>{{ data.summary.pending }}</strong></div>
</section>
{% if management_enabled %}
<section class="manage">
<h2>新增監控帳號</h2>
<form method="post" action="{{ url_for('add_account') }}">
  <input type="url" name="url" required placeholder="https://www.instagram.com/username/" autocomplete="off">
  <input type="text" name="label" maxlength="100" placeholder="顯示標籤（選填）">
  <button type="submit">驗證並新增</button>
</form>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<p class="muted">請輸入 Instagram 個人檔案連結，也相容舊版來源連結。新增前會實際驗證公開資料，可能需要約 45～90 秒。最多監控 16 個帳號。</p>
</section>
{% endif %}
<h2>巡檢帳號</h2>
<section class="accounts">
{% for a in data.accounts %}
<article class="account-card" data-account-id="{{ a.id }}" {% if management_enabled %}draggable="true"{% endif %}>
{% if management_enabled %}<div class="drag-handle" title="拖曳調整順序">⠿ 拖曳排序</div>{% endif %}
<a href="{{ url_for('account_detail', account_id=a.id) }}">
  <div class="identity">
    {% if a.has_avatar %}<img class="avatar" src="{{ url_for('avatar_asset', account_id=a.id) }}" alt="{{ a.label }}">
    {% else %}<div class="avatar avatar-fallback">{{ (a.username or a.label or '?')[0]|upper }}</div>{% endif %}
    <div><div class="name">{{ a.display_name or a.label }}</div><div class="handle">@{{ a.username or a.label }}</div></div>
  </div>
  <div class="facts">
    <div class="fact"><strong class="{{ 'delta-up' if a.posts_delta > 0 else 'delta-down' if a.posts_delta < 0 else '' }}">{{ a.posts }}{% if a.posts_delta %}<span class="delta">({{ '+' if a.posts_delta > 0 else '' }}{{ a.posts_delta }})</span>{% endif %}</strong>發文</div>
    <div class="fact"><strong class="{{ 'delta-up' if a.followers_delta > 0 else 'delta-down' if a.followers_delta < 0 else '' }}">{{ a.followers }}{% if a.followers_delta %}<span class="delta">({{ '+' if a.followers_delta > 0 else '' }}{{ a.followers_delta }})</span>{% endif %}</strong>跟隨者</div>
    <div class="fact"><strong class="{{ 'delta-up' if a.following_delta > 0 else 'delta-down' if a.following_delta < 0 else '' }}">{{ a.following }}{% if a.following_delta %}<span class="delta">({{ '+' if a.following_delta > 0 else '' }}{{ a.following_delta }})</span>{% endif %}</strong>追蹤中</div>
  </div>
  <div class="row"><span>狀態</span><span class="{{ 'bad' if a.fail_count >= 3 else 'ok' }}">{{ a.privacy }} / {{ a.fail_count }} 次失敗</span></div>
  <div class="row"><span>Profile ID</span><span class="value">{{ a.instagram_profile_id or '尚未建立' }}</span></div>
  <div class="row"><span>媒體</span><span>{{ a.downloaded }} 已下載 / {{ a.pending }} 待處理</span></div>
</a>
{% if management_enabled %}
<form class="remove-form" method="post" action="{{ url_for('toggle_relationship_tracking', account_id=a.id) }}">
  <input type="hidden" name="enabled" value="{{ 0 if a.relationship_tracking else 1 }}">
  <button type="submit">名單巡檢：{{ '開啟' if a.relationship_tracking else '關閉' }}</button>
</form>
<form class="remove-form" method="post" action="{{ url_for('remove_account', account_id=a.id) }}" onsubmit="return confirm('確定停止監控這個帳號？既有照片與影片會保留。')">
  <button type="submit">移除監控</button>
</form>
{% endif %}
</article>
{% else %}<p class="muted">尚無巡檢帳號資料。</p>{% endfor %}
</section>
<h2>服務狀態</h2><section class="services">
<div class="service">巡檢服務：<strong>{{ data.services.monitor }}</strong></div>
<div class="service">排程器：<strong>{{ data.services.timer }}</strong></div>
<div class="service">下次排程：<span>{{ data.services.next_run }}</span></div>
</section>
{% if management_enabled %}
<script>
const accountList=document.querySelector('.accounts');let dragged=null;
accountList?.querySelectorAll('.account-card').forEach(card=>{
  card.addEventListener('dragstart',event=>{dragged=card;card.classList.add('dragging');event.dataTransfer.effectAllowed='move'});
  card.addEventListener('dragend',()=>{card.classList.remove('dragging');dragged=null});
});
accountList?.addEventListener('dragover',event=>{
  event.preventDefault();const target=event.target.closest('.account-card');
  if(!dragged||!target||target===dragged)return;
  const box=target.getBoundingClientRect();
  const before=event.clientY<box.top+box.height/2||(Math.abs(event.clientY-(box.top+box.height/2))<box.height/3&&event.clientX<box.left+box.width/2);
  accountList.insertBefore(dragged,before?target:target.nextSibling);
});
accountList?.addEventListener('drop',async event=>{
  event.preventDefault();
  const account_ids=[...accountList.querySelectorAll('.account-card')].map(card=>Number(card.dataset.accountId));
  const response=await fetch('{{ url_for("reorder_accounts") }}',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account_ids})});
  if(!response.ok){alert('排序儲存失敗，頁面將重新整理。');location.reload()}
});
</script>
{% endif %}
</main></body></html>"""


DETAIL_PAGE = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ account.display_name or account.label }} · IG Monitor</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{font-family:system-ui,-apple-system,sans-serif;background:#0b1120;color:#e5e7eb;margin:0;padding:24px}main{max-width:1200px;margin:auto}a{color:#c4b5fd;text-decoration:none}.profile{display:flex;gap:18px;align-items:center;background:#172033;border:1px solid #27344d;border-radius:16px;padding:20px}.avatar{width:96px;height:96px;border-radius:50%;object-fit:cover;background:#27344d}.avatar-fallback{display:grid;place-items:center;font-size:2rem;font-weight:700}.muted{color:#94a3b8}.stats{display:flex;gap:18px;flex-wrap:wrap;margin-top:10px}.stats strong{display:block;font-size:1.25rem}.delta{margin-left:4px;font-size:.8em}.delta-up{color:#4ade80}.delta-down{color:#fb7185}.meta{background:#172033;border-radius:12px;padding:16px;margin:16px 0;overflow-wrap:anywhere}.quarantine-alert{display:flex;justify-content:space-between;align-items:center;gap:16px;background:#422006;border:1px solid #d97706;border-radius:12px;padding:14px 16px;margin:16px 0}.quarantine-alert a{font-weight:700;white-space:nowrap}.trend{background:#172033;border-radius:12px;margin:16px 0;overflow:hidden}.trend summary{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px;cursor:pointer;font-size:1.35rem;font-weight:700;list-style:none;user-select:none}.trend summary::-webkit-details-marker{display:none}.trend summary:after{content:'展開';color:#94a3b8;font-size:.88rem;font-weight:500}.trend[open] summary{border-bottom:1px solid #334155}.trend[open] summary:after{content:'收合'}.trend-content{padding:16px}.chart-panel h3{margin-bottom:4px}.chart-panel+.chart-panel{border-top:1px solid #334155;margin-top:24px;padding-top:16px}.chart-wrap{position:relative;width:100%;height:360px}.chart-wrap canvas{display:block;width:100%;height:360px;touch-action:pan-y}.chart-legend{display:flex;gap:18px;flex-wrap:wrap;color:#cbd5e1}.legend-key:before{content:'';display:inline-block;width:12px;height:3px;margin-right:6px;vertical-align:middle;background:var(--legend-color)}.chart-tooltip{position:absolute;z-index:2;pointer-events:none;transform:translate(-50%,-100%);padding:7px 10px;border:1px solid #64748b;border-radius:8px;background:#020617;color:#f8fafc;white-space:nowrap;font-size:.88rem;box-shadow:0 6px 18px #0008}.chart-tooltip[hidden]{display:none}.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.tabs button{border:1px solid #334155;background:#172033;color:#cbd5e1;border-radius:999px;padding:9px 14px;cursor:pointer}.tabs button.active{background:#7c3aed;border-color:#8b5cf6;color:white}.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}.media{background:#172033;border-radius:14px;overflow:hidden;border:1px solid #27344d}.media[hidden]{display:none}.media img,.media video{width:100%;aspect-ratio:1/1;display:block;object-fit:cover;background:#020617}.caption{padding:10px;font-size:.85rem;color:#94a3b8}@media(max-width:600px){body{padding:14px}.profile{align-items:flex-start}.avatar{width:72px;height:72px}.gallery{grid-template-columns:repeat(2,minmax(0,1fr))}.quarantine-alert{align-items:flex-start;flex-direction:column}.chart-wrap,.chart-wrap canvas{height:300px}}\n</style></head><body><main>
<style>.media-photo{display:block;cursor:zoom-in}.media-photo:focus-visible{outline:3px solid #a78bfa;outline-offset:-3px}body.lightbox-open{overflow:hidden}.lightbox{position:fixed;inset:0;z-index:1000;display:grid;place-items:center;padding:20px}.lightbox[hidden]{display:none}.lightbox-backdrop{position:absolute;inset:0;border:0;background:#020617e8;cursor:zoom-out}.lightbox-panel{position:relative;z-index:1;width:min(1200px,96vw);height:min(92vh,900px);display:grid;grid-template-rows:auto minmax(0,1fr) auto;background:#0b1120;border:1px solid #334155;border-radius:18px;overflow:hidden;box-shadow:0 24px 80px #000}.lightbox-header{display:flex;justify-content:flex-end;padding:8px 10px}.lightbox-close,.lightbox-nav,.lightbox-slideshow{border:1px solid #475569;background:#172033;color:#f8fafc;cursor:pointer}.lightbox-close{width:42px;height:42px;border-radius:50%;font-size:1.6rem;line-height:1}.lightbox-stage{position:relative;min-height:0;display:grid;place-items:center;padding:0 70px}.lightbox-image{max-width:100%;max-height:100%;width:auto;height:auto;object-fit:contain}.lightbox-nav{position:absolute;top:50%;transform:translateY(-50%);width:50px;height:64px;border-radius:14px;font-size:2.6rem;line-height:1}.lightbox-nav.previous{left:12px}.lightbox-nav.next{right:12px}.lightbox-nav:disabled,.lightbox-slideshow:disabled{opacity:.35;cursor:not-allowed}.lightbox-footer{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:12px 18px;background:#111827}.lightbox-caption{min-width:0;color:#cbd5e1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.lightbox-controls{display:flex;align-items:center;gap:12px;flex:none}.lightbox-counter{color:#94a3b8;font-variant-numeric:tabular-nums}.lightbox-slideshow{border-radius:999px;padding:8px 14px}.lightbox button:focus-visible{outline:3px solid #a78bfa;outline-offset:2px}@media(max-width:600px){.lightbox{padding:0}.lightbox-panel{width:100vw;height:100vh;border:0;border-radius:0}.lightbox-stage{padding:0 48px}.lightbox-nav{width:40px;height:54px;font-size:2rem}.lightbox-nav.previous{left:4px}.lightbox-nav.next{right:4px}.lightbox-footer{align-items:flex-start;flex-direction:column}.lightbox-caption{white-space:normal}.lightbox-controls{width:100%;justify-content:space-between}}</style>
<style>.media img{object-fit:contain}.lightbox{padding:0}.lightbox-panel{width:100vw;height:100vh;height:100dvh;border:0;border-radius:0}.lightbox-stage{overflow:hidden}.lightbox-image{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}</style>
<p><a href="{{ url_for('index') }}">← 返回帳號列表</a></p>
<section class="profile">
{% if account.has_avatar %}<img class="avatar" src="{{ url_for('avatar_asset', account_id=account.id) }}" alt="{{ account.label }}">
{% else %}<div class="avatar avatar-fallback">{{ (account.username or account.label or '?')[0]|upper }}</div>{% endif %}
<div><h1>{{ account.display_name or account.label }}</h1><div class="muted">@{{ account.username or account.label }}</div>
<div class="stats">
<span><strong class="{{ 'delta-up' if account.posts_delta > 0 else 'delta-down' if account.posts_delta < 0 else '' }}">{{ account.posts }}{% if account.posts_delta %}<span class="delta">({{ '+' if account.posts_delta > 0 else '' }}{{ account.posts_delta }})</span>{% endif %}</strong>發文</span>
<span><strong class="{{ 'delta-up' if account.followers_delta > 0 else 'delta-down' if account.followers_delta < 0 else '' }}">{{ account.followers }}{% if account.followers_delta %}<span class="delta">({{ '+' if account.followers_delta > 0 else '' }}{{ account.followers_delta }})</span>{% endif %}</strong>跟隨者</span>
<span><strong class="{{ 'delta-up' if account.following_delta > 0 else 'delta-down' if account.following_delta < 0 else '' }}">{{ account.following }}{% if account.following_delta %}<span class="delta">({{ '+' if account.following_delta > 0 else '' }}{{ account.following_delta }})</span>{% endif %}</strong>追蹤中</span>
</div></div>
</section>
<section class="meta"><div>Instagram Profile ID：{{ account.instagram_profile_id or '尚未建立' }}</div><div>有效網址：{{ account.effective_url }}</div>{% if account.bio %}<p>{{ account.bio }}</p>{% endif %}<p><a href="{{ url_for('account_relationships', account_id=account.id) }}">Followers／Following／共同名單／異動紀錄</a></p></section>
{% if account.quarantined_videos %}<section class="quarantine-alert"><span>已隱藏疑似來源污染影片，不影響原始檔案。</span><a href="{{ url_for('account_quarantine', account_id=account.id) }}">隔離影片 {{ account.quarantined_videos }} →</a></section>{% endif %}
<details class="trend" id="social-trends">
<summary><span>社群趨勢</span></summary>
<div class="trend-content">
<p class="muted">最近 90 個台灣日期；X 軸為日期，Y 軸為數量。</p>
<article class="chart-panel"><h3>貼文數量</h3><div class="chart-legend"><span class="legend-key" style="--legend-color:#a78bfa">貼文</span></div><div class="chart-wrap"><canvas id="posts-history-chart" role="img" aria-label="貼文數量趨勢圖"></canvas><div id="posts-history-tooltip" class="chart-tooltip" role="status" hidden></div></div></article>
<article class="chart-panel"><h3>跟隨者與追蹤中</h3><div class="chart-legend"><span class="legend-key" style="--legend-color:#4ade80">跟隨者</span><span class="legend-key" style="--legend-color:#38bdf8">追蹤中</span></div><div class="chart-wrap"><canvas id="relationships-history-chart" role="img" aria-label="跟隨者與追蹤中數量趨勢圖"></canvas><div id="relationships-history-tooltip" class="chart-tooltip" role="status" hidden></div></div></article>
</div>
</details>
<h2>照片與影片</h2>
<p class="muted">匿名來源：AnonyIG（正式環境驗證中）。此處僅顯示已保存的本地媒體；分類巡檢成功不代表正式媒體下載已啟用。時間顯示：台北時間（UTC+08:00）。</p>
<style>.gallery-time{display:inline-block;white-space:nowrap}.collection-times{display:flex;flex-wrap:wrap;gap:4px 18px}.collection-times>span{display:inline-block}</style>
<style>.lightbox-nav{z-index:1}.carousel-controls{display:flex;justify-content:space-between;gap:8px;padding:6px 10px}.carousel-controls button{font:inherit;font-size:.8rem;border:1px solid #475569;border-radius:8px;background:#172033;color:#e5e7eb;padding:7px 9px;cursor:pointer}.carousel-controls button:disabled{opacity:.35;cursor:not-allowed}.carousel-controls button:focus-visible{outline:3px solid #a78bfa;outline-offset:2px}</style>
<style>.gallery-entry[hidden],.collection-state[hidden],.gallery-empty[hidden]{display:none}.group-heading{padding:14px 14px 0;margin:0;overflow-wrap:anywhere}.group-caption{white-space:pre-wrap;overflow-wrap:anywhere}.media-children{display:flex;overflow-x:auto;scroll-snap-type:x mandatory;gap:2px}.media-child{flex:0 0 100%;min-width:0;scroll-snap-align:start}.media-child img,.media-child video{object-fit:contain}.media-position{padding:5px 10px;color:#cbd5e1;font-size:.8rem}.collection-state{background:#172033;border:1px solid #334155;border-radius:12px;padding:14px;margin:12px 0;overflow-wrap:anywhere}.collection-state[data-state="partial"],.collection-state[data-state="blocked"],.collection-state[data-state="error"],.collection-state[data-state="unknown"]{border-color:#b45309}.collection-state p{margin:6px 0}.gallery-empty{grid-column:1/-1}.album-contents{padding-top:12px}.album-contents summary{cursor:pointer;padding:0 14px 12px}.legacy-note{margin:0 0 12px}@media(max-width:600px){.gallery{grid-template-columns:1fr}}</style>
<nav class="tabs source-tabs">
<button class="active" data-source="posts" aria-pressed="true">貼文 {{ account.gallery_counts.posts.all }}</button>
<button data-source="stories" aria-pressed="false">限時動態 {{ account.gallery_counts.stories.all }}</button>
<button data-source="highlights" aria-pressed="false">精選動態 {{ account.gallery_counts.highlights.all }}</button>
<button data-source="reels" aria-pressed="false">Reels 短片 {{ account.gallery_counts.reels.all }}</button>
<button data-source="legacy" aria-pressed="false">舊版媒體 {{ account.gallery_counts.legacy.all }}</button>
</nav>
{% for category, observation in account.collection_observations.items() %}
<section class="collection-state" data-status-source="{{ category }}" data-state="{{ observation.display_state }}" {% if category != 'posts' %}hidden{% endif %}>
<strong>{{ observation.label }}</strong><p>{{ observation.message }}</p>
<p class="muted collection-times"><span>最後成功：{% if observation.last_success_at %}<time class="gallery-time" datetime="{{ observation.last_success_at }}">{{ observation.last_success_at|taipei_time }}</time>{% else %}尚未成功{% endif %}</span><span>最後嘗試：{% if observation.last_attempt_at %}<time class="gallery-time" datetime="{{ observation.last_attempt_at }}">{{ observation.last_attempt_at|taipei_time }}</time>{% else %}尚未嘗試{% endif %}</span></p>
{% if observation.error %}<p>原因：{{ observation.error }}</p>{% endif %}
</section>{% endfor %}
<p class="muted legacy-note" data-status-source="legacy" hidden>保留原分類與檔案；缺少可靠貼文或專輯歸屬的舊資料不推測分組。</p>
<p class="muted">數量以貼文、限時動態、精選專輯或短片為單位；照片／影片篩選會保留符合條件卡片的全部內容與原始順序。</p>
<nav class="tabs kind-tabs">
<button class="active" data-kind="all" aria-pressed="true">全部</button>
<button data-kind="image" aria-pressed="false">照片</button>
<button data-kind="video" aria-pressed="false">影片</button>
</nav>
<section class="gallery">
{% for group in account.gallery %}<article class="media gallery-entry" data-collection="{{ group.category }}" data-sources="{{ group.categories|join(' ') }}" data-kinds="{{ group.kinds|join(' ') }}" data-group-id="{{ group.group_id }}" {% if group.category != 'posts' %}hidden{% endif %}>
{% if group.category == 'highlights' %}<h3 class="group-heading">{{ group.album_title or '未命名精選專輯' }}</h3><details class="album-contents" open><summary>專輯內容 · 本地已下載 {{ group.children|length }} 個媒體</summary>{% endif %}
<div class="media-children" aria-label="依來源順序排列的媒體">
{% for item in group.children %}<div class="media-child" data-media-id="{{ item.id }}" data-position="{{ item.position }}">
{% if item.kind == 'video' %}<video controls preload="metadata" src="{{ url_for('media_asset', media_id=item.id) }}" aria-label="{{ '精選動態' if group.category == 'highlights' else '影片' }} {{ loop.index }}"></video>
{% else %}<a class="media-photo" href="{{ url_for('media_asset', media_id=item.id) }}"><img loading="lazy" src="{{ url_for('media_asset', media_id=item.id) }}" alt="{{ group.album_title or '照片' }} {{ loop.index }}"></a>{% endif %}
{% if group.children|length > 1 %}<div class="media-position">本地 {{ loop.index }} / {{ group.children|length }} · 來源第 {{ item.position + 1 }} 項 · 左右滑動瀏覽</div>{% endif %}
</div>{% endfor %}</div>
{% if group.children|length > 1 %}<nav class="carousel-controls" aria-label="輪播媒體切換"><button type="button" data-carousel-step="-1" disabled>← 上一項</button><button type="button" data-carousel-step="1">下一項 →</button></nav>{% endif %}
{% if group.category == 'highlights' %}</details>{% endif %}
<div class="caption"><div>{{ group.category_labels|join(' · ') }}{% if group.published_at %} · <time class="gallery-time" datetime="{{ group.published_at }}">{{ group.published_at|taipei_time }}</time>{% endif %}</div>{% if group.caption %}<div class="group-caption">{{ group.caption }}</div>{% endif %}</div>
</article>
{% endfor %}
<p class="muted gallery-empty" id="gallery-empty" role="status" hidden></p>
</section>
<div class="lightbox" id="photo-lightbox" role="dialog" aria-modal="true" aria-label="照片檢視器" hidden>
<button class="lightbox-backdrop" type="button" data-lightbox-action="close" aria-label="關閉照片檢視器"></button>
<section class="lightbox-panel">
<header class="lightbox-header"><button class="lightbox-close" type="button" data-lightbox-action="close" aria-label="關閉">×</button></header>
<div class="lightbox-stage">
<button class="lightbox-nav previous" type="button" data-lightbox-action="previous" aria-label="上一張照片">‹</button>
<img class="lightbox-image" id="lightbox-image" alt="">
<button class="lightbox-nav next" type="button" data-lightbox-action="next" aria-label="下一張照片">›</button>
</div>
<footer class="lightbox-footer"><div class="lightbox-caption" id="lightbox-caption"></div><div class="lightbox-controls"><span class="lightbox-counter" id="lightbox-counter" aria-live="polite"></span><button class="lightbox-slideshow" id="lightbox-slideshow" type="button" aria-pressed="false">播放投影片</button></div></footer>
</section>
</div>
<script>
let selectedSource='posts',selectedKind='all';
const galleryCounts={{ account.gallery_counts|tojson }};
const collectionObservations={{ account.collection_observations|tojson }};
function updateCarouselControls(card){
 const track=card.querySelector('.media-children'),previous=card.querySelector('[data-carousel-step="-1"]'),next=card.querySelector('[data-carousel-step="1"]');
 if(!previous||!next||!track.clientWidth)return;
 previous.disabled=track.scrollLeft<=2;next.disabled=track.scrollLeft+track.clientWidth>=track.scrollWidth-2;
}
document.querySelectorAll('.gallery-entry').forEach(card=>{
 const track=card.querySelector('.media-children');
 card.querySelectorAll('[data-carousel-step]').forEach(button=>button.addEventListener('click',()=>{
  const width=track.firstElementChild.getBoundingClientRect().width+2;
  track.scrollBy({left:Number(button.dataset.carouselStep)*width,behavior:'smooth'});
 }));
 track.addEventListener('scroll',()=>updateCarouselControls(card),{passive:true});
 card.querySelector('details')?.addEventListener('toggle',()=>updateCarouselControls(card));
});
window.addEventListener('resize',()=>document.querySelectorAll('.gallery-entry:not([hidden])').forEach(updateCarouselControls));
function filterMedia(){
 let visible=0;
 document.querySelectorAll('.gallery-entry').forEach(el=>{
  const sourceMatch=el.dataset.collection===selectedSource;
  const kindMatch=selectedKind==='all'||el.dataset.kinds.split(' ').includes(selectedKind);
  el.hidden=!(sourceMatch&&kindMatch);
  if(!el.hidden){visible++;updateCarouselControls(el)}
  else el.querySelectorAll('video').forEach(video=>video.pause());
 });
 document.querySelectorAll('[data-status-source]').forEach(el=>{el.hidden=el.dataset.statusSource!==selectedSource});
 const c=galleryCounts[selectedSource];
 document.querySelector('.kind-tabs [data-kind="all"]').textContent=`全部 ${c.all}`;
 document.querySelector('.kind-tabs [data-kind="image"]').textContent=`照片 ${c.image}`;
 document.querySelector('.kind-tabs [data-kind="video"]').textContent=`影片 ${c.video}`;
 const empty=document.getElementById('gallery-empty');empty.hidden=visible>0;
 empty.textContent=selectedKind!=='all'&&c.all?'目前沒有符合此媒體類型的卡片。':selectedSource==='legacy'?'目前沒有未分組的舊版媒體。':collectionObservations[selectedSource].empty_message;
}
document.querySelectorAll('[data-source]').forEach(button=>button.addEventListener('click',()=>{
 selectedSource=button.dataset.source;
 document.querySelectorAll('[data-source]').forEach(x=>{x.classList.toggle('active',x===button);x.setAttribute('aria-pressed',String(x===button))});
 filterMedia();
}));
document.querySelectorAll('.kind-tabs [data-kind]').forEach(button=>button.addEventListener('click',()=>{
 selectedKind=button.dataset.kind;
 document.querySelectorAll('.kind-tabs [data-kind]').forEach(x=>{x.classList.toggle('active',x===button);x.setAttribute('aria-pressed',String(x===button))});
 filterMedia();
}));
filterMedia();
const lightbox=document.getElementById('photo-lightbox');
const lightboxImage=document.getElementById('lightbox-image');
const lightboxCaption=document.getElementById('lightbox-caption');
const lightboxCounter=document.getElementById('lightbox-counter');
const slideshowButton=document.getElementById('lightbox-slideshow');
const previousButton=lightbox.querySelector('[data-lightbox-action="previous"]');
const nextButton=lightbox.querySelector('[data-lightbox-action="next"]');
let lightboxIndex=0,slideshowTimer=null,lightboxReturnFocus=null;
function visiblePhotos(){return [...document.querySelectorAll('.media:not([hidden]) .media-photo')].filter(photo=>!photo.closest('details')||photo.closest('details').open)}
function stopSlideshow(){
 if(slideshowTimer){clearInterval(slideshowTimer);slideshowTimer=null}
 slideshowButton.textContent='播放投影片';
 slideshowButton.setAttribute('aria-pressed','false');
}
function renderLightbox(index){
 const photos=visiblePhotos();
 if(!photos.length){closeLightbox();return}
 lightboxIndex=(index%photos.length+photos.length)%photos.length;
 const photo=photos[lightboxIndex],thumbnail=photo.querySelector('img');
 lightboxImage.src=photo.href;
 lightboxImage.alt=thumbnail?.alt||'照片';
 lightboxCaption.textContent=photo.closest('.media')?.querySelector('.caption')?.textContent.trim()||'';
 lightboxCounter.textContent=`${lightboxIndex+1} / ${photos.length}`;
 const single=photos.length<2;
 previousButton.disabled=single;nextButton.disabled=single;slideshowButton.disabled=single;
 if(single)stopSlideshow();
 if(!single){const preload=new Image();preload.src=photos[(lightboxIndex+1)%photos.length].href}
}
function openLightbox(photo){
 const photos=visiblePhotos(),index=photos.indexOf(photo);
 if(index<0)return;
 lightboxReturnFocus=photo;lightbox.hidden=false;document.body.classList.add('lightbox-open');
 renderLightbox(index);lightbox.querySelector('.lightbox-close').focus();
}
function closeLightbox(){
 if(lightbox.hidden)return;
 stopSlideshow();lightbox.hidden=true;lightboxImage.removeAttribute('src');document.body.classList.remove('lightbox-open');
 lightboxReturnFocus?.focus();lightboxReturnFocus=null;
}
function changePhoto(step){renderLightbox(lightboxIndex+step)}
function toggleSlideshow(){
 if(slideshowButton.disabled)return;
 if(slideshowTimer){stopSlideshow();return}
 slideshowButton.textContent='暫停投影片';slideshowButton.setAttribute('aria-pressed','true');
 slideshowTimer=setInterval(()=>changePhoto(1),4000);
}
document.querySelectorAll('.media-photo').forEach(photo=>photo.addEventListener('click',event=>{event.preventDefault();openLightbox(photo)}));
lightbox.querySelectorAll('[data-lightbox-action="close"]').forEach(button=>button.addEventListener('click',closeLightbox));
previousButton.addEventListener('click',()=>changePhoto(-1));
nextButton.addEventListener('click',()=>changePhoto(1));
slideshowButton.addEventListener('click',toggleSlideshow);
document.addEventListener('keydown',event=>{
 if(lightbox.hidden)return;
 if(event.key==='Escape')closeLightbox();
 else if(event.key==='ArrowLeft')changePhoto(-1);
 else if(event.key==='ArrowRight')changePhoto(1);
 else if(event.key===' '){event.preventDefault();toggleSlideshow()}
});
document.addEventListener('visibilitychange',()=>{if(document.hidden)stopSlideshow()});
const profileHistory={{ account.history|tojson }};
const chartAxes={{ account.chart_axes|tojson }};
const chartConfigs=[
 {canvasId:'posts-history-chart',tooltipId:'posts-history-tooltip',axis:chartAxes.posts,series:[{key:'posts',label:'貼文',color:'#a78bfa'}],points:[],hoveredIndex:null},
 {canvasId:'relationships-history-chart',tooltipId:'relationships-history-tooltip',axis:chartAxes.relationships,series:[{key:'followers',label:'跟隨者',color:'#4ade80'},{key:'following',label:'追蹤中',color:'#38bdf8'}],points:[],hoveredIndex:null}
];
function drawProfileHistory(config){
 const canvas=document.getElementById(config.canvasId);
 if(!canvas||!profileHistory.length)return;
 const rect=canvas.getBoundingClientRect(),ratio=window.devicePixelRatio||1;
 const width=Math.max(320,rect.width),height=rect.height||360;
 canvas.width=Math.round(width*ratio);canvas.height=Math.round(height*ratio);
 const ctx=canvas.getContext('2d');ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,width,height);
 const pad={left:60,right:18,top:18,bottom:48},plotW=width-pad.left-pad.right,plotH=height-pad.top-pad.bottom;
 const min=config.axis.min,max=config.axis.max;
 const x=index=>pad.left+(profileHistory.length===1?plotW/2:index*plotW/(profileHistory.length-1));
 const y=value=>pad.top+(max-value)*plotH/(max-min);
 ctx.font='12px system-ui';ctx.textBaseline='middle';ctx.fillStyle='#94a3b8';ctx.strokeStyle='#334155';ctx.lineWidth=1;
 config.axis.ticks.slice().reverse().forEach(value=>{const yy=y(value);ctx.beginPath();ctx.moveTo(pad.left,yy);ctx.lineTo(width-pad.right,yy);ctx.stroke();ctx.textAlign='right';ctx.fillText(value.toLocaleString(),pad.left-8,yy)});
 const labelStep=Math.max(1,Math.ceil(profileHistory.length/6));ctx.textAlign='center';ctx.textBaseline='top';
 profileHistory.forEach((point,index)=>{if(index%labelStep===0||index===profileHistory.length-1)ctx.fillText(point.date.slice(5),x(index),height-pad.bottom+10)});
 config.points=profileHistory.map((point,index)=>({x:x(index),point,values:config.series.map(series=>({series,value:Number(point[series.key]),y:y(Number(point[series.key]))}))}));
 config.series.forEach((series,seriesIndex)=>{ctx.strokeStyle=series.color;ctx.fillStyle=series.color;ctx.lineWidth=2.5;ctx.beginPath();config.points.forEach((item,index)=>{const yy=item.values[seriesIndex].y;index?ctx.lineTo(item.x,yy):ctx.moveTo(item.x,yy)});ctx.stroke();config.points.forEach((item,index)=>{ctx.beginPath();ctx.arc(item.x,item.values[seriesIndex].y,index===config.hoveredIndex?6:profileHistory.length===1?4:3,0,Math.PI*2);ctx.fill()})});
}
function hideChartPoint(config){config.hoveredIndex=null;document.getElementById(config.tooltipId).hidden=true;drawProfileHistory(config)}
function showChartPoint(event,config){
 if(!config.points.length)return;
 const canvas=event.currentTarget,rect=canvas.getBoundingClientRect(),pointerX=event.clientX-rect.left;
 const index=config.points.reduce((best,item,current)=>Math.abs(item.x-pointerX)<Math.abs(config.points[best].x-pointerX)?current:best,0);
 if(Math.abs(config.points[index].x-pointerX)>32){hideChartPoint(config);return}
 config.hoveredIndex=index;drawProfileHistory(config);const item=config.points[index],tooltip=document.getElementById(config.tooltipId);
 tooltip.textContent=`${item.point.date}　${item.values.map(value=>`${value.series.label}：${value.value.toLocaleString()}`).join('　')}`;
 tooltip.style.left=`${item.x}px`;tooltip.style.top=`${Math.max(44,Math.min(...item.values.map(value=>value.y))-8)}px`;tooltip.hidden=false;
}
const socialTrends=document.getElementById('social-trends');
chartConfigs.forEach(config=>{const canvas=document.getElementById(config.canvasId);canvas.addEventListener('pointermove',event=>showChartPoint(event,config));canvas.addEventListener('pointerdown',event=>showChartPoint(event,config));canvas.addEventListener('pointerleave',()=>hideChartPoint(config))});
function redrawSocialTrends(){chartConfigs.forEach(config=>{document.getElementById(config.tooltipId).hidden=true;config.hoveredIndex=null;drawProfileHistory(config)})}
socialTrends.addEventListener('toggle',()=>{if(socialTrends.open)requestAnimationFrame(redrawSocialTrends)});
if(socialTrends.open)requestAnimationFrame(redrawSocialTrends);
window.addEventListener('resize',()=>{if(socialTrends.open)redrawSocialTrends()});
</script>
</main></body></html>"""


QUARANTINE_PAGE = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ account.label }} · 隔離影片審核</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}body{font-family:system-ui,-apple-system,sans-serif;background:#0b1120;color:#e5e7eb;margin:0;padding:24px}main{max-width:1200px;margin:auto}a{color:#c4b5fd;text-decoration:none}.muted{color:#94a3b8}.notice{background:#422006;border:1px solid #d97706;border-radius:12px;padding:14px 16px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px;margin-top:20px}.item{background:#172033;border:1px solid #334155;border-radius:14px;overflow:hidden}.item video{display:block;width:100%;aspect-ratio:9/16;max-height:520px;object-fit:contain;background:#020617}.details{padding:14px}.details p{margin:6px 0;overflow-wrap:anywhere}.actions{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px}.actions button{border-radius:9px;padding:10px;border:1px solid #475569;color:white;cursor:pointer}.keep{background:#166534}.delete{background:#7f1d1d}.missing{display:grid;place-items:center;aspect-ratio:9/16;max-height:520px;background:#020617;color:#fca5a5}@media(max-width:600px){body{padding:14px}.grid{grid-template-columns:1fr}.actions{grid-template-columns:1fr}}</style>
</head><body><main><p><a href="{{ url_for('account_detail', account_id=account.id) }}">← {{ account.label }}</a></p>
<h1>隔離影片審核</h1><p class="notice">這些影片已從一般相簿隱藏，但檔案仍完整保留。「保留」會恢復顯示並加入人工信任；「永久刪除」才會移除檔案。</p>
<section class="grid">{% for item in media %}<article class="item">
{% if item.has_file %}<video controls preload="metadata" src="{{ url_for('media_asset',media_id=item.id) }}#t=0.1"></video>{% else %}<div class="missing">尚未下載或檔案不存在</div>{% endif %}
<div class="details"><strong>媒體 #{{ item.id }}</strong>
<p>{{ item.categories|join(' · ') }}{% if item.published_at %} · {{ item.published_at }}{% endif %}</p>
<p>{{ item.width or '?' }} × {{ item.height or '?' }}{% if item.video_duration is not none %} · {{ '%.1f'|format(item.video_duration) }} 秒{% endif %}{% if item.file_size %} · {{ item.file_size }} bytes{% endif %}</p>
<p class="muted">比對：{{ item.signal }}<br>同組帳號：{{ item.accounts|join('、') }}<br>隔離時間：{{ item.quarantined_at }}</p>
{% if management_enabled %}<div class="actions"><form method="post" action="{{ url_for('keep_quarantined_media',media_id=item.id) }}"><button class="keep" type="submit">保留此影片</button></form><form method="post" action="{{ url_for('delete_quarantined_media_route',media_id=item.id) }}" onsubmit="return confirm('永久刪除此影片檔案？這個動作無法復原。')"><button class="delete" type="submit">永久刪除此影片</button></form></div>{% endif %}
</div></article>{% else %}<p class="muted">目前沒有待審核的隔離影片。</p>{% endfor %}</section>
</main></body></html>"""


RELATIONSHIP_PAGE = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ account.label }} 名單</title>
<style>:root{color-scheme:dark}body{font-family:system-ui;background:#0b1120;color:#e5e7eb;margin:0;padding:24px}main{max-width:1100px;margin:auto}a{color:#c4b5fd;text-decoration:none}.tabs{display:flex;gap:8px;flex-wrap:wrap}.tabs a{padding:9px 14px;background:#172033;border-radius:999px}.tabs .active{background:#7c3aed;color:white}form{display:flex;gap:8px;margin:18px 0}input,select,button{padding:10px;border-radius:9px;border:1px solid #334155;background:#172033;color:#e5e7eb}table{width:100%;border-collapse:collapse;background:#172033;border-radius:12px;overflow:hidden}th,td{text-align:left;padding:10px;border-bottom:1px solid #334155}.avatar{width:42px;height:42px;object-fit:cover;border-radius:50%;background:#27344d;vertical-align:middle}.avatar-placeholder{display:inline-flex;align-items:center;justify-content:center;color:#94a3b8;font-size:20px}.muted{color:#94a3b8}.pager{display:flex;justify-content:space-between;margin-top:15px}@media(max-width:700px){body{padding:12px}table{font-size:.82rem}.optional{display:none}}</style></head><body><main>
<p><a href="{{ url_for('account_detail', account_id=account.id) }}">← {{ account.label }}</a></p>
<h1>關係名單</h1><p class="muted">整體：{{ account.relationship_status }}　Followers：{{ account.followers_state }}（{{ account.followers_baseline_at or '-' }}）　Following：{{ account.following_state }}（{{ account.following_baseline_at or '-' }}）</p>
<nav class="tabs">{% for value,label in [('followers','Followers'),('following','Following'),('mutual','共同名單'),('history','異動紀錄')] %}<a class="{{ 'active' if tab==value else '' }}" href="{{ url_for('account_relationships',account_id=account.id,tab=value) }}">{{ label }}</a>{% endfor %}</nav>
<form method="get"><input type="hidden" name="tab" value="{{ tab }}"><input name="q" value="{{ q }}" placeholder="搜尋 username／名稱"><select name="filter"><option value="current" {{ 'selected' if filter_value=='current' else '' }}>目前</option><option value="left" {{ 'selected' if filter_value=='left' else '' }}>已退出</option><option value="all" {{ 'selected' if filter_value=='all' else '' }}>全部</option></select><button>搜尋</button></form>
<table><thead><tr><th>帳號</th><th class="optional">Profile ID</th><th>狀態／時間</th></tr></thead><tbody>{% for row in rows %}<tr><td>{% if row.has_avatar %}<img class="avatar" src="{{ url_for('relationship_member_avatar',profile_id=row.instagram_profile_id) }}" loading="lazy">{% else %}<span class="avatar avatar-placeholder" aria-label="頭像等待補充">◎</span>{% endif %} <a href="{{ url_for('relationship_member_detail',profile_id=row.instagram_profile_id) }}">@{{ row.username }}</a><br><span class="muted">{{ row.display_name or '' }}</span></td><td class="optional">{{ row.instagram_profile_id }}</td><td>{{ row.change_kind or ('目前' if row.active else '已退出') }}<br><span class="muted">{{ row.observed_at or row.last_seen_at or '-' }}</span></td></tr>{% else %}<tr><td colspan="3">目前沒有資料</td></tr>{% endfor %}</tbody></table>
<div class="pager">{% if page>1 %}<a href="{{ url_for('account_relationships',account_id=account.id,tab=tab,q=q,filter=filter_value,page=page-1) }}">← 上一頁</a>{% else %}<span></span>{% endif %}<span>第 {{ page }} 頁</span>{% if has_next %}<a href="{{ url_for('account_relationships',account_id=account.id,tab=tab,q=q,filter=filter_value,page=page+1) }}">下一頁 →</a>{% endif %}</div>
</main></body></html>"""


MEMBER_PAGE = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>@{{ member.username }}</title><style>:root{color-scheme:dark}body{font-family:system-ui;background:#0b1120;color:#e5e7eb;padding:24px}main{max-width:800px;margin:auto}a{color:#c4b5fd}.card{background:#172033;border-radius:16px;padding:20px}.avatar{width:96px;height:96px;border-radius:50%;object-fit:cover;background:#27344d}.avatar-placeholder{display:flex;align-items:center;justify-content:center;color:#94a3b8;font-size:36px}.muted{color:#94a3b8}</style></head><body><main><p><a href="javascript:history.back()">← 返回</a></p><section class="card">{% if member.has_avatar %}<img class="avatar" src="{{ url_for('relationship_member_avatar',profile_id=member.instagram_profile_id) }}">{% else %}<div class="avatar avatar-placeholder" aria-label="頭像等待補充">◎</div>{% endif %}<h1>@{{ member.username }}</h1><p>{{ member.display_name or '' }}</p><p>Profile ID：{{ member.instagram_profile_id }}</p><p>貼文 {{ member.posts or 0 }}　Followers {{ member.followers or 0 }}　Following {{ member.following or 0 }}</p><p>{{ member.bio or '' }}</p><p class="muted">最後補資料：{{ member.profile_observed_at or '尚未' }}　隱私：{{ member.privacy or 'unknown' }}</p></section></main></body></html>"""


def _systemctl_status(command: list[str]) -> str:
    try:
        output = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False).stdout.strip()
        return output or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def system_status() -> dict[str, str]:
    if os.getenv("IG_MONITOR_RUNTIME") == "docker":
        return {
            "monitor": "Docker Compose",
            "timer": "內建排程器",
            "next_run": "依 config.yaml 的 interval_minutes",
        }
    timer_rows = _systemctl_status(["systemctl", "list-timers", "--all", "ig-monitor.timer", "--no-pager"]).splitlines()
    next_run = timer_rows[-1] if len(timer_rows) > 1 else "unknown"
    return {
        "monitor": _systemctl_status(["systemctl", "is-active", "ig-monitor.service"]),
        "timer": _systemctl_status(["systemctl", "is-active", "ig-monitor.timer"]),
        "next_run": next_run,
    }


def validate_account_page(config_path: Path, url: str) -> None:
    async def validate() -> None:
        from .scraper import ProfileScraper

        config = load_config(config_path, require_telegram=False, require_apify=False)
        async with ProfileScraper(config.browser) as scraper:
            await scraper.scrape(url)

    try:
        asyncio.run(validate())
    except Exception as exc:
        raise ValueError(f"網址驗證失敗：{exc}") from exc


def dashboard_data(db_path: Path, status_provider: Callable[[], dict[str, str]] = system_status) -> dict[str, Any]:
    summary = {"accounts": 0, "public": 0, "private": 0, "error": 0, "pending": 0}
    accounts: list[dict[str, Any]] = []
    if db_path.is_file():
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute("""
                SELECT id,label,url,effective_url,instagram_profile_id,snapshot_json,fail_count,last_error,
                       last_success_at,relationship_tracking,relationship_status,relationship_reconciled_at
                FROM accounts WHERE enabled=1 ORDER BY sort_order,id
            """).fetchall()
            summary["accounts"] = len(rows)
            for row in rows:
                deltas = _latest_profile_deltas(connection, row["id"])
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
                    "posts_delta": deltas["posts"],
                    "followers_delta": deltas["followers"],
                    "following_delta": deltas["following"],
                    "has_avatar": bool(snapshot.get("avatar_path") and Path(snapshot["avatar_path"]).is_file()),
                    "instagram_profile_id": row["instagram_profile_id"],
                    "effective_url": row["effective_url"] or row["url"], "last_success_at": row["last_success_at"],
                    "fail_count": int(row["fail_count"] or 0), "last_error": row["last_error"],
                    "downloaded": int(media.get("downloaded", 0)), "pending": pending,
                    "relationship_tracking": bool(row["relationship_tracking"]),
                    "relationship_status": row["relationship_status"],
                    "relationship_reconciled_at": row["relationship_reconciled_at"],
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
            SELECT id,label,url,effective_url,instagram_profile_id,snapshot_json,
                   relationship_status,followers_baseline_at,following_baseline_at
            FROM accounts WHERE id=? AND enabled=1
        """, (account_id,)).fetchone()
        if row is None:
            return None, [], _empty_collection_counts()
        snapshot = json.loads(row["snapshot_json"]) if row["snapshot_json"] else {}
        deltas = _latest_profile_deltas(connection, account_id)
        history = _daily_profile_history(connection, account_id)
        account = {
            "id": row["id"], "label": row["label"], "username": snapshot.get("username"),
            "display_name": snapshot.get("display_name"), "posts": snapshot.get("posts", 0),
            "followers": snapshot.get("followers", 0), "following": snapshot.get("following", 0),
            "posts_delta": deltas["posts"],
            "followers_delta": deltas["followers"],
            "following_delta": deltas["following"],
            "history": history,
            "chart_axes": {
                "posts": _chart_axis([point["posts"] for point in history]),
                "relationships": _chart_axis([
                    value for point in history
                    for value in (point["followers"], point["following"])
                ]),
            },
            "bio": snapshot.get("bio"), "instagram_profile_id": row["instagram_profile_id"],
            "effective_url": row["effective_url"] or row["url"],
            "relationship_status": row["relationship_status"],
            "followers_baseline_at": row["followers_baseline_at"],
            "following_baseline_at": row["following_baseline_at"],
            "has_avatar": bool(snapshot.get("avatar_path") and Path(snapshot["avatar_path"]).is_file()),
            "quarantined_videos": int(connection.execute(
                """SELECT COUNT(*) FROM media m JOIN media_quarantine mq ON mq.media_id=m.id
                   WHERE m.account_id=? AND m.kind='video' AND m.status='quarantined'
                     AND mq.decision='pending'""",
                (account_id,),
            ).fetchone()[0]),
        }
        media_rows = connection.execute("""
            SELECT m.id,m.kind,m.published_at,m.local_path,GROUP_CONCAT(ms.category) AS categories
            FROM media m JOIN media_sources ms ON ms.media_id=m.id
            WHERE m.account_id=? AND m.status='downloaded' AND m.duplicate_of_id IS NULL
              AND m.local_path IS NOT NULL
              AND NOT EXISTS(SELECT 1 FROM media_quarantine mq
                             WHERE mq.media_id=m.id AND mq.decision='pending')
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
        from .anonymous_store import read_collection_observations, read_gallery_memberships

        account["gallery"], account["gallery_counts"] = _group_gallery(
            media, read_gallery_memberships(connection, account_id)
        )
        account["collection_observations"] = _collection_statuses(
            read_collection_observations(connection, account_id, source="anonyig")
        )
        return account, media, counts
    finally:
        connection.close()


def account_quarantine_data(
    db_path: Path, account_id: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not db_path.is_file():
        return None, []
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        account_row = connection.execute(
            "SELECT id,label FROM accounts WHERE id=? AND enabled=1", (account_id,)
        ).fetchone()
        if account_row is None:
            return None, []
        rows = connection.execute("""
            SELECT m.id,m.kind,m.published_at,m.local_path,m.width,m.height,
                   m.file_size,m.video_duration,mq.signal,mq.signal_value,mq.quarantined_at,
                   GROUP_CONCAT(ms.category) AS categories
            FROM media m JOIN media_quarantine mq ON mq.media_id=m.id
            LEFT JOIN media_sources ms ON ms.media_id=m.id
            WHERE m.account_id=? AND m.kind='video' AND m.status='quarantined'
              AND mq.decision='pending'
            GROUP BY m.id,m.kind,m.published_at,m.local_path,m.width,m.height,m.file_size,
                     m.video_duration,mq.signal,mq.signal_value,mq.quarantined_at
            ORDER BY mq.quarantined_at DESC,m.id DESC
        """, (account_id,)).fetchall()
        media = []
        for row in rows:
            shared_accounts = [item[0] for item in connection.execute("""
                SELECT DISTINCT a.label
                FROM media_quarantine mq
                JOIN media m ON m.id=mq.media_id
                JOIN accounts a ON a.id=m.account_id
                WHERE mq.signal=? AND mq.signal_value=? AND mq.decision='pending'
                ORDER BY a.label
            """, (row["signal"], row["signal_value"])).fetchall()]
            path = Path(row["local_path"]) if row["local_path"] else None
            media.append({
                "id": row["id"], "kind": row["kind"],
                "published_at": row["published_at"], "width": row["width"],
                "height": row["height"], "file_size": row["file_size"],
                "video_duration": row["video_duration"], "signal": row["signal"],
                "quarantined_at": row["quarantined_at"], "accounts": shared_accounts,
                "categories": sorted({
                    _collection_name(value) for value in (row["categories"] or "").split(",")
                    if value
                }),
                "has_file": bool(path and path.is_file()),
            })
        return dict(account_row), media
    finally:
        connection.close()


def _chart_axis(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"min": 0, "max": 4, "ticks": [0, 1, 2, 3, 4]}
    data_min, data_max = min(values), max(values)
    span = data_max - data_min
    if span < 4:
        missing = 4 - span
        lower = max(0, data_min - math.ceil(missing / 2))
        upper = max(data_max, lower + 4)
        lower = max(0, upper - 4)
        return {"min": lower, "max": upper, "ticks": list(range(lower, upper + 1))}
    rough_step = span / 4
    magnitude = 10 ** math.floor(math.log10(rough_step))
    normalized = rough_step / magnitude
    multiplier = 1 if normalized <= 1 else 2 if normalized <= 2 else 5 if normalized <= 5 else 10
    step = max(1, int(multiplier * magnitude))
    lower = max(0, math.floor(data_min / step) * step)
    upper = math.ceil(data_max / step) * step
    ticks = list(range(lower, upper + step, step))
    return {"min": lower, "max": upper, "ticks": ticks}


def _latest_profile_deltas(connection: sqlite3.Connection, account_id: int) -> dict[str, int]:
    rows = connection.execute(
        """SELECT posts,followers,following FROM profile_history
           WHERE account_id=? ORDER BY observed_at DESC,id DESC LIMIT 2""",
        (account_id,),
    ).fetchall()
    if len(rows) < 2:
        return {"posts": 0, "followers": 0, "following": 0}
    return {
        field: int(rows[0][field]) - int(rows[1][field])
        for field in ("posts", "followers", "following")
    }


def _daily_profile_history(
    connection: sqlite3.Connection, account_id: int, limit: int = 90
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """WITH ranked AS (
             SELECT date(datetime(observed_at), '+8 hours') AS local_date,
                    posts,followers,following,observed_at,id,
                    ROW_NUMBER() OVER (
                      PARTITION BY date(datetime(observed_at), '+8 hours')
                      ORDER BY observed_at DESC,id DESC
                    ) AS row_number
             FROM profile_history WHERE account_id=?
           )
           SELECT local_date,posts,followers,following FROM (
             SELECT local_date,posts,followers,following FROM ranked
             WHERE row_number=1 ORDER BY local_date DESC LIMIT ?
           ) ORDER BY local_date""",
        (account_id, limit),
    ).fetchall()
    return [
        {
            "date": row["local_date"], "posts": row["posts"],
            "followers": row["followers"], "following": row["following"],
        }
        for row in rows
    ]


def relationship_page_data(
    db_path: Path, account_id: int, tab: str, query: str, filter_value: str, page: int
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], bool]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        account_row = connection.execute(
            """SELECT id,label,relationship_status,followers_baseline_at,following_baseline_at,snapshot_json
               FROM accounts WHERE id=? AND enabled=1""", (account_id,)
        ).fetchone()
        if account_row is None:
            return None, [], False
        account = dict(account_row)
        snapshot = json.loads(account.pop("snapshot_json")) if account.get("snapshot_json") else {}
        for direction, count_field, baseline_field in (
            ("followers", "followers", "followers_baseline_at"),
            ("following", "following", "following_baseline_at"),
        ):
            latest = connection.execute(
                """SELECT status FROM relationship_runs WHERE account_id=? AND direction=?
                   ORDER BY id DESC LIMIT 1""", (account_id, direction)
            ).fetchone()
            if int(snapshot.get(count_field, 0)) > 1000:
                state = "scope_exceeded"
            elif latest:
                state = latest["status"]
            elif account.get(baseline_field):
                state = "complete"
            else:
                state = "not_requested"
            account[f"{direction}_state"] = state
        pattern = f"%{query.strip()}%"
        offset = (page - 1) * 50
        if tab == "history":
            rows = connection.execute(
                """SELECT h.instagram_profile_id,h.username,h.change_kind,h.observed_at,
                          NULL AS active,NULL AS last_seen_at,m.display_name,m.avatar_url,m.avatar_path
                   FROM relationship_history h LEFT JOIN relationship_members m
                     ON m.instagram_profile_id=h.instagram_profile_id
                   WHERE h.account_id=? AND (h.username LIKE ? OR m.display_name LIKE ?)
                   ORDER BY h.observed_at DESC,h.id DESC LIMIT 51 OFFSET ?""",
                (account_id, pattern, pattern, offset),
            ).fetchall()
        elif tab == "mutual":
            rows = connection.execute(
                """SELECT f.instagram_profile_id,f.username,f.active,f.last_seen_at,
                          NULL AS change_kind,NULL AS observed_at,m.display_name,m.avatar_url,m.avatar_path
                   FROM account_relationships f JOIN account_relationships g
                     ON g.account_id=f.account_id AND g.instagram_profile_id=f.instagram_profile_id
                    AND g.direction='following' AND g.active=1
                   JOIN relationship_members m ON m.instagram_profile_id=f.instagram_profile_id
                   WHERE f.account_id=? AND f.direction='followers' AND f.active=1
                     AND (f.username LIKE ? OR m.display_name LIKE ?)
                   ORDER BY f.username LIMIT 51 OFFSET ?""",
                (account_id, pattern, pattern, offset),
            ).fetchall()
        else:
            active_clause = ""
            if filter_value == "current":
                active_clause = "AND ar.active=1"
            elif filter_value == "left":
                active_clause = "AND ar.active=0"
            rows = connection.execute(
                f"""SELECT ar.instagram_profile_id,ar.username,ar.active,ar.last_seen_at,
                           NULL AS change_kind,NULL AS observed_at,m.display_name,m.avatar_url,m.avatar_path
                    FROM account_relationships ar JOIN relationship_members m
                      ON m.instagram_profile_id=ar.instagram_profile_id
                    WHERE ar.account_id=? AND ar.direction=? {active_clause}
                      AND (ar.username LIKE ? OR m.display_name LIKE ?)
                    ORDER BY ar.username LIMIT 51 OFFSET ?""",
                (account_id, tab, pattern, pattern, offset),
            ).fetchall()
        items = [dict(row) for row in rows[:50]]
        for item in items:
            path = Path(item["avatar_path"]) if item.get("avatar_path") else None
            item["has_avatar"] = bool(path and path.is_file())
        return account, items, len(rows) > 50
    finally:
        connection.close()


def relationship_member_data(db_path: Path, profile_id: str) -> dict[str, Any] | None:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM relationship_members WHERE instagram_profile_id=?", (profile_id,)
        ).fetchone()
        if row is None:
            return None
        member = dict(row)
        path = Path(member["avatar_path"]) if member.get("avatar_path") else None
        member["has_avatar"] = bool(path and path.is_file())
        return member
    finally:
        connection.close()


def _empty_collection_counts() -> dict[str, dict[str, int]]:
    return {
        name: {"all": 0, "image": 0, "video": 0}
        for name in ("posts", "stories", "highlights", "reels", "legacy")
    }


def _collection_name(value: str) -> str:
    lowered = value.strip().lower()
    if "highlight" in lowered:
        return "highlights"
    if "stor" in lowered:
        return "stories"
    if "reel" in lowered:
        return "reels"
    if lowered in {"post", "posts"}:
        return "posts"
    return "legacy"


_COLLECTION_LABELS = {
    "posts": "貼文", "stories": "限時動態", "highlights": "精選動態",
    "reels": "Reels 短片", "legacy": "舊版媒體",
}


def _format_taipei_time(value: str | None) -> str:
    """Format aware observation timestamps without changing their stored/API values."""
    if not value:
        return "—"
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if observed.tzinfo is None:
        # Legacy data without an offset do not establish a timezone.
        return value
    # Instagram-era Taipei uses UTC+8; avoid an extra Windows tzdata dependency.
    taipei = timezone(timedelta(hours=8), "Asia/Taipei")
    return observed.astimezone(taipei).strftime("%Y-%m-%d %H:%M")


def _group_gallery(
    media: list[dict[str, Any]], memberships: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    """Keep content membership separate from the canonical file and legacy metadata."""
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    represented: dict[int, set[str]] = {}
    seen: set[tuple[str, str, str, str, int, int]] = set()
    for membership in memberships:
        category = _collection_name(membership["category"])
        group_id = membership.get("group_id")
        if category == "legacy" or not group_id:
            continue
        path = membership.get("local_path")
        if not path or not Path(path).is_file():
            continue
        source = membership["source"]
        key = (source, category, str(group_id))
        media_id = int(membership["media_id"])
        position = int(membership.get("position") or 0)
        source_media_id = str(membership.get("source_media_id") or "")
        identity = (*key, source_media_id, position, media_id)
        if identity in seen:
            continue
        seen.add(identity)
        group = groups.setdefault(key, {
            "category": category, "categories": [category], "source": source,
            "group_id": str(group_id), "album_title": None, "caption": None,
            "published_at": None, "children": [],
        })
        for field in ("album_title", "caption", "published_at"):
            if not group[field] and membership.get(field):
                group[field] = membership[field]
        group["children"].append({
            "id": media_id, "kind": membership["kind"], "position": position,
            "source_media_id": source_media_id,
        })
        represented.setdefault(media_id, set()).add(category)

    gallery = list(groups.values())
    for group in gallery:
        group["children"].sort(key=lambda item: (item["position"], item["source_media_id"], item["id"]))
    gallery.sort(key=lambda group: (group["published_at"] or "", group["group_id"]), reverse=True)
    for item in media:
        categories = sorted(set(item["categories"]) - represented.get(item["id"], set()))
        if not categories:
            continue
        gallery.append({
            "category": "legacy", "categories": categories, "source": "legacy",
            "group_id": f"legacy-{item['id']}", "album_title": None, "caption": None,
            "published_at": item.get("published_at"),
            "children": [{"id": item["id"], "kind": item["kind"], "position": 0}],
        })

    counts = _empty_collection_counts()
    for group in gallery:
        group["kinds"] = sorted({child["kind"] for child in group["children"]})
        group["category_labels"] = [_COLLECTION_LABELS[value] for value in group["categories"]]
        counts[group["category"]]["all"] += 1
        for kind in group["kinds"]:
            counts[group["category"]][kind] += 1
    return gallery, counts


def _collection_statuses(observations: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    labels = {
        "media": "已更新", "complete": "已更新", "empty": "來源確認無內容",
        "private": "私人帳號", "partial": "部分完成", "blocked": "來源受阻",
        "error": "巡檢失敗", "unknown": "尚未確認",
    }
    messages = {
        "media": "來源內容已更新，仍保留先前保存的資料。",
        "complete": "來源內容已更新，仍保留先前保存的資料。",
        "empty": "來源本次確認無內容，先前保存的資料不會因此刪除。",
        "private": "來源回報私人帳號，先前保存的資料仍保留。",
        "partial": "本次尚未完整取得此分類；已保存成功部分，先前資料仍保留。",
        "blocked": "來源遇驗證或限流，系統將自動退避重試；不視為空集合。",
        "error": "本次取得失敗，先前資料仍保留；不視為空集合。",
        "unknown": "尚無完整巡檢結果，目前只顯示已保存內容。",
    }
    result = {}
    for category in ("posts", "stories", "highlights", "reels"):
        observation = observations.get(category, {})
        state = observation.get("state", "unknown")
        if state == "failed":
            state = "error"
        if state not in labels:
            state = "unknown"
        if observation.get("error") and state in {"media", "complete", "empty", "private"}:
            state = "partial" if state in {"media", "complete"} else "error"
        elif state in {"media", "complete", "empty"} and not observation.get("complete", False):
            state = "partial"
        success = state in {"media", "complete", "empty", "private"}
        result[category] = {
            "state": observation.get("state", "unknown"), "display_state": state,
            "label": f"{_COLLECTION_LABELS[category]}：{labels[state]}",
            "message": messages[state], "error": observation.get("error"),
            "last_success_at": observation.get("last_success_at"),
            "last_attempt_at": observation.get("last_attempt_at"),
            "empty_message": (
                "目前沒有可顯示的本地已下載媒體；來源結果與下載狀態請見上方說明。"
                if success else "目前沒有可顯示的本地媒體；此分類尚未完整成功，不能判定來源沒有內容。"
            ),
        }
    return result


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


def _relationship_member_avatar_path(db_path: Path, profile_id: str) -> Path | None:
    if not db_path.is_file():
        return None
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT avatar_path FROM relationship_members WHERE instagram_profile_id=?",
            (profile_id,),
        ).fetchone()
        path = Path(row[0]) if row and row[0] else None
        return path if path and path.is_file() else None
    finally:
        connection.close()


def _media_path(db_path: Path, media_id: int) -> Path | None:
    if not db_path.is_file():
        return None
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = connection.execute("""
            SELECT local_path FROM media WHERE id=?
              AND status IN ('downloaded','quarantined') AND local_path IS NOT NULL
        """, (media_id,)).fetchone()
        path = Path(row[0]) if row else None
        return path if path and path.is_file() else None
    finally:
        connection.close()


def _require_same_origin() -> None:
    origin = request.headers.get("Origin")
    if not origin:
        return
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.casefold() != request.host.casefold():
        abort(403)


def create_app(
    db_path: Path,
    status_provider: Callable[[], dict[str, str]] = system_status,
    *,
    config_path: Path | None = None,
    account_validator: AccountValidator | None = None,
) -> Flask:
    app = Flask(__name__)
    app.jinja_env.filters["taipei_time"] = _format_taipei_time
    if config_path is not None and account_validator is None:
        account_validator = lambda url: validate_account_page(config_path, url)
    registry = (
        AccountRegistry(config_path, db_path, account_validator)
        if config_path is not None and account_validator is not None
        else None
    )

    @app.after_request
    def no_store(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return render_template_string(
            CARD_PAGE,
            data=dashboard_data(db_path, status_provider),
            management_enabled=registry is not None,
            error=None,
        )

    @app.post("/accounts")
    def add_account():
        if registry is None:
            abort(404)
        _require_same_origin()
        try:
            registry.add(request.form.get("url", ""), request.form.get("label"))
        except ValueError as exc:
            return render_template_string(
                CARD_PAGE,
                data=dashboard_data(db_path, status_provider),
                management_enabled=True,
                error=str(exc),
            ), 400
        return redirect(url_for("index"), code=303)

    @app.post("/accounts/<int:account_id>/remove")
    def remove_account(account_id: int):
        if registry is None:
            abort(404)
        _require_same_origin()
        try:
            registry.remove(account_id)
        except ValueError as exc:
            return render_template_string(
                CARD_PAGE,
                data=dashboard_data(db_path, status_provider),
                management_enabled=True,
                error=str(exc),
            ), 400
        return redirect(url_for("index"), code=303)

    @app.post("/accounts/<int:account_id>/relationship-tracking")
    def toggle_relationship_tracking(account_id: int):
        if registry is None:
            abort(404)
        _require_same_origin()
        try:
            registry.set_relationship_tracking(account_id, request.form.get("enabled") == "1")
        except ValueError as exc:
            return str(exc), 400
        return redirect(url_for("index"), code=303)

    @app.post("/accounts/reorder")
    def reorder_accounts():
        if registry is None:
            abort(404)
        _require_same_origin()
        payload = request.get_json(silent=True) or {}
        account_ids = payload.get("account_ids")
        if not isinstance(account_ids, list) or any(type(value) is not int for value in account_ids):
            return {"error": "排序格式錯誤"}, 400
        try:
            registry.reorder(account_ids)
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return "", 204

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/account/<int:account_id>")
    def account_detail(account_id: int):
        account, media, counts = account_detail_data(db_path, account_id)
        if account is None:
            abort(404)
        return render_template_string(DETAIL_PAGE, account=account, media=media, counts=counts)

    @app.get("/account/<int:account_id>/quarantine")
    def account_quarantine(account_id: int):
        account, media = account_quarantine_data(db_path, account_id)
        if account is None:
            abort(404)
        return render_template_string(
            QUARANTINE_PAGE, account=account, media=media,
            management_enabled=registry is not None,
        )

    @app.post("/media/<int:media_id>/quarantine/keep")
    def keep_quarantined_media(media_id: int):
        if registry is None:
            abort(404)
        _require_same_origin()
        from .db import Database
        writable = Database(db_path)
        try:
            row = writable.conn.execute(
                "SELECT account_id FROM media WHERE id=? AND status='quarantined'", (media_id,)
            ).fetchone()
            if row is None:
                abort(404)
            account_id = int(row["account_id"])
            try:
                restored = restore_quarantined_media(writable, media_id)
            except FileNotFoundError as exc:
                return str(exc), 409
            if not restored:
                abort(404)
        finally:
            writable.close()
        return redirect(url_for("account_quarantine", account_id=account_id), code=303)

    @app.post("/media/<int:media_id>/quarantine/delete")
    def delete_quarantined_media_route(media_id: int):
        if registry is None:
            abort(404)
        _require_same_origin()
        from .db import Database
        writable = Database(db_path)
        try:
            row = writable.conn.execute(
                "SELECT account_id FROM media WHERE id=? AND status='quarantined'", (media_id,)
            ).fetchone()
            if row is None:
                abort(404)
            account_id = int(row["account_id"])
            result = delete_quarantined_media(writable, media_id)
            if not result["deleted"]:
                abort(404)
            if result["error"]:
                return f"資料已移除，但檔案刪除失敗：{result['error']}", 500
        finally:
            writable.close()
        return redirect(url_for("account_quarantine", account_id=account_id), code=303)

    @app.get("/account/<int:account_id>/relationships")
    def account_relationships(account_id: int):
        tab = request.args.get("tab", "followers")
        if tab not in {"followers", "following", "mutual", "history"}:
            abort(400)
        filter_value = request.args.get("filter", "current")
        if filter_value not in {"current", "left", "all"}:
            abort(400)
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            abort(400)
        query = request.args.get("q", "")[:100]
        account, rows, has_next = relationship_page_data(
            db_path, account_id, tab, query, filter_value, page
        )
        if account is None:
            abort(404)
        return render_template_string(
            RELATIONSHIP_PAGE, account=account, rows=rows, has_next=has_next,
            tab=tab, q=query, filter_value=filter_value, page=page,
        )

    @app.get("/relationship-member/<profile_id>")
    def relationship_member_detail(profile_id: str):
        member = relationship_member_data(db_path, profile_id)
        if member is None:
            abort(404)
        if config_path is not None:
            from datetime import UTC, datetime, timedelta
            config = load_config(config_path, require_telegram=False, require_apify=False)
            observed = (
                datetime.fromisoformat(member["profile_observed_at"])
                if member.get("profile_observed_at") else None
            )
            now = datetime.now(UTC)
            if observed is None or now - observed >= timedelta(
                days=config.instagram_enrichment.member_stale_days
            ):
                from .db import Database
                writable = Database(db_path)
                try:
                    writable.enqueue_member_enrichment(profile_id, "manual", now.isoformat(timespec="seconds"))
                finally:
                    writable.close()
        return render_template_string(MEMBER_PAGE, member=member)

    @app.get("/account/<int:account_id>/avatar")
    def avatar_asset(account_id: int):
        path = _avatar_path(db_path, account_id)
        if path is None:
            abort(404)
        return send_file(path, conditional=True)

    @app.get("/relationship-member/<profile_id>/avatar")
    def relationship_member_avatar(profile_id: str):
        path = _relationship_member_avatar_path(db_path, profile_id)
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
    from waitress import serve

    parser = argparse.ArgumentParser(description="IG Monitor dashboard")
    parser.add_argument("--db", default="data/state.sqlite3")
    parser.add_argument("--config", default=None, help="Enable account management with this config.yaml")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8888)
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve() if args.config else None
    serve(
        create_app(Path(args.db).expanduser().resolve(), config_path=config_path),
        host=args.host,
        port=args.port,
        threads=4,
    )


if __name__ == "__main__":
    main()
