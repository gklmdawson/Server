"""Coordinator entry point.

Run in development:   python -m coordinator.main --config config/coordinator.yaml
Installed script:     data-intake-coordinator
PyInstaller build:    py build.py coordinator  ->  DataIntakeCoordinator.exe

Always run with a single worker: assignment correctness relies on one process
owning the SQLite database.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from coordinator import __version__
from coordinator.api import router
from coordinator.config import CoordinatorConfig, load_config
from coordinator.db import init_db, make_engine, make_session_factory

logger = logging.getLogger("coordinator")


def _dashboard_dir() -> Path:
    if getattr(sys, "frozen", False):  # PyInstaller: bundled via --add-data
        return Path(getattr(sys, "_MEIPASS")) / "coordinator" / "dashboard"
    return Path(__file__).resolve().parent / "dashboard"


def create_app(config: Optional[CoordinatorConfig] = None) -> FastAPI:
    cfg = config or load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    db_file = Path(cfg.db_path)
    if "://" not in cfg.db_path and db_file.parent != Path("."):
        db_file.parent.mkdir(parents=True, exist_ok=True)

    engine = make_engine(cfg.db_path)
    init_db(engine)

    app = FastAPI(title="Data Intake Coordinator", version=__version__)
    app.state.cfg = cfg
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.include_router(router)

    if not cfg.admin_token:
        logger.warning("No admin token configured — admin endpoints are open (LAN dev mode)")
    if not cfg.require_agent_tokens:
        logger.warning("require_agent_tokens=false — nodes auto-register without tokens")

    index_html = (_dashboard_dir() / "templates" / "index.html").read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> str:
        return index_html

    @app.get("/health", include_in_schema=False)
    def health_root() -> dict:
        return {"ok": True, "version": __version__}

    return app


def run() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Data Intake Coordinator")
    parser.add_argument("--config", default=None, help="path to coordinator YAML config")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port

    app = create_app(cfg)
    logger.info("Coordinator %s listening on %s:%s (db: %s)",
                __version__, cfg.host, cfg.port, cfg.db_path)
    uvicorn.run(app, host=cfg.host, port=cfg.port, workers=1, log_level="info")


if __name__ == "__main__":
    run()
