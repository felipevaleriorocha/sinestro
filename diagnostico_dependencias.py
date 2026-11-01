#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Diagnóstico de dependências do jogo (SOMENTE LEITURA, sem modelos, sem rede, sem custo).

Mostra, a partir dos arquivos .lua/.luau do PROJECT_ROOT:
  1. grafo de require (resolvido por caminho relativo, por nome ou ambíguo) e ciclos;
  2. fan-in (direto e transitivo) e fan-out, com a distribuição que calibra o limite "fan-in <= 3" do LOW;
  3. módulos ambíguos (mesmo nome em mais de um arquivo) e requires incertos (dinâmicos, por string, sem alvo);
  4. requires usados só por efeito colateral (variável nunca usada), como no init.server.luau;
  5. remotes (RemoteEvent/RemoteFunction): quem trata, quem envia, órfãos e nomes dinâmicos;
  6. módulos de contrato/persistência (DataService, *Config, Network...) e arquivos que tocam DataStore;
  7. recursos de Studio/MCP potencialmente compartilhados (configs MCP, mapeamento Rojo, Workspace, ferramentas citadas).

Uso, na pasta do orquestrador (usa PROJECT_ROOT do .env, que só é LIDO):
    .\.venv\Scripts\python.exe diagnostico_dependencias.py --autoteste     # valida os parsers num projeto de mentira
    .\.venv\Scripts\python.exe diagnostico_dependencias.py                 # diagnóstico completo do jogo
    .\.venv\Scripts\python.exe diagnostico_dependencias.py --raiz "C:\caminho\do\jogo"
    .\.venv\Scripts\python.exe diagnostico_dependencias.py --relacao src/shared/modules/BigNum.luau src/server/services/FarmService.luau

Saídas (só dentro de runtime\diagnostico\ e uma cópia do resumo na Área de Trabalho):
    diagnostico_resumo.txt, diagnostico.json, grafo_require.dot

Garantias: nenhum arquivo do jogo é alterado; nenhum modelo é chamado; nenhum comando externo é executado;
configs MCP são lidas apenas para listar nomes de servidores (valores e variáveis de ambiente nunca são exibidos).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from bisect import bisect_right
from collections import Counter, defaultdict, deque
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

EXCLUDE_DIRS = {"node_modules", "Packages", "DevPackages", "build", "out", "dist", "__pycache__", "venv"}
LUA_EXT = (".lua", ".luau")
MAX_FILE_BYTES = 2_000_000

CONTRACT_NAME_RE = re.compile(
    r"(?i)^(DataService|ProfileService|ProfileStore|DataStore\w*|\w*Config|Network|Types|Constants|Remotes?|Schema|Profile\w*)$")
PERSISTENCE_RE = re.compile(
    r"\b(DataStoreService|GetDataStore|GetOrderedDataStore|UpdateAsync|SetAsync|GetAsync|ProfileService|ProfileStore|LoadProfileAsync)\b")
GEOMETRY_NEW_RE = re.compile(
    r"\bInstance\s*\.\s*new\s*\(\s*[\"'](Part|MeshPart|WedgePart|SpawnLocation|UnionOperation|Model|Folder)[\"']")
WORKSPACE_RE = re.compile(
    r"(?:\bworkspace|\bgame\s*\.\s*Workspace|\bgame\s*:\s*GetService\s*\(\s*[\"']Workspace[\"']\s*\))"
    r"\s*(?:\.\s*([A-Za-z_]\w*)|:\s*(?:WaitForChild|FindFirstChild)\s*\(\s*[\"']([^\"']+)[\"'])")
WORKSPACE_BUILTINS = {
    "CurrentCamera", "Gravity", "Terrain", "FindFirstChild", "WaitForChild", "GetDescendants", "GetChildren", "Raycast",
    "GetServerTimeNow", "DistributedGameTime", "StreamingEnabled", "FallenPartsDestroyHeight", "ClearAllChildren", "IsA",
    "Destroy", "FindFirstChildOfClass", "GetPartBoundsInBox", "GetPartBoundsInRadius", "GetPartsInPart", "Name",
}
MCP_TOKEN_RE = re.compile(r"\b(execute_luau|generate_mesh|segment_mesh)\b|\bMCP\b")
MCP_CANDIDATES_PROJECT = [".mcp.json", "opencode.json", "opencode.jsonc", ".opencode/opencode.json",
                          ".gemini/settings.json", ".vscode/mcp.json", ".cursor/mcp.json"]
MCP_CANDIDATES_ORCH = ["config/mcp_servers.json", "config/mcp_servers.example.json"]

# ---------------------------------------------------------------- limpeza de comentários (mantém strings)
SPECIAL = re.compile(r"--|\[=*\[|[\"'`]")
LONG_OPEN = re.compile(r"\[(=*)\[")


class Spans:
    """Intervalos [início, fim) ocupados por strings, para ignorar 'require(' dentro de texto."""

    def __init__(self) -> None:
        self.starts: list[int] = []
        self.ends: list[int] = []

    def add(self, a: int, b: int) -> None:
        self.starts.append(a)
        self.ends.append(b)

    def inside(self, pos: int) -> bool:
        i = bisect_right(self.starts, pos) - 1
        return i >= 0 and pos < self.ends[i]


def strip_comments(src: str) -> tuple[str, Spans]:
    """Remove comentários (trocando por espaços/quebras, mantendo posições) e registra onde ficam as strings."""
    out: list[str] = []
    spans = Spans()
    i, n = 0, len(src)
    while i < n:
        m = SPECIAL.search(src, i)
        if not m:
            out.append(src[i:])
            break
        out.append(src[i:m.start()])
        i = m.start()
        tok = m.group(0)
        if tok == "--":
            lm = LONG_OPEN.match(src, i + 2)
            if lm:
                close = "]" + lm.group(1) + "]"
                j = src.find(close, lm.end())
                j = n if j == -1 else j + len(close)
                out.append(re.sub(r"[^\n]", " ", src[i:j]))
            else:
                j = src.find("\n", i)
                j = n if j == -1 else j
                out.append(" " * (j - i))
            i = j
        elif tok.startswith("["):
            close = "]" + tok[1:-1] + "]"
            j = src.find(close, m.end())
            j = n if j == -1 else j + len(close)
            out.append(src[i:j])
            spans.add(i, j)
            i = j
        else:
            q = tok
            j = i + 1
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == q:
                    j += 1
                    break
                if ch == "\n" and q != "`":
                    break
                j += 1
            j = min(j, n)
            out.append(src[i:j])
            spans.add(i, j)
            i = j
    return "".join(out), spans


