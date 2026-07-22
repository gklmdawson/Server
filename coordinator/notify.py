"""Push notifications via ntfy (https://ntfy.sh) — optional, off by default.

The coordinator publishes a small, curated set of alerts to one ntfy topic;
anyone who should get them subscribes to that topic in the ntfy phone app or
browser. The topic name is the only credential, so it must be unguessable —
generate one with:  python -c "import secrets; print('data-intake-' + secrets.token_urlsafe(24))"

Design constraints:
  * Never slow down or fail a request. Publishing is fire-and-forget from a
    single daemon worker thread with a bounded queue; when ntfy is unreachable
    the alert is dropped with a WARNING log, nothing else.
  * Alert only on transitions a human acts on (failure, needs-attention,
    node offline) or waits for (chain progress, project complete). Routine
    machinery — assignments, progress ticks, retries, cancels — stays out.
  * Stdlib only (urllib), so the PyInstaller coordinator build needs nothing new.

What gets published and at which ntfy priority:
  high (4):    job FAILED · job NEEDS_ATTENTION (node lost / lease expired /
               no longer reported)
  default (3): project complete (every job succeeded) · node offline
  low (2):     job succeeded (chain progress, silent) · job recovered after
               needs-attention · node back online · new intake submitted

The module-level notifier is configured once by create_app(); an empty topic
leaves every publish a no-op.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import urllib.request
from datetime import datetime
from typing import Any, Callable, Optional

logger = logging.getLogger("coordinator.notify")

# ntfy priorities (https://docs.ntfy.sh/publish/#message-priority)
PRIORITY_LOW = 2
PRIORITY_DEFAULT = 3
PRIORITY_HIGH = 4

_MAX_MESSAGE_CHARS = 600
_QUEUE_SIZE = 100
_HTTP_TIMEOUT_SECONDS = 10.0


def _http_transport(server: str, token: str, payload: dict[str, Any]) -> None:
    """POST one message to the ntfy server using the JSON publishing endpoint
    (topic in the body, so titles/messages are clean UTF-8 — no header
    encoding games)."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(server, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
        resp.read()


class Notifier:
    """Serialized, non-blocking publisher for one ntfy topic.

    `transport` is injectable for tests; production uses `_http_transport`.
    """

    def __init__(self, server: str = "", topic: str = "", token: str = "",
                 transport: Optional[Callable[[str, str, dict], None]] = None):
        self.server = (server or "https://ntfy.sh").rstrip("/")
        self.topic = topic.strip()
        self.token = token
        self._transport = transport or _http_transport
        self._queue: queue.Queue[dict] = queue.Queue(maxsize=_QUEUE_SIZE)
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # node_name -> last known online state, for offline/online transition
        # alerts. First sighting is a silent baseline (no alert storm when a
        # freshly restarted coordinator discovers its nodes).
        self.node_online: dict[str, bool] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.topic)

    def publish(self, title: str, message: str,
                priority: int = PRIORITY_DEFAULT, tags: Optional[list[str]] = None) -> None:
        if not self.enabled:
            return
        payload = {
            "topic": self.topic,
            "title": title,
            "message": message[:_MAX_MESSAGE_CHARS],
            "priority": priority,
            "tags": tags or [],
        }
        self._ensure_worker()
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            logger.warning("ntfy queue full; dropping alert: %s", title)

    def flush(self, timeout: float = 5.0) -> None:
        """Block until every queued alert has been handed to the transport
        (tests; also usable at shutdown)."""
        if self._worker is None:
            return
        deadline = threading.Event()
        t = threading.Thread(target=lambda: (self._queue.join(), deadline.set()), daemon=True)
        t.start()
        deadline.wait(timeout)

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._run, name="ntfy-notifier", daemon=True)
                self._worker.start()

    def _run(self) -> None:
        while True:
            payload = self._queue.get()
            try:
                self._transport(self.server, self.token, payload)
            except Exception as exc:
                logger.warning("ntfy publish failed (%s): %s",
                               payload.get("title", "?"), exc)
            finally:
                self._queue.task_done()


# ---------------------------------------------------------------------------
# Module singleton — configured by create_app(), no-op until then.
# ---------------------------------------------------------------------------

_notifier = Notifier()


def configure(cfg) -> Notifier:
    """(Re)build the module notifier from a CoordinatorConfig."""
    global _notifier
    _notifier = Notifier(server=cfg.ntfy_server, topic=cfg.ntfy_topic,
                         token=cfg.ntfy_token)
    if _notifier.enabled:
        logger.info("ntfy alerts enabled -> %s/%s", _notifier.server, _notifier.topic)
    return _notifier


