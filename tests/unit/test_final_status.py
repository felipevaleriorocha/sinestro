"""[PURO] final_status(subs, cancelled, paused, final_review_state): única decisão do status final do workflow."""
import pytest

from orchestrator.task_manager import FR, WF, final_status

DONE = [{"status": "approved"}, {"status": "approved"}]


@pytest.mark.parametrize("state, expected", [
    (FR.PASSED, WF.COMPLETED),
    (FR.DISABLED, WF.COMPLETED),
    (FR.ACCEPTED_BY_HUMAN, WF.COMPLETED),
    (FR.SKIPPED, WF.COMPLETED_UNREVIEWED),
    (FR.FAILED, WF.COMPLETED_UNREVIEWED),
    (None, WF.COMPLETED_UNREVIEWED),          # sem estado gravado nunca vira completed
    ("valor_desconhecido", WF.COMPLETED_UNREVIEWED),
    (FR.CHANGES_REQUESTED, WF.NEEDS_HUMAN),
    (FR.REJECTED, WF.NEEDS_HUMAN),
    (FR.UNVERIFIED, WF.NEEDS_HUMAN),
    (FR.INVALIDATED, WF.NEEDS_HUMAN),
    (FR.REJECTED_BY_HUMAN, WF.FAILED),
])
def test_all_subtasks_done_depends_on_final_review(state, expected):
    assert final_status(DONE, False, False, state) == expected


def test_cancel_wins_and_partial_states_unchanged():
    assert final_status(DONE, True, False, FR.PASSED) == WF.CANCELED
    assert final_status([{"status": "approved"}, {"status": "needs_human"}], False, False, FR.NOT_REACHED) == WF.NEEDS_HUMAN
    assert final_status([{"status": "approved"}, {"status": "awaiting_review"}], False, True, FR.NOT_REACHED) == WF.PAUSED
    assert final_status([{"status": "approved"}, {"status": "blocked"}], False, False, FR.NOT_REACHED) == WF.BLOCKED
    assert final_status([{"status": "approved"}, {"status": "rejected"}], False, False, FR.NOT_REACHED) == WF.COMPLETED_WITH_ISSUES
    assert final_status([], False, False, FR.PASSED) == WF.FAILED
