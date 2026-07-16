// Small shared pieces: status chips (label always present — color is never
// the only signal), the single-hue progress meter, and formatting helpers.

const STATUS_STYLE = {
  QUEUED: ["", "Queued"],
  ASSIGNED: ["accent", "Assigned"],
  RUNNING: ["accent", "Running"],
  SUCCEEDED: ["good", "Succeeded"],
  FAILED: ["critical", "Failed"],
  CANCELLED: ["", "Cancelled"],
  NEEDS_ATTENTION: ["serious", "Needs attention"],
};

export function StatusChip({ status, stalled }) {
  const [cls, label] = STATUS_STYLE[status] || ["", status];
  return (
    <span className={`chip ${cls}`}>
      <span className="dot" />
      {label}
      {stalled ? " · stalled?" : ""}
    </span>
  );
}

export function OnlineChip({ node }) {
  if (!node.online) {
    return (
      <span className="chip critical">
        <span className="dot" />
        Offline
      </span>
    );
  }
  if (!node.enabled) {
    return (
      <span className="chip">
        <span className="dot" />
        Disabled
      </span>
    );
  }
  if (node.draining) {
    return (
      <span className="chip warning">
        <span className="dot" />
        Draining
      </span>
    );
  }
  if (!node.accepting_jobs) {
    return (
      <span className="chip warning">
        <span className="dot" />
        Paused
      </span>
    );
  }
  return (
    <span className="chip good">
      <span className="dot" />
      Online
    </span>
  );
}

export function Meter({ percent }) {
  const pct = percent == null ? null : Math.max(0, Math.min(100, percent));
  return (
    <span className="meter">
      <span className="track">
        <span className="fill" style={{ width: `${pct ?? 4}%` }} />
      </span>
      <span className="pct">{pct == null ? "—" : `${Math.round(pct)}%`}</span>
    </span>
  );
}

export function shortId(uuid) {
  return uuid ? uuid.slice(0, 8) : "";
}

export function timeAgo(iso) {
  if (!iso) return "—";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function localTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function ErrorBanner({ error, prefix }) {
  if (!error) return null;
  const hint =
    error.status === 401
      ? " — set the admin token (⚙ in the header)"
      : "";
  return (
    <div className="banner error">
      {prefix ? `${prefix}: ` : ""}
      {error.message}
      {hint}
    </div>
  );
}
