from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from coordinator.config import CoordinatorConfig
from coordinator.main import create_app


def make_config(tmp_path, **overrides) -> CoordinatorConfig:
    cfg = CoordinatorConfig(
        db_path=str(tmp_path / "coordinator-test.db"),
        require_agent_tokens=False,
        admin_token="",
    )
    cfg.templates["mock_chain"] = [
        {"job_type": "MOCK_A"},
        {"job_type": "MOCK_B", "depends_on": ["MOCK_A"]},
    ]
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


@pytest.fixture
def cfg(tmp_path) -> CoordinatorConfig:
    return make_config(tmp_path)


@pytest.fixture
def app(cfg):
    return create_app(cfg)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db(app):
    """Open a raw DB session on the app's database (for backdating timestamps
    in recovery tests — the only place tests reach around the API)."""
    def _open():
        return app.state.session_factory()
    return _open
