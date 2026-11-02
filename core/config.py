"""Configuração central do orquestrador (lida do .env sem nunca alterá-lo)."""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Chaves cujo valor nunca pode aparecer em logs, respostas da API ou erros.
SECRET_KEYS = ("OPENCODE_SERVER_PASSWORD", "A2A_API_TOKEN", "GEMINI_API_KEY")


def read_env_file(path: Path) -> dict[str, str]:
    r"""Parser de .env sem processar escapes (caminhos Windows com '\' ficam intactos).

    O python-dotenv interpreta escapes em valores entre aspas duplas ("C:\...\antigravity" vira \a = BEL).
    Aqui: KEY=VALUE, aspas externas removidas, comentários '#' só no início da linha ou após espaço em valor sem aspas.
    """
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.lower().startswith("export "):
            stripped = stripped[7:].lstrip()
        key, _, value = stripped.partition("=")
        key, value = key.strip(), value.strip()
        if not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        else:
            for marker in (" #", "\t#"):
                if marker in value:
                    value = value.split(marker, 1)[0].rstrip()
        values[key] = value
    return values


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "sim", "on"}


def _int(value: str | None, default: int, minimum: int | None = None) -> int:
    try:
        result = int(str(value).strip()) if value not in (None, "") else default
    except ValueError:
        result = default
    if minimum is not None and result < minimum:
        result = minimum
    return result


def _clean(value: str | None) -> str:
    return (value or "").strip().strip('"').strip("'").strip()


def _path(value: str | None, default: Path) -> Path:
    raw = _clean(value)
    if not raw:
        return default
    p = Path(os.path.expandvars(raw)).expanduser()
    return p if p.is_absolute() else (BASE_DIR / p)


@dataclass(frozen=True)
class Settings:
    project_root: Path
    host: str = "127.0.0.1"
    port: int = 8787
    public_url: str = "http://127.0.0.1:8787"
    db_path: Path = BASE_DIR / "orchestrator.db"
    runtime_dir: Path = BASE_DIR / "runtime"
    log_dir: Path = BASE_DIR / "logs"

    # OpenCode / DeepSeek
    opencode_url: str = "http://127.0.0.1:4096"
    opencode_model: str = "opencode/deepseek-v4.1-flash-free"
    opencode_variant: str = ""
    opencode_agent: str = ""
    opencode_username: str = "opencode"
    opencode_password: str = field(default="", repr=False)
    opencode_connect_timeout: int = 10
    opencode_timeout: int = 1800
    opencode_quota_cooldown_minutes: int = 60
    opencode_routes_file: Path = BASE_DIR / "config" / "opencode_routes.json"
    opencode_require_verified_routes: bool = True

    # Antigravity / Gemini
    agy_bin: str = "agy"
    agy_model: str = "gemini-4-argon-high"
    agy_effort: str = ""
    agy_timeout: int = 1200
    agy_prompt_mode: str = "auto"  # auto | inline | file
    agy_resume_conversations: str = "auto"  # auto | on | off
    agy_resume_flag: str = ""  # vazio = detectar via `agy --help`
    agy_effort_flag: str = ""  # vazio = detectar via `agy --help`
    agy_review_mode: str = "plan"     # --mode para planejar/revisar (somente leitura), se `agy --help` anunciar --mode
    agy_implement_mode: str = ""      # --mode para implementar; vazio = comportamento padrão do agy (o validado)
    agy_quota_cooldown_minutes: int = 60
    agy_auto_approve_permissions: bool = False
    gemini_channel: str = "agy"  # agy | api | off
    gemini_channel_risk_ack: bool = False
    gemini_api_key: str = ""
    gemini_api_model: str = "gemini-2.5-pro"
    opencode_review_agent: str = "Plan"  # agente somente leitura do OpenCode para planejar/revisar

    # Fluxo
    max_review_cycles: int = 2
    max_subtasks: int = 5
    max_parallel_subtasks: int = 1
    parallel_execution: bool = False
    mock_agents: bool = False  # ORCH_MOCK_AGENTS: agentes simulados, sem agy/OpenCode
    parallel_per_agent: int = 1
    dependency_policy_file: Path = BASE_DIR / "config" / "dependency_policy.json"
    allow_agent_fallback: bool = False
    final_review: bool = True
    a2a_poll_interval: float = 2.0
    a2a_task_timeout: int = 3600
    max_check_fix_attempts: int = 2
    max_implementation_attempts: int = 4
    max_blocked_subtasks: int = 3
    assignment_negotiation: bool = True
    assignment_rules_file: Path = BASE_DIR / "config" / "assignment_rules.json"
    checks_file: Path = BASE_DIR / "config" / "checks.json"

    # Concorrência
    file_lock_ttl_seconds: int = 1800
    file_lock_wait_seconds: int = 60

    # Contexto e MCP
    context_max_chars: int = 40000
    context_file_max_chars: int = 12000
    context_sources_file: Path = BASE_DIR / "config" / "context_sources.json"
    mcp_config_file: Path = BASE_DIR / "config" / "mcp_servers.json"
    vector_store_file: Path = BASE_DIR / "runtime" / "vector_store.db"

    def secrets(self) -> list[str]:
        return [s for s in (self.opencode_password, self.gemini_api_key) if s]

    def public_summary(self) -> dict:
        """Resumo seguro (sem segredos) para /health e diagnósticos."""
        return {
            "project_root": str(self.project_root),
            "project_root_exists": self.project_root.is_dir(),
            "public_url": self.public_url,
            "db_path": str(self.db_path),
            "opencode_url": self.opencode_url,
            "opencode_model": self.opencode_model,
            "opencode_variant": self.opencode_variant or None,
            "opencode_agent": self.opencode_agent or None,
            "opencode_review_agent": self.opencode_review_agent or None,
            "opencode_auth_configured": bool(self.opencode_username and self.opencode_password),
            "agy_bin": self.agy_bin,
            "agy_model": self.agy_model,
            "agy_effort": self.agy_effort or None,
            "agy_review_mode": self.agy_review_mode or None,
            "agy_implement_mode": self.agy_implement_mode or None,
            "agy_auto_approve_permissions": self.agy_auto_approve_permissions,
            "max_review_cycles": self.max_review_cycles,
            "max_check_fix_attempts": self.max_check_fix_attempts,
            "max_implementation_attempts": self.max_implementation_attempts,
            "max_blocked_subtasks": self.max_blocked_subtasks,
            "assignment_negotiation": self.assignment_negotiation,
            "max_parallel_subtasks": self.max_parallel_subtasks,
            "parallel_execution": self.parallel_execution,
            "parallel_per_agent": self.parallel_per_agent,
        }