def iter_code(rx: re.Pattern, text: str, spans: Spans):
    for m in rx.finditer(text):
        if not spans.inside(m.start()):
            yield m


# ---------------------------------------------------------------- require
REQ = re.compile(r"(?<![\w.:])require\s*\(")
BIND_REQ = re.compile(r"\blocal\s+([A-Za-z_]\w*)\s*(?::[^=\n]+)?=\s*require\s*\(")
Q = "(?:\"[^\"]*\"|'[^']*')"
SEG = re.compile(
    r"\s*(?:\.\s*(?P<attr>[A-Za-z_]\w*)"
    r"|:\s*(?P<method>WaitForChild|FindFirstChild|FindFirstChildOfClass|FindFirstDescendant|GetService)"
    r"\s*\(\s*(?P<arg>" + Q + r")\s*(?:,[^()]*)?\)"
    r"|\[\s*(?P<idx>" + Q + r")\s*\])")
STR_LIT = re.compile(r"^\s*([\"'])(.*)\1\s*$", re.S)
NUM_LIT = re.compile(r"^\s*\d+\s*$")


def balanced(text: str, start: int) -> tuple[str, int]:
    """Texto entre o '(' (já consumido, start aponta para depois dele) e o ')' correspondente. Retorna (expr, índice do ')')."""
    depth, i, n = 1, start, len(text)
    while i < n:
        c = text[i]
        if c in "\"'`":
            j = i + 1
            while j < n and text[j] != c:
                if text[j] == "\\":
                    j += 1
                j += 1
            i = j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[start:i], i
        i += 1
    return text[start:], n


def parse_require_expr(expr: str) -> dict:
    e = expr.strip()
    m = STR_LIT.match(e)
    if m:
        return {"kind": "string", "value": m.group(2)}
    if NUM_LIT.match(e):
        return {"kind": "asset_id", "value": e}
    rm = re.match(r"\s*([A-Za-z_]\w*)", e)
    if not rm:
        return {"kind": "dynamic", "why": "expressão não reconhecida"}
    root, pos, segs = rm.group(1), rm.end(), []
    while pos < len(e):
        sm = SEG.match(e, pos)
        if not sm or sm.end() == pos:
            break
        name = sm.group("attr")
        if name is None:
            name = (sm.group("arg") or sm.group("idx")).strip("\"'")
        segs.append(name)
        pos = sm.end()
    rest = e[pos:].strip()
    if rest:
        return {"kind": "dynamic", "why": f"trecho não resolvível estaticamente: {rest[:60]}"}
    if not segs:
        return {"kind": "variable", "root": root, "why": f"require de variável ({root})"}
    return {"kind": "path", "root": root, "segs": segs}


# ---------------------------------------------------------------- remotes
RE_BIND_REMOTE = re.compile(
    r"\blocal\s+([A-Za-z_]\w*)\s*(?::[^=\n]+)?=\s*(?:[\w.]+\.)?getRemote(Event|Function)\s*\(\s*(" + Q + r")\s*\)")
RE_GET_REMOTE = re.compile(r"\bgetRemote(Event|Function)\s*\(\s*(" + Q + r"|[^)]*?)\s*\)")
RE_CHAIN = re.compile(
    r"\bgetRemote(?:Event|Function)\s*\(\s*(" + Q + r")\s*\)\s*(?:\.\s*(OnServerEvent|OnClientEvent|OnServerInvoke|OnClientInvoke)"
    r"|:\s*(FireServer|FireClient|FireAllClients|FireClients|InvokeServer|InvokeClient))")
ROLE = {
    "OnServerEvent": "servidor trata", "OnServerInvoke": "servidor trata",
    "OnClientEvent": "cliente trata", "OnClientInvoke": "cliente trata",
    "FireServer": "cliente envia", "InvokeServer": "cliente envia",
    "FireClient": "servidor envia", "FireAllClients": "servidor envia", "FireClients": "servidor envia",
    "InvokeClient": "servidor envia",
}


def classify_file(rel: str) -> dict:
    p = PurePosixPath(rel)
    base = p.name
    stem = base[: base.rfind(".")]
    kind = "module"
    if stem.endswith(".server"):
        kind, stem = "server", stem[:-7]
    elif stem.endswith(".client"):
        kind, stem = "client", stem[:-7]
    parent = p.parent.parts
    is_init = stem == "init"
    name = (parent[-1] if parent else "init") if is_init else stem
    inst = tuple(parent) if is_init else tuple(parent) + (name,)
    return {"path": rel, "kind": kind, "name": name, "is_init": is_init, "inst": inst, "dir": tuple(parent)}


def parse_lua(text: str) -> dict:
    clean, spans = strip_comments(text)

    def line_of(pos: int) -> int:
        return clean.count("\n", 0, pos) + 1

    requires, unused = [], []
    for m in iter_code(REQ, clean, spans):
        expr, _ = balanced(clean, m.end())
        requires.append({"line": line_of(m.start()), "expr": " ".join(expr.split())[:200], "parsed": parse_require_expr(expr)})
    for m in iter_code(BIND_REQ, clean, spans):
        var = m.group(1)
        if var == "_":
            continue
        _, end = balanced(clean, m.end())
        rest = clean[:m.start()] + " " + clean[end + 1:]
        if not re.search(r"\b" + re.escape(var) + r"\b", rest):
            unused.append({"var": var, "line": line_of(m.start())})

    remote_vars: dict[str, tuple[str, str]] = {}
    remote_usage: list[dict] = []
    remote_calls: list[dict] = []
    for m in iter_code(RE_BIND_REMOTE, clean, spans):
        remote_vars[m.group(1)] = (m.group(3).strip("\"'"), m.group(2).lower())
    for m in iter_code(RE_GET_REMOTE, clean, spans):
        arg = m.group(2).strip()
        literal = arg[:1] in "\"'" and arg[-1:] == arg[:1] and len(arg) >= 2
        remote_calls.append({"line": line_of(m.start()), "type": m.group(1).lower(),
                             "name": arg.strip("\"'") if literal else None, "expr": None if literal else arg[:80]})
    for var, (rname, rtype) in remote_vars.items():
        rx = re.compile(r"\b" + re.escape(var) + r"\s*(?:\.\s*(OnServerEvent|OnServerInvoke|OnClientEvent|OnClientInvoke)\b"
                        r"|:\s*(FireServer|InvokeServer|FireClient|FireAllClients|FireClients|InvokeClient)\b)")
        for m in iter_code(rx, clean, spans):
            remote_usage.append({"remote": rname, "type": rtype, "role": ROLE[m.group(1) or m.group(2)], "line": line_of(m.start())})
    for m in iter_code(RE_CHAIN, clean, spans):
        remote_usage.append({"remote": m.group(1).strip("\"'"), "type": None, "role": ROLE[m.group(2) or m.group(3)],
                             "line": line_of(m.start())})

    ws = Counter()
    for m in iter_code(WORKSPACE_RE, clean, spans):
        name = m.group(1) or m.group(2)
        if name and name not in WORKSPACE_BUILTINS:
            ws[name] += 1
    return {
        "lines": clean.count("\n") + 1,
        "requires": requires,
        "unused_require_bindings": unused,
        "remote_calls": remote_calls,
        "remote_usage": remote_usage,
        "persistence": any(True for _ in iter_code(PERSISTENCE_RE, clean, spans)),
        "geometry_new": sum(1 for _ in iter_code(GEOMETRY_NEW_RE, clean, spans)),
        "workspace_refs": dict(ws),
    }


