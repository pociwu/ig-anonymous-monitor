from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .config import ApifyConfig


class ApifyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UsageState:
    cycle_key: str
    current_usd: float
    remote_limit_usd: float | None


@dataclass(frozen=True, slots=True)
class IdentityResult:
    profile_id: str
    username: str


class ApifyClient:
    base_url = "https://api.apify.com/v2"

    def __init__(self, config: ApifyConfig):
        self.config = config

    def _params(self) -> dict[str, str]:
        if not self.config.token:
            raise ApifyError("APIFY_API_TOKEN is not configured")
        return {"token": self.config.token}

    async def usage_state(self) -> UsageState:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{self.base_url}/users/me/limits", params=self._params())
        self._raise(response)
        data = response.json().get("data", {})
        cycle = data.get("monthlyUsageCycle", {})
        start = str(cycle.get("startAt") or "")
        if not start:
            raise ApifyError("Apify limits response has no monthlyUsageCycle")
        limits = data.get("limits", {})
        current = data.get("current", {})
        remote = limits.get("maxMonthlyUsageUsd")
        return UsageState(start, float(current.get("monthlyUsageUsd", 0)), None if remote is None else float(remote))

    async def enforce_monthly_limit(self) -> None:
        state = await self.usage_state()
        if state.remote_limit_usd is not None and state.remote_limit_usd <= self.config.monthly_cap_usd:
            return
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.put(f"{self.base_url}/users/me/limits", params=self._params(),
                                        json={"maxMonthlyUsageUsd": self.config.monthly_cap_usd})
        self._raise(response)

    async def resolve(self, identifier: str) -> IdentityResult:
        actor = self.config.actor_id.replace("/", "~")
        url = f"{self.base_url}/acts/{actor}/run-sync-get-dataset-items"
        async with httpx.AsyncClient(timeout=self.config.request_timeout_seconds) as client:
            response = await client.post(url, params=self._params(), json={"usernames": [identifier]})
        self._raise(response)
        items = response.json()
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise ApifyError("Apify returned no profile data")
        item: dict[str, Any] = items[0]
        username = self._first_text(item, "username", "userName", "handle")
        profile_id = self._first_text(item, "id", "profileId", "profile_id", "pk", "userId")
        if not username or not profile_id:
            raise ApifyError("Apify response has no username or Profile ID")
        return IdentityResult(profile_id=profile_id, username=username.lstrip("@"))

    @staticmethod
    def _first_text(data: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = data.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        if response.is_success:
            return
        raise ApifyError(f"Apify API {response.status_code}: {response.text[:500]}")
