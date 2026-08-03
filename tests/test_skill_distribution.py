"""Public Skill package, manifest, and local account configuration coverage."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "tnalpha-content-ops"


def _load_client_module():
    path = SKILL_DIR / "scripts" / "tnalpha_client.py"
    spec = importlib.util.spec_from_file_location("tnalpha_skill_client", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skill_registry_and_manifest_are_consistent():
    registry = json.loads((ROOT / "skills" / "registry.json").read_text())
    manifest = json.loads((SKILL_DIR / "skill.json").read_text())

    assert registry["schemaVersion"] == 1
    assert len(registry["skills"]) == 1
    entry = registry["skills"][0]
    assert entry["id"] == manifest["id"] == "tnalpha-content-ops"
    assert entry["version"] == manifest["version"]
    assert manifest["entry"] == "SKILL.md"
    assert manifest["permissions"]["externalWrite"] is True
    assert manifest["approval"]["externalWrite"] == "required"

    skill_text = (SKILL_DIR / "SKILL.md").read_text().lower()
    client_text = (SKILL_DIR / "scripts" / "tnalpha_client.py").read_text().lower()
    assert "openloomi" not in skill_text
    assert "openloomi" not in client_text
    assert "tnalpha_agent_api_token=" not in skill_text


def test_public_skill_manifest_download_is_reproducible(anon_client):
    first = anon_client.get("/skills/tnalpha-content-ops/manifest.json")
    second = anon_client.get("/skills/tnalpha-content-ops/manifest.json")
    assert first.status_code == second.status_code == 200
    manifest = first.json()
    assert manifest["id"] == "tnalpha-content-ops"
    assert manifest["version"] == "1.0.0"
    assert manifest["package"]["url"].endswith(
        "/skills/tnalpha-content-ops/1.0.0.zip"
    )

    package = anon_client.get("/skills/tnalpha-content-ops/1.0.0.zip")
    repeated = anon_client.get("/skills/tnalpha-content-ops/1.0.0.zip")
    assert package.status_code == repeated.status_code == 200
    assert package.content == repeated.content
    assert hashlib.sha256(package.content).hexdigest() == manifest["package"]["sha256"]
    assert len(package.content) == manifest["package"]["sizeBytes"]

    with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
        names = archive.namelist()
        assert "tnalpha-content-ops/SKILL.md" in names
        assert "tnalpha-content-ops/skill.json" in names
        assert "tnalpha-content-ops/agents/openai.yaml" in names
        assert "tnalpha-content-ops/references/openapi.yaml" in names
        assert all("__pycache__" not in name for name in names)


def test_local_account_config_never_persists_token(tmp_path, monkeypatch):
    client = _load_client_module()
    config_dir = tmp_path / "tnalpha-config"
    monkeypatch.setenv("TNALPHA_CONFIG_DIR", str(config_dir))

    client._save_config(
        {
            "base_url": "https://alpha.traditionow.ai",
            "account": "research-user",
            "org_id": "research-team",
            "keychain_service": "ai.traditionow.tnalpha.agent-api.prod",
        }
    )
    saved = json.loads((config_dir / "agent.json").read_text())
    assert saved["account"] == "research-user"
    assert "token" not in saved
    assert (config_dir / "agent.json").stat().st_mode & 0o777 == 0o600


def test_account_config_drives_identity_and_environment_overrides(monkeypatch, tmp_path):
    client = _load_client_module()
    monkeypatch.setenv("TNALPHA_CONFIG_DIR", str(tmp_path))
    client._save_config(
        {
            "base_url": "https://configured.example",
            "account": "configured-user",
            "org_id": "configured-org",
            "keychain_service": "configured-service",
        }
    )

    assert client._base_url() == "https://configured.example"
    assert client._actor_id() == "configured-user"
    assert client._org_id() == "configured-org"

    monkeypatch.setenv("TNALPHA_API_URL", "https://override.example/")
    monkeypatch.setenv("TNALPHA_AGENT_ACTOR", "override-user")
    monkeypatch.setenv("TNALPHA_AGENT_ORG", "override-org")
    assert client._base_url() == "https://override.example"
    assert client._actor_id() == "override-user"
    assert client._org_id() == "override-org"


def test_keychain_write_keeps_credential_out_of_process_arguments(monkeypatch):
    client = _load_client_module()
    calls = []

    monkeypatch.setattr(client.sys, "platform", "darwin")
    monkeypatch.setattr(
        client,
        "_run_security",
        lambda arguments, *, input_text=None: calls.append((arguments, input_text)),
    )

    client._keychain_set("tnalpha-test", "research-user", "secret-value")

    arguments, input_text = calls[0]
    assert arguments[-1] == "-w"
    assert "secret-value" not in arguments
    assert input_text == "secret-value\n"


def test_disconnect_requires_confirmation_and_removes_local_config(tmp_path, monkeypatch):
    client = _load_client_module()
    monkeypatch.setenv("TNALPHA_CONFIG_DIR", str(tmp_path))
    client._save_config(
        {
            "base_url": "https://configured.example",
            "account": "research-user",
            "org_id": "research-team",
            "keychain_service": "tnalpha-test",
        }
    )

    with pytest.raises(SystemExit, match="--confirm"):
        client._disconnect(False)

    deleted = []
    monkeypatch.setattr(
        client,
        "_keychain_delete",
        lambda service, account: deleted.append((service, account)) or True,
    )
    result = client._disconnect(True)

    assert result["disconnected"] is True
    assert result["credential_removed"] is True
    assert deleted == [("tnalpha-test", "research-user")]
    assert not (tmp_path / "agent.json").exists()
