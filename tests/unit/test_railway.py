from pathlib import Path

import notebooklm.railway as railway


def test_auth_source_env_takes_priority(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '{"cookies": []}')
    monkeypatch.setattr(railway, "get_storage_path", lambda: tmp_path / "storage_state.json")

    assert railway._auth_source() == "env"


def test_auth_source_file(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    storage = tmp_path / "storage_state.json"
    storage.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(railway, "get_storage_path", lambda: storage)

    assert railway._auth_source() == "file"


def test_auth_source_none(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    monkeypatch.setattr(railway, "get_storage_path", lambda: tmp_path / "missing.json")

    assert railway._auth_source() == "none"


def test_port_parsing(monkeypatch):
    monkeypatch.setenv("PORT", "9090")
    assert railway._port() == 9090

    monkeypatch.setenv("PORT", "not-a-number")
    assert railway._port() == 8080

    monkeypatch.setenv("PORT", "70000")
    assert railway._port() == 8080

