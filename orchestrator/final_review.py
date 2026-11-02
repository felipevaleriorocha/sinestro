"""Revisão final do workflow (mixin do Coordinator).

Decide o revisor (nunca quem implementou tudo), executa a revisão global somente leitura, valida a evidência dos achados
e PERSISTE `final_review_state` em todos os caminhos. Usa os serviços do Coordinator (`tasks`, `repo`, `registry`, `context`,
`_delegate`, `_hashes`, `_safe_rel`); não é instanciado sozinho.
"""
from __future__ import annotations

from typing import Any

from communication.a2a_client import A2ADelegationError
from orchestrator.codes import UNAVAILABLE_CODES
from orchestrator.evidence import review_problem, validate_review
from orchestrator.schemas import ReviewResult
from orchestrator.task_manager import FR, ST, WF


class FinalReviewMixin:
    @staticmethod
    def _implementer_of(sub: dict) -> str:
        res = sub.get("result") if isinstance(sub.get("result"), dict) else {}
        return res.get("implemented_by") or sub.get("assigned_to") or sub["agent"]

    def _final_reviewer(self, subs: list[dict]) -> tuple[str, list[str]]:
        """Revisor da revisão final: nunca quem implementou tudo. Misto -> Gemini, com regra assimétrica."""
        authors = {self._implementer_of(s) for s in subs}
        if authors == {"gemini"}:
            reviewer = "deepseek"
        elif authors == {"deepseek"}:
            reviewer = "gemini"
        else:
            reviewer = "gemini"
        self_authored = [s["plan_id"] for s in subs if self._implementer_of(s) == reviewer]
        return reviewer, self_authored

    def _final_watched(self, subs: list[dict]) -> list[str]:
        paths: set[str] = set()
        for s in subs:
            paths |= set(s.get("files") or [])
            if isinstance(s.get("result"), dict):
                paths |= set(s["result"].get("files_modified") or [])
        return sorted(p for p in paths if self._safe_rel(p))

    @staticmethod
    def _snapshot_hash(hashes: dict) -> str:
        import hashlib
        import json as _json
        return hashlib.sha256(_json.dumps(sorted(hashes.items()), ensure_ascii=False).encode("utf-8")).hexdigest()[:16]

    def _coverage(self, subs: list[dict], self_authored: list[str], state: str) -> list[dict]:
        out = []
        for s in subs:
            hd = s.get("human_decision") if isinstance(s.get("human_decision"), dict) else {}
            approved_by = "human" if hd.get("action") == "approve" else s.get("reviewer")
            authored = s["plan_id"] in self_authored
            out.append({"id": s["plan_id"], "title": s["title"], "implemented_by": self._implementer_of(s), "approved_by": approved_by,
                        "covered_by_final_review": state == FR.PASSED and not authored,
                        "coverage": "cross_review_only" if authored else ("cross_review+final" if state == FR.PASSED else "cross_review")})
        return out

    async def _final_review_stage(self, wf_id: str, goal: str, subs: list[dict]) -> dict:
        """Decide e persiste o final_review_state em TODOS os caminhos (nenhum estado final silencioso)."""
        if not subs or not all(s["status"] in ST.DONE for s in subs) or self.tasks.cancel_requested(wf_id):
            outcome = {"state": FR.NOT_REACHED, "detail": "Nem todas as subtarefas foram aprovadas."}
            self.repo.update_workflow(wf_id, final_review_state=FR.NOT_REACHED, final_review_json=outcome)
            return outcome
        wf = self.repo.get_workflow(wf_id) or {}
        mode = wf.get("mode") or "full"
        if mode == "simple" and len(subs) == 1 and all(s["status"] == ST.APPROVED for s in subs):
            reviewer, self_authored = self._final_reviewer(subs)
            outcome = {"state": FR.SKIPPED_BY_POLICY, "detail": "Dispensada por política em modo simples (subtarefa única já aprovada na revisão cruzada)",
                       "coverage": self._coverage(subs, [], FR.SKIPPED_BY_POLICY)}
            self.repo.update_workflow(wf_id, final_review_state=FR.SKIPPED_BY_POLICY, final_review_json=outcome)
            self.tasks.event(wf_id, "orchestrator", "final_review_skipped_by_policy", "Revisão final dispensada por política no modo simples (1 subtarefa aprovada).", "info")
            return outcome
        if not self.s.final_review:
            reviewer, self_authored = self._final_reviewer(subs)
            outcome = {"state": FR.DISABLED, "detail": "FINAL_REVIEW=false", "coverage": self._coverage(subs, [], FR.DISABLED)}
            self.repo.update_workflow(wf_id, final_review_state=FR.DISABLED, final_review_json=outcome)
            self.tasks.event(wf_id, "orchestrator", "final_review_disabled", "FINAL_REVIEW=false: revisão final desligada.", "warning")
            return outcome
        return await self._final_review(wf_id, goal, subs)

    @staticmethod
    def _final_error(status: str, fr: dict) -> str | None:
        state = fr.get("state")
        if status == WF.COMPLETED_UNREVIEWED:
            return f"Revisão final não realizada ({state}: {fr.get('detail', '')}). Use Retomar para rodar só a revisão final."
        if status == WF.NEEDS_HUMAN and state in FR.NEEDS_HUMAN:
            rv = fr.get("review") or {}
            return (f"Revisão final por {fr.get('reviewer')}: {state}. {rv.get('summary') or fr.get('detail', '')} "
                    "Decida em POST /tasks/{id}/final-review/decision (accept|reject) ou Retome para nova revisão final.")[:4000]
        return None

    async def _final_review(self, wf_id: str, goal: str, subs: list[dict]) -> dict:
        """Executa a revisão final e PERSISTE o resultado (final_review_state + final_review_json). Sempre retorna um estado."""
        prev = (self.repo.get_workflow(wf_id) or {}).get("final_review") or {}
        attempt = int(prev.get("attempt") or 0) + 1
        reviewer, self_authored = self._final_reviewer(subs)
        watched = self._final_watched(subs)
        before = self._hashes(watched)
        snap = self._snapshot_hash(before)
        outcome: dict[str, Any] = {"attempt": attempt, "reviewer": reviewer, "self_authored_subtasks": self_authored,
                                   "snapshot_hash": snap, "watched_files": watched}
        if prev.get("snapshot_hash") and prev["snapshot_hash"] != snap:
            outcome["workspace_changed_since_last_attempt"] = True
            self.tasks.event(wf_id, "orchestrator", "final_review_workspace_changed",
                             f"Arquivos mudaram desde a tentativa {prev.get('attempt')} (snapshot {prev['snapshot_hash']} -> {snap}); "
                             "a revisão final roda no estado atual.", "warning")
        if self_authored:
            self.tasks.event(wf_id, "orchestrator", "final_review_self_authored",
                             f"{reviewer} implementou {self_authored}: achados dele bloqueiam, mas a aprovação não cobre essas "
                             "subtarefas (cobertura só pela revisão cruzada).", "warning")

        def finish(state: str, detail: str = "", review: ReviewResult | None = None, valid=None, discarded=None) -> dict:
            outcome.update(state=state, detail=detail, coverage=self._coverage(subs, self_authored, state))
            if review is not None:
                outcome["review"] = {**review.model_dump(), "findings": [f.model_dump() for f in (valid or [])]}
                outcome["discarded_findings"] = discarded or []
            if state in FR.NEEDS_HUMAN:
                outcome["escalation"] = {"code": f"final_review_{state}", "reason": detail, "reviewer": reviewer,
                                         "verdict": (outcome.get("review") or {}).get("verdict"),
                                         "summary": (outcome.get("review") or {}).get("summary"),
                                         "findings": (outcome.get("review") or {}).get("findings"),
                                         "requested_changes": (outcome.get("review") or {}).get("requested_changes"),
                                         "rejection_reason": (outcome.get("review") or {}).get("rejection_reason"),
                                         "options": ["accept", "reject", "resume (nova revisão final)"]}
            self.repo.update_workflow(wf_id, final_review_state=state, final_review_json=outcome)
            level = "info" if state == FR.PASSED else "warning"
            self.tasks.event(wf_id, reviewer if state != FR.SKIPPED else "orchestrator", f"final_review_{state}",
                             {"attempt": attempt, "reviewer": reviewer, "detail": detail, "self_authored_subtasks": self_authored}, level)
            return outcome

        ok, reason = self.registry.can_accept(reviewer)
        if not ok:
            return finish(FR.SKIPPED, f"{reviewer} indisponível: {reason}")
        self.tasks.set_status(wf_id, WF.REVIEWING, phase="final_review")
        summary = [{"id": s["plan_id"], "title": s["title"], "implemented_by": self._implementer_of(s),
                    "cross_reviewed_by": s.get("reviewer"), "files": s.get("files") or [],
                    "files_modified": (s.get("result") or {}).get("files_modified") if isinstance(s.get("result"), dict) else [],
                    "result": (s.get("result") or {}).get("summary") if isinstance(s.get("result"), dict) else None,
                    "authored_by_you": s["plan_id"] in self_authored} for s in subs]
        validation_errors, previous_review, ctx_id = None, None, None
        for turn in (1, 2):
            hashes_before = self._hashes(watched)
            try:
                res = await self._delegate(
                    wf_id, None, reviewer, "final_review",
                    {"goal": goal, "subtasks": summary, "self_authored": self_authored, "watched_files": watched,
                     "context_text": self.context.for_workflow(wf_id).text, "validation_errors": validation_errors,
                     "previous_review": previous_review},
                    summary=("Reformular revisão final" if turn == 2 else f"Revisão final (tentativa {attempt})"), context_id=ctx_id)
            except A2ADelegationError as exc:
                return finish(FR.FAILED, f"{exc.code}: {exc}")
            ctx_id = res.context_id
            if self._hashes(watched) != hashes_before:
                self.tasks.event(wf_id, "orchestrator", "review_invalidated_files_changed",
                                 f"{reviewer} alterou arquivos durante a revisão final somente leitura; veredito descartado.", "error")
                return finish(FR.INVALIDATED, f"{reviewer} alterou arquivos durante a revisão final.")
            if not res.ok:
                code = res.error_code or res.state
                if code in UNAVAILABLE_CODES or res.state == "rejected":
                    return finish(FR.SKIPPED, f"{reviewer}: {res.state}/{code}")
                return finish(FR.FAILED, f"{reviewer}: {res.state}/{code}: {res.status_message[:300]}")
            review = ReviewResult.model_validate(res.data or {"verdict": "changes_requested", "structured": False})
            if review.verdict == "needs_clarification":
                review = review.model_copy(update={"verdict": "changes_requested"})
            valid, discarded = validate_review(self.s.project_root, review)
            if discarded:
                self.tasks.event(wf_id, "orchestrator", "findings_discarded", discarded, "warning")
            problem = review_problem(review, valid)
            if not problem:
                state = FR.PASSED if review.verdict == "approved" else review.verdict
                return finish(state, review.summary[:300], review, valid, discarded)
            if turn == 1:
                validation_errors = [problem] + [d["reason"] for d in discarded]
                previous_review = review.model_dump()
                self.tasks.event(wf_id, "orchestrator", "review_reformulation_requested", validation_errors, "warning")
                continue
            self.tasks.event(wf_id, "orchestrator", "review_unverified", f"revisão final: {problem}", "warning")
            return finish(FR.UNVERIFIED, f"Revisão final sem evidência verificável após reformulação: {problem}", review, valid, discarded)
        raise RuntimeError("inalcançável")
