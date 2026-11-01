"""Contrato de rotas do OpenCode, verificável contra a OpenAPI exposta pelo servidor."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents.base import ContractNotVerifiedError

_PARAM_RE = re.compile(r"\{[^}]+\}")


def _norm(path: str) -> str:
    return _PARAM_RE.sub("{}", path.rstrip("/") or "/")


@dataclass
class Route:
    name: str
    method: str
    path: str
    verified: bool
    source: str = ""
    query: dict = field(default_factory=dict)
    spec_status: str = "not_checked"  # not_checked | found | missing | no_spec

    @property
    def status(self) -> str:
        if self.spec_status == "found":
            return "verified_by_spec"
        if self.spec_status == "missing":
            return "missing_in_spec"
        return "verified_manual" if self.verified else "unverified"

    @property
    def usable(self) -> bool:
        return self.status in {"verified_by_spec", "verified_manual"}

    def to_dict(self) -> dict:
        return {"method": self.method, "path": self.path, "status": self.status, "source": self.source}


class OpenCodeContract:
    def __init__(self, routes_file: Path, require_verified: bool = True):
        self.routes_file = routes_file
        self.require_verified = require_verified
        data = json.loads(Path(routes_file).read_text(encoding="utf-8"))
        self.openapi_candidates: list[str] = list(data.get("openapi_candidates", []))
        self.routes: dict[str, Route | None] = {}
        for name, r in (data.get("routes") or {}).items():
            if r is None:
                self.routes[name] = None
                continue
            self.routes[name] = Route(
                name=name, method=str(r["method"]).upper(), path=str(r["path"]), verified=bool(r.get("verified")),
                source=str(r.get("source", "")), query=dict(r.get("query") or {}),
            )
        self.spec: dict | None = None
        self.spec_source: str | None = None

    # ---------------------------------------------------------------- OpenAPI
    def apply_spec(self, spec: dict | None, source: str | None) -> None:
        self.spec, self.spec_source = spec, source
        paths = (spec or {}).get("paths") or {}
        index = {_norm(p): {m.upper() for m in (item or {}).keys()} for p, item in paths.items()}
        for route in self.routes.values():
            if route is None:
                continue
            if not spec:
                route.spec_status = "no_spec"
            else:
                methods = index.get(_norm(route.path))
                route.spec_status = "found" if methods and route.method in methods else "missing"

    def _resolve_ref(self, schema: Any, depth: int = 0) -> Any:
        if depth > 10 or not isinstance(schema, dict):
            return schema
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/") and self.spec:
            node: Any = self.spec
            for part in ref[2:].split("/"):
                node = node.get(part, {}) if isinstance(node, dict) else {}
            return self._resolve_ref(node, depth + 1)
        return schema

    def body_properties(self, route_name: str) -> set[str] | None:
        """Propriedades aceitas no corpo segundo a OpenAPI (None = desconhecido)."""
        route = self.routes.get(route_name)
        if not route or not self.spec:
            return None
        for p, item in (self.spec.get("paths") or {}).items():
            if _norm(p) != _norm(route.path):
                continue
            op = (item or {}).get(route.method.lower()) or {}
            content = ((op.get("requestBody") or {}).get("content") or {})
            schema = self._resolve_ref((content.get("application/json") or {}).get("schema"))
            if not isinstance(schema, dict):
                return None
            props: set[str] = set((schema.get("properties") or {}).keys())
            for key in ("allOf", "anyOf", "oneOf"):
                for sub in schema.get(key) or []:
                    props |= set((self._resolve_ref(sub) or {}).get("properties", {}).keys())
            return props or None
        return None

    def filter_body(self, route_name: str, body: dict) -> tuple[dict, list[str]]:
        allowed = self.body_properties(route_name)
        if allowed is None:
            return body, []
        dropped = [k for k in body if k not in allowed]
        return {k: v for k, v in body.items() if k in allowed}, dropped

    # ---------------------------------------------------------------- uso
    def route(self, name: str) -> Route | None:
        return self.routes.get(name)

    def require(self, name: str) -> Route:
        route = self.routes.get(name)
        if route is None:
            raise ContractNotVerifiedError(f"Rota '{name}' não está definida em {self.routes_file.name}.")
        if route.status == "missing_in_spec":
            raise ContractNotVerifiedError(
                f"Rota '{name}' ({route.method} {route.path}) NÃO existe na OpenAPI do servidor ({self.spec_source}). "
                f"Corrija {self.routes_file.name} conforme a documentação do OpenCode 2.2.5."
            )
        if self.require_verified and not route.usable:
            raise ContractNotVerifiedError(
                f"Rota '{name}' ({route.method} {route.path}) ainda não foi confirmada para o OpenCode instalado. "
                f"Confirme na documentação/OpenAPI e marque verified=true em {self.routes_file.name}, "
                "ou defina OPENCODE_REQUIRE_VERIFIED_ROUTES=false por sua conta e risco."
            )
        return route

    def summary(self) -> dict:
        return {
            "spec_source": self.spec_source,
            "require_verified": self.require_verified,
            "routes": {n: (r.to_dict() if r else None) for n, r in self.routes.items()},
        }
