from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from flask import Flask, abort, redirect, render_template_string, request, send_file, url_for

from .account_registry import AccountRegistry, AccountValidator
from .config import load_config


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
  <input type="url" name="url" required placeholder="https://insta-stories-viewer.com/username/" autocomplete="off">
  <input type="text" name="label" maxlength="100" placeholder="顯示標籤（選填）">
  <button type="submit">驗證並新增</button>
</form>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<p class="muted">新增前會實際載入頁面；驗證可能需要約 45～90 秒。最多監控 16 個帳號。</p>
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
:root{color-scheme:dark}*{box-sizing:border-box}body{font-family:system-ui,-apple-system,sans-serif;background:#0b1120;color:#e5e7eb;margin:0;padding:24px}main{max-width:1200px;margin:auto}a{color:#c4b5fd;text-decoration:none}.profile{display:flex;gap:18px;align-items:center;background:#172033;border:1px solid #27344d;border-radius:16px;padding:20px}.avatar{width:96px;height:96px;border-radius:50%;object-fit:cover;background:#27344d}.avatar-fallback{display:grid;place-items:center;font-size:2rem;font-weight:700}.muted{color:#94a3b8}.stats{display:flex;gap:18px;flex-wrap:wrap;margin-top:10px}.stats strong{display:block;font-size:1.25rem}.delta{margin-left:4px;font-size:.8em}.delta-up{color:#4ade80}.delta-down{color:#fb7185}.meta,.trend{background:#172033;border-radius:12px;padding:16px;margin:16px 0;overflow-wrap:anywhere}.trend h2{margin-top:0}.chart-wrap{position:relative;width:100%;height:360px}.chart-wrap canvas{display:block;width:100%;height:360px}.chart-legend{display:flex;gap:18px;flex-wrap:wrap;margin-top:10px;color:#cbd5e1}.legend-key:before{content:'';display:inline-block;width:12px;height:3px;margin-right:6px;vertical-align:middle;background:var(--legend-color)}.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.tabs button{border:1px solid #334155;background:#172033;color:#cbd5e1;border-radius:999px;padding:9px 14px;cursor:pointer}.tabs button.active{background:#7c3aed;border-color:#8b5cf6;color:white}.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}.media{background:#172033;border-radius:14px;overflow:hidden;border:1px solid #27344d}.media[hidden]{display:none}.media img,.media video{width:100%;aspect-ratio:1/1;display:block;object-fit:cover;background:#020617}.caption{padding:10px;font-size:.85rem;color:#94a3b8}@media(max-width:600px){body{padding:14px}.profile{align-items:flex-start}.avatar{width:72px;height:72px}.gallery{grid-template-columns:repeat(2,minmax(0,1fr))}.chart-wrap,.chart-wrap canvas{height:300px}}\n</style></head><body><main>
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
<section class="trend">
<h2>數量趨勢</h2>
<p class="muted">最近 90 個台灣日期；X 軸為日期，Y 軸為數量。</p>
<div class="chart-wrap"><canvas id="profile-history-chart" role="img" aria-label="貼文、跟隨者與追蹤中數量趨勢圖"></canvas></div>
<div class="chart-legend"><span class="legend-key" style="--legend-color:#a78bfa">貼文</span><span class="legend-key" style="--legend-color:#4ade80">跟隨者</span><span class="legend-key" style="--legend-color:#38bdf8">追蹤中</span></div>
</section>
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
const profileHistory={{ account.history|tojson }};
const chartSeries=[
 {key:'posts',color:'#a78bfa'},
 {key:'followers',color:'#4ade80'},
 {key:'following',color:'#38bdf8'}
];
function drawProfileHistory(){
 const canvas=document.getElementById('profile-history-chart');
 if(!canvas||!profileHistory.length)return;
 const rect=canvas.getBoundingClientRect(),ratio=window.devicePixelRatio||1;
 const width=Math.max(320,rect.width),height=rect.height||360;
 canvas.width=Math.round(width*ratio);canvas.height=Math.round(height*ratio);
 const ctx=canvas.getContext('2d');ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,width,height);
 const pad={left:60,right:18,top:18,bottom:48},plotW=width-pad.left-pad.right,plotH=height-pad.top-pad.bottom;
 const values=profileHistory.flatMap(point=>chartSeries.map(series=>Number(point[series.key])));
 let min=Math.min(...values),max=Math.max(...values);
 if(min===max){min=Math.max(0,min-1);max+=1}else{const margin=(max-min)*.08;min=Math.max(0,Math.floor(min-margin));max=Math.ceil(max+margin)}
 const x=index=>pad.left+(profileHistory.length===1?plotW/2:index*plotW/(profileHistory.length-1));
 const y=value=>pad.top+(max-value)*plotH/(max-min);
 ctx.font='12px system-ui';ctx.textBaseline='middle';ctx.fillStyle='#94a3b8';ctx.strokeStyle='#334155';ctx.lineWidth=1;
 for(let index=0;index<=4;index++){const value=min+(max-min)*(4-index)/4,yy=pad.top+index*plotH;ctx.beginPath();ctx.moveTo(pad.left,yy);ctx.lineTo(width-pad.right,yy);ctx.stroke();ctx.textAlign='right';ctx.fillText(Math.round(value).toLocaleString(),pad.left-8,yy)}
 const labelStep=Math.max(1,Math.ceil(profileHistory.length/6));ctx.textAlign='center';ctx.textBaseline='top';
 profileHistory.forEach((point,index)=>{if(index%labelStep===0||index===profileHistory.length-1)ctx.fillText(point.date.slice(5),x(index),height-pad.bottom+10)});
 chartSeries.forEach(series=>{ctx.strokeStyle=series.color;ctx.fillStyle=series.color;ctx.lineWidth=2.5;ctx.beginPath();profileHistory.forEach((point,index)=>{const xx=x(index),yy=y(Number(point[series.key]));index?ctx.lineTo(xx,yy):ctx.moveTo(xx,yy)});ctx.stroke();profileHistory.forEach((point,index)=>{const xx=x(index),yy=y(Number(point[series.key]));ctx.beginPath();ctx.arc(xx,yy,profileHistory.length===1?4:2.5,0,Math.PI*2);ctx.fill()})});
}
drawProfileHistory();window.addEventListener('resize',drawProfileHistory);
</script>
</main></body></html>"""


RELATIONSHIP_PAGE = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ account.label }} 名單</title>
<style>:root{color-scheme:dark}body{font-family:system-ui;background:#0b1120;color:#e5e7eb;margin:0;padding:24px}main{max-width:1100px;margin:auto}a{color:#c4b5fd;text-decoration:none}.tabs{display:flex;gap:8px;flex-wrap:wrap}.tabs a{padding:9px 14px;background:#172033;border-radius:999px}.tabs .active{background:#7c3aed;color:white}form{display:flex;gap:8px;margin:18px 0}input,select,button{padding:10px;border-radius:9px;border:1px solid #334155;background:#172033;color:#e5e7eb}table{width:100%;border-collapse:collapse;background:#172033;border-radius:12px;overflow:hidden}th,td{text-align:left;padding:10px;border-bottom:1px solid #334155}.avatar{width:42px;height:42px;object-fit:cover;border-radius:50%;background:#27344d}.muted{color:#94a3b8}.pager{display:flex;justify-content:space-between;margin-top:15px}@media(max-width:700px){body{padding:12px}table{font-size:.82rem}.optional{display:none}}</style></head><body><main>
<p><a href="{{ url_for('account_detail', account_id=account.id) }}">← {{ account.label }}</a></p>
<h1>關係名單</h1><p class="muted">整體：{{ account.relationship_status }}　Followers：{{ account.followers_state }}（{{ account.followers_baseline_at or '-' }}）　Following：{{ account.following_state }}（{{ account.following_baseline_at or '-' }}）</p>
<nav class="tabs">{% for value,label in [('followers','Followers'),('following','Following'),('mutual','共同名單'),('history','異動紀錄')] %}<a class="{{ 'active' if tab==value else '' }}" href="{{ url_for('account_relationships',account_id=account.id,tab=value) }}">{{ label }}</a>{% endfor %}</nav>
<form method="get"><input type="hidden" name="tab" value="{{ tab }}"><input name="q" value="{{ q }}" placeholder="搜尋 username／名稱"><select name="filter"><option value="current" {{ 'selected' if filter_value=='current' else '' }}>目前</option><option value="left" {{ 'selected' if filter_value=='left' else '' }}>已退出</option><option value="all" {{ 'selected' if filter_value=='all' else '' }}>全部</option></select><button>搜尋</button></form>
<table><thead><tr><th>帳號</th><th class="optional">Profile ID</th><th>狀態／時間</th></tr></thead><tbody>{% for row in rows %}<tr><td>{% if row.avatar_url %}<img class="avatar" src="{{ row.avatar_url }}" loading="lazy">{% endif %} <a href="{{ url_for('relationship_member_detail',profile_id=row.instagram_profile_id) }}">@{{ row.username }}</a><br><span class="muted">{{ row.display_name or '' }}</span></td><td class="optional">{{ row.instagram_profile_id }}</td><td>{{ row.change_kind or ('目前' if row.active else '已退出') }}<br><span class="muted">{{ row.observed_at or row.last_seen_at or '-' }}</span></td></tr>{% else %}<tr><td colspan="3">目前沒有資料</td></tr>{% endfor %}</tbody></table>
<div class="pager">{% if page>1 %}<a href="{{ url_for('account_relationships',account_id=account.id,tab=tab,q=q,filter=filter_value,page=page-1) }}">← 上一頁</a>{% else %}<span></span>{% endif %}<span>第 {{ page }} 頁</span>{% if has_next %}<a href="{{ url_for('account_relationships',account_id=account.id,tab=tab,q=q,filter=filter_value,page=page+1) }}">下一頁 →</a>{% endif %}</div>
</main></body></html>"""


