"""Tray-mode surface of the Agent: the pause switch, the status snapshot the
tray window reads, and the pure status-line helpers. The tray UI itself
(pystray/tkinter) is Windows-only and not exercised here."""
import time

from agent.client import CoordinatorClient
from agent.config import AgentConfig
from agent.main import Agent
from agent.tray import state_text, sync_text
from processors import build_registry


def _make_agent(tmp_path) -> Agent:
    cfg = AgentConfig(node_name="TRAY-01",
                      coordinator_url="http://testserver",
                      work_root=str(tmp_path / "agent"),
                      capabilities=["MOCK"])
    cfg.ensure_dirs()
    client = CoordinatorClient("http://testserver", "tok")
    return Agent(cfg, client, build_registry(cfg, ["MOCK"]))


def test_pause_switch_stops_accepting(tmp_path):
    agent = _make_agent(tmp_path)
    assert agent.compute_accepting() is True

    agent.paused = True
    assert agent.compute_accepting() is False
    # The reason travels in telemetry so the dashboard can show it.
    req = agent.build_sync_request()
    assert req.accepting_jobs is False
    assert "paused from the agent's tray menu" in req.telemetry["preflight"]

    agent.paused = False
    assert agent.compute_accepting() is True


def test_status_snapshot_shape(tmp_path):
    agent = _make_agent(tmp_path)
    snap = agent.status_snapshot()
    assert snap["node"] == "TRAY-01"
    assert snap["job"] is None
    assert snap["paused"] is False
    assert snap["last_sync_ok"] is None


def test_state_text_variants():
    running = {"job": {"uuid": "abcd1234ef", "job_type": "TERRA_PPK",
                       "percent": 42.0, "message": "PPK pass"},
               "paused": False, "preflight": []}
    line, _ = state_text(running)
    assert "TERRA_PPK" in line and "42%" in line and "abcd1234" in line

    paused = {"job": None, "paused": True, "preflight": []}
    assert "Paused" in state_text(paused)[0]

    blocked = {"job": None, "paused": False, "preflight": ["locked desktop"]}
    assert "locked desktop" in state_text(blocked)[0]

    idle = {"job": None, "paused": False, "preflight": []}
    assert "accepting" in state_text(idle)[0]


def test_sync_text_variants():
    assert "Connecting" in sync_text({"last_sync_ok": None})[0]
    ok = {"last_sync_ok": True, "last_sync_at": time.time() - 5}
    assert "OK" in sync_text(ok)[0]
    bad = {"last_sync_ok": False, "last_sync_at": time.time(),
           "last_sync_error": "boom"}
    assert "boom" in sync_text(bad)[0]
