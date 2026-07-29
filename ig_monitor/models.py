from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class PrivacyState(StrEnum):
    PRIVATE = "private"
    PUBLIC = "public"
    UNKNOWN = "unknown"


class TerminalState(StrEnum):
    PRIVATE = "private"
    MEDIA = "media"
    EMPTY = "empty"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class MediaCandidate:
    media_key: str
    category: str
    kind: str
    url: str
    logical_id: str | None = None
    position: int = 0
    published_at: str | None = None
    width: int | None = None
    height: int | None = None
    source_rank: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProfileSnapshot:
    username: str
    display_name: str | None
    posts: int
    followers: int
    following: int
    bio: str
    privacy: PrivacyState
    avatar_url: str
    avatar_sha256: str | None = None
    avatar_path: str | None = None
    observed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["privacy"] = self.privacy.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProfileSnapshot":
        copied = dict(data)
        copied["privacy"] = PrivacyState(copied["privacy"])
        return cls(**copied)


@dataclass(slots=True)
class ScrapeResult:
    snapshot: ProfileSnapshot
    media: list[MediaCandidate] = field(default_factory=list)
    posts_state: TerminalState = TerminalState.UNKNOWN
    stories_state: TerminalState = TerminalState.UNKNOWN


@dataclass(slots=True)
class ScrapeFailure(Exception):
    reason: str
    stage: str
    html: str | None = None
    screenshot: bytes | None = None
    blocker: str | None = None

    def __str__(self) -> str:
        return f"{self.stage}: {self.reason}"


PROFILE_FIELDS = (
    "username", "display_name", "posts", "followers", "following",
    "bio", "privacy", "avatar_sha256",
)

FIELD_LABELS = {
    "username": "使用者名稱", "display_name": "顯示名稱",
    "posts": "發文篇數", "followers": "跟隨者", "following": "追蹤者",
    "bio": "自介", "privacy": "帳號狀態", "avatar_sha256": "IG 圖像",
}
