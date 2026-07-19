"""Agent setup: token resolution order, UI-managed settings overlay, and the
shared check_connection status logic."""
import json

from agent.config import AgentConfig, load_config
from agent import setup as agent_setup


# --- token resolution order: explicit > token_file > env --------------------

def test_token_from_explicit_file(tmp_path):
    tf = tmp_path / "tok.txt"
    tf.write_text("  filetoken\n")
    cfg = AgentConfig(node_name="N", token_file=str(tf))
    cfg.resolve_token()
    assert cfg.token == "filetoken"


def test_token_from_default_work_root_file(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "node_token").write_text("wrtoken\n")
    cfg = AgentConfig(node_name="N", work_root=str(work))
    cfg.resolve_token()
    assert cfg.token == "wrtoken"


def test_explicit_token_beats_file(tmp_path):
    tf = tmp_path / "tok.txt"
    tf.write_text("filetoken")
    cfg = AgentConfig(node_name="N", token="explicit", token_file=str(tf))
    cfg.resolve_token()
    assert cfg.token == "explicit"


def test_token_from_env_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_INTAKE_NODE_TOKEN", "envtoken")
    cfg = AgentConfig(node_name="N", work_root=str(tmp_path / "nope"))
    cfg.resolve_token()
    assert cfg.token == "envtoken"


# --- UI-managed local settings ----------------------------------------------

def test_save_and_apply_local_settings(tmp_path):
    cfg = AgentConfig(node_name="OLD", coordinator_url="http://old:8443",
                      work_root=str(tmp_path / "work"))
    path = cfg.save_local_settings("http://192.168.35.25:8443", "WIN-01", "tok123")
    assert path.is_file()

    # A fresh config for the same work root picks the values up.
    fresh = AgentConfig(work_root=str(tmp_path / "work"))
    fresh.apply_local_settings()
    assert fresh.coordinator_url == "http://192.168.35.25:8443"
    assert fresh.node_name == "WIN-01"
    assert fresh.token == "tok123"


def test_load_config_overlays_settings_over_yaml(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / "agent_setup.json").write_text(json.dumps({
        "coordinator_url": "http://192.168.35.25:8443",
        "node_name": "WIN-01", "token": "uitok"}))
    yaml = tmp_path / "agent.yaml"
    yaml.write_text(f"node_name: PLACEHOLDER\nwork_root: {work.as_posix()}\n"
                    "coordinator_url: http://placeholder:8443\n"
                    "capabilities: [RINEX_CONVERT]\n")
    monkeypatch.delenv("DATA_INTAKE_NODE_TOKEN", raising=False)

    cfg = load_config(str(yaml))
    assert cfg.coordinator_url == "http://192.168.35.25:8443"
    assert cfg.node_name == "WIN-01"
    assert cfg.token == "uitok"
    assert cfg.capabilities == ["RINEX_CONVERT"]   # untouched by the overlay


# --- check_connection (shared with the window) ------------------------------

def test_check_connection_validates_fields():
    ok, msg = agent_setup.check_connection("", "N", "t")
    assert not ok and "URL" in msg
    ok, msg = agent_setup.check_connection("http://x", "", "t")
    assert not ok and "node name" in msg
    ok, msg = agent_setup.check_connection("http://x", "N", "")
    assert not ok and "token" in msg


def test_check_connection_401(monkeypatch):
    from agent.client import ReportConflict

    class FakeClient:
        def __init__(self, *a, **k): pass
        def sync(self, node, req): raise ReportConflict(401, "invalid node token")
        def close(self): pass

    monkeypatch.setattr("agent.client.CoordinatorClient", FakeClient)
    ok, msg = agent_setup.check_connection("http://x:8443", "WIN-01", "bad")
    assert not ok and "401" in msg


def test_check_connection_success(monkeypatch):
    from shared.schemas import SyncResponse

    class FakeClient:
        def __init__(self, *a, **k): pass
        def sync(self, node, req):
            return SyncResponse(node_name=node, enabled=True)
        def close(self): pass

    monkeypatch.setattr("agent.client.CoordinatorClient", FakeClient)
    ok, msg = agent_setup.check_connection("http://x:8443", "WIN-01", "good",
                                           capabilities=["RINEX_CONVERT"])
    assert ok and "WIN-01" in msg