def get_notifier() -> Notifier:
    return _notifier


# ---------------------------------------------------------------------------
# Message composition — one function per alert, called at the transition site.
# Job/Project are coordinator.db models; typed loosely to avoid an import cycle.
# ---------------------------------------------------------------------------

def _job_label(job) -> str:
    project = getattr(job, "project", None)
    if project is not None:
        client = f" ({project.client})" if project.client else ""
        return f"{job.job_type} — {project.name}{client}"
    return job.job_type


def _duration(start: Optional[datetime], end: Optional[datetime]) -> str:
    if not start or not end:
        return ""
    total = int((end - start).total_seconds())
    if total < 0:
        return ""
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def job_failed(job) -> None:
    detail = job.error_message or "no error detail"
    parts = []
    if job.assigned_node:
        parts.append(f"on {job.assigned_node}")
    if job.error_code:
        parts.append(f"[{job.error_code}]")
    prefix = " ".join(parts)
    _notifier.publish(
        title=f"Failed: {_job_label(job)}",
        message=f"{prefix}: {detail}" if prefix else detail,
        priority=PRIORITY_HIGH,
        tags=["rotating_light"],
    )


def job_needs_attention(job) -> None:
    _notifier.publish(
        title=f"Needs attention: {_job_label(job)}",
        message=job.error_message or job.error_code or "Check the dashboard.",
        priority=PRIORITY_HIGH,
        tags=["warning"],
    )


def job_recovered(job, reason: str = "") -> None:
    """A NEEDS_ATTENTION job went back to RUNNING — call off the search."""
    _notifier.publish(
        title=f"Recovered: {_job_label(job)}",
        message=reason or "The node reconnected and the job is running again.",
        priority=PRIORITY_LOW,
        tags=["white_check_mark"],
    )


def job_succeeded(job, done: int = 0, total: int = 0) -> None:
    """Silent chain-progress tick for one finished processing step."""
    parts = []
    if job.assigned_node:
        parts.append(f"on {job.assigned_node}")
    took = _duration(job.started_at, job.finished_at)
    if took:
        parts.append(f"in {took}")
    progress = f" · {done}/{total} jobs done" if total > 1 else ""
    _notifier.publish(
        title=f"Done: {_job_label(job)}",
        message=(" ".join(parts) or "Completed") + progress,
        priority=PRIORITY_LOW,
        tags=["white_check_mark"],
    )


def project_complete(project, total: int,
                     finished_at: Optional[datetime] = None) -> None:
    client = f" ({project.client})" if project.client else ""
    msg = f"All {total} jobs succeeded." if total > 1 else "Processing succeeded."
    took = _duration(project.created_at, finished_at)
    if took:
        msg += f" Total {took} from submission."
    _notifier.publish(
        title=f"Complete: {project.name}{client}",
        message=msg,
        priority=PRIORITY_DEFAULT,
        tags=["tada"],
    )


def intake_submitted(project, job_count: int, chains: list[str]) -> None:
    client = f" ({project.client})" if project.client else ""
    chain_txt = f" · chains: {', '.join(chains)}" if chains else ""
    _notifier.publish(
        title=f"New intake: {project.name}{client}",
        message=f"{project.sensor_type or 'unknown sensor'} · "
                f"{job_count} jobs queued{chain_txt}",
        priority=PRIORITY_LOW,
        tags=["inbox_tray"],
    )


def check_nodes(nodes, now: datetime, offline_after_seconds: int) -> None:
    """Alert on node online/offline transitions. Called from housekeeping with
    the full node list; disabled and draining nodes are expected to disappear,
    so they only ever update the baseline silently."""
    n = _notifier
    if not n.enabled:
        return
    for node in nodes:
        online = node.is_online(now, offline_after_seconds)
        previous = n.node_online.get(node.node_name)
        n.node_online[node.node_name] = online
        if previous is None or previous == online:
            continue
        if not node.enabled or node.draining:
            continue
        if online:
            n.publish(
                title=f"Node back online: {node.node_name}",
                message="The agent is syncing again.",
                priority=PRIORITY_LOW,
                tags=["electric_plug", "white_check_mark"],
            )
        else:
            n.publish(
                title=f"Node offline: {node.node_name}",
                message=f"No sync for over {offline_after_seconds}s. Queued jobs "
                        "needing its capabilities will wait until it returns.",
                priority=PRIORITY_DEFAULT,
                tags=["electric_plug"],
            )
