"""Offline event-boundary contracts for the probe's redacted diagnostics."""
from __future__ import annotations

import json
import gc
import weakref
from types import SimpleNamespace

import pytest

from ig_monitor.browser_probe_diagnostics import ProbeDiagnostics


class FakeContext:
    def __init__(self, *, fail_on=None):
        self.listeners = {}
        self.fail_on = fail_on

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)
        if event == self.fail_on:
            raise RuntimeError("SECRET_REGISTRATION_ERROR")

    def remove_listener(self, event, callback):
        self.listeners[event].remove(callback)

    def emit(self, event, value):
        for callback in list(self.listeners.get(event, [])):
            callback(value)


class ForbiddenFieldAccess(BaseException):
    pass


class SafeFieldsOnly:
    """Fail if the observer touches browser payload fields outside its contract."""
    def __init__(self, **fields):
        self.__dict__.update(fields)

    def __getattr__(self, name):
        raise ForbiddenFieldAccess(f"Sensitive or unavailable field accessed: {name}")


def test_empty_snapshot_is_fixed_json_and_is_independent_of_previous_snapshots():
    diagnostics = ProbeDiagnostics()
    snapshot = diagnostics.snapshot()
    assert snapshot == {
        "active": False,
        "collection_error": False,
        "resources": {
            "turnstile_script": {"requested": 0, "responses": 0, "finished": 0, "failed": 0,
                                 "http_statuses": [], "failure_kinds": []},
            "challenge_resource": {"requested": 0, "responses": 0, "finished": 0, "failed": 0,
                                   "http_statuses": [], "failure_kinds": []},
            "source_script": {"requested": 0, "responses": 0, "finished": 0, "failed": 0,
                              "http_statuses": [], "failure_kinds": []},
        },
        "js_errors": {
            "uncaught": {"Error": 0, "EvalError": 0, "RangeError": 0, "ReferenceError": 0,
                         "SyntaxError": 0, "TypeError": 0, "URIError": 0, "other": 0},
            "console_errors": 0, "console_warnings": 0,
        },
        "truncated": False,
    }
    assert json.loads(json.dumps(snapshot)) == snapshot
    snapshot["resources"]["turnstile_script"]["http_statuses"].append(200)
    snapshot["js_errors"]["uncaught"]["Error"] = 99
    assert diagnostics.snapshot()["resources"]["turnstile_script"]["http_statuses"] == []
    assert diagnostics.snapshot()["js_errors"]["uncaught"]["Error"] == 0


def test_start_stop_preserves_other_listeners_and_remembers_observation_was_enabled():
    context = FakeContext()
    foreign = lambda _event: None
    context.on("request", foreign)
    diagnostics = ProbeDiagnostics()
    diagnostics.start(context)
    diagnostics.start(context)
    assert set(context.listeners) == {
        "request", "response", "requestfinished", "requestfailed", "weberror", "console",
    }
    assert len(context.listeners["request"]) == 2
    assert diagnostics.snapshot()["active"] is True
    callbacks = [callback for values in context.listeners.values() for callback in values]
    diagnostics.stop()
    diagnostics.stop()
    assert context.listeners["request"] == [foreign]
    assert all(not values for event, values in context.listeners.items() if event != "request")
    stopped = diagnostics.snapshot()
    for callback in callbacks:
        callback(SimpleNamespace(url="https://anonyig.com/app.js", resource_type="script"))
    assert diagnostics.snapshot() == stopped
    assert stopped["active"] is True
    with pytest.raises(RuntimeError):
        diagnostics.start(FakeContext())


@pytest.mark.parametrize("failed_event", ["request", "response", "requestfinished", "requestfailed", "weberror", "console"])
def test_partial_registration_failure_removes_own_listeners_and_releases_context(failed_event):
    context = FakeContext(fail_on=failed_event)
    reference = weakref.ref(context)
    diagnostics = ProbeDiagnostics()
    with pytest.raises(RuntimeError, match="SECRET_REGISTRATION_ERROR"):
        diagnostics.start(context)
    assert all(not values for values in context.listeners.values())
    assert diagnostics.snapshot()["active"] is False
    assert diagnostics.snapshot()["collection_error"] is True
    assert "SECRET" not in json.dumps(diagnostics.snapshot())
    del context
    gc.collect()
    assert reference() is None


