from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pyotp
from instagrapi import Client
from instagrapi import exceptions as instagram_exceptions

from .relationships import (
    CollectorFatalError,
    CollectorIdentity,
    Direction,
    MemberIdentity,
    RelationshipPage,
    RelationshipTarget,
    TargetIneligibleError,
)


_FATAL_NAMES = (
    "ChallengeRequired", "ChallengeUnknownStep", "CheckpointRequired", "LoginRequired",
    "BadPassword", "TwoFactorRequired", "FeedbackRequired", "PleaseWaitFewMinutes",
    "RateLimitError", "ClientThrottledError", "SentryBlock", "ConsentRequired",
    "AccountSuspended", "TermsConsentRequired", "GeoBlockRequired",
)
_TARGET_NAMES = ("UserNotFound", "PrivateError", "InvalidTargetUser")
FATAL_EXCEPTIONS = tuple(
    value for name in _FATAL_NAMES if isinstance((value := getattr(instagram_exceptions, name, None)), type)
)
TARGET_EXCEPTIONS = tuple(
    value for name in _TARGET_NAMES if isinstance((value := getattr(instagram_exceptions, name, None)), type)
)


class InstagrapiRelationshipSource:
    """The sole adapter allowed to know instagrapi and collector credentials."""

    def __init__(self, session_path: Path):
        self.session_path = session_path
        self.client = Client()

    def login_or_validate_saved_session(self) -> CollectorIdentity:
        username = os.getenv("IG_COLLECTOR_USERNAME", "").strip()
        password = os.getenv("IG_COLLECTOR_PASSWORD", "")
        if not username or not password:
            raise ValueError("IG_COLLECTOR_USERNAME and IG_COLLECTOR_PASSWORD are required")
        try:
            if self.session_path.is_file():
                self.client.set_settings(self.client.load_settings(self.session_path))
            secret = os.getenv("IG_COLLECTOR_TOTP_SECRET", "").replace(" ", "")
            code = pyotp.TOTP(secret).now() if secret else ""
            self.client.login(username, password, verification_code=code)
            self._save_session()
            return CollectorIdentity(str(self.client.user_id), str(self.client.username or username))
        except FATAL_EXCEPTIONS as exc:
            raise CollectorFatalError(type(exc).__name__) from exc

    def own_account_health(self) -> None:
        try:
            self._load_saved_session()
            self.client.user_info(str(self.client.user_id))
            self._save_session()
        except FATAL_EXCEPTIONS as exc:
            raise CollectorFatalError(type(exc).__name__) from exc

    def resolve_public_user(self, username: str) -> RelationshipTarget:
        try:
            self._load_saved_session()
            user = self.client.user_info_by_username(username)
            return RelationshipTarget(str(user.pk), user.username, not bool(user.is_private))
        except TARGET_EXCEPTIONS as exc:
            raise TargetIneligibleError(type(exc).__name__) from exc
        except FATAL_EXCEPTIONS as exc:
            raise CollectorFatalError(type(exc).__name__) from exc

    def iter_members(
        self, user_id: str, direction: Direction, page_size: int, limit: int
    ) -> Iterator[RelationshipPage]:
        try:
            self._load_saved_session()
            method = (
                self.client.iter_user_followers_v1
                if direction == Direction.FOLLOWERS else self.client.iter_user_following_v1
            )
            users = method(user_id, amount=limit + 1, page_size=page_size)
            page: list[MemberIdentity] = []
            for user in users:
                page.append(MemberIdentity(
                    str(user.pk), user.username, getattr(user, "full_name", None),
                    str(getattr(user, "profile_pic_url", "") or "") or None,
                ))
                if len(page) == page_size:
                    yield RelationshipPage(tuple(page), False)
                    page = []
            yield RelationshipPage(tuple(page), True)
            self._save_session()
        except TARGET_EXCEPTIONS as exc:
            raise TargetIneligibleError(type(exc).__name__) from exc
        except FATAL_EXCEPTIONS as exc:
            raise CollectorFatalError(type(exc).__name__) from exc

    def _load_saved_session(self) -> None:
        if not self.session_path.is_file():
            raise CollectorFatalError("SessionMissing")
        if not self.client.user_id:
            self.client.set_settings(self.client.load_settings(self.session_path))
            auth = self.client.get_settings().get("authorization_data") or {}
            self.client.user_id = auth.get("ds_user_id")

    def _save_session(self) -> None:
        self.session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.session_path.parent.chmod(0o700)
        except OSError:
            pass
        self.client.dump_settings(self.session_path)
        try:
            self.session_path.chmod(0o600)
        except OSError:
            pass
