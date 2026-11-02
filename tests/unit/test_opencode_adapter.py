"""[MOCK] OpenCodeAdapter contra um servidor OpenCode SIMULADO (httpx.MockTransport)."""
import base64
import json

import httpx
import pytest

from agents.base import (
    AgentAuthError,
    AgentConnectionError,
    AgentTimeoutError,
    ContractNotVerifiedError,
    ModelUnavailableError,
    QuotaExceededError,
)
from agents.opencode_adapter import OpenCodeAdapter, agent_names, extract_assistant_reply, find_model
from database.repository import Repository

MODEL_LIST = {"data": [{"providerID": "opencode", "id": "deepseek-v4.1-flash-free", "enabled": True, "variants": {"high": {}}}]}
SPEC = {"openapi": "3.1.0", "paths": {
    "/api/info": {"get": {}}, "/api/model": {"get": {}}, "/api/agent": {"get": {}},
    "/api/session": {"post": {"requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/SessionCreate"}}}}}},
    "/api/session/{id}/prompt": {"post": {}}, "/api/experimental/session/{id}/wait": {"post": {}},
    "/api/session/{id}/message": {"get": {}}},
    "components": {"schemas": {"SessionCreate": {"type": "object", "properties": {"title": {}, "model": {}, "location": {}, "agent": {}}}}}}


class FakeOpenCode:
    def __init__(self, spec=True, models=MODEL_LIST, agents=None):
        self.requests: list[httpx.Request] = []
        self.spec, self.models, self.agents = spec, models, agents if agents is not None else []
        self.messages: list[dict] = [{"info": {"id": "old", "role": "assistant", "time": {"created": 1}}, "parts": [{"type": "text", "text": "antiga"}]}]
        self.reply = {"type": "text", "text": '{"status": "done", "summary": "feito"}'}
        self.reply_error = None
        self.fail: dict[str, httpx.Response] = {}
        self.session_bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        for prefix, resp in self.fail.items():
            if path.startswith(prefix):
                return resp
        if path == "/doc" and self.spec:
            return httpx.Response(200, json=SPEC)
        if path in {"/doc", "/api/doc", "/openapi.json", "/api/openapi.json"}:
            return httpx.Response(404, text="not found")
        if path == "/api/info":
            return httpx.Response(200, json={"version": "2.2.5"})
        if path == "/api/model":
            return httpx.Response(200, json=self.models)
        if path == "/api/agent":
            return httpx.Response(200, json=self.agents)
        if path == "/api/session" and request.method == "POST":
            self.session_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {"id": f"ses_{len(self.session_bodies)}"}})
        if path.endswith("/prompt"):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/wait"):
            info = {"id": f"msg{len(self.messages)}", "role": "assistant", "time": {"created": 10 + len(self.messages)},
                    "tokens": {"input": 5, "output": 7}}
            if self.reply_error:
                info["error"] = self.reply_error
            self.messages.append({"info": info, "parts": [self.reply] if self.reply else []})
            return httpx.Response(204)
        if path.endswith("/message"):
            return httpx.Response(200, json={"data": list(reversed(self.messages))})
        return httpx.Response(404, text="<html>nope</html>", headers={"content-type": "text/html"})


def adapter(make_settings, fake, **over):
    s = make_settings(**over)
    return OpenCodeAdapter(s, session_store=Repository(s.db_path), transport=httpx.MockTransport(fake)), s


def test_basic_auth_header_and_no_password_leak(make_settings):
    fake = FakeOpenCode()
    a, _ = adapter(make_settings, fake)
    a.info()
    assert fake.requests[-1].headers["authorization"] == "Basic " + base64.b64encode(b"opencode:s3cr3t-test-pass").decode()


def test_auth_failure(make_settings):
    fake = FakeOpenCode()
    fake.fail["/api/info"] = httpx.Response(401, text="unauthorized")
    a, _ = adapter(make_settings, fake)
    with pytest.raises(AgentAuthError) as e:
        a.info()
    assert "s3cr3t-test-pass" not in str(e.value)
    assert a.health().status == "unavailable"


def test_model_query_found_enabled_and_variants(make_settings):
    a, _ = adapter(make_settings, FakeOpenCode())
    assert a.model_status() == {"model": "opencode/deepseek-v4.1-flash-free", "found": True, "enabled": True, "variants": ["high"]}