# ---------------------------------------------------------------- análise
class Analysis:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.mods: dict[str, dict] = {}
        self.by_name: dict[str, list[str]] = defaultdict(list)
        self.inst_index: dict[tuple, str] = {}
        self.parsed: dict[str, dict] = {}
        self.edges: dict[str, set] = defaultdict(set)
        self.rev: dict[str, set] = defaultdict(set)
        self.stats: Counter = Counter()
        self.issues: dict[str, list] = {"ambiguous": [], "unresolved": [], "dynamic": [], "uncertain": []}
        self.skipped: list[str] = []
        self.remotes: dict[str, dict] = {}
        self.remote_dynamic: list[dict] = []
        self.studio: dict = {}

    # -- varredura
    def run(self) -> "Analysis":
        self._scan()
        self._parse()
        self._resolve()
        self._remotes()
        self._studio()
        return self

    def _scan(self) -> None:
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith("."))
            for fn in sorted(filenames):
                if not fn.lower().endswith(LUA_EXT):
                    continue
                full = Path(dirpath) / fn
                rel = full.relative_to(self.root).as_posix()
                try:
                    if full.stat().st_size > MAX_FILE_BYTES:
                        self.skipped.append(rel)
                        continue
                except OSError:
                    continue
                self.mods[rel] = classify_file(rel)
        for rel, info in self.mods.items():
            if info["kind"] == "module":
                self.by_name[info["name"]].append(rel)
                self.inst_index[info["inst"]] = rel

    def _parse(self) -> None:
        for rel in self.mods:
            try:
                text = (self.root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            self.parsed[rel] = parse_lua(text)

    # -- resolução de require
    def _resolve_one(self, mod: dict, parsed: dict) -> dict:
        k = parsed["kind"]
        if k in ("string", "asset_id"):
            return {"status": "uncertain", "reason": f"require por {'string' if k == 'string' else 'asset id'}: {parsed['value'][:60]}"}
        if k in ("dynamic", "variable"):
            return {"status": "dynamic", "reason": parsed.get("why", "dinâmico")}
        segs, root = parsed["segs"], parsed["root"]
        if root == "script":
            loc = list(mod["inst"])
            for s in segs:
                loc = loc[:-1] if s == "Parent" else loc + [s]
            tgt = self.inst_index.get(tuple(loc))
            if tgt:
                return {"status": "resolved", "target": tgt, "method": "caminho relativo"}
        name = segs[-1] if segs and segs[-1] != "Parent" else None
        if not name:
            return {"status": "unresolved", "name": "Parent", "reason": "script.Parent sem init correspondente"}
        cands = self.by_name.get(name, [])
        if not cands:
            return {"status": "unresolved", "name": name, "reason": "nenhum ModuleScript com esse nome (pacote externo, serviço ou pasta?)"}
        if len(cands) == 1:
            return {"status": "resolved", "target": cands[0], "method": "nome"}
        prev = (segs[-2] if len(segs) >= 2 else root).lower()
        filt = [c for c in cands if self.mods[c]["inst"][-2:-1] and self.mods[c]["inst"][-2].lower() == prev]
        if len(filt) == 1:
            return {"status": "resolved", "target": filt[0], "method": "nome+pasta"}
        return {"status": "ambiguous", "name": name, "candidates": sorted(cands)}

    def _resolve(self) -> None:
        for rel, p in self.parsed.items():
            mod = self.mods[rel]
            for r in p["requires"]:
                res = self._resolve_one(mod, r["parsed"])
                r["resolution"] = res
                self.stats[res["status"]] += 1
                item = {"file": rel, "line": r["line"], "expr": r["expr"], **{k: v for k, v in res.items() if k != "status"}}
                if res["status"] == "resolved":
                    if res["target"] != rel:
                        self.edges[rel].add(res["target"])
                        self.rev[res["target"]].add(rel)
                    self.stats["via " + res["method"]] += 1
                else:
                    self.issues[res["status"]].append(item)

    # -- métricas
    def fan_in(self, path: str) -> int:
        return len(self.rev.get(path, ()))

    def fan_out(self, path: str) -> int:
        return len(self.edges.get(path, ()))

    def _closure(self, start: str, graph: dict) -> set:
        seen, dq = set(), deque([start])
        while dq:
            cur = dq.popleft()
            for nxt in graph.get(cur, ()):
                if nxt not in seen and nxt != start:
                    seen.add(nxt)
                    dq.append(nxt)
        return seen

    def transitive_dependents(self, path: str) -> set:
        return self._closure(path, self.rev)

    def transitive_deps(self, path: str) -> set:
        return self._closure(path, self.edges)

    def modules(self) -> list[str]:
        return [p for p, m in self.mods.items() if m["kind"] == "module"]

    def ambiguous_names(self) -> dict[str, list[str]]:
        return {n: sorted(c) for n, c in self.by_name.items() if len(c) > 1}

    def cycles(self) -> list[list[str]]:
        graph = {n: set(t for t in tg if self.mods[t]["kind"] == "module") for n, tg in self.edges.items()
                 if self.mods[n]["kind"] == "module"}
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        onstack: set = set()
        stack: list[str] = []
        result: list[list[str]] = []
        counter = 0
        for root in list(graph):
            if root in index:
                continue
            index[root] = low[root] = counter
            counter += 1
            stack.append(root)
            onstack.add(root)
            work = [(root, iter(sorted(graph.get(root, ()))))]
            while work:
                node, it = work[-1]
                advanced = False
                for nb in it:
                    if nb not in index:
                        index[nb] = low[nb] = counter
                        counter += 1
                        stack.append(nb)
                        onstack.add(nb)
                        work.append((nb, iter(sorted(graph.get(nb, ())))))
                        advanced = True
                        break
                    if nb in onstack:
                        low[node] = min(low[node], index[nb])
                if advanced:
                    continue
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    comp = []
                    while True:
                        w = stack.pop()
                        onstack.discard(w)
                        comp.append(w)
                        if w == node:
                            break
                    if len(comp) > 1 or node in graph.get(node, ()):
                        result.append(sorted(comp))
        return sorted(result)

    def contract_modules(self) -> list[str]:
        return sorted(p for p in self.modules() if CONTRACT_NAME_RE.match(self.mods[p]["name"]))

    def persistence_files(self) -> list[str]:
        return sorted(p for p, d in self.parsed.items() if d["persistence"])

    # -- remotes
    def _remotes(self) -> None:
        for rel, p in self.parsed.items():
            for c in p["remote_calls"]:
                if c["name"] is None:
                    self.remote_dynamic.append({"file": rel, "line": c["line"], "expr": c["expr"]})
                    continue
                r = self.remotes.setdefault(c["name"], {"types": set(), "refs": set(), "roles": defaultdict(set)})
                r["types"].add(c["type"])
                r["refs"].add(rel)
            for u in p["remote_usage"]:
                r = self.remotes.setdefault(u["remote"], {"types": set(), "refs": set(), "roles": defaultdict(set)})
                if u["type"]:
                    r["types"].add(u["type"])
                r["refs"].add(rel)
                r["roles"][u["role"]].add(rel)

    def remote_findings(self) -> dict:
        orphans = []
        for name, r in sorted(self.remotes.items()):
            roles = r["roles"]
            if roles.get("servidor trata") and not roles.get("cliente envia"):
                orphans.append((name, "servidor trata, mas nenhum arquivo do cliente envia"))
            if roles.get("cliente envia") and not roles.get("servidor trata"):
                orphans.append((name, "cliente envia, mas nenhum arquivo do servidor trata"))
            if roles.get("cliente trata") and not roles.get("servidor envia"):
                orphans.append((name, "cliente trata, mas nenhum arquivo do servidor envia"))
            if roles.get("servidor envia") and not roles.get("cliente trata"):
                orphans.append((name, "servidor envia, mas nenhum arquivo do cliente trata"))
            if not roles:
                orphans.append((name, "obtido por getRemote*, mas nunca usado (sem handler nem envio)"))
        return {"orphans": orphans}

    # -- Studio / MCP
    def _studio(self) -> None:
        st: dict = {"project_files": [], "mcp_configs": [], "doc_mentions": [], "workspace_refs": {}, "geometry_builders": []}
        for pf in sorted(self.root.glob("*.project.json")):
            try:
                data = json.loads(pf.read_text(encoding="utf-8-sig"))
            except Exception as exc:  # noqa: BLE001
                st["project_files"].append({"file": pf.name, "error": type(exc).__name__})
                continue
            entries: list[dict] = []

            def walk(node, trail, depth):
                if not isinstance(node, dict) or depth > 4:
                    return
                path_val = node.get("$path")
                entries.append({"node": "/".join(trail) or "(raiz)", "className": node.get("$className"),
                                "path": None if path_val is None else str(path_val),
                                "ignoreUnknownInstances": node.get("$ignoreUnknownInstances")})
                for k, v in node.items():
                    if not k.startswith("$"):
                        walk(v, trail + [k], depth + 1)

            walk(data.get("tree"), [], 0)
            st["project_files"].append({"file": pf.name, "name": data.get("name"), "entries": entries})
        candidates = [(self.root / c, c) for c in MCP_CANDIDATES_PROJECT] + [(Path.cwd() / c, "(orquestrador) " + c) for c in MCP_CANDIDATES_ORCH]
        for path, label in candidates:
            if path.is_file():
                st["mcp_configs"].append(self._mcp_summary(path, label))
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith("."))
            for fn in filenames:
                if not fn.lower().endswith(".md"):
                    continue
                full = Path(dirpath) / fn
                try:
                    if full.stat().st_size > 1_000_000:
                        continue
                    lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                hits = [(i + 1, ln.strip()[:160]) for i, ln in enumerate(lines) if MCP_TOKEN_RE.search(ln)]
                if hits:
                    st["doc_mentions"].append({"file": full.relative_to(self.root).as_posix(), "count": len(hits), "samples": hits[:3]})
        ws_files: dict[str, set] = defaultdict(set)
        for rel, p in self.parsed.items():
            for name in p["workspace_refs"]:
                ws_files[name].add(rel)
            if p["geometry_new"]:
                st["geometry_builders"].append({"file": rel, "count": p["geometry_new"]})
        st["workspace_refs"] = {n: sorted(f) for n, f in ws_files.items()}
        st["geometry_builders"].sort(key=lambda x: -x["count"])
        self.studio = st

    @staticmethod
    def _mcp_summary(path: Path, label: str) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            return {"file": label, "error": f"não foi possível ler ({type(exc).__name__}; JSONC?)"}
        servers = next((data[k] for k in ("mcpServers", "mcp", "servers") if isinstance(data.get(k), dict)), None)
        out = []
        for name, cfg in (servers or {}).items():
            if not isinstance(cfg, dict):
                continue
            cmd = cfg.get("command")
            if isinstance(cmd, list) and cmd:
                cmd = cmd[0]
            host = urlparse(str(cfg["url"])).netloc.split("@")[-1] if cfg.get("url") else None
            agents = cfg.get("agents")
            out.append({"name": name, "enabled": cfg.get("enabled"), "command": Path(str(cmd)).name if cmd else None, "host": host,
                        "agents": {a: (v.get("enabled") if isinstance(v, dict) else None) for a, v in agents.items()}
                        if isinstance(agents, dict) else None})
        return {"file": label, "servers": out}

    # -- relação entre dois arquivos
    def find(self, query: str) -> str:
        q = query.replace("\\", "/").lstrip("./").lower()
        exact = [p for p in self.mods if p.lower() == q]
        if len(exact) == 1:
            return exact[0]
        hits = [p for p in self.mods if p.lower().endswith(q)]
        if len(hits) == 1:
            return hits[0]
        raise ValueError(f"'{query}': " + ("não encontrado" if not hits else "ambíguo entre " + ", ".join(sorted(hits)[:6])))

    def relation(self, qa: str, qb: str) -> list[str]:
        a, b = self.find(qa), self.find(qb)
        out = [f"A = {a}", f"B = {b}"]
        hints: list[str] = []
        if a == b:
            return out + ["mesmo arquivo: conflito certo (SHARED)."]
        if b in self.edges[a]:
            hints.append("A requer B diretamente")
        if a in self.edges[b]:
            hints.append("B requer A diretamente")
        if b in self.transitive_deps(a) and b not in self.edges[a]:
            hints.append("A depende de B por caminho indireto")
        if a in self.transitive_deps(b) and a not in self.edges[b]:
            hints.append("B depende de A por caminho indireto")
        common = sorted(self.edges[a] & self.edges[b])
        contract = [c for c in common if c in set(self.contract_modules())]
        if contract:
            hints.append("importam o mesmo módulo de contrato: " + ", ".join(contract))
        elif common:
            out.append("dependências em comum (informativo): " + ", ".join(common[:8]))
        shared_remotes = sorted(n for n, r in self.remotes.items() if a in r["refs"] and b in r["refs"])
        if shared_remotes:
            hints.append("usam os mesmos remotes: " + ", ".join(shared_remotes))
        if self.parsed[a]["persistence"] and self.parsed[b]["persistence"]:
            hints.append("os dois tocam persistência (DataStore/Profile)")
        uncertain = [i for i in self.issues["dynamic"] + self.issues["uncertain"] if i["file"] in (a, b)]
        if uncertain:
            hints.append("há require dinâmico/incerto em um dos arquivos -> sequencial por segurança")
        out.append(f"fan-in A={self.fan_in(a)} (transitivo {len(self.transitive_dependents(a))}), "
                   f"B={self.fan_in(b)} (transitivo {len(self.transitive_dependents(b))})")
        if hints:
            out.append("INDÍCIOS DE SHARED/SEQUENCIAL:")
            out += [f"  - {h}" for h in hints]
        else:
            out.append("nenhum indício estático de dependência. ISTO NÃO PROVA independência: o planejador pode omitir arquivos "
                       "e os fatos do código só cobrem require, remotes e persistência.")
        return out

    # -- exportação
    def to_json(self) -> dict:
        files = {}
        for rel, m in self.mods.items():
            p = self.parsed.get(rel, {})
            files[rel] = {
                "kind": m["kind"], "name": m["name"], "lines": p.get("lines"),
                "fan_in": self.fan_in(rel), "fan_in_transitive": len(self.transitive_dependents(rel)) if m["kind"] == "module" else None,
                "fan_out": self.fan_out(rel),
                "requires_resolvidos": sorted(self.edges.get(rel, ())),
                "persistencia": p.get("persistence", False),
            }
        return {
            "root": str(self.root), "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "totais": {"arquivos": len(self.mods), "modulos": len(self.modules()), "requires": dict(self.stats)},
            "arquivos": files,
            "ambiguos": self.ambiguous_names(),
            "problemas": self.issues,
            "ciclos": self.cycles(),
            "contratos": self.contract_modules(),
            "persistencia": self.persistence_files(),
            "remotes": {n: {"tipos": sorted(r["types"]), "arquivos": sorted(r["refs"]),
                            "papeis": {k: sorted(v) for k, v in r["roles"].items()}} for n, r in sorted(self.remotes.items())},
            "remotes_dinamicos": self.remote_dynamic,
            "remotes_orfaos": self.remote_findings()["orphans"],
            "requires_so_efeito_colateral": {rel: p["unused_require_bindings"] for rel, p in self.parsed.items() if p["unused_require_bindings"]},
            "studio": self.studio,
            "ignorados_por_tamanho": self.skipped,
        }

    def to_dot(self) -> str:
        lines = ["digraph require {", "  rankdir=LR;", "  node [shape=box, fontsize=10];"]
        for rel, m in self.mods.items():
            if self.edges.get(rel) or self.rev.get(rel):
                shape = "box" if m["kind"] == "module" else "ellipse"
                lines.append(f'  "{rel}" [label="{m["name"]}", shape={shape}];')
        for src, tgts in sorted(self.edges.items()):
            for t in sorted(tgts):
                lines.append(f'  "{src}" -> "{t}";')
        lines.append("}")
        return "\n".join(lines)