@pytest.mark.parametrize(("url", "resource_type"), [
    ("http://challenges.cloudflare.com/turnstile/v0/api.js", "script"),
    ("https://challenges.cloudflare.com.evil.test/turnstile/v0/api.js", "script"),
    ("https://challenges.cloudflare.com@evil.test/turnstile/v0/api.js", "script"),
    ("https://evil.test@challenges.cloudflare.com/turnstile/v0/api.js", "script"),
    ("https://challenges.cloudflare.com./turnstile/v0/api.js", "script"),
    ("https://challenges.cloudflare.com/unrelated/SECRET", "script"),
    ("https://evil.test/?url=https://challenges.cloudflare.com/turnstile/v0/api.js", "script"),
    ("https://anonyig.com.evil.test/app.js", "script"),
    ("https://notanonyig.com/app.js", "script"),
    ("https://.anonyig.com/app.js", "script"),
    ("https://cdn..anonyig.com/app.js", "script"),
    ("https://anonyig.com/api/v1/instagram/userInfo", "fetch"),
    ("https://anonyig.com/image.jpg", "image"),
    ("https://cha\nllenges.cloudflare.com/turnstile/v0/api.js", "script"),
    ("https://challenges.cloudflare.com:SECRET/turnstile/v0/api.js", "script"),
    ("https://challenges.cloudflare.com:99999/turnstile/v0/api.js", "script"),
    ("https://[invalid/turnstile/v0/api.js", "script"),
    ("https://anonyig.com\\evil.test/app.js", "script"),
    ("//anonyig.com/app.js", "script"),
    (None, "script"), (17, "script"), (b"https://anonyig.com/app.js", "script"),
])
def test_malformed_unrelated_and_spoofed_urls_are_ignored(url, resource_type):
    diagnostics, context = ProbeDiagnostics(), FakeContext()
    diagnostics.start(context)
    before = diagnostics.snapshot()
    request = SafeFieldsOnly(url=url, resource_type=resource_type)
    context.emit("request", request)
    context.emit("response", SafeFieldsOnly(request=request, status=200))
    context.emit("requestfinished", request)
    context.emit("requestfailed", request)
    assert diagnostics.snapshot() == before


def test_stop_attempts_all_listener_removals_even_if_one_removal_raises():
    class RemovalFailureContext(FakeContext):
        def remove_listener(self, event, callback):
            super().remove_listener(event, callback)
            if event == "response":
                raise RuntimeError("SECRET_REMOVAL_ERROR")

    context = RemovalFailureContext()
    reference = weakref.ref(context)
    diagnostics = ProbeDiagnostics()
    diagnostics.start(context)
    diagnostics.stop()
    assert all(not values for values in context.listeners.values())
    assert diagnostics.snapshot()["collection_error"] is True
    assert "SECRET" not in json.dumps(diagnostics.snapshot())
    diagnostics.stop()
    assert diagnostics.snapshot()["collection_error"] is True
    del context
    gc.collect()
    assert reference() is None


@pytest.mark.parametrize(("url", "resource_type", "kind"), [
    ("https://challenges.cloudflare.com/turnstile/v0/api.js?render=SECRET_QUERY", "other", "turnstile_script"),
    ("https://challenges.cloudflare.com/turnstile/v0/api.js?onload=SECRET_CALLBACK", "script", "turnstile_script"),
    ("https://challenges.cloudflare.com/turnstile/v0/widget?token=SECRET_TOKEN", "document", "challenge_resource"),
    ("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g?key=SECRET_KEY", "fetch", "challenge_resource"),
    ("https://anonyig.com/assets/SECRET_SCRIPT.js?query=SECRET_QUERY", "script", "source_script"),
    ("https://cdn.anonyig.com:443/SECRET_SCRIPT.js", "script", "source_script"),
])
def test_resource_lifecycle_is_aggregated_without_retaining_browser_objects(url, resource_type, kind):
    diagnostics, context = ProbeDiagnostics(), FakeContext()
    diagnostics.start(context)
    fields = {"url": url}
    if kind == "source_script":
        fields["resource_type"] = resource_type
    request = SafeFieldsOnly(**fields)
    reference = weakref.ref(request)
    context.emit("request", request)
    context.emit("response", SafeFieldsOnly(request=request, status=403))
    context.emit("requestfinished", request)
    snapshot = diagnostics.snapshot()
    assert snapshot["resources"][kind] == {
        "requested": 1, "responses": 1, "finished": 1, "failed": 0,
        "http_statuses": [403], "failure_kinds": [],
    }
    # requestfinished means transport finished, even when HTTP refused access.
    assert "success" not in json.dumps(snapshot)
    assert "SECRET" not in json.dumps(snapshot)
    assert "https" not in json.dumps(snapshot)
    del request
    gc.collect()
    assert reference() is None


