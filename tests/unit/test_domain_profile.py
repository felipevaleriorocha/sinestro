"""Testes da Fase 1: DomainProfile e desacoplamento de domínio."""
import re
from pathlib import Path

from core.domain import DomainProfile
from domains.roblox.profile import RobloxDomainProfile


def test_roblox_domain_profile_implements_interface(make_settings):
    s = make_settings()
    profile = RobloxDomainProfile(s)
    assert isinstance(profile, DomainProfile)
    assert profile.name == "roblox"


def test_roblox_domain_profile_provides_prompts(make_settings):
    s = make_settings()
    profile = RobloxDomainProfile(s)
    pr = profile.get_prompts()
    assert hasattr(pr, "plan_prompt")
    assert hasattr(pr, "respond_assignment_prompt")
    assert hasattr(pr, "implement_prompt")
    assert hasattr(pr, "review_prompt")
    assert hasattr(pr, "final_review_prompt")


def test_roblox_domain_profile_loads_parsed_configs(make_settings):
    s = make_settings()
    profile = RobloxDomainProfile(s)

    rules = profile.load_assignment_rules()
    assert isinstance(rules, dict)
    assert "default_agent" in rules or "categories" in rules

    checks = profile.load_checks_config()
    assert isinstance(checks, dict)
    assert "checks" in checks
    assert "allowed_tools" in checks

    sources = profile.load_context_sources()
    assert isinstance(sources, dict)
    assert "categories" in sources


def test_orchestrator_and_communication_are_domain_agnostic():
    root = Path(__file__).parents[2]
    pattern = re.compile(r"Luau|Roblox|DataStore|RemoteEvent")

    matches = []
    for folder in ("orchestrator", "communication"):
        for py_file in (root / folder).glob("*.py"):
            text = py_file.read_text(encoding="utf-8")
            found = pattern.findall(text)
            if found:
                matches.append((py_file.name, found))

    assert not matches, f"Termos de domínio encontrados em {matches}"
