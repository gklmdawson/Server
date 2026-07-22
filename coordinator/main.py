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
from fastapi.staticfiles import StaticFiles

from coordinator import __version__, notify
from coordinator.api import router
from coordinator.config import CoordinatorConfig, load_config
from coordinator.db import init_db, make_engine, make_session_factory

logger = logging.getLogger("coordinator")


def _dashboard_dir() -> Path:
    if getattr(sys, "frozen", False):  # PyInstaller: bundled via --add-data
        return Path(getattr(sys, "_MEIPASS")) / "coordinator" / "dashboard"
    return Path(__file__).resolve().parent / "dashboard"


def _webui_dir() -> Optional[Path]:
    """The built React app (web/dist), when present. Search order:
    $DATA_INTAKE_WEBUI_DIR (Docker sets it), the PyInstaller bundle, then the
    repo checkout. None -> the legacy single-file dashboard is served instead,
    so a coordinator without a Node build still has a monitoring page."""
    import os
    candidates = []
    env = os.environ.get("DATA_INTAKE_WEBUI_DIR")
    if env:
        candidates.append(Path(env))
    if getattr(sys, "frozen", False):
        candidates.append(Path(getattr(sys, "_MEIPASS")) / "web" / "dist")
    candidates.append(Path(__file__).resolve().parent.parent / "web" / "dist")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


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
    notify.configure(cfg)

    app = FastAPI(title="Data Intake Coordinator", version=__version__)
    app.state.cfg = cfg
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.include_router(router)

    @app.middleware("http")
    async def commit_before_response(request, call_next):
        """Commit the request's DB session BEFORE the response goes out, so a
        client's next request always sees this one's writes (read-your-writes;
        see get_session in api.py). Error responses roll back, matching the
        old yield-dependency semantics."""
        from starlette.concurrency import run_in_threadpool
        try:
            response = await call_next(request)
        except Exception:
            session = getattr(request.state, "db_session", None)
            if session is not None:
                await run_in_threadpool(session.rollback)
                await run_in_threadpool(session.close)
            raise
        session = getattr(request.state, "db_session", None)
        if session is not None:
            try:
                if response.status_code < 400:
                    await run_in_threadpool(session.commit)
                else:
                    await run_in_threadpool(session.rollback)
            finally:
                await run_in_threadpool(session.close)
        return response

    if not cfg.admin_token:
        logger.warning("No admin token configured — admin endpoints are open (LAN dev mode)")
    if not cfg.require_agent_tokens:
        logger.warning("require_agent_tokens=false — nodes auto-register without tokens")

    @app.get("/health", include_in_schema=False)
    def health_root() -> dict:
        return {"ok": True, "version": __version__}

    webui = _webui_dir()
    if webui is not None:
        # React UI. Mounted at "/" LAST so /api/v1/* and /health win; html=True
        # serves index.html for "/".
        logger.info("Serving web UI from %s", webui)
        app.mount("/", StaticFiles(directory=str(webui), html=True), name="webui")
    else:
        logger.warning("web/dist not found — serving the legacy fallback dashboard "
                       "(build the UI with: cd web && npm install && npm run build)")
        index_html = (_dashboard_dir() / "templates" / "index.html").read_text(encoding="utf-8")

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        def dashboard() -> str:
            return index_html

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