def load_settings(env_file: Path | None = None, overrides: Mapping[str, str] | None = None) -> Settings:
    """Lê .env (somente leitura) + variáveis de ambiente. Ambiente do processo tem prioridade."""
    env_file = env_file or (BASE_DIR / ".env")
    values: dict[str, str] = {}
    if env_file.is_file():
        values.update(read_env_file(env_file))
    values.update({k: v for k, v in os.environ.items()})
    if overrides:
        values.update(overrides)
    g = values.get

    host = _clean(g("ORCH_HOST")) or "127.0.0.1"
    port = _int(g("ORCH_PORT"), 8787, 1)
    public_url = (_clean(g("ORCH_PUBLIC_URL")) or f"http://{host}:{port}").rstrip("/")

    project_raw = _clean(g("PROJECT_ROOT"))
    # Vazio NÃO pode virar "." (diretório atual): usa um caminho inexistente para falhar de forma explícita.
    project_root = Path(os.path.expandvars(project_raw)).expanduser() if project_raw else BASE_DIR / "__PROJECT_ROOT_NAO_CONFIGURADO__"

    return Settings(
        project_root=project_root,
        host=host,
        port=port,
        public_url=public_url,
        db_path=_path(g("ORCH_DB_PATH"), BASE_DIR / "orchestrator.db"),
        runtime_dir=_path(g("ORCH_RUNTIME_DIR"), BASE_DIR / "runtime"),
        log_dir=_path(g("ORCH_LOG_DIR"), BASE_DIR / "logs"),
        opencode_url=(_clean(g("OPENCODE_URL")) or "http://127.0.0.1:4096").rstrip("/"),
        opencode_model=_clean(g("OPENCODE_MODEL")) or "opencode/deepseek-v4.1-flash-free",
        opencode_variant=_clean(g("OPENCODE_VARIANT")),
        opencode_agent=_clean(g("OPENCODE_AGENT")),
        opencode_username=_clean(g("OPENCODE_SERVER_USERNAME")) or "opencode",
        opencode_password=_clean(g("OPENCODE_SERVER_PASSWORD")),
        opencode_connect_timeout=_int(g("OPENCODE_CONNECT_TIMEOUT_SECONDS"), 10, 1),
        opencode_timeout=_int(g("OPENCODE_TIMEOUT_SECONDS"), 1800, 5),
        opencode_quota_cooldown_minutes=_int(g("OPENCODE_QUOTA_COOLDOWN_MINUTES"), 60, 0),
        opencode_routes_file=_path(g("OPENCODE_ROUTES_FILE"), BASE_DIR / "config" / "opencode_routes.json"),
        opencode_require_verified_routes=_bool(g("OPENCODE_REQUIRE_VERIFIED_ROUTES"), True),
        agy_bin=_clean(g("AGY_BIN")) or "agy",
        agy_model=_clean(g("AGY_MODEL")) or "gemini-4-argon-high",
        agy_effort=_clean(g("AGY_EFFORT")),
        agy_timeout=_int(g("AGY_TIMEOUT_SECONDS"), 1200, 5),
        agy_prompt_mode=(_clean(g("AGY_PROMPT_MODE")) or "auto").lower(),
        agy_resume_conversations=(_clean(g("AGY_RESUME_CONVERSATIONS")) or "auto").lower(),
        agy_resume_flag=_clean(g("AGY_RESUME_FLAG")),
        agy_effort_flag=_clean(g("AGY_EFFORT_FLAG")),
        agy_review_mode=(_clean(g("AGY_REVIEW_MODE")) if g("AGY_REVIEW_MODE") is not None else "plan").lower(),
        agy_implement_mode=_clean(g("AGY_IMPLEMENT_MODE")).lower(),
        agy_quota_cooldown_minutes=_int(g("AGY_QUOTA_COOLDOWN_MINUTES"), 60, 0),
        agy_auto_approve_permissions=_bool(g("AGY_AUTO_APPROVE_PERMISSIONS") or g("AGY_AUTO_APPROVE"), False),
        gemini_channel=(_clean(g("GEMINI_CHANNEL")) or "agy").lower(),
        gemini_channel_risk_ack=_bool(g("GEMINI_CHANNEL_ACK_RISK") or g("GEMINI_RISK_ACCEPTED"), False),
        gemini_api_key=_clean(g("GEMINI_API_KEY")),
        gemini_api_model=_clean(g("GEMINI_API_MODEL")) or "gemini-2.5-pro",
        opencode_review_agent=_clean(g("OPENCODE_REVIEW_AGENT")) if g("OPENCODE_REVIEW_AGENT") is not None else "Plan",
        max_review_cycles=_int(g("MAX_REVIEW_CYCLES"), 2, 0),
        max_subtasks=_int(g("MAX_SUBTASKS"), 5, 1),
        max_parallel_subtasks=_int(g("MAX_PARALLEL_SUBTASKS"), 1, 1),
        parallel_execution=_bool(g("PARALLEL_EXECUTION"), False),
        mock_agents=_bool(g("ORCH_MOCK_AGENTS"), False),
        parallel_per_agent=_int(g("PARALLEL_PER_AGENT"), 1, 1),
        dependency_policy_file=_path(g("DEPENDENCY_POLICY_FILE"), BASE_DIR / "config" / "dependency_policy.json"),
        allow_agent_fallback=_bool(g("ALLOW_AGENT_FALLBACK"), False),
        final_review=_bool(g("FINAL_REVIEW"), True),
        a2a_poll_interval=float(_int(g("A2A_POLL_INTERVAL_MS"), 2000, 50)) / 1000.0,
        a2a_task_timeout=_int(g("A2A_TASK_TIMEOUT_SECONDS"), 3600, 5),
        max_check_fix_attempts=_int(g("MAX_CHECK_FIX_ATTEMPTS"), 2, 0),
        max_implementation_attempts=_int(g("MAX_IMPLEMENTATION_ATTEMPTS"), 4, 1),
        max_blocked_subtasks=_int(g("MAX_BLOCKED_SUBTASKS"), 3, 1),
        assignment_negotiation=_bool(g("ASSIGNMENT_NEGOTIATION"), True),
        assignment_rules_file=_path(g("ASSIGNMENT_RULES_FILE"), BASE_DIR / "config" / "assignment_rules.json"),
        checks_file=_path(g("CHECKS_FILE"), BASE_DIR / "config" / "checks.json"),
        file_lock_ttl_seconds=_int(g("FILE_LOCK_TTL_SECONDS"), 1800, 5),
        file_lock_wait_seconds=_int(g("FILE_LOCK_WAIT_SECONDS"), 60, 0),
        context_max_chars=_int(g("CONTEXT_MAX_CHARS"), 40000, 1000),
        context_file_max_chars=_int(g("CONTEXT_FILE_MAX_CHARS"), 12000, 500),
        context_sources_file=_path(g("CONTEXT_SOURCES_FILE"), BASE_DIR / "config" / "context_sources.json"),
        mcp_config_file=_path(g("MCP_CONFIG_FILE"), BASE_DIR / "config" / "mcp_servers.json"),
        vector_store_file=_path(g("VECTOR_STORE_FILE"), BASE_DIR / "runtime" / "vector_store.db"),
    )


def update_env_variable(key: str, value: str, env_file: Path | None = None) -> bool:
    """Atualiza ou adiciona uma variável de ambiente no arquivo .env mantendo comentários e formatação."""
    target = env_file or (BASE_DIR / ".env")
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=.*$", re.IGNORECASE)
    new_line = f"{key}={value}\n"

    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_line, encoding="utf-8")
        return True

    lines = target.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    found = False
    updated_lines: list[str] = []

    for line in lines:
        if pattern.match(line):
            updated_lines.append(new_line)
            found = True
        else:
            updated_lines.append(line)

    if not found:
        if updated_lines and not updated_lines[-1].endswith("\n"):
            updated_lines[-1] = updated_lines[-1] + "\n"
        updated_lines.append(new_line)

    target.write_text("".join(updated_lines), encoding="utf-8")
    return True


