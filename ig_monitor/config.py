from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class AccountConfig:
    url: str
    enabled: bool
    label: str

    @property
    def key(self) -> str:
        return self.url.rstrip("/").rsplit("/", 1)[-1]


@dataclass(frozen=True, slots=True)
class PathsConfig:
    data_dir: Path
    download_root: Path
    diagnostics_dir: Path


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    headless: bool
    timeout_seconds: int
    retry_count: int
    browsers_path: Path


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    interval_minutes: int
    account_delay_min_seconds: int
    account_delay_max_seconds: int
    media_limit_per_account: int


@dataclass(frozen=True, slots=True)
class HeartbeatConfig:
    enabled: bool
    time: str
    timezone: str


@dataclass(frozen=True, slots=True)
class RetentionConfig:
    diagnostic_runs: int
    database_backups: int


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    enabled: bool
    retry_limit_per_run: int
    send_new_media: bool
    max_new_media_attachments: int
    bot_token: str | None
    chat_id: str | None
    message_thread_id: int | None


@dataclass(frozen=True, slots=True)
class ApifyConfig:
    enabled: bool
    actor_id: str
    monthly_cap_usd: float
    request_reservation_usd: float
    request_timeout_seconds: int
    token: str | None


@dataclass(frozen=True, slots=True)
class DedupConfig:
    enabled: bool
    image_phash_distance: int
    aspect_ratio_tolerance_percent: float
    video_duration_tolerance_seconds: float
    video_duration_tolerance_percent: float


@dataclass(frozen=True, slots=True)
class AppConfig:
    accounts: tuple[AccountConfig, ...]
    paths: PathsConfig
    browser: BrowserConfig
    schedule: ScheduleConfig
    heartbeat: HeartbeatConfig
    retention: RetentionConfig
    telegram: TelegramConfig
    apify: ApifyConfig
    dedup: DedupConfig
    config_path: Path