# ---------------------------------------------------------------- relatório em texto
def render(a: Analysis, top: int) -> str:
    L: list[str] = []

    def sec(title: str) -> None:
        L.extend(["", "=" * 78, title, "=" * 78])

    def more(items: list, limit: int) -> list:
        if len(items) > limit:
            L.append(f"  ... (+{len(items) - limit} não exibidos; veja diagnostico.json)")
        return items[:limit]

    mods = a.modules()
    scripts = [p for p, m in a.mods.items() if m["kind"] != "module"]
    L.append(f"DIAGNÓSTICO DE DEPENDÊNCIAS - {a.root}")
    L.append(f"gerado em {time.strftime('%Y-%m-%d %H:%M:%S')} | somente leitura | nenhum modelo foi chamado")

    sec("1. RESUMO")
    s = a.stats
    total_req = sum(s[k] for k in ("resolved", "ambiguous", "unresolved", "dynamic", "uncertain"))
    L.append(f"arquivos Lua/Luau: {len(a.mods)} ({len(mods)} ModuleScripts, {len(scripts)} scripts de entrada server/client)")
    L.append(f"chamadas require: {total_req} | resolvidas: {s['resolved']} (caminho relativo {s['via caminho relativo']}, "
             f"nome {s['via nome']}, nome+pasta {s['via nome+pasta']})")
    L.append(f"ambíguas: {s['ambiguous']} | sem alvo: {s['unresolved']} | dinâmicas: {s['dynamic']} | por string/asset: {s['uncertain']}")
    if total_req:
        certain = s["resolved"] / total_req * 100
        L.append(f"cobertura da análise estática de require: {certain:.0f}% resolvido com certeza")
    if a.skipped:
        L.append(f"ignorados por tamanho (> {MAX_FILE_BYTES // 1000} KB): {len(a.skipped)}")

    sec("2. FAN-IN E FAN-OUT (módulos)")
    rows = sorted(mods, key=lambda p: (-a.fan_in(p), -len(a.transitive_dependents(p)), p))
    L.append("direto transit. saída  módulo")
    for p in rows[:top]:
        L.append(f"{a.fan_in(p):>6} {len(a.transitive_dependents(p)):>6} {a.fan_out(p):>5}  {p}")
    if len(rows) > top:
        L.append(f"  ... (+{len(rows) - top} módulos; veja diagnostico.json)")
    buckets = Counter()
    for p in mods:
        f = a.fan_in(p)
        buckets["0" if f == 0 else "1" if f == 1 else "2-3" if f <= 3 else "4-9" if f <= 9 else "10+"] += 1
    L.append("distribuição do fan-in direto: " + " | ".join(f"{k}: {buckets[k]}" for k in ("0", "1", "2-3", "4-9", "10+")))
    low = sum(1 for p in mods if a.fan_in(p) <= 3)
    L.append(f"módulos com fan-in <= 3 (limite do LOW): {low} de {len(mods)}"
             + (f" ({low / len(mods) * 100:.0f}%)" if mods else ""))
    L.append("fan-in 0 = não requerido por ninguém (ponto de entrada indireto, código morto ou carregado por outro meio)")

    sec("3. MÓDULOS AMBÍGUOS (mesmo nome em mais de um arquivo)")
    amb = a.ambiguous_names()
    if not amb:
        L.append("nenhum")
    for n, c in more(sorted(amb.items()), top):
        L.append(f"  {n}: " + " | ".join(c))
    if a.issues["ambiguous"]:
        L.append(f"requires que continuam ambíguos mesmo com caminho/pasta ({len(a.issues['ambiguous'])}):")
        for i in more(a.issues["ambiguous"], top):
            L.append(f"  {i['file']}:{i['line']}  {i['expr']}  -> {len(i['candidates'])} candidatos")

    sec("4. REQUIRES INCERTOS (forçam execução sequencial por segurança)")
    unc = a.issues["dynamic"] + a.issues["uncertain"]
    if not unc:
        L.append("nenhum")
    for i in more(unc, max(top, 20)):
        L.append(f"  {i['file']}:{i['line']}  {i['expr']}  [{i.get('reason', '')}]")
    L.append("")
    L.append(f"requires sem alvo no projeto ({len(a.issues['unresolved'])}): nomes mais frequentes")
    names = Counter(i["name"] for i in a.issues["unresolved"])
    for n, c in names.most_common(top):
        ex = next(i for i in a.issues["unresolved"] if i["name"] == n)
        L.append(f"  {n}: {c}x  (ex.: {ex['file']}:{ex['line']})")
    if not names:
        L.append("  nenhum")

    sec("5. CICLOS DE REQUIRE")
    cyc = a.cycles()
    if not cyc:
        L.append("nenhum ciclo entre ModuleScripts")
    for c in cyc:
        L.append("  ciclo: " + " <-> ".join(c))

    sec("6. REQUIRES SÓ POR EFEITO COLATERAL (variável nunca usada)")
    se = {rel: p["unused_require_bindings"] for rel, p in a.parsed.items() if p["unused_require_bindings"]}
    if not se:
        L.append("nenhum")
    for rel, items in more(sorted(se.items()), top):
        L.append(f"  {rel}: " + ", ".join(f"{i['var']}(L{i['line']})" for i in items[:8]) + (" ..." if len(items) > 8 else ""))
    if se:
        L.append("ATENÇÃO: remover esses require muda o que é carregado no jogo, mesmo com o selene apontando 'unused_variable'.")

    sec("7. REMOTES")
    L.append(f"remotes distintos: {len(a.remotes)} | nomes dinâmicos (não analisáveis): {len(a.remote_dynamic)}")
    for name, r in more(sorted(a.remotes.items()), max(top, 25)):
        roles = "; ".join(f"{k}: {len(v)} arq." for k, v in sorted(r["roles"].items())) or "sem handler/envio detectado"
        L.append(f"  {name} [{'/'.join(sorted(r['types'])) or '?'}]  {roles}")
    orphans = a.remote_findings()["orphans"]
    if orphans:
        L.append("possíveis órfãos (código morto ou contrato incompleto):")
        for n, why in more(orphans, top):
            L.append(f"  {n}: {why}")
    for d in more(a.remote_dynamic, 8):
        L.append(f"  nome dinâmico: {d['file']}:{d['line']}  {d['expr']}")

    sec("8. CONTRATOS E PERSISTÊNCIA")
    L.append("módulos com nome de contrato (DataService, *Config, Network, Types...):")
    for p in a.contract_modules():
        L.append(f"  {p}  (fan-in {a.fan_in(p)}, transitivo {len(a.transitive_dependents(p))})")
    if not a.contract_modules():
        L.append("  nenhum")
    pf = a.persistence_files()
    L.append(f"arquivos que tocam DataStore/Profile ({len(pf)}):")
    for p in more(pf, top):
        L.append(f"  {p}")
    L.append("(GetAsync/SetAsync também existem em HttpService; trate como indício, não como prova)")

    sec("9. STUDIO / MCP - RECURSOS POTENCIALMENTE COMPARTILHADOS")
    st = a.studio
    if st["mcp_configs"]:
        L.append("configurações MCP encontradas (só nomes; valores e variáveis de ambiente não são lidos para exibição):")
        for c in st["mcp_configs"]:
            if "error" in c:
                L.append(f"  {c['file']}: {c['error']}")
                continue
            L.append(f"  {c['file']}: " + (", ".join(
                f"{s['name']}" + (f" (enabled={s['enabled']})" if s["enabled"] is not None else "")
                + (f" cmd={s['command']}" if s["command"] else "") + (f" host={s['host']}" if s["host"] else "")
                + (f" agentes={s['agents']}" if s["agents"] else "") for s in c["servers"]) or "nenhum servidor listado"))
    else:
        L.append("nenhum arquivo de configuração MCP encontrado no projeto nem em config/ do orquestrador")
        L.append("(a conexão MCP pode estar configurada dentro do agy/OpenCode; confira com `agy mcp list` e `opencode mcp list`, "
                 "que não chamam modelo)")
    if st["doc_mentions"]:
        L.append("documentos que citam MCP/ferramentas do Studio:")
        for d in st["doc_mentions"][:8]:
            L.append(f"  {d['file']}: {d['count']} menção(ões)")
            for ln, txt in d["samples"]:
                L.append(f"      L{ln}: {txt}")
    for pf_ in st["project_files"]:
        if "error" in pf_:
            L.append(f"{pf_['file']}: erro ao ler ({pf_['error']})")
            continue
        L.append(f"mapeamento Rojo ({pf_['file']}, projeto '{pf_.get('name')}'): instâncias sem $path = conteúdo que vive só no Studio:")
        for e in pf_["entries"]:
            if e["path"] is None and e["className"]:
                L.append(f"  {e['node']} [{e['className']}] ignoreUnknownInstances={e['ignoreUnknownInstances']}")
    if not st["project_files"]:
        L.append("nenhum *.project.json na raiz (sem Rojo, todo o conteúdo do Studio é estado compartilhado)")
    ws = sorted(st["workspace_refs"].items(), key=lambda kv: -len(kv[1]))
    L.append(f"objetos do Workspace referenciados pelo código ({len(ws)}): estado de Studio que tarefas de mapa podem disputar")
    for n, f in ws[:12]:
        L.append(f"  Workspace.{n}: {len(f)} arquivo(s), ex.: {f[0]}")
    gb = st["geometry_builders"][:8]
    if gb:
        L.append("arquivos que criam Part/Model/Folder em runtime (top): " + ", ".join(f"{g['file']} ({g['count']})" for g in gb))
    L.append("")
    L.append("Conclusão operacional (heurística): enquanto não houver reservas reais `mcp:` e confirmação de término do agente,")
    L.append("qualquer tarefa que possa usar ferramentas MCP deve ser tratada como SHARED em `mcp:roblox_studio` e rodar em sequência.")

    sec("10. PONTOS DE ATENÇÃO PARA O ANALYZER")
    flags = []
    if unc:
        flags.append(f"{len(unc)} require(s) incertos: arquivos afetados nunca devem entrar em grupos paralelos")
    if a.issues["ambiguous"] or amb:
        flags.append(f"{len(amb)} nome(s) de módulo ambíguo(s): o Analyzer deve resolver por caminho, não só por nome")
    if cyc:
        flags.append(f"{len(cyc)} ciclo(s) de require: tarefas nesses módulos são SHARED por definição")
    if se:
        flags.append("requires por efeito colateral: tarefas de limpeza de 'unused' não são mecânicas")
    if orphans:
        flags.append(f"{len(orphans)} remote(s) órfão(s): candidatos a código morto ou contrato incompleto")
    if pf:
        flags.append(f"{len(pf)} arquivo(s) tocam persistência: mudanças neles vão para HIGH")
    if flags:
        L.extend(f"  - {f}" for f in flags)
    else:
        L.append("  nenhum ponto crítico detectado")
    return "\n".join(L)


