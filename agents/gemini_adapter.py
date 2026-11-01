"""Adaptador do Gemini 4 Argon High via Antigravity CLI (`agy`).

Contrato validado no ambiente:
    agy -p "PROMPT" --model gemini-4-argon-high --output-format json
Retorno JSON: conversation_id, status, response, duration_seconds, num_turns, usage.

Flags adicionais só são usadas se aparecerem em `agy --help` (sem consumir tokens):
  --effort        nível de raciocínio (AGY_EFFORT)
  --conversation  retomada de conversa por ID
  --mode          plan para skills somente leitura (AGY_REVIEW_MODE); AGY_IMPLEMENT_MODE para implementar
  --add-dir       libera a pasta runtime/prompts quando o prompt vai por arquivo
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from agents.base import (
    READ_ONLY_SKILLS, AgentAuthError, AgentBackend, QuotaCooldown, AgentCancelledError, AgentHealth, AgentResponseError, AgentResult,
    AgentTimeoutError, AgentUnavailableError, QuotaExceededError, looks_like_quota_error,
)
from core.config import Settings
from core.logging_setup import redact
from core.safe_exec import IS_WINDOWS, resolve_binary, run_process

log = logging.getLogger("agents.gemini")

EFFORT_FLAG_CANDIDATES = ("--effort", "--reasoning-effort", "--thinking-level")
RESUME_FLAG_CANDIDATES = ("--conversation", "--conversation-id")
# cmd.exe interpreta estes caracteres em argumentos de arquivos .cmd/.bat.
CMD_UNSAFE = re.compile(r'[\r\n"%^&|<>!]')
INLINE_LIMIT_EXE = 24000  # limite do CreateProcess é 32767 caracteres para a linha inteira
INLINE_LIMIT_CMD = 7000


def parse_agy_output(stdout: str) -> dict[str, Any]:
    """Interpreta a saída `--output-format json`. Tolera linhas de aviso antes do JSON."""
    text = (stdout or "").strip()
    if not text:
        raise AgentResponseError("Antigravity não retornou saída.")
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    candidates = []
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                value, _ = decoder.raw_decode(text[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and ("response" in value or "status" in value):
                candidates.append(value)
    if candidates:
        return candidates[-1]
    raise AgentResponseError(f"Antigravity não retornou JSON válido: {redact(text[-800:])}")


def result_from_payload(payload: dict[str, Any], model: str) -> AgentResult:
    status = str(payload.get("status") or "").upper() or "UNKNOWN"
    response = payload.get("response")
    if status != "SUCCESS":
        detail = payload.get("error") or response or "sem detalhes"
        detail_text = redact(json.dumps(detail, ensure_ascii=False) if not isinstance(detail, str) else detail)
        if looks_like_quota_error(detail_text):
            raise QuotaExceededError(f"Gemini/Antigravity sem cota: {detail_text[:500]}")
        raise AgentResponseError(f"Antigravity terminou com status {status}: {detail_text[:1500]}")
    if not isinstance(response, str) or not response.strip():
        denied = payload.get("denied_actions") or []
        if denied:
            names = ", ".join(str(d.get("action") or d) if isinstance(d, dict) else str(d) for d in denied)
            raise AgentResponseError(f"Antigravity terminou sem resposta: permiss?o negada pelo agy ({names}). "
                                     "O agente tentou uma a??o n?o permitida em modo n?o interativo.")
        raise AgentResponseError("Antigravity retornou status SUCCESS com resposta vazia.")
    duration = payload.get("duration_seconds")
    return AgentResult(
        text=response,
        model=model,
        session_id=payload.get("conversation_id"),
        usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else ({"raw": payload.get("usage")} if payload.get("usage") else None),
        duration_seconds=float(duration) if isinstance(duration, (int, float)) else None,
        num_turns=payload.get("num_turns") if isinstance(payload.get("num_turns"), int) else None,
        raw_status=status,
    )


class GeminiAdapter(AgentBackend):
    name = "gemini"
    kind = "antigravity-cli"

    def __init__(
        self,
        settings: Settings,
        command_prefix: list[str] | None = None,
        session_store: Any | None = None,
        runner: Callable[..., Any] = run_process,
    ):
        self.settings = settings
        self._command_prefix = command_prefix
        self._sessions = session_store  # Repository (get_session/save_session) ou None
        self._runner = runner
        self._help_text: str | None = None
        self._help_checked_at = 0.0
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._quota = QuotaCooldown(settings.runtime_dir / "gemini_state.json", settings.agy_model, settings.agy_quota_cooldown_minutes)

    # ---------------------------------------------------------------- binário
    @property
    def model(self) -> str:
        return self.settings.agy_model

    def command_prefix(self) -> list[str]:
        if self._command_prefix:
            return list(self._command_prefix)
        resolved = resolve_binary(self.settings.agy_bin)
        if not resolved:
            raise AgentUnavailableError(
                f"Executável '{self.settings.agy_bin}' não encontrado no PATH. Configure AGY_BIN com o caminho completo (agy.cmd/agy.exe)."
            )
        if resolved.lower().endswith(".ps1"):
            raise AgentUnavailableError(
                "AGY_BIN aponta para um .ps1, que não pode ser executado diretamente via subprocess. "
                "Use o agy.cmd ou agy.exe correspondente (veja `Get-Command agy -All`)."
            )
        return [resolved]

    def _is_cmd_wrapper(self, prefix: list[str]) -> bool:
        return IS_WINDOWS and prefix[0].lower().endswith((".cmd", ".bat"))

    def help_text(self, refresh: bool = False) -> str:
        """`agy --help` não chama o modelo (não consome tokens); usado para detectar flags suportadas."""
        if self._help_text is not None and not refresh and time.time() - self._help_checked_at < 3600:
            return self._help_text
        prefix = self.command_prefix()
        res = self._runner([*prefix, "--help"], cwd=None, timeout=30)
        self._help_text = (res.stdout or "") + "\n" + (res.stderr or "")
        self._help_checked_at = time.time()
        return self._help_text

    def _flag_supported(self, flag: str) -> bool:
        try:
            return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", self.help_text()) is not None
        except Exception:
            return False

    def effort_flag(self) -> str | None:
        if self.settings.agy_effort_flag:
            return self.settings.agy_effort_flag
        return next((f for f in EFFORT_FLAG_CANDIDATES if self._flag_supported(f)), None)

    def resume_flag(self) -> str | None:
        mode = self.settings.agy_resume_conversations
        if mode == "off":
            return None
        if self.settings.agy_resume_flag:
            return self.settings.agy_resume_flag
        return next((f for f in RESUME_FLAG_CANDIDATES if self._flag_supported(f)), None)

    def mode_for(self, options: dict) -> str:
        read_only = options.get("read_only", options.get("skill") in READ_ONLY_SKILLS)
        return (options.get("mode") or (self.settings.agy_review_mode if read_only else self.settings.agy_implement_mode) or "").strip()

    # ---------------------------------------------------------------- comando
    def build_command(self, prompt: str, run_id: str, options: dict | None = None) -> tuple[list[str], Path | None]:
        options = options or {}
        prefix = self.command_prefix()
        model = options.get("model") or self.settings.agy_model
        effort = options.get("effort", self.settings.agy_effort)
        prompt_arg, prompt_file = self._prompt_argument(prompt, run_id, prefix)
        cmd = [*prefix, "-p", prompt_arg, "--model", model, "--output-format", "json"]
        if effort:
            flag = self.effort_flag()
            if flag:
                cmd += [flag, str(effort)]
            else:
                log.warning("AGY_EFFORT=%s ignorado: nenhuma flag de raciocínio confirmada em `agy --help`.", effort)
        conversation_id = options.get("conversation_id")
        if conversation_id:
            flag = self.resume_flag()
            if flag:
                cmd += [flag, str(conversation_id)]
        mode = self.mode_for(options)
        if mode:
            if self._flag_supported("--mode"):
                cmd += ["--mode", mode]
            else:
                log.warning("Modo '%s' não aplicado: `agy --help` não anuncia --mode (restrição fica só no prompt).", mode)
        if prompt_file is not None and self._flag_supported("--add-dir"):
            cmd += ["--add-dir", str(prompt_file.parent.resolve())]
        return cmd, prompt_file

    def _prompt_argument(self, prompt: str, run_id: str, prefix: list[str]) -> tuple[str, Path | None]:
        mode = self.settings.agy_prompt_mode
        is_cmd = self._is_cmd_wrapper(prefix)
        fits_inline = len(prompt) <= (INLINE_LIMIT_CMD if is_cmd else INLINE_LIMIT_EXE) and not (is_cmd and CMD_UNSAFE.search(prompt))
        if mode == "inline" or (mode == "auto" and fits_inline):
            if is_cmd and CMD_UNSAFE.search(prompt):
                raise AgentResponseError(
                    "AGY_PROMPT_MODE=inline com agy.cmd: o prompt contém aspas/quebras de linha que o cmd.exe corrompe. Use AGY_PROMPT_MODE=auto."
                )
            return prompt, None
        prompts_dir = self.settings.runtime_dir / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        file = prompts_dir / f"{re.sub(r'[^A-Za-z0-9_-]', '_', run_id)}.md"
        file.write_text(prompt, encoding="utf-8")
        short = (
            f"Suas instrucoes completas estao no arquivo UTF-8 {file.resolve()} . "
            "Leia o arquivo inteiro antes de qualquer acao e siga-o exatamente, inclusive o formato de resposta exigido nele."
        )
        return short, file

    # ---------------------------------------------------------------- execução
    # ---------------------------------------------------------------- quota
    def unavailable_reason(self) -> str | None:
        return self._quota.reason()

    def clear_quota(self) -> None:
        self._quota.clear()

    def run(self, prompt: str, *, context_key: str, run_id: str, options: dict | None = None) -> AgentResult:
        try:
            return self._run(prompt, context_key=context_key, run_id=run_id, options=options)
        except QuotaExceededError as exc:
            self._quota.mark(str(exc), exc.retry_after_seconds)
            raise

    def _run(self, prompt: str, *, context_key: str, run_id: str, options: dict | None = None) -> AgentResult:
        options = dict(options or {})
        reason = self._quota.reason()
        if reason:
            raise QuotaExceededError(reason)
        if not self.settings.project_root.is_dir():
            raise AgentUnavailableError("PROJECT_ROOT não existe. Confira o caminho no .env.")
        resume = self.resume_flag() is not None
        if resume and self._sessions and not options.get("conversation_id"):
            previous = self._sessions.get_session(self.name, context_key)
            if previous:
                options["conversation_id"] = previous
        cmd, prompt_file = self.build_command(prompt, run_id, options)
        cancel = threading.Event()
        with self._lock:
            self._cancel_events[run_id] = cancel
        started = time.monotonic()
        try:
            try:
                res = self._runner(cmd, cwd=self.settings.project_root, timeout=self.settings.agy_timeout, cancel_event=cancel)
            except FileNotFoundError as exc:
                raise AgentUnavailableError(f"Não foi possível iniciar o Antigravity CLI: {exc}") from exc
        finally:
            with self._lock:
                self._cancel_events.pop(run_id, None)
            if prompt_file:
                prompt_file.unlink(missing_ok=True)
        elapsed = time.monotonic() - started
        if cancel.is_set():
            raise AgentCancelledError("Execução do Gemini cancelada.")
        if res.timed_out:
            raise AgentTimeoutError(f"Antigravity excedeu {self.settings.agy_timeout}s e foi encerrado.")
        if res.returncode != 0:
            details = redact((res.stderr or "").strip() or (res.stdout or "").strip())[-2000:]
            # Mesmo com código != 0 o CLI pode ter emitido JSON com status de erro.
            try:
                payload = parse_agy_output(res.stdout)
                result_from_payload(payload, self.model)
            except QuotaExceededError:
                raise
            except AgentResponseError:
                pass
            if looks_like_quota_error(details):
                raise QuotaExceededError(f"Gemini/Antigravity sem cota: {details[:500]}")
            if re.search(r"(unauthenticated|not logged in|login required|401|403)", details, re.IGNORECASE):
                raise AgentAuthError(f"Antigravity CLI não autenticado: {details[:500]}")
            raise AgentResponseError(f"Antigravity CLI falhou (código {res.returncode}): {details}")
        payload = parse_agy_output(res.stdout)
        result = result_from_payload(payload, options.get("model") or self.model)
        if result.duration_seconds is None:
            result.duration_seconds = round(elapsed, 3)
        if result.session_id and resume and self._sessions:
            self._sessions.save_session(self.name, context_key, result.session_id, result.model)
        result.extra["resume_supported"] = resume
        return result

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            ev = self._cancel_events.get(run_id)
        if ev:
            ev.set()
            return True
        return False

    # ---------------------------------------------------------------- saúde
    def health(self, deep: bool = False) -> AgentHealth:
        checks: dict[str, Any] = {"project_root_exists": self.settings.project_root.is_dir()}
        try:
            prefix = self.command_prefix()
            checks["binary"] = prefix[-1] if self._command_prefix else prefix[0]
            checks["cmd_wrapper"] = self._is_cmd_wrapper(prefix)
        except AgentUnavailableError as exc:
            return AgentHealth(self.name, False, "not_configured", str(exc), self.model, checks)
        try:
            help_text = self.help_text(refresh=deep)
        except Exception as exc:  # noqa: BLE001
            return AgentHealth(self.name, False, "unavailable", f"`agy --help` falhou: {redact(exc)}", self.model, checks)
        checks["help_ok"] = bool(help_text.strip())
        checks["effort_flag"] = self.effort_flag()
        checks["resume_flag"] = self.resume_flag()
        checks["validated_flags"] = ["-p", "--model", "--output-format json"]
        checks["mode_flag"] = self._flag_supported("--mode")
        checks["read_only_mode"] = (self.settings.agy_review_mode or None) if checks["mode_flag"] else None
        checks["implement_mode"] = self.settings.agy_implement_mode or None
        checks["add_dir_flag"] = self._flag_supported("--add-dir")
        quota = self._quota.reason()
        if quota:
            return AgentHealth(self.name, False, "quota_exceeded", quota, self.model, checks)
        ok = checks["help_ok"] and checks["project_root_exists"]
        detail = "CLI encontrado (verificação sem chamada ao modelo)." if ok else "Verifique PROJECT_ROOT e o AGY_BIN."
        return AgentHealth(self.name, ok, "ok" if ok else "degraded", detail, self.model, checks)
