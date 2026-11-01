"""Construção/interpretação de mensagens A2A (tipos oficiais do a2a-sdk) e histórico de comunicação."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from a2a.helpers import get_data_parts, get_text_parts, new_data_part, new_text_part
from a2a.types import Message, Role, SendMessageConfiguration, SendMessageRequest, Task, TaskState
from google.protobuf.json_format import MessageToDict

TERMINAL_STATES = {
    TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
}
INTERRUPTED_STATES = {TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED}


def state_name(state: int) -> str:
    return TaskState.Name(state).replace("TASK_STATE_", "").lower()


def build_request(skill: str, payload: dict, *, sender: str, summary: str, context_id: str | None = None,
                  reference_task_ids: list[str] | None = None, metadata: dict | None = None) -> SendMessageRequest:
    """Mensagem do usuário (orquestrador/agente delegante) com DataPart estruturado + TextPart legível."""
    msg = Message(
        message_id=str(uuid.uuid4()),
        role=Role.ROLE_USER,
        parts=[new_data_part({"skill": skill, "payload": payload}), new_text_part(summary)],
    )
    if context_id:
        msg.context_id = context_id
    if reference_task_ids:
        msg.reference_task_ids.extend(reference_task_ids)
    meta = {"skill": skill, "sender": sender, **(metadata or {})}
    msg.metadata.update(meta)
    return SendMessageRequest(
        message=msg,
        configuration=SendMessageConfiguration(return_immediately=True, accepted_output_modes=["application/json", "text/plain"]),
        metadata=meta,
    )


def _to_py(value: Any) -> Any:
    """Converte floats inteiros vindos do protobuf Struct (1.0 -> 1)."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_to_py(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_py(v) for k, v in value.items()}
    return value


def parse_request(message: Message) -> tuple[str | None, dict]:
    for data in get_data_parts(message.parts):
        if isinstance(data, dict) and "skill" in data:
            return str(data["skill"]), _to_py(dict(data.get("payload") or {}))
    return None, {}


@dataclass
class DelegationResult:
    agent: str
    skill: str
    task_id: str
    context_id: str
    state: str
    data: dict | None = None
    text: str = ""
    status_message: str = ""
    error_code: str | None = None
    metadata: dict = field(default_factory=dict)
    artifacts: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state == "completed"


def result_from_task(agent: str, skill: str, task: Task) -> DelegationResult:
    data, texts, artifacts = None, [], []
    for art in task.artifacts:
        parts_data = [_to_py(d) for d in get_data_parts(art.parts)]
        parts_text = get_text_parts(art.parts)
        artifacts.append({"artifact_id": art.artifact_id, "name": art.name, "data": parts_data, "text_chars": sum(len(t) for t in parts_text)})
        for d in parts_data:
            if isinstance(d, dict) and d.get("skill") == skill and data is None:
                data = d.get("result")
        texts += parts_text
    status_msg = ""
    meta: dict = {}
    if task.status.HasField("message"):
        status_msg = "\n".join(get_text_parts(task.status.message.parts))
        if task.status.message.HasField("metadata"):
            meta = _to_py(MessageToDict(task.status.message.metadata))
    return DelegationResult(
        agent=agent, skill=skill, task_id=task.id, context_id=task.context_id, state=state_name(task.status.state),
        data=data, text="\n".join(texts), status_message=status_msg, error_code=meta.get("error_code"), metadata=meta,
        artifacts=artifacts,
    )


def summarize_payload(skill: str, payload: dict) -> dict:
    """Versão enxuta do payload para o histórico (o contexto completo fica no hash do workflow)."""
    out = {k: v for k, v in payload.items() if k not in {"context_text"}}
    if "context_text" in payload:
        out["context_chars"] = len(payload["context_text"] or "")
    return out
