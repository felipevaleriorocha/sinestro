"""Validação de evidência dos achados de revisão.

Um achado é válido somente se:
  - o arquivo está dentro do workspace (mesma trava de caminho das reservas) e existe;
  - a linha existe no arquivo;
  - o `snippet` (trecho literal curto) aparece no arquivo a até ±TOLERANCE linhas da linha informada
    (comparação com espaços normalizados).
"""
from __future__ import annotations

from pathlib import Path

from orchestrator.file_locks import PathOutsideWorkspaceError, normalize_file
from orchestrator.schemas import ReviewFinding, ReviewResult

TOLERANCE = 5
MAX_SNIPPET = 500


def _norm(text: str) -> str:
    return " ".join(text.split())


def validate_finding(root: Path, f: ReviewFinding, tolerance: int = TOLERANCE) -> tuple[bool, str]:
    if not f.file.strip():
        return False, "achado sem arquivo"
    try:
        rel = normalize_file(root, f.file)
    except PathOutsideWorkspaceError:
        return False, f"arquivo fora do workspace: {f.file}"
    path = Path(root) / rel
    if not path.is_file():
        return False, f"arquivo inexistente: {f.file}"
    snippet = _norm(f.snippet)
    if not snippet:
        return False, f"{f.file}:{f.line}: achado sem trecho (snippet)"
    if len(f.snippet) > MAX_SNIPPET:
        return False, f"{f.file}:{f.line}: trecho longo demais (máx. {MAX_SNIPPET} caracteres)"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return False, f"{f.file}: não foi possível ler ({exc})"
    if f.line < 1 or f.line > len(lines):
        return False, f"{f.file}:{f.line}: linha fora do arquivo ({len(lines)} linhas)"
    start, end = max(0, f.line - 1 - tolerance), min(len(lines), f.line + tolerance)
    if snippet in _norm("\n".join(lines[start:end])):
        return True, ""
    return False, f"{f.file}:{f.line}: trecho não encontrado a ±{tolerance} linhas"


def validate_review(root: Path, review: ReviewResult) -> tuple[list[ReviewFinding], list[dict]]:
    valid, discarded = [], []
    for f in review.findings:
        ok, reason = validate_finding(root, f)
        if ok:
            valid.append(f)
        else:
            discarded.append({"finding": f.model_dump(), "reason": reason})
    return valid, discarded


def review_problem(review: ReviewResult, valid: list[ReviewFinding]) -> str | None:
    """Motivo para pedir reformulação (None = revisão utilizável)."""
    if not review.structured:
        return "revisão sem JSON estruturado"
    if review.verdict in {"changes_requested", "rejected"} and not valid:
        return f"veredito '{review.verdict}' sem nenhum achado com evidência válida"
    if review.verdict == "rejected" and not review.rejection_reason.strip():
        return "rejeição sem justificativa (rejection_reason)"
    if review.verdict == "needs_clarification" and not review.questions and not valid:
        return "pedido de esclarecimento sem perguntas"
    return None
