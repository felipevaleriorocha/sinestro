"""Testes da aprovação configurável de permissões (AGY_AUTO_APPROVE_PERMISSIONS)."""
import sys

import pytest

from agents.base import AgentPermissionError
from agents.gemini_adapter import GeminiAdapter, result_from_payload
from agents.mock_adapters import build_mock_backends, symmetric_responder
from orchestrator.schemas import PlannedTask
from tests.conftest import FAKE_AGY, run


def make_adapter(settings, repo=None):
    return GeminiAdapter(settings, command_prefix=[sys.executable, str(FAKE_AGY)], session_store=repo)


def test_settings_permission_configuration(make_settings):
    s_default = make_settings()
    assert s_default.agy_auto_approve_permissions is False
    assert s_default.public_summary()["agy_auto_approve_permissions"] is False

    s_enabled = make_settings(AGY_AUTO_APPROVE_PERMISSIONS="true")
    assert s_enabled.agy_auto_approve_permissions is True
    assert s_enabled.public_summary()["agy_auto_approve_permissions"] is True

    s_alias = make_settings(AGY_AUTO_APPROVE="1")
    assert s_alias.agy_auto_approve_permissions is True


def test_gemini_adapter_skip_permissions_flag(make_settings, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_HELP", "-p --model --output-format --dangerously-skip-permissions")

    # 1. Padrão (false): não adiciona a flag
    s = make_settings(AGY_AUTO_APPROVE_PERMISSIONS="false")
    a = make_adapter(s)
    cmd, _ = a.build_command("echo test", "r1", {})
    assert "--dangerously-skip-permissions" not in cmd

    # 2. Configurado globalmente como true: adiciona a flag
    s_auto = make_settings(AGY_AUTO_APPROVE_PERMISSIONS="true")
    a_auto = make_adapter(s_auto)
    cmd, _ = a_auto.build_command("echo test", "r2", {})
    assert "--dangerously-skip-permissions" in cmd

    # 3. Override por opção (ex: tarefa com aprovação concedida): adiciona mesmo com config global false
    cmd, _ = a.build_command("echo test", "r3", {"auto_approve_permissions": True})
    assert "--dangerously-skip-permissions" in cmd

    # 4. Override por opção (false): não adiciona mesmo com config global true
    cmd, _ = a_auto.build_command("echo test", "r4", {"auto_approve_permissions": False})
    assert "--dangerously-skip-permissions" not in cmd

    # 5. Se o binário não suportar a flag, não quebra nem adiciona
    monkeypatch.setenv("FAKE_AGY_HELP", "-p --model --output-format")
    a_no_flag = make_adapter(s_auto)
    cmd, _ = a_no_flag.build_command("echo test", "r5", {"auto_approve_permissions": True})
    assert "--dangerously-skip-permissions" not in cmd


def test_result_from_payload_permission_denied():
    payload = {
        "status": "SUCCESS",
        "response": "",
        "denied_actions": [{"action": "command", "target": "bash"}],
    }
    with pytest.raises(AgentPermissionError) as exc_info:
        result_from_payload(payload, "gemini-4-argon-high")

    assert exc_info.value.code == "permission_denied"
    assert "permissão negada pelo agy (command)" in str(exc_info.value)
    assert exc_info.value.denied_actions == [{"action": "command", "target": "bash"}]


def test_fake_agy_permission_denied_exception(make_settings, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_MODE", "permission_denied")
    a = make_adapter(make_settings())
    with pytest.raises(AgentPermissionError):
        a.run("run bash command", context_key="ctx", run_id="r-denied")


def test_planned_task_schema_requires_approval_fields():
    task = PlannedTask(title="Run terminal command", requires_approval=True, requires_permission=True)
    assert task.requires_approval is True
    assert task.requires_permission is True
    d = task.model_dump()
    assert d["requires_approval"] is True
    assert d["requires_permission"] is True


def test_coordinator_pre_execution_approval_flow(harness, workspace):
    async def go():
        task_spec = {
            "id": "T1",
            "title": "Subtarefa restrita com comando",
            "assigned_to": "gemini",
            "category": "mechanical",
            "requires_approval": True,
            "objective": "Executar comando restrito",
            "instructions": "Criar src/shared/Cmd.lua",
            "files": ["src/shared/Cmd.lua"],
            "acceptance_criteria": [],
            "constraints": [],
            "depends_on": [],
        }
        b = build_mock_backends()
        b["gemini"].responder = symmetric_responder("gemini", tasks=[task_spec], files=["src/shared/Cmd.lua"])
        b["deepseek"].responder = symmetric_responder("deepseek")

        async with harness(b, AGY_AUTO_APPROVE_PERMISSIONS="false") as h:
            res = await h.client.post("/tasks", json={"goal": "Executar tarefa com aprovacao"})
            wf_id = res.json()["id"]
            d = await h.wait_done(wf_id, timeout=10)

            # Workflow para em needs_human porque T1 requer aprovação prévia
            assert d["status"] == "needs_human"
            subs = d["subtasks"]
            assert len(subs) == 1
            st = subs[0]
            assert st["status"] == "needs_human"
            assert st["escalation"]["code"] == "pre_execution_approval_required"

            # Desenvolvedor aprova no dashboard
            dec_res = await h.client.post(f"/tasks/{wf_id}/subtasks/{st['id']}/decision", json={"action": "approve", "note": "Liberado"})
            assert dec_res.status_code == 200

            # Subtarefa volta para pending com allow_permissions=True
            st_after_dec = (await h.client.get(f"/tasks/{wf_id}")).json()["subtasks"][0]
            assert st_after_dec["status"] == "pending"
            assert st_after_dec["human_decision"]["allow_permissions"] is True

            # Retoma workflow
            resume_res = await h.client.post(f"/tasks/{wf_id}/resume")
            assert resume_res.status_code in {200, 202}

            d2 = await h.wait_done(wf_id, timeout=10)
            assert d2["status"] == "completed"
            assert d2["subtasks"][0]["status"] == "approved"

    run(go())


def test_coordinator_runtime_permission_denied_recovery(harness, workspace):
    async def go():
        task_spec = {
            "id": "T1",
            "title": "Subtarefa falha permissao agy",
            "assigned_to": "gemini",
            "category": "mechanical",
            "objective": "Executar acao agy",
            "instructions": "Criar src/shared/Perm.lua",
            "files": ["src/shared/Perm.lua"],
            "acceptance_criteria": [],
            "constraints": [],
            "depends_on": [],
        }
        b = build_mock_backends()
        calls = {"count": 0}

        b["gemini"].responder = symmetric_responder("gemini", tasks=[task_spec], files=["src/shared/Perm.lua"])
        b["deepseek"].responder = symmetric_responder("deepseek")

        async with harness(b, AGY_AUTO_APPROVE_PERMISSIONS="false") as h:
            c = h.app.state.container.coordinator
            orig_delegate = c._delegate

            async def patched_delegate(wf_id, sid, agent, skill, payload, **kw):
                if skill == "implement_task" and calls["count"] == 0:
                    calls["count"] += 1
                    from communication.messaging import DelegationResult
                    return DelegationResult(
                        agent=agent,
                        skill=skill,
                        task_id="t-perm",
                        context_id="c-perm",
                        state="failed",
                        error_code="permission_denied",
                        status_message="Antigravity terminou sem resposta: permissão negada pelo agy (command).",
                    )
                return await orig_delegate(wf_id, sid, agent, skill, payload, **kw)

            c._delegate = patched_delegate

            res = await h.client.post("/tasks", json={"goal": "Executar tarefa com runtime perm error"})
            wf_id = res.json()["id"]
            d = await h.wait_done(wf_id, timeout=10)

            # Workflow para em needs_human porque falhou com permissão negada
            assert d["status"] == "needs_human"
            subs = d["subtasks"]
            st = subs[0]
            assert st["status"] == "needs_human"
            assert st["escalation"]["code"] == "permission_denied"

            # Desenvolvedor aprova no dashboard
            dec_res = await h.client.post(f"/tasks/{wf_id}/subtasks/{st['id']}/decision", json={"action": "approve", "note": "Permissao concedida apos erro"})
            assert dec_res.status_code == 200

            st_after_dec = (await h.client.get(f"/tasks/{wf_id}")).json()["subtasks"][0]
            assert st_after_dec["status"] == "pending"
            assert st_after_dec["human_decision"]["allow_permissions"] is True

            # Retoma e finaliza com sucesso
            resume_res = await h.client.post(f"/tasks/{wf_id}/resume")
            assert resume_res.status_code in {200, 202}

            d2 = await h.wait_done(wf_id, timeout=10)
            assert d2["status"] == "completed"

    run(go())


def test_update_env_variable(tmp_path):
    from core.config import update_env_variable

    env_path = tmp_path / ".env"
    env_path.write_text("# Config\nFOO=bar\nAGY_AUTO_APPROVE_PERMISSIONS=false\n# Fim\n", encoding="utf-8")

    # Atualiza variável existente preservando comentários
    assert update_env_variable("AGY_AUTO_APPROVE_PERMISSIONS", "true", env_file=env_path) is True
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert "# Config" in lines
    assert "FOO=bar" in lines
    assert "AGY_AUTO_APPROVE_PERMISSIONS=true" in lines
    assert "# Fim" in lines

    # Adiciona nova variável
    assert update_env_variable("NEW_KEY", "value123", env_file=env_path) is True
    assert "NEW_KEY=value123" in env_path.read_text(encoding="utf-8")

    # Cria arquivo novo se não existir
    non_existing = tmp_path / "subdir" / ".env"
    assert update_env_variable("KEY", "val", env_file=non_existing) is True
    assert non_existing.read_text(encoding="utf-8") == "KEY=val\n"


def test_auto_approve_toggle_api_and_env_persistence(harness, tmp_path):
    from unittest.mock import patch

    async def go():
        env_file = tmp_path / ".env"
        env_file.write_text("SOME_VAR=123\nAGY_AUTO_APPROVE_PERMISSIONS=false\n", encoding="utf-8")

        b = build_mock_backends()
        async with harness(b, AGY_AUTO_APPROVE_PERMISSIONS="false") as h:
            container = h.app.state.container

            # 1. GET inicial
            res = await h.client.get("/settings/auto-approve")
            assert res.status_code == 200
            assert res.json()["enabled"] is False

            # 2. POST ligando
            with patch("core.config.BASE_DIR", tmp_path):
                post_res = await h.client.post("/settings/auto-approve", json={"enabled": True})
                assert post_res.status_code == 200
                assert post_res.json() == {"ok": True, "enabled": True}

                # Verifica em memória
                assert container.settings.agy_auto_approve_permissions is True
                assert container.coordinator.s.agy_auto_approve_permissions is True
                if "gemini" in container.backends and hasattr(container.backends["gemini"], "settings"):
                    assert container.backends["gemini"].settings.agy_auto_approve_permissions is True

                # Verifica no arquivo .env
                content = (tmp_path / ".env").read_text(encoding="utf-8")
                assert "AGY_AUTO_APPROVE_PERMISSIONS=true" in content
                assert "SOME_VAR=123" in content

                # Verifica no health
                health_res = await h.client.get("/health")
                assert health_res.status_code == 200
                assert health_res.json()["agy_auto_approve_permissions"] is True

                # 3. POST desligando
                post_res2 = await h.client.post("/settings/auto-approve", json={"enabled": False})
                assert post_res2.status_code == 200
                assert post_res2.json() == {"ok": True, "enabled": False}

                assert container.settings.agy_auto_approve_permissions is False
                assert container.coordinator.s.agy_auto_approve_permissions is False
                content2 = (tmp_path / ".env").read_text(encoding="utf-8")
                assert "AGY_AUTO_APPROVE_PERMISSIONS=false" in content2

    run(go())