MEMBER_PAGE = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>@{{ member.username }}</title><style>:root{color-scheme:dark}body{font-family:system-ui;background:#0b1120;color:#e5e7eb;padding:24px}main{max-width:800px;margin:auto}a{color:#c4b5fd}.card{background:#172033;border-radius:16px;padding:20px}.avatar{width:96px;height:96px;border-radius:50%;object-fit:cover}.muted{color:#94a3b8}</style></head><body><main><p><a href="javascript:history.back()">← 返回</a></p><section class="card">{% if member.avatar_url %}<img class="avatar" src="{{ member.avatar_url }}">{% endif %}<h1>@{{ member.username }}</h1><p>{{ member.display_name or '' }}</p><p>Profile ID：{{ member.instagram_profile_id }}</p><p>貼文 {{ member.posts or 0 }}　Followers {{ member.followers or 0 }}　Following {{ member.following or 0 }}</p><p>{{ member.bio or '' }}</p><p class="muted">最後補資料：{{ member.profile_observed_at or '尚未' }}　隱私：{{ member.privacy or 'unknown' }}</p></section></main></body></html>"""


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
        account = {
            "id": row["id"], "label": row["label"], "username": snapshot.get("username"),
            "display_name": snapshot.get("display_name"), "posts": snapshot.get("posts", 0),
            "followers": snapshot.get("followers", 0), "following": snapshot.get("following", 0),
            "posts_delta": deltas["posts"],
            "followers_delta": deltas["followers"],
            "following_delta": deltas["following"],
            "history": _daily_profile_history(connection, account_id),
            "bio": snapshot.get("bio"), "instagram_profile_id": row["instagram_profile_id"],
            "effective_url": row["effective_url"] or row["url"],
            "relationship_status": row["relationship_status"],
            "followers_baseline_at": row["followers_baseline_at"],
            "following_baseline_at": row["following_baseline_at"],
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
                          NULL AS active,NULL AS last_seen_at,m.display_name,m.avatar_url
                   FROM relationship_history h LEFT JOIN relationship_members m
                     ON m.instagram_profile_id=h.instagram_profile_id
                   WHERE h.account_id=? AND (h.username LIKE ? OR m.display_name LIKE ?)
                   ORDER BY h.observed_at DESC,h.id DESC LIMIT 51 OFFSET ?""",
                (account_id, pattern, pattern, offset),
            ).fetchall()
        elif tab == "mutual":
            rows = connection.execute(
                """SELECT f.instagram_profile_id,f.username,f.active,f.last_seen_at,
                          NULL AS change_kind,NULL AS observed_at,m.display_name,m.avatar_url
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
                           NULL AS change_kind,NULL AS observed_at,m.display_name,m.avatar_url
                    FROM account_relationships ar JOIN relationship_members m
                      ON m.instagram_profile_id=ar.instagram_profile_id
                    WHERE ar.account_id=? AND ar.direction=? {active_clause}
                      AND (ar.username LIKE ? OR m.display_name LIKE ?)
                    ORDER BY ar.username LIMIT 51 OFFSET ?""",
                (account_id, tab, pattern, pattern, offset),
            ).fetchall()
        return account, [dict(row) for row in rows[:50]], len(rows) > 50
    finally:
        connection.close()


def relationship_member_data(db_path: Path, profile_id: str) -> dict[str, Any] | None:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM relationship_members WHERE instagram_profile_id=?", (profile_id,)
        ).fetchone()
        return dict(row) if row else None
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