def _resolve(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _section(data: dict, name: str) -> dict:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必須是 YAML 物件")
    return value


def normalize_account_url(value: object) -> str:
    url = str(value)
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if parsed.scheme != "https" or parsed.hostname != "insta-stories-viewer.com" or len(parts) != 1:
        raise ValueError(f"帳號網址格式錯誤：{url}")
    return f"https://insta-stories-viewer.com/{parts[0]}/"


def load_config(path: str | Path, require_telegram: bool = True) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"找不到設定檔：{config_path}")
    load_dotenv(config_path.parent / ".env")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("config.yaml 最上層必須是物件")

    items = raw.get("accounts", [])
    if not isinstance(items, list) or not items:
        raise ValueError("accounts 至少需要一個帳號")
    if len(items) > 10:
        raise ValueError("accounts 最多只能設定 10 個網址")
    accounts: list[AccountConfig] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or not item.get("url"):
            raise ValueError("每個 accounts 項目都必須包含 url")
        url = normalize_account_url(item["url"])
        if url in seen:
            raise ValueError(f"重複網址：{url}")
        seen.add(url)
        key = url.rstrip("/").rsplit("/", 1)[-1]
        accounts.append(AccountConfig(url, bool(item.get("enabled", True)), str(item.get("label") or key)))

    base = config_path.parent
    paths = _section(raw, "paths")
    data_dir = _resolve(base, str(paths.get("data_dir", "./data")))
    path_cfg = PathsConfig(data_dir, _resolve(base, str(paths.get("download_root", "./downloads"))),
                           _resolve(base, str(paths.get("diagnostics_dir", "./data/diagnostics"))))

    browser = _section(raw, "browser")
    timeout = int(browser.get("timeout_seconds", 45))
    if timeout < 10:
        raise ValueError("browser.timeout_seconds 不得小於 10")
    browser_cfg = BrowserConfig(bool(browser.get("headless", True)), timeout,
                                max(0, int(browser.get("retry_count", 1))),
                                _resolve(base, str(browser.get("browsers_path", "./data/ms-playwright"))))

    schedule = _section(raw, "schedule")
    delay_min = int(schedule.get("account_delay_min_seconds", 10))
    delay_max = int(schedule.get("account_delay_max_seconds", 20))
    if delay_min < 0 or delay_max < delay_min:
        raise ValueError("帳號間隔必須滿足 0 <= min <= max")
    interval_minutes = int(schedule.get("interval_minutes", 15))
    if interval_minutes < 1:
        raise ValueError("schedule.interval_minutes must be at least 1")
    schedule_cfg = ScheduleConfig(interval_minutes, delay_min, delay_max,
                                  max(1, int(schedule.get("media_limit_per_account", 50))))

    heartbeat = _section(raw, "heartbeat")
    heartbeat_cfg = HeartbeatConfig(bool(heartbeat.get("enabled", True)), str(heartbeat.get("time", "09:00")),
                                    str(heartbeat.get("timezone", "Asia/Taipei")))
    try:
        hour, minute = (int(x) for x in heartbeat_cfg.time.split(":"))
        assert 0 <= hour <= 23 and 0 <= minute <= 59
    except (ValueError, AssertionError) as exc:
        raise ValueError("heartbeat.time 必須是 HH:MM") from exc

    retention = _section(raw, "retention")
    retention_cfg = RetentionConfig(max(1, int(retention.get("diagnostic_runs", 10))),
                                    max(1, int(retention.get("database_backups", 7))))
    telegram = _section(raw, "telegram")
    topic = os.getenv("TELEGRAM_MESSAGE_THREAD_ID", "").strip()
    telegram_cfg = TelegramConfig(bool(telegram.get("enabled", True)),
                                  max(1, int(telegram.get("retry_limit_per_run", 20))),
                                  bool(telegram.get("send_new_media", True)),
                                  max(0, int(telegram.get("max_new_media_attachments", 10))),
                                  os.getenv("TELEGRAM_BOT_TOKEN") or None,
                                  os.getenv("TELEGRAM_CHAT_ID") or None,
                                  int(topic) if topic else None)
    if require_telegram and telegram_cfg.enabled and (not telegram_cfg.bot_token or not telegram_cfg.chat_id):
        raise ValueError("Telegram 已啟用，但 .env 缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")

    apify = _section(raw, "apify")
    cap = float(apify.get("monthly_cap_usd", 5.0))
    reservation = float(apify.get("request_reservation_usd", 0.01))
    if cap <= 0 or cap > 5:
        raise ValueError("apify.monthly_cap_usd must be greater than 0 and no more than 5")
    if reservation <= 0 or reservation > cap:
        raise ValueError("apify.request_reservation_usd must be greater than 0 and no more than monthly_cap_usd")
    apify_cfg = ApifyConfig(
        enabled=bool(apify.get("enabled", False)),
        actor_id=str(apify.get("actor_id", "apify/instagram-profile-scraper")),
        monthly_cap_usd=cap,
        request_reservation_usd=reservation,
        request_timeout_seconds=max(30, int(apify.get("request_timeout_seconds", 180))),
        token=os.getenv("APIFY_API_TOKEN") or None,
    )
    if apify_cfg.enabled and not apify_cfg.token:
        raise ValueError("APIFY_API_TOKEN is required when apify.enabled is true")

    dedup = _section(raw, "media_dedup")
    phash_distance = int(dedup.get("image_phash_distance", 4))
    aspect_tolerance = float(dedup.get("aspect_ratio_tolerance_percent", 1.0))
    duration_seconds = float(dedup.get("video_duration_tolerance_seconds", 1.0))
    duration_percent = float(dedup.get("video_duration_tolerance_percent", 1.0))
    if not 0 <= phash_distance <= 16:
        raise ValueError("media_dedup.image_phash_distance must be between 0 and 16")
    if not 0 <= aspect_tolerance <= 10:
        raise ValueError("media_dedup.aspect_ratio_tolerance_percent must be between 0 and 10")
    if duration_seconds < 0 or duration_percent < 0:
        raise ValueError("media_dedup video duration tolerances cannot be negative")
    dedup_cfg = DedupConfig(
        enabled=bool(dedup.get("enabled", True)),
        image_phash_distance=phash_distance,
        aspect_ratio_tolerance_percent=aspect_tolerance,
        video_duration_tolerance_seconds=duration_seconds,
        video_duration_tolerance_percent=duration_percent,
    )

    return AppConfig(tuple(accounts), path_cfg, browser_cfg, schedule_cfg, heartbeat_cfg,
                     retention_cfg, telegram_cfg, apify_cfg, dedup_cfg, config_path)
