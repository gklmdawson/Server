import { useState } from "react";
import { api } from "./api.js";
import { usePoll } from "./usePoll.js";
import { ErrorBanner, OnlineChip, timeAgo } from "./ui.jsx";

// Per-machine management: what each box is allowed to run (capability
// toggles = declared-by-agent ∩ enabled-here), plus enable/disable/drain.
// Turning a capability off here is instant and reversible — the agent keeps
// declaring what's installed; the coordinator just stops routing that type.

function CapToggle({ node, cap, onChange, busy }) {
  const enabledList = node.enabled_capabilities; // null = all allowed
  const isOn = enabledList == null || enabledList.includes(cap);

  const toggle = () => {
    const current = enabledList == null ? [...node.capabilities] : [...enabledList];
    const next = isOn ? current.filter((c) => c !== cap) : [...current, cap];
    // Back to "everything declared" -> clear the restriction entirely.
    const allOn = node.capabilities.every((c) => next.includes(c));
    onChange(allOn ? null : next);
  };

  return (
    <label className={`cap ${isOn ? "" : "off"}`}>
      <input type="checkbox" checked={isOn} onChange={toggle} disabled={busy} />
      {cap}
    </label>
  );
}

function MachineCard({ node, refresh, setError }) {
  const [busy, setBusy] = useState(false);

  const run = async (fn) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await refresh();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const t = node.telemetry || {};

  return (
    <div className="node-card">
      <header>
        <span className="name">{node.node_name}</span>
        <OnlineChip node={node} />
        <span style={{ flex: 1 }} />
        <span className="sub">agent {node.agent_version || "—"}</span>
      </header>

      <div className="sub">
        {node.computer_name || "unknown host"}
        {node.current_user ? ` · ${node.current_user}` : ""} · last sync{" "}
        {timeAgo(node.last_sync_at)}
      </div>

      <div>
        <div className="sub" style={{ marginBottom: 4 }}>
          Allowed job types (installed on the box → toggle what it may run):
        </div>
        {node.capabilities.length === 0 && (
          <span className="faint">agent has not declared capabilities yet</span>
        )}
        {node.capabilities.map((cap) => (
          <CapToggle
            key={cap}
            node={node}
            cap={cap}
            busy={busy}
            onChange={(enabled) => run(() => api.setNodeCapabilities(node.node_name, enabled))}
          />
        ))}
      </div>

      <dl className="kv">
        {"cpu_percent" in t && (
          <>
            <dt>CPU</dt>
            <dd>{Math.round(t.cpu_percent)}%</dd>
          </>
        )}
        {"ram_percent" in t && (
          <>
            <dt>RAM</dt>
            <dd>{Math.round(t.ram_percent)}%</dd>
          </>
        )}
        {"disk_free_gb" in t && (
          <>
            <dt>Disk free</dt>
            <dd>{Math.round(t.disk_free_gb)} GB</dd>
          </>
        )}
        {(t.preflight || []).length > 0 && (
          <>
            <dt>Paused</dt>
            <dd>{t.preflight.join("; ")}</dd>
          </>
        )}
      </dl>

      <div className="actions">
        {node.enabled ? (
          <>
            <button
              className="btn small"
              disabled={busy || node.draining}
              onClick={() => run(() => api.drainNode(node.node_name))}
              title="Finish the current job, take nothing new"
            >
              Drain
            </button>
            <button
              className="btn small danger"
              disabled={busy}
              onClick={() => run(() => api.disableNode(node.node_name))}
            >
              Disable
            </button>
          </>
        ) : (
          <button
            className="btn small"
            disabled={busy}
            onClick={() => run(() => api.enableNode(node.node_name))}
          >
            Enable
          </button>
        )}
        {node.draining && (
          <button
            className="btn small"
            disabled={busy}
            onClick={() => run(() => api.enableNode(node.node_name))}
          >
            Stop draining
          </button>
        )}
        {/* Remove is offered only once the node is safely idle (offline or
            disabled), so a machine can't be deleted mid-work. */}
        {(!node.online || !node.enabled) && (
          <button
            className="btn small danger"
            disabled={busy}
            title="Remove this machine from the coordinator"
            onClick={() => {
              if (
                window.confirm(
                  `Remove ${node.node_name} from the coordinator?\n\n` +
                    "Stop this machine's agent first, or it will re-register " +
                    "on its next sync."
                )
              ) {
                run(() => api.deleteNode(node.node_name));
              }
            }}
          >
            Remove
          </button>
        )}
      </div>
    </div>
  );
}

export default function Machines() {
  const { data, error, refresh } = usePoll(api.nodes, 5000);
  const [actionError, setActionError] = useState(null);

  if (!data) {
    return error ? <ErrorBanner error={error} prefix="Cannot reach coordinator" /> : <div className="empty">Loading…</div>;
  }

  return (
    <>
      <ErrorBanner error={error} prefix="Live updates interrupted" />
      <ErrorBanner error={actionError} prefix="Change failed" />
      <section className="card">
        <h2>
          Machines<span className="count">{data.nodes.length}</span>
        </h2>
        <div className="node-grid">
          {data.nodes.map((n) => (
            <MachineCard
              key={n.node_name}
              node={n}
              refresh={refresh}
              setError={setActionError}
            />
          ))}
        </div>
        {data.nodes.length === 0 && (
          <div className="empty">
            No machines registered. Provision one with POST /api/v1/nodes and
            install the agent — see DEPLOY.md.
          </div>
        )}
      </section>
    </>
  );
}