@pytest.mark.parametrize(("failure", "kind"), [
    ("net::ERR_NAME_NOT_RESOLVED", "dns"),
    ("net::ERR_CERT_AUTHORITY_INVALID", "tls"),
    ("net::ERR_SSL_PROTOCOL_ERROR", "tls"),
    ("net::ERR_TIMED_OUT", "timeout"),
    ("net::ERR_CONNECTION_TIMED_OUT", "timeout"),
    ("net::ERR_BLOCKED_BY_CLIENT", "blocked"),
    ("net::ERR_BLOCKED_BY_RESPONSE", "blocked"),
    ("net::ERR_ABORTED", "aborted"),
    ("net::ERR_CONNECTION_RESET", "connection"),
    ("net::ERR_CONNECTION_REFUSED", "connection"),
    ("net::ERR_CERT_SECRET_NEW_CODE", "other"),
    ("net::ERR_NAME_NOT_RESOLVED SECRET_HOST", "other"),
    ("SECRET_FAILURE", "other"), (None, "other"), ({"errorText": "SECRET"}, "other"),
])
def test_network_failure_matches_only_exact_allowlisted_values(failure, kind):
    diagnostics, context = ProbeDiagnostics(), FakeContext()
    diagnostics.start(context)
    request = SafeFieldsOnly(url="https://challenges.cloudflare.com/turnstile/v0/api.js?SECRET", failure=failure)
    context.emit("request", request)
    context.emit("requestfailed", request)
    resource = diagnostics.snapshot()["resources"]["turnstile_script"]
    assert resource == {
        "requested": 1, "responses": 0, "finished": 0, "failed": 1,
        "http_statuses": [], "failure_kinds": [kind],
    }
    assert "SECRET" not in json.dumps(diagnostics.snapshot())


def test_javascript_events_read_only_error_name_and_console_type():
    diagnostics, context = ProbeDiagnostics(), FakeContext()
    diagnostics.start(context)
    for name in ("Error", "EvalError", "RangeError", "ReferenceError", "SyntaxError", "TypeError", "URIError",
                 "TypeError SECRET_MESSAGE", "SECRET_ERROR_NAME", None, ["TypeError"]):
        error = SafeFieldsOnly(name=name)
        context.emit("weberror", SafeFieldsOnly(error=error))
    reference = weakref.ref(error)
    del error
    gc.collect()
    assert reference() is None
    for level in ("error", "warning", "warn", "info", "log", "error SECRET", None, ["error"]):
        context.emit("console", SafeFieldsOnly(type=level))
    assert diagnostics.snapshot()["js_errors"] == {
        "uncaught": {"Error": 1, "EvalError": 1, "RangeError": 1, "ReferenceError": 1,
                     "SyntaxError": 1, "TypeError": 1, "URIError": 1, "other": 4},
        "console_errors": 1, "console_warnings": 1,
    }
    assert "SECRET" not in json.dumps(diagnostics.snapshot())


@pytest.mark.parametrize(("event", "counter"), [
    ("request", "requested"), ("response", "responses"),
    ("requestfinished", "finished"), ("requestfailed", "failed"),
])
def test_resource_counters_saturate_at_99_and_only_overflow_marks_truncated(event, counter):
    diagnostics, context = ProbeDiagnostics(), FakeContext()
    diagnostics.start(context)
    request = SafeFieldsOnly(url="https://challenges.cloudflare.com/turnstile/v0/api.js", failure="net::ERR_ABORTED")
    value = SafeFieldsOnly(request=request, status=200) if event == "response" else request
    for _ in range(99):
        context.emit(event, value)
    assert diagnostics.snapshot()["resources"]["turnstile_script"][counter] == 99
    assert diagnostics.snapshot()["truncated"] is False
    context.emit(event, value)
    assert diagnostics.snapshot()["resources"]["turnstile_script"][counter] == 99
    assert diagnostics.snapshot()["truncated"] is True


@pytest.mark.parametrize("name", ["Error", "EvalError", "RangeError", "ReferenceError", "SyntaxError",
                                 "TypeError", "URIError", "other", "console_errors", "console_warnings"])
