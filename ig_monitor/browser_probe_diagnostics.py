"""Bounded, allowlisted aggregate diagnostics; never retain browser payloads."""
from __future__ import annotations

import re
from urllib.parse import urlsplit


_RESOURCE_KINDS = ("turnstile_script", "challenge_resource", "source_script")
_ERROR_NAMES = ("Error", "EvalError", "RangeError", "ReferenceError", "SyntaxError",
                "TypeError", "URIError", "other")
_FAILURE_KINDS = {
    "net::ERR_NAME_NOT_RESOLVED": "dns",
    "net::ERR_CERT_AUTHORITY_INVALID": "tls",
    "net::ERR_SSL_PROTOCOL_ERROR": "tls",
    "net::ERR_TIMED_OUT": "timeout",
    "net::ERR_CONNECTION_TIMED_OUT": "timeout",
    "net::ERR_BLOCKED_BY_CLIENT": "blocked",
    "net::ERR_BLOCKED_BY_RESPONSE": "blocked",
    "net::ERR_ABORTED": "aborted",
    "net::ERR_CONNECTION_RESET": "connection",
    "net::ERR_CONNECTION_REFUSED": "connection",
}


def _resource_kind(request) -> str | None:
    url = request.url
    if type(url) is not str or "\\" in url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return None
    try:
        parsed = urlsplit(url)
        # Force validation: urlsplit otherwise accepts malformed port strings.
        parsed.port
    except ValueError:
        return None
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or not host or len(host) > 253
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                   for label in host.split("."))):
        return None
    if host == "challenges.cloudflare.com":
        if parsed.path == "/turnstile/v0/api.js":
            return "turnstile_script"
        if parsed.path.startswith(("/turnstile/", "/cdn-cgi/challenge-platform/")):
            return "challenge_resource"
    if (host == "anonyig.com" or host.endswith(".anonyig.com")) and request.resource_type == "script":
        return "source_script"
    return None


class ProbeDiagnostics:
    def __init__(self) -> None:
        self._active = False
        self._observing = False
        self._context = None
        self._listeners = []
        self._resources = {
            kind: {"requested": 0, "responses": 0, "finished": 0, "failed": 0,
                   "http_statuses": set(), "failure_kinds": set()}
            for kind in _RESOURCE_KINDS
        }
        self._uncaught = dict.fromkeys(_ERROR_NAMES, 0)
        self._console = {"console_errors": 0, "console_warnings": 0}
        self._truncated = False
        self._collection_error = False

    def start(self, context) -> None:
        if self._active:
            if self._observing and context is self._context:
                return
            raise RuntimeError("ProbeDiagnostics is one-shot")
        self._context = context
        try:
            for event in ("request", "response", "requestfinished", "requestfailed", "weberror", "console"):
                def callback(value, event=event):
                    if self._observing:
                        try:
                            self._record(event, value)
                        except Exception:
                            # Do not retain or log errors from a disposed browser.
                            self._collection_error = True
                # Track before registration so even an on() failure after adding
                # a listener can be unwound without retaining the context.
                self._listeners.append((event, callback))
                context.on(event, callback)
        except Exception:
            self._collection_error = True
            self.stop()
            raise
        self._active = self._observing = True

    def stop(self) -> None:
        self._observing = False
        context, self._context = self._context, None
        listeners, self._listeners = self._listeners, []
        if context is not None:
            for event, callback in listeners:
                try:
                    context.remove_listener(event, callback)
                except Exception:
                    # A disposed emitter must not prevent the remaining cleanup.
                    self._collection_error = True

    def _increment(self, counters: dict, key: str) -> None:
        if counters[key] < 99:
            counters[key] += 1
        else:
            self._truncated = True

    def _record(self, event: str, value) -> None:
        if event == "weberror":
            name = value.error.name
            kind = name if type(name) is str and name in _ERROR_NAMES else "other"
            self._increment(self._uncaught, kind)
            return
        if event == "console":
            level = value.type
            if level == "error":
                self._increment(self._console, "console_errors")
            elif level == "warning":
                self._increment(self._console, "console_warnings")
            return
        request = value.request if event == "response" else value
        kind = _resource_kind(request)
        if kind is None:
            return
        resource = self._resources[kind]
        counter = {"request": "requested", "response": "responses", "requestfinished": "finished",
                   "requestfailed": "failed"}[event]
        self._increment(resource, counter)
        if event == "response":
            status = value.status
            if type(status) is int and 100 <= status <= 599:
                statuses = resource["http_statuses"]
                if status not in statuses:
                    if len(statuses) < 8:
                        statuses.add(status)
                    else:
                        self._truncated = True
        elif event == "requestfailed":
            failure = request.failure
            kind = _FAILURE_KINDS.get(failure, "other") if type(failure) is str else "other"
            resource["failure_kinds"].add(kind)

    def snapshot(self) -> dict:
        return {
            "active": self._active,
            "collection_error": self._collection_error,
            "resources": {
                kind: {**values, "http_statuses": sorted(values["http_statuses"]),
                       "failure_kinds": sorted(values["failure_kinds"])}
                for kind, values in self._resources.items()
            },
            "js_errors": {"uncaught": dict(self._uncaught), **self._console},
            "truncated": self._truncated,
        }