# ---------------------------------------------------------------- autoteste
FIXTURE = {
    "default.project.json": json.dumps({"name": "Teste", "tree": {"$className": "DataModel",
                                                                 "Workspace": {"$className": "Workspace"},
                                                                 "ReplicatedStorage": {"$className": "ReplicatedStorage",
                                                                                       "Shared": {"$path": "src/shared"}}}}),
    "src/shared/config/GameConfig.luau": "return {}\n",
    "src/shared/modules/Util.luau": "return {}\n",
    "src/shared/modules/Helpers.luau": "return {}\n",
    "src/shared/Network.luau": "return {}\n",
    "src/client/Helpers.luau": "return {}\n",
    "src/server/services/DataService.luau": (
        'local DataStoreService = game:GetService("DataStoreService")\n'
        "local GameConfig = require(Shared.config.GameConfig)\nreturn {}\n"),
    "src/server/services/FarmService.luau": (
        "local DataService = require(script.Parent.DataService)\n"
        "local Network = require(Shared.Network)\n"
        "local Util = require(Shared.modules.Util)\n"
        "local H = require(Shared.modules.Helpers)\n"
        'local buy = Network.getRemoteFunction("BuyItem")\n'
        'local ping = Network.getRemoteEvent("Ping")\n'
        "buy.OnServerInvoke = function(player, id) return true end\n"
        "ping.OnServerEvent:Connect(function() end)\n"
        "-- local Ghost = require(script.Parent.Ghost)\n"
        "--[[ local Ghost2 = require(script.Parent.Ghost2) ]]\n"
        'local note = "require(script.Parent.Fake)"\n'
        "return {}\n"),
    "src/server/services/CircA.luau": "local B = require(script.Parent.CircB)\nreturn {}\n",
    "src/server/services/CircB.luau": "local A = require(script.Parent.CircA)\nreturn {}\n",
    "src/server/init.server.luau": (
        "local FarmService = require(script.services.FarmService)\n"
        "local DataService = require(script.services.DataService)\n"
        "DataService.start()\n"),
    "src/client/ui/Hud.luau": (
        "local Util = require(Shared.modules.Util)\n"
        "local H = require(Shared.Helpers)\n"
        "local mod = require(modName)\n"
        "local dyn = require(script.Parent:FindFirstChild(name))\n"
        'local buy = Network.getRemoteFunction("BuyItem")\n'
        "buy:InvokeServer(1)\n"
        'local ghost = Network.getRemoteEvent("Orphan")\n'
        "ghost:FireServer()\n"
        'local part = Instance.new("Part")\n'
        "workspace.Map.Spawn.Position = Vector3.new()\n"
        "return {}\n"),
}