def test_each_javascript_counter_has_the_same_99_limit(name):
    diagnostics, context = ProbeDiagnostics(), FakeContext()
    diagnostics.start(context)
    if name.startswith("console_"):
        event = "console"
        value = SafeFieldsOnly(type="error" if name == "console_errors" else "warning")
    else:
        event = "weberror"
        value = SafeFieldsOnly(error=SafeFieldsOnly(name=name))
    for _ in range(99):
        context.emit(event, value)
    snapshot = diagnostics.snapshot()
    counters = snapshot["js_errors"] if name.startswith("console_") else snapshot["js_errors"]["uncaught"]
    assert counters[name] == 99
    assert snapshot["truncated"] is False
    context.emit(event, value)
    snapshot = diagnostics.snapshot()
    counters = snapshot["js_errors"] if name.startswith("console_") else snapshot["js_errors"]["uncaught"]
    assert counters[name] == 99
    assert snapshot["truncated"] is True


def test_http_statuses_are_sorted_unique_valid_integers_and_limited_to_eight():
    diagnostics, context = ProbeDiagnostics(), FakeContext()
    diagnostics.start(context)
    request = SafeFieldsOnly(url="https://challenges.cloudflare.com/turnstile/v0/api.js")
    for status in (599, 100, 302, 200, 401, 403, 404, 500, 200, True, False, None, "422", 200.0, 99, 600):
        context.emit("response", SafeFieldsOnly(request=request, status=status))
    assert diagnostics.snapshot()["resources"]["turnstile_script"]["http_statuses"] == [100, 200, 302, 401, 403, 404, 500, 599]
    assert diagnostics.snapshot()["truncated"] is False
    context.emit("response", SafeFieldsOnly(request=request, status=429))
    assert diagnostics.snapshot()["resources"]["turnstile_script"]["http_statuses"] == [100, 200, 302, 401, 403, 404, 500, 599]
    assert diagnostics.snapshot()["truncated"] is True


def test_failure_kind_set_is_sorted_deduplicated_and_snapshots_are_independent():
    diagnostics, context = ProbeDiagnostics(), FakeContext()
    diagnostics.start(context)
    for failure in ("net::ERR_SSL_PROTOCOL_ERROR", "net::ERR_ABORTED", "net::ERR_ABORTED", "SECRET_ERROR"):
        context.emit("requestfailed", SafeFieldsOnly(url="https://challenges.cloudflare.com/turnstile/v0/api.js", failure=failure))
    snapshot = diagnostics.snapshot()
    assert snapshot["resources"]["turnstile_script"]["failure_kinds"] == ["aborted", "other", "tls"]
    snapshot["resources"]["turnstile_script"]["failure_kinds"].append("SECRET_MUTATION")
    assert diagnostics.snapshot()["resources"]["turnstile_script"]["failure_kinds"] == ["aborted", "other", "tls"]


def test_unavailable_safe_properties_do_not_raise_from_event_handlers_or_leak_errors():
    class Unavailable:
        def __getattr__(self, name):
            raise RuntimeError("SECRET_DISPOSED_ERROR")

    diagnostics, context = ProbeDiagnostics(), FakeContext()
    diagnostics.start(context)
    before = diagnostics.snapshot()
    for event in context.listeners:
        context.emit(event, Unavailable())
    before["collection_error"] = True
    assert diagnostics.snapshot() == before
    context.emit("request", SafeFieldsOnly(url="https://challenges.cloudflare.com/turnstile/v0/api.js?SECRET"))
    assert diagnostics.snapshot()["resources"]["turnstile_script"]["requested"] == 1
    diagnostics.stop()
    assert diagnostics.snapshot()["collection_error"] is True
    assert "SECRET" not in json.dumps(diagnostics.snapshot())


def test_every_late_callback_is_inert_after_stop_with_valid_event_objects():
    diagnostics, context = ProbeDiagnostics(), FakeContext()
    diagnostics.start(context)
    callbacks = {event: values[0] for event, values in context.listeners.items()}
    diagnostics.stop()
    before = diagnostics.snapshot()
    request = SafeFieldsOnly(url="https://challenges.cloudflare.com/turnstile/v0/api.js?SECRET", failure="net::ERR_ABORTED")
    callbacks["request"](request)
    callbacks["response"](SafeFieldsOnly(request=request, status=403))
    callbacks["requestfinished"](request)
    callbacks["requestfailed"](request)
    callbacks["weberror"](SafeFieldsOnly(error=SafeFieldsOnly(name="TypeError")))
    callbacks["console"](SafeFieldsOnly(type="error"))
    assert diagnostics.snapshot() == before
