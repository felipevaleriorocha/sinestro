"""Adaptador do DeepSeek V4.1 Flash Free via servidor HTTP do OpenCode 2.x.

- Autenticação HTTP Basic (usuário/senha do .env; a senha nunca é logada).
- Rotas vêm de config/opencode_routes.json e são conferidas contra a OpenAPI do servidor.
- Rotas validadas no ambiente: GET /api/info, /api/model, /api/agent.
- Agente do OpenCode só é enviado se existir em /api/agent.
- Quota excedida entra em cooldown persistido: nenhuma tarefa é enviada até expirar.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from agents.base import (
    READ_ONLY_SKILLS, AgentAuthError, AgentBackend, QuotaCooldown, AgentCancelledError, AgentConnectionError, AgentHealth, AgentResponseError, AgentResult,
    AgentTimeoutError, ContractNotVerifiedError, ModelUnavailableError, QuotaExceededError, looks_like_quota_error,
)
from agents.opencode_contract import OpenCodeContract
from core.config import Settings
from core.logging_setup import redact

log = logging.getLogger("agents.opencode")


# ---------------------------------------------------------------- parsing tolerante
def _as_list(payload: Any) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "items", "messages", "models", "agents", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def find_model(payload: Any, provider_id: str, model_id: str) -> dict | None:
    """Procura o modelo na resposta de /api/model, aceitando formatos de lista e de dicionário por provider."""
    full = f"{provider_id}/{model_id}"

    def matches(entry: dict, provider_hint: str | None) -> bool:
        ids = {str(entry.get(k)) for k in ("id", "modelID", "model", "name", "key") if entry.get(k)}
        prov = entry.get("providerID") or entry.get("provider") or entry.get("provider_id") or provider_hint
        if isinstance(prov, dict):
            prov = prov.get("id")
        return full in ids or (model_id in ids and (prov is None or str(prov) == provider_id))

    def walk(node: Any, provider_hint: str | None, depth: int = 0) -> dict | None:
        if depth > 6:
            return None
        if isinstance(node, dict):
            if matches(node, provider_hint):
                return node
            for key, value in node.items():
                hint = provider_hint
                if key == "models" and isinstance(node.get("id"), str):
                    hint = node["id"]  # {"id": "opencode", "models": {...}}
                elif key == provider_id:
                    hint = provider_id  # {"opencode": {...}}
                if key == model_id and isinstance(value, dict) and provider_hint == provider_id:
                    return value  # {"models": {"deepseek-...": {...}}} sem "id" no item
                found = walk(value, hint, depth + 1)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item, provider_hint, depth + 1)
                if found:
                    return found
        return None

    return walk(payload, None)


def model_variants(entry: dict | None) -> list[str]:
    if not entry:
        return []
    v = entry.get("variants")
    if isinstance(v, dict):
        return [str(k) for k in v.keys()]
    if isinstance(v, list):
        return [str(x.get("id") if isinstance(x, dict) else x) for x in v]
    return []


def agent_names(payload: Any) -> list[str]:
    names = []
    for a in _as_list(payload):
        if isinstance(a, dict):
            n = a.get("name") or a.get("id")
            if n:
                names.append(str(n))
        elif isinstance(a, str):
            names.append(a)
    if isinstance(payload, dict) and not names and all(isinstance(v, dict) for v in payload.values()) and payload:
        names = list(payload.keys())
    return names


def _msg_meta(m: dict) -> tuple[str | None, str | None, Any, list]:
    info = m.get("info") if isinstance(m.get("info"), dict) else {}
    role = m.get("role") or info.get("role") or m.get("type")
    mid = m.get("id") or info.get("id")
    error = m.get("error") or info.get("error")
    parts = m.get("parts") if isinstance(m.get("parts"), list) else m.get("content")
    if isinstance(parts, str):
        parts = [{"type": "text", "text": parts}]
    return (str(role) if role else None), (str(mid) if mid else None), error, (parts or [])


def message_ids(payload: Any) -> set[str]:
    return {mid for m in _as_list(payload) if isinstance(m, dict) for _, mid, _, _ in [_msg_meta(m)] if mid}


def extract_assistant_reply(payload: Any, exclude_ids: set[str] | None = None) -> dict:
    """Retorna {text, message_id, error, usage} da resposta do assistente mais recente que não estava antes."""
    exclude_ids = exclude_ids or set()
    messages = [m for m in _as_list(payload) if isinstance(m, dict)]
    candidates = []
    for idx, m in enumerate(messages):
        role, mid, error, parts = _msg_meta(m)
        if role != "assistant" or (mid and mid in exclude_ids):
            continue
        text = "\n".join(
            str(p.get("text", "")) for p in parts if isinstance(p, dict) and p.get("type") == "text" and p.get("text")
        ).strip()
        info = m.get("info") if isinstance(m.get("info"), dict) else m
        usage = {k: info.get(k) for k in ("tokens", "cost", "usage") if info.get(k) is not None} or None
        created = None
        t = info.get("time")
        if isinstance(t, dict):
            created = t.get("created")
        candidates.append({"idx": idx, "created": created, "text": text, "message_id": mid, "error": error, "usage": usage})
    if not candidates:
        return {"text": "", "message_id": None, "error": None, "usage": None}
    if all(c["created"] is not None for c in candidates):
        candidates.sort(key=lambda c: c["created"], reverse=True)
    # Sem timestamps: a rota v0.2 usa order=desc, então o primeiro é o mais recente.
    with_text = [c for c in candidates if c["text"] or c["error"]]
    return (with_text or candidates)[0]


# ---------------------------------------------------------------- adaptador
class OpenCodeAdapter(AgentBackend):
    name = "deepseek"
    kind = "opencode-http"

    def __init__(self, settings: Settings, session_store: Any | None = None, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self._sessions = session_store
        self.contract = OpenCodeContract(settings.opencode_routes_file, settings.opencode_require_verified_routes)
        auth = None
        if settings.opencode_password:
            auth = httpx.BasicAuth(settings.opencode_username or "opencode", settings.opencode_password)
        self._client = httpx.Client(
            base_url=settings.opencode_url,
            auth=auth,
            transport=transport,
            timeout=httpx.Timeout(connect=settings.opencode_connect_timeout, read=settings.opencode_timeout, write=60, pool=30),
            headers={"Accept": "application/json"},
        )
        self._spec_checked = False
        self._model_cache: tuple[float, dict] | None = None
        self._agents_cache: tuple[float, list[str]] | None = None
        self._quota = QuotaCooldown(settings.runtime_dir / "opencode_state.json", settings.opencode_model,
                                    settings.opencode_quota_cooldown_minutes)
        self._lock = threading.Lock()
        self._cancelled: set[str] = set()
        self._active_sessions: dict[str, str] = {}

    @property
    def model(self) -> str:
        return self.settings.opencode_model

    def model_ref(self) -> tuple[str, str]:
        m = self.settings.opencode_model
        if "/" not in m:
            raise ModelUnavailableError("OPENCODE_MODEL deve estar no formato provider/model (ex.: opencode/deepseek-v4.1-flash-free).")
        provider, model_id = m.split("/", 1)
        return provider, model_id

    # ---------------------------------------------------------------- HTTP
    def _request(self, method: str, path: str, *, json_body: Any = None, params: dict | None = None, timeout: float | None = None) -> Any:
        try:
            resp = self._client.request(method, path, json=json_body, params=params, timeout=timeout if timeout else httpx.USE_CLIENT_DEFAULT)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise AgentConnectionError(
                f"Servidor OpenCode indisponível em {self.settings.opencode_url}. Inicie com .\\run_opencode.ps1. ({type(exc).__name__})"
            ) from exc
        except httpx.TimeoutException as exc:
            raise AgentTimeoutError(f"OpenCode não respondeu a tempo em {method} {path}.") from exc
        except httpx.HTTPError as exc:
            raise AgentConnectionError(f"Falha HTTP ao falar com o OpenCode: {redact(exc)}") from exc
        return self._handle_response(resp, method, path)

    def _handle_response(self, resp: httpx.Response, method: str, path: str) -> Any:
        ctype = resp.headers.get("content-type", "")
        body_text = redact(resp.text[:2000]) if resp.content else ""
        if resp.status_code == 401:
            raise AgentAuthError("OpenCode recusou a autenticação (HTTP 401). Confira OPENCODE_SERVER_USERNAME/OPENCODE_SERVER_PASSWORD.")
        if resp.status_code in (402, 429) or (resp.status_code >= 400 and looks_like_quota_error(body_text)):
            retry_after = resp.headers.get("retry-after")
            raise QuotaExceededError(
                f"Quota/limite excedido no OpenCode (HTTP {resp.status_code}): {body_text[:300]}",
                int(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        if resp.status_code >= 400:
            if "text/html" in ctype:
                body_text = "o servidor retornou HTML (rota inexistente nesta versão ou proxy)."
            raise AgentResponseError(f"OpenCode HTTP {resp.status_code} em {method} {path}: {body_text}")
        if resp.status_code == 204 or not resp.content:
            return None
        if "text/html" in ctype:
            raise AgentResponseError(f"OpenCode retornou HTML em {path}; a rota provavelmente não existe nesta versão.")
        try:
            return resp.json()
        except ValueError as exc:
            raise AgentResponseError(f"OpenCode não retornou JSON em {path}: {body_text[:300]}") from exc

    def _call(self, route_name: str, *, path_params: dict | None = None, json_body: Any = None, timeout: float | None = None) -> Any:
        route = self.contract.require(route_name)
        path = route.path.format(**(path_params or {}))
        if json_body is not None and isinstance(json_body, dict):
            json_body, dropped = self.contract.filter_body(route_name, json_body)
            if dropped:
                log.warning("Campos removidos de %s por não constarem na OpenAPI: %s", route_name, dropped)
        return self._request(route.method, path, json_body=json_body, params=route.query or None, timeout=timeout)

    # ---------------------------------------------------------------- endpoints validados
    def info(self) -> Any:
        return self._call("info", timeout=self.settings.opencode_connect_timeout + 5)

    def models(self) -> Any:
        return self._call("models", timeout=30)

    def agents(self) -> list[str]:
        now = time.time()
        if self._agents_cache and now - self._agents_cache[0] < 120:
            return self._agents_cache[1]
        names = agent_names(self._call("agents", timeout=30))
        self._agents_cache = (now, names)
        return names

    def discover_spec(self) -> dict:
        """Lê a OpenAPI do próprio servidor (somente GET) e confere as rotas configuradas."""
        found, source = None, None
        for candidate in self.contract.openapi_candidates:
            try:
                data = self._request("GET", candidate, timeout=15)
            except (AgentResponseError, AgentAuthError):
                continue
            if isinstance(data, dict) and isinstance(data.get("paths"), dict):
                found, source = data, candidate
                break
        self.contract.apply_spec(found, source)
        self._spec_checked = True
        return self.contract.summary()

    def model_status(self, refresh: bool = False) -> dict:
        now = time.time()
        if self._model_cache and not refresh and now - self._model_cache[0] < 60:
            return self._model_cache[1]
        provider, model_id = self.model_ref()
        entry = find_model(self.models(), provider, model_id)
        enabled = None
        if entry is not None:
            for key in ("enabled", "available", "active"):
                if isinstance(entry.get(key), bool):
                    enabled = entry[key]
                    break
            if isinstance(entry.get("status"), str):
                enabled = entry["status"].lower() in {"active", "enabled", "available", "ok"} if enabled is None else enabled
        status = {"model": self.settings.opencode_model, "found": entry is not None, "enabled": enabled, "variants": model_variants(entry)}
        self._model_cache = (now, status)
        return status

    # ---------------------------------------------------------------- quota
    def mark_quota_exceeded(self, message: str, retry_after: int | None = None) -> None:
        self._quota.mark(message, retry_after)

    def clear_quota(self) -> None:
        self._quota.clear()

    def unavailable_reason(self) -> str | None:
        return self._quota.reason()

    # ---------------------------------------------------------------- execução
    def _preflight(self) -> dict:
        reason = self.unavailable_reason()
        if reason:
            raise QuotaExceededError(reason)
        if not self.settings.project_root.is_dir():
            raise ModelUnavailableError("PROJECT_ROOT não existe; o OpenCode não deve trabalhar fora do workspace.")
        if not self._spec_checked:
            self.discover_spec()
        for r in ("session_create", "session_prompt", "session_wait", "session_messages"):
            self.contract.require(r)
        status = self.model_status()
        if not status["found"]:
            raise ModelUnavailableError(f"Modelo {self.model} não aparece em GET /api/model. Troque OPENCODE_MODEL no .env.")
        if status["enabled"] is False:
            raise ModelUnavailableError(f"Modelo {self.model} está desabilitado no OpenCode (enabled=false).")
        return status

    def agent_for(self, options: dict) -> str:
        read_only = options.get("read_only", options.get("skill") in READ_ONLY_SKILLS)
        return (self.settings.opencode_review_agent if read_only else self.settings.opencode_agent) or ""

    def _create_session(self, title: str, model_status: dict, agent: str = "") -> str:
        provider, model_id = self.model_ref()
        model_ref: dict[str, Any] = {"providerID": provider, "id": model_id}
        variant = self.settings.opencode_variant
        if variant:
            if variant in model_status.get("variants", []):
                model_ref["variant"] = variant
            else:
                log.warning("OPENCODE_VARIANT=%s ignorado: o modelo não lista essa variante em /api/model.", variant)
        body: dict[str, Any] = {
            "title": title[:100],
            "model": model_ref,
            "location": {"directory": str(self.settings.project_root.resolve())},
        }
        if agent:
            available = self.agents()
            if agent in available:
                body["agent"] = agent
            else:
                log.warning("Agente OpenCode '%s' ignorado: /api/agent retornou %s.", agent, available or "lista vazia")
        result = self._call("session_create", json_body=body, timeout=60)
        session = result.get("data", result) if isinstance(result, dict) else {}
        session_id = session.get("id") or session.get("sessionID") if isinstance(session, dict) else None
        if not session_id:
            raise AgentResponseError(f"OpenCode não retornou ID de sessão: {redact(json.dumps(result)[:500])}")
        return str(session_id)

    def ensure_session(self, context_key: str, title: str, model_status: dict, agent: str = "") -> str:
        # A sessão fica presa ao agente com que foi criada; por isso a chave inclui o agente (Build x Plan).
        key = f"{context_key}|{agent}" if agent else context_key
        if self._sessions:
            existing = self._sessions.get_session(self.name, key)
            if existing:
                return existing
        session_id = self._create_session(title, model_status, agent)
        if self._sessions:
            self._sessions.save_session(self.name, key, session_id, self.model)
        return session_id

    def run(self, prompt: str, *, context_key: str, run_id: str, options: dict | None = None) -> AgentResult:
        options = options or {}
        started = time.monotonic()
        try:
            status = self._preflight()
            session_id = self.ensure_session(context_key, options.get("title") or f"Orchestrator {context_key}", status,
                                             self.agent_for(options))
            with self._lock:
                self._active_sessions[run_id] = session_id
            before = message_ids(self._call("session_messages", path_params={"session_id": session_id}, timeout=60))
            self._call("session_prompt", path_params={"session_id": session_id}, json_body={"text": prompt}, timeout=120)
            self._call("session_wait", path_params={"session_id": session_id}, timeout=self.settings.opencode_timeout)
            if run_id in self._cancelled:
                raise AgentCancelledError("Execução do DeepSeek cancelada.")
            payload = self._call("session_messages", path_params={"session_id": session_id}, timeout=60)
        except QuotaExceededError as exc:
            self.mark_quota_exceeded(str(exc), exc.retry_after_seconds)
            raise
        finally:
            with self._lock:
                self._active_sessions.pop(run_id, None)
                self._cancelled.discard(run_id)
        reply = extract_assistant_reply(payload, before)
        if reply["error"]:
            err_text = redact(json.dumps(reply["error"], ensure_ascii=False) if not isinstance(reply["error"], str) else reply["error"])
            if looks_like_quota_error(err_text):
                self.mark_quota_exceeded(err_text)
                raise QuotaExceededError(f"DeepSeek sem cota: {err_text[:300]}")
            if not reply["text"]:
                raise AgentResponseError(f"OpenCode retornou erro na mensagem do assistente: {err_text[:800]}")
        if not reply["text"]:
            raise AgentResponseError("OpenCode concluiu sem texto do assistente.")
        return AgentResult(
            text=reply["text"], model=self.model, session_id=session_id, usage=reply["usage"],
            duration_seconds=round(time.monotonic() - started, 3), extra={"message_id": reply["message_id"]},
        )

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            session_id = self._active_sessions.get(run_id)
            self._cancelled.add(run_id)
        abort = self.contract.route("session_abort")
        if session_id and abort is not None and abort.usable:
            try:
                self._request(abort.method, abort.path.format(session_id=session_id), timeout=15)
                return True
            except Exception as exc:  # noqa: BLE001
                log.warning("Falha ao abortar sessão OpenCode: %s", redact(exc))
        return False

    # ---------------------------------------------------------------- saúde
    def health(self, deep: bool = False) -> AgentHealth:
        checks: dict[str, Any] = {"auth_configured": bool(self.settings.opencode_password)}
        try:
            info = self.info()
            checks["server"] = "ok"
            if isinstance(info, dict):
                data = info.get("data") if isinstance(info.get("data"), dict) else info
                checks["server_version"] = data.get("version")
        except AgentAuthError as exc:
            return AgentHealth(self.name, False, "unavailable", str(exc), self.model, checks)
        except (AgentConnectionError, AgentTimeoutError) as exc:
            return AgentHealth(self.name, False, "unavailable", str(exc), self.model, checks)
        except Exception as exc:  # noqa: BLE001
            return AgentHealth(self.name, False, "degraded", f"/api/info: {redact(exc)}", self.model, checks)
        try:
            checks["model"] = self.model_status(refresh=deep)
            checks["opencode_agents"] = self.agents()
            for label, wanted in (("implement_agent", self.settings.opencode_agent), ("review_agent", self.settings.opencode_review_agent)):
                checks[label] = {"name": wanted or None, "available": (wanted in checks["opencode_agents"]) if wanted else None}
        except Exception as exc:  # noqa: BLE001
            checks["model_error"] = redact(exc)
        if deep or not self._spec_checked:
            try:
                checks["contract"] = self.discover_spec()
            except Exception as exc:  # noqa: BLE001
                checks["contract_error"] = redact(exc)
        else:
            checks["contract"] = self.contract.summary()
        reason = self.unavailable_reason()
        model = checks.get("model") or {}
        if reason:
            return AgentHealth(self.name, False, "quota_exceeded", reason, self.model, checks)
        if not model.get("found"):
            return AgentHealth(self.name, False, "unavailable", f"Modelo {self.model} não encontrado em /api/model.", self.model, checks)
        if model.get("enabled") is False:
            return AgentHealth(self.name, False, "unavailable", f"Modelo {self.model} desabilitado.", self.model, checks)
        try:
            for r in ("session_create", "session_prompt", "session_wait", "session_messages"):
                self.contract.require(r)
        except ContractNotVerifiedError as exc:
            return AgentHealth(self.name, False, "contract_not_verified", str(exc), self.model, checks)
        return AgentHealth(self.name, True, "ok", "Servidor e modelo reconhecidos (sem consumo de tokens).", self.model, checks)

    def close(self) -> None:
        self._client.close()