def selftest() -> int:
    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        print(("  OK     " if cond else "  FALHOU ") + label)
        if not cond:
            failures.append(label)

    clean, spans = strip_comments('local a = "--x" -- comentario\n--[[ bloco\nlinha ]] local b = 1\n')
    check("comentario" not in clean and '"--x"' in clean and "local b = 1" in clean, "limpeza de comentários mantém strings")
    check(len(clean) == len('local a = "--x" -- comentario\n--[[ bloco\nlinha ]] local b = 1\n')
          and clean.count("\n") == 3, "limpeza preserva tamanho e quebras de linha")
    check(spans.inside(clean.index('"--x"') + 1), "strings são registradas")
    check(parse_require_expr("script.Parent.Foo") == {"kind": "path", "root": "script", "segs": ["Parent", "Foo"]},
          "parse de require por caminho")
    check(parse_require_expr('services:WaitForChild("DataService")')["segs"] == ["DataService"], "parse de WaitForChild literal")
    check(parse_require_expr("script.Parent:FindFirstChild(name)")["kind"] == "dynamic", "FindFirstChild com variável é dinâmico")
    check(parse_require_expr("modName")["kind"] == "variable", "require de variável")

    tmp = Path(tempfile.mkdtemp(prefix="diag_dep_"))
    try:
        for rel, content in FIXTURE.items():
            f = tmp / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content, encoding="utf-8")
        a = Analysis(tmp).run()
        farm, data, hud, init = ("src/server/services/FarmService.luau", "src/server/services/DataService.luau",
                                 "src/client/ui/Hud.luau", "src/server/init.server.luau")
        check(len(a.edges[farm]) == 4, "FarmService tem 4 dependências (comentário e string não contam)")
        check(a.fan_in(data) == 2, "DataService tem fan-in direto 2 (FarmService e init.server)")
        check("Helpers" in a.ambiguous_names(), "Helpers detectado como nome ambíguo")
        check(any(i["file"] == hud for i in a.issues["ambiguous"]) and len(a.issues["ambiguous"]) == 1,
              "só o require sem pista de pasta continua ambíguo (o de 'Shared.modules.Helpers' foi desambiguado)")
        check(len([i for i in a.issues["dynamic"] if i["file"] == hud]) == 2, "2 requires dinâmicos no Hud")
        check(not any(i["name"] in ("Ghost", "Ghost2", "Fake") for i in a.issues["unresolved"]), "comentários/strings não geram require")
        cyc = a.cycles()
        check(len(cyc) == 1 and len(cyc[0]) == 2, "ciclo CircA <-> CircB detectado")
        check([u["var"] for u in a.parsed[init]["unused_require_bindings"]] == ["FarmService"], "require só por efeito colateral no init.server")
        r = a.remotes
        check(r["BuyItem"]["roles"]["servidor trata"] == {farm} and r["BuyItem"]["roles"]["cliente envia"] == {hud}, "remote BuyItem: trata e envia")
        orph = dict(a.remote_findings()["orphans"])
        check("Ping" in orph and "Orphan" in orph and "BuyItem" not in orph, "remotes órfãos (Ping, Orphan) e BuyItem não")
        check(a.persistence_files() == [data], "persistência só no DataService")
        check({"src/server/services/DataService.luau", "src/shared/Network.luau", "src/shared/config/GameConfig.luau"} <= set(a.contract_modules()),
              "módulos de contrato detectados")
        check("Map" in a.studio["workspace_refs"] and a.studio["geometry_builders"][0]["file"] == hud, "Workspace e geometria em runtime")
        check(any("Workspace" == e["node"] for pf in a.studio["project_files"] for e in pf["entries"]), "mapeamento Rojo lido")
        rel = " ".join(a.relation("FarmService.luau", "DataService.luau"))
        check("A requer B diretamente" in rel, "relação: requer diretamente")
        check("INDÍCIOS" not in " ".join(a.relation("CircA.luau", "Util.luau")), "relação: sem indícios entre arquivos sem vínculo")
        txt = render(a, 15)
        check("FAN-IN" in txt and "REMOTES" in txt and "STUDIO / MCP" in txt, "relatório em texto gerado")
        check(a.to_dot().startswith("digraph") and "->" in a.to_dot() and bool(json.dumps(a.to_json(), default=str)),
              "DOT e JSON gerados")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nAUTOTESTE: " + ("APROVADO" if not failures else f"{len(failures)} FALHA(S) - me envie esta saída"))
    return 0 if not failures else 1