def test_model_lookup_formats():
    assert find_model({"opencode": {"models": {"deepseek-v4.1-flash-free": {"id": "deepseek-v4.1-flash-free"}}}}, "opencode", "deepseek-v4.1-flash-free")
    assert find_model([{"id": "opencode/deepseek-v4.1-flash-free"}], "opencode", "deepseek-v4.1-flash-free")
    assert find_model({"providers": [{"id": "opencode", "models": {"deepseek-v4.1-flash-free": {"name": "x"}}}]}, "opencode", "deepseek-v4.1-flash-free") is not None
    assert find_model({"data": [{"providerID": "other", "id": "deepseek-v4.1-flash-free"}]}, "opencode", "deepseek-v4.1-flash-free") is None
    assert agent_names([]) == [] and agent_names({"data": [{"name": "build"}]}) == ["build"]


def test_model_disabled_or_missing_blocks_execution(make_settings):
    fake = FakeOpenCode(models={"data": [{"providerID": "opencode", "id": "deepseek-v4.1-flash-free", "enabled": False}]})
    a, _ = adapter(make_settings, fake)
    with pytest.raises(ModelUnavailableError):
        a.run("x", context_key="c", run_id="r")
    assert not any(r.url.path.endswith("/prompt") for r in fake.requests)
    a2, _ = adapter(make_settings, FakeOpenCode(models={"data": []}))
    assert a2.health().status == "unavailable"


def test_unverified_contract_blocks_without_spec(make_settings):
    fake = FakeOpenCode(spec=False)
    a, _ = adapter(make_settings, fake)
    with pytest.raises(ContractNotVerifiedError):
        a.run("x", context_key="c", run_id="r")
    assert not any(r.method == "POST" for r in fake.requests)
    assert a.health().status == "contract_not_verified"


def test_full_run_with_spec_verified_session_reuse_and_agent_not_presumed(make_settings):
    fake = FakeOpenCode(agents=[])
    a, s = adapter(make_settings, fake, OPENCODE_AGENT="build", OPENCODE_VARIANT="high")
    r = a.run("implemente", context_key="ctx-1", run_id="r1")
    assert r.text.startswith('{"status": "done"') and r.session_id == "ses_1"
    assert r.usage == {"tokens": {"input": 5, "output": 7}}
    body = fake.session_bodies[0]
    assert "agent" not in body
    assert body["model"] == {"providerID": "opencode", "id": "deepseek-v4.1-flash-free", "variant": "high"}
    assert body["location"]["directory"]
    a.run("corrija", context_key="ctx-1", run_id="r2")
    assert len(fake.session_bodies) == 1


def test_spec_filters_unknown_body_fields(make_settings):
    a, _ = adapter(make_settings, FakeOpenCode())
    a.discover_spec()
    body, dropped = a.contract.filter_body("session_create", {"title": "t", "model": {}, "foo": "x"})
    assert dropped == ["foo"] and "foo" not in body


def test_quota_429_sets_cooldown_and_blocks_next_runs(make_settings):
    fake = FakeOpenCode()
    fake.fail["/api/session/ses_1/prompt"] = httpx.Response(429, text="free usage limit exceeded", headers={"retry-after": "120"})
    a, _ = adapter(make_settings, fake)
    with pytest.raises(QuotaExceededError):
        a.run("x", context_key="c", run_id="r")
    assert a.unavailable_reason() is not None
    n = len(fake.requests)
    with pytest.raises(QuotaExceededError):
        a.run("x", context_key="c2", run_id="r2")
    assert len(fake.requests) == n
    assert a.health().status == "quota_exceeded"
    a.clear_quota()
    assert a.unavailable_reason() is None


def test_quota_reported_inside_assistant_message(make_settings):
    fake = FakeOpenCode()
    fake.reply = None
    fake.reply_error = {"name": "APIError", "data": {"message": "Insufficient quota for free tier", "statusCode": 429}}
    a, _ = adapter(make_settings, fake)
    with pytest.raises(QuotaExceededError):
        a.run("x", context_key="c", run_id="r")
    assert a.unavailable_reason()


def test_server_down_and_timeout(make_settings):
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    s = make_settings()
    a = OpenCodeAdapter(s, transport=httpx.MockTransport(down))
    with pytest.raises(AgentConnectionError):
        a.info()
    assert a.health().status == "unavailable"

    def slow(request):
        raise httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(AgentTimeoutError):
        OpenCodeAdapter(s, transport=httpx.MockTransport(slow)).info()


def test_extract_reply_ignores_previous_messages():
    payload = {"data": [{"info": {"id": "m2", "role": "assistant"}, "parts": [{"type": "text", "text": "nova"}]},
                        {"info": {"id": "m1", "role": "assistant"}, "parts": [{"type": "text", "text": "velha"}]}]}
    assert extract_assistant_reply(payload, {"m1"})["text"] == "nova"
    v02 = {"data": [{"type": "assistant", "content": [{"type": "text", "text": "formato v0.2"}]}]}
    assert extract_assistant_reply(v02)["text"] == "formato v0.2"
