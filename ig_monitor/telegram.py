from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .config import TelegramConfig
from .db import Database
from .models import FIELD_LABELS
from .utils import privacy_label


def event_steps(kind: str, payload: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    steps: list[tuple[str, str, str | None]] = [("text", format_event(kind, payload), None)]
    if kind == "initial" and payload.get("avatar_path"):
        steps.append(("photo", "目前的 IG 圖像", payload["avatar_path"]))
    if kind == "change" and "avatar_sha256" in payload.get("changes", {}):
        if payload.get("old_avatar_path"):
            steps.append(("photo", "IG 圖像：變更前", payload["old_avatar_path"]))
        if payload.get("new_avatar_path"):
            steps.append(("photo", "IG 圖像：變更後", payload["new_avatar_path"]))
    if kind == "media_summary":
        attachments = payload.get("attachments", [])
        photo_total = sum(1 for item in attachments if item.get("kind") == "image")
        video_total = sum(1 for item in attachments if item.get("kind") == "video")
        photo_index = video_index = 0
        for item in attachments:
            if item.get("kind") == "video":
                video_index += 1
                steps.append(("video", f"{payload.get('label', 'IG')}：新增影片 {video_index}/{video_total}", item["path"]))
            else:
                photo_index += 1
                steps.append(("photo", f"{payload.get('label', 'IG')}：新增照片 {photo_index}/{photo_total}", item["path"]))
    return steps


def _value(field: str, value: Any) -> str:
    if field == "privacy":
        return privacy_label(value)
    if field == "avatar_sha256":
        return "已變更"
    if value is None:
        return "（無）"
    text = str(value)
    return text if len(text) <= 900 else text[:897] + "..."


def format_event(kind: str, payload: dict[str, Any]) -> str:
    label = payload.get("label", "IG Monitor")
    if kind == "relationship_digest":
        direction = "Followers" if payload.get("direction") == "followers" else "Following"
        if payload.get("baseline"):
            return f"{label}：{direction} 初始名單完成\n總數：{payload.get('total', 0)}"
        interval = "（私人期間的淨異動）" if payload.get("private_interval") else ""
        lines = [
            f"{label}：{direction} 名單異動{interval}",
            f"新增：{payload.get('joined_count', 0)}　移除：{payload.get('left_count', 0)}",
        ]
        if payload.get("joined"):
            lines.append("新增帳號：" + "、".join(payload["joined"][:20]))
        if payload.get("left"):
            lines.append("移除帳號：" + "、".join(payload["left"][:20]))
        if payload.get("mutual_available"):
            lines.append(
                f"共同名單新增：{payload.get('mutual_joined_count', 0)}　移除：{payload.get('mutual_left_count', 0)}"
            )
        else:
            lines.append("共同名單：尚無另一方向的完整基準")
        if payload.get("private_interval_started_at"):
            lines.append(f"私人期間開始：{payload['private_interval_started_at']}")
        return "\n".join(lines)
    if kind == "collector_state":
        text = f"Instagram collector：{payload.get('old_state')} → {payload.get('state')}"
        if payload.get("reason"):
            text += f"\n原因分類：{payload['reason']}"
        return text
    if kind == "identity_conflict":
        return "Instagram Profile ID 來源不一致，relationship 巡檢已停止；請由 CLI 檢查。"
    if kind == "queue_stuck":
        queue = "關係名單" if payload.get("queue") == "relationship" else "成員補資料"
        label_text = f"（{payload['label']}）" if payload.get("label") else ""
        return f"{queue}佇列已超過等待上限{label_text}，請檢查 worker 與 collector 狀態。"
    if kind == "initial":
        s = payload["snapshot"]
        return "\n".join([
            f"{label}：初始載入完成",
            f"使用者名稱：{s['username']}",
            f"顯示名稱：{_value('display_name', s.get('display_name'))}",
            f"發文篇數：{s['posts']}", f"跟隨者：{s['followers']}", f"追蹤者：{s['following']}",
            f"帳號狀態：{privacy_label(s['privacy'])}", f"自介：{_value('bio', s.get('bio'))}",
        ])
    if kind == "change":
        changes = payload["changes"]
        privacy = changes.get("privacy")
        if privacy and privacy[0] == "private" and privacy[1] == "public":
            title = f"帳號 {label} 開放"
        elif privacy and privacy[1] == "private":
            title = f"帳號 {label} 已設為私人"
        else:
            title = f"{label}：資料變更"
        lines = [title]
        for field, pair in changes.items():
            if field == "avatar_sha256":
                lines.append("IG 圖像：已變更")
            else:
                lines.append(f"{FIELD_LABELS.get(field, field)}：{_value(field, pair[0])} → {_value(field, pair[1])}")
        return "\n".join(lines)
    if kind == "failure":
        blocker = f"\n判定原因：{payload['blocker']}" if payload.get("blocker") else ""
        return (f"{label}：連續無法取得（{payload.get('fail_count', 3)} 次）\n"
                f"可能改名、刪除或網站異常\n錯誤：{payload.get('error', '未知')}" + blocker)
    if kind == "recovery":
        return f"帳號 {label} 已恢復監控"
    if kind == "username_change":
        return f"{label} IG username changed\n{payload['old_username']} -> {payload['new_username']}\n(confirmed by Instagram Profile ID)"
    if kind == "apify_budget_exhausted":
        return (f"Apify monthly usage reached the {payload['cap_usd']:.2f} USD limit\n"
                "Username resolution is paused until the next Apify usage cycle.")
    if kind == "media_summary":
        return "\n".join([
            f"{label}：媒體同步完成", f"新增照片：{payload.get('photos', 0)}",
            f"新增影片：{payload.get('videos', 0)}",
            f"略過既有檔案：{payload.get('duplicate', 0)}", f"下載失敗：{payload.get('failed', 0)}",
            f"尚待下載：{payload.get('pending', 0)}",
        ])
    if kind == "heartbeat":
        return "\n".join([
            "IG Monitor 運作正常", f"監控帳號：{payload['accounts']}", f"正常：{payload['normal']}",
            f"私人：{payload['private']}", f"公開：{payload['public']}", f"異常：{payload['error']}",
            f"待下載：{payload['pending']}",
        ])
    return str(payload.get("text") or f"{label}：{kind}")


class TelegramSender:
    def __init__(self, config: TelegramConfig):
        self.config = config
        self.base = f"https://api.telegram.org/bot{config.bot_token}" if config.bot_token else ""

    def _common(self) -> dict[str, Any]:
        data: dict[str, Any] = {"chat_id": self.config.chat_id}
        if self.config.message_thread_id is not None:
            data["message_thread_id"] = self.config.message_thread_id
        return data

    async def send_test(self) -> None:
        await self._send_text("IG Monitor：Telegram 測試成功")

    async def deliver_pending(self, db: Database) -> tuple[int, int]:
        if not self.config.enabled:
            return 0, 0
        sent = failed = 0
        async with httpx.AsyncClient(timeout=30) as client:
            self._client = client
            for event in db.pending_events(self.config.retry_limit_per_run):
                try:
                    await self._deliver_event(db, event)
                    db.mark_event_sent(event["id"])
                    sent += 1
                except Exception as exc:
                    db.mark_event_failed(event["id"], str(exc))
                    failed += 1
        return sent, failed

    async def _deliver_event(self, db: Database, event: dict[str, Any]) -> None:
        payload = event["payload"]
        steps = event_steps(event["kind"], payload)
        stage = int(payload.get("delivery_stage", 0))
        while stage < len(steps):
            action, caption, path = steps[stage]
            if action == "text":
                await self._send_text(caption)
            elif action == "photo":
                await self._send_photo(Path(path), caption)
            else:
                await self._send_video(Path(path), caption)
            stage += 1
            payload["delivery_stage"] = stage
            db.update_event_payload(event["id"], payload)

    async def _send_text(self, text: str) -> None:
        client = getattr(self, "_client", None)
        owns = client is None
        if owns:
            client = httpx.AsyncClient(timeout=30)
        try:
            response = await client.post(f"{self.base}/sendMessage", data={**self._common(), "text": text[:4096]})
            self._check(response)
        finally:
            if owns:
                await client.aclose()

    async def _send_photo(self, path: Path, caption: str) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"找不到 Telegram 圖片：{path}")
        data = path.read_bytes()
        response = await self._client.post(f"{self.base}/sendPhoto", data={**self._common(), "caption": caption},
                                           files={"photo": (path.name, data, "application/octet-stream")})
        self._check(response)

    async def _send_video(self, path: Path, caption: str) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"找不到 Telegram 影片：{path}")
        data = path.read_bytes()
        response = await self._client.post(f"{self.base}/sendVideo", data={**self._common(), "caption": caption},
                                           files={"video": (path.name, data, "application/octet-stream")})
        self._check(response)

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.is_success:
            data = response.json()
            if data.get("ok"):
                return
        raise RuntimeError(f"Telegram API {response.status_code}: {response.text[:500]}")
