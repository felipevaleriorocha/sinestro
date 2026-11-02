"""PROJECT_ROOT opcional: seleção (explorador de arquivos) e criação do projeto pelo painel/API."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from agents.mock_adapters import build_mock_backends
from core.config import load_settings
from core.workspace import (
    PickerUnavailable,
    WorkspaceError,
    create_project_dir,
    sanitize_project_name,
    validate_project_dir,
)
from tests.conftest import run


@pytest.fixture
def env_writes(monkeypatch):
    """Nunca escreve no .env real: captura as chamadas de persistência."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr("api.container.update_env_variable", lambda key, value, env_file=None: calls.append((key, value)) or True)
    return calls


# ---------------------------------------------------------------- funções puras
def test_validate_project_dir_accepts_existing_folder(tmp_path):
    assert validate_project_dir(str(tmp_path)) == tmp_path.resolve()


@pytest.mark.parametrize("raw", ["", "   ", "relativo/pasta"])
def test_validate_project_dir_rejects_empty_and_relative(raw):
    with pytest.raises(WorkspaceError):
        validate_project_dir(raw)


def test_validate_project_dir_rejects_missing_anchor_and_home(tmp_path):
    with pytest.raises(WorkspaceError, match="não existe"):
        validate_project_dir(str(tmp_path / "nao-existe"))
    with pytest.raises(WorkspaceError):
        validate_project_dir(Path(tmp_path.anchor).as_posix())
    with pytest.raises(WorkspaceError):
        validate_project_dir(str(Path.home()))


def test_sanitize_project_name():
    assert sanitize_project_name("  Meu Jogo: v2!  ") == "Meu-Jogo-v2"
    assert sanitize_project_name("../../etc") == "etc"
    assert sanitize_project_name("???") == ""


def test_create_project_dir_default_name_and_collision(tmp_path):
    now = datetime(2026, 11, 2, 3, 4, 5)
    first = create_project_dir(tmp_path / "base", None, now=now)
    assert first.name == "projeto-20261102-030405" and first.is_dir()
    second = create_project_dir(tmp_path / "base", None, now=now)
    assert second.name == "projeto-20261102-030405-2"
    named = create_project_dir(tmp_path / "base", "Meu Jogo")
    assert named.name == "Meu-Jogo" and named.parent == (tmp_path / "base").resolve()


def test_create_project_dir_rejects_unusable_name(tmp_path):
    with pytest.raises(WorkspaceError):
        create_project_dir(tmp_path, "???")


def test_project_root_is_optional_in_settings(make_settings):
    assert make_settings(PROJECT_ROOT="").project_configured is False
    s = make_settings()
    assert s.project_configured is True and s.public_summary()["project_root_exists"] is True
    assert load_settings(env_file=Path("nao-existe.env"), overrides={}).default_projects_dir.name == "Sinestro"


# ---------------------------------------------------------------- API
def test_tasks_are_refused_until_a_project_is_selected(harness, tmp_path, env_writes):
    target = tmp_path / "novo-projeto"
    target.mkdir()

    async def go():
        async with harness(build_mock_backends(), PROJECT_ROOT="") as h:
            info = (await h.client.get("/workspace")).json()
            assert info["configured"] is False and info["ready"] is False and info["project_root"] == ""
            health = (await h.client.get("/health")).json()
            assert health["project_root_configured"] is False and health["project_root_exists"] is False

            blocked = await h.client.post("/tasks", json={"goal": "Criar um modulo hello-world", "mode": "plan_only"})
            assert blocked.status_code == 409 and blocked.json()["detail"].startswith("project_root_required")

            chosen = await h.client.post("/workspace/select", json={"path": str(target)})
            assert chosen.status_code == 200 and chosen.json()["ready"] is True
            assert Path(chosen.json()["project_root"]) == target.resolve()
            ok = await h.client.post("/tasks", json={"goal": "Criar um modulo hello-world", "mode": "plan_only"})
            assert ok.status_code == 202
            await h.wait_done(ok.json()["id"])
    run(go())
    assert env_writes == [("PROJECT_ROOT", str(target.resolve()))]


def test_select_propagates_the_new_root_to_every_component(harness, tmp_path, env_writes):
    target = tmp_path / "outro"
    target.mkdir()

    async def go():
        async with harness(build_mock_backends()) as h:
            c = h.app.state.container
            assert (await h.client.post("/workspace/select", json={"path": str(target)})).status_code == 200
            root = target.resolve()
            assert c.settings.project_root == root and c.coordinator.s.project_root == root
            assert c.git.root == root and c.locks.project_root == root and c.checks.root == root
            assert c.context.project_root == root and c.context._indexed is False
            assert all(b.settings.project_root == root for b in c.backends.values() if hasattr(b, "settings"))
    run(go())


def test_select_rejects_missing_folder(harness, tmp_path, env_writes):
    async def go():
        async with harness(build_mock_backends()) as h:
            r = await h.client.post("/workspace/select", json={"path": str(tmp_path / "fantasma")})
            assert r.status_code == 400 and "não existe" in r.json()["detail"]
    run(go())
    assert env_writes == []


def test_create_makes_folder_in_default_dir_and_selects_it(harness, tmp_path, env_writes):
    base = tmp_path / "padrao"

    async def go():
        async with harness(build_mock_backends(), PROJECT_ROOT="", DEFAULT_PROJECTS_DIR=base) as h:
            r = await h.client.post("/workspace/create", json={"name": "Meu Jogo"})
            assert r.status_code == 201
            body = r.json()
            assert body["created"] is True and body["ready"] is True
            assert Path(body["project_root"]) == (base / "Meu-Jogo").resolve() and (base / "Meu-Jogo").is_dir()
            unnamed = await h.client.post("/workspace/create", json={})
            assert unnamed.status_code == 201 and Path(unnamed.json()["project_root"]).name.startswith("projeto-")
            bad = await h.client.post("/workspace/create", json={"name": "???"})
            assert bad.status_code == 400
    run(go())
    assert env_writes[0] == ("PROJECT_ROOT", str((base / "Meu-Jogo").resolve()))


def test_switching_project_is_refused_while_a_workflow_runs(harness, tmp_path, env_writes, monkeypatch):
    target = tmp_path / "x"
    target.mkdir()

    async def go():
        async with harness(build_mock_backends()) as h:
            monkeypatch.setattr(h.app.state.container.coordinator, "any_running", lambda: True)
            assert (await h.client.post("/workspace/select", json={"path": str(target)})).status_code == 409
            assert (await h.client.post("/workspace/create", json={"name": "y"})).status_code == 409
    run(go())
    assert env_writes == []
    assert not (tmp_path / "y").exists()


def test_pick_endpoint_returns_choice_cancel_and_unavailable(harness, tmp_path, env_writes, monkeypatch):
    async def go():
        async with harness(build_mock_backends()) as h:
            monkeypatch.setattr("api.routes.pick_folder", lambda initial=None: str(tmp_path))
            assert (await h.client.post("/workspace/pick")).json() == {"path": str(tmp_path), "cancelled": False}
            monkeypatch.setattr("api.routes.pick_folder", lambda initial=None: None)
            assert (await h.client.post("/workspace/pick")).json() == {"path": None, "cancelled": True}

            def boom(initial=None):
                raise PickerUnavailable("Seletor de pastas indisponível neste ambiente")

            monkeypatch.setattr("api.routes.pick_folder", boom)
            r = await h.client.post("/workspace/pick")
            assert r.status_code == 501 and "indisponível" in r.json()["detail"]
    run(go())
    assert env_writes == []  # escolher a pasta não altera o projeto: só /workspace/select altera