# ---------------------------------------------------------------- main
def default_root() -> Path | None:
    env = os.environ.get("PROJECT_ROOT")
    if env:
        return Path(os.path.expandvars(env))
    sys.path.insert(0, str(Path.cwd()))
    try:
        from core.config import load_settings
        return load_settings().project_root
    except Exception:  # noqa: BLE001
        return None


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="Diagnóstico de dependências (somente leitura, sem modelos).")
    ap.add_argument("--raiz", help="pasta do jogo (padrão: PROJECT_ROOT do .env)")
    ap.add_argument("--relacao", nargs=2, action="append", metavar=("ARQ_A", "ARQ_B"),
                    help="mostra indícios de dependência entre dois arquivos (pode repetir)")
    ap.add_argument("--top", type=int, default=15, help="linhas por tabela no resumo (padrão 15)")
    ap.add_argument("--autoteste", action="store_true", help="valida os parsers com um projeto de mentira")
    ap.add_argument("--sem-desktop", action="store_true", help="não copia o resumo para a Área de Trabalho")
    args = ap.parse_args(argv)
    if args.autoteste:
        return selftest()
    root = Path(args.raiz) if args.raiz else default_root()
    if not root or not root.is_dir():
        print("PROJECT_ROOT não encontrado. Rode na pasta do orquestrador ou use --raiz \"C:\\caminho\\do\\jogo\".")
        return 2
    a = Analysis(root).run()
    if args.relacao:
        for qa, qb in args.relacao:
            print("-" * 78)
            try:
                print("\n".join(a.relation(qa, qb)))
            except ValueError as exc:
                print(f"erro: {exc}")
        return 0
    text = render(a, max(5, args.top))
    print(text)
    out = Path.cwd() / "runtime" / "diagnostico"
    out.mkdir(parents=True, exist_ok=True)
    (out / "diagnostico_resumo.txt").write_text(text, encoding="utf-8")
    (out / "diagnostico.json").write_text(json.dumps(a.to_json(), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    (out / "grafo_require.dot").write_text(a.to_dot(), encoding="utf-8")
    desk = Path.home() / "Desktop"
    if not args.sem_desktop and desk.is_dir():
        shutil.copy2(out / "diagnostico_resumo.txt", desk / "diagnostico_resumo.txt")
    print(f"\nok. Arquivos em {out} (resumo copiado para a Área de Trabalho). Nada no jogo foi alterado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
