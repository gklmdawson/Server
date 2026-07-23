import { useState } from "react";
import Box from "@mui/material/Box";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Grid from "@mui/material/Grid";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import { api } from "./api.js";
import { usePoll } from "./usePoll.js";
import {
  ErrorBanner,
  Meter,
  OnlineChip,
  StatusChip,
  shortId,
  timeAgo,
} from "./ui.jsx";

// MUI reference implementation (the Machines tab still uses the .node-grid
// CSS — compare the two). Layout is MUI (Grid/Card/Stack, theme spacing
// units); Sunrise presentation pieces (OnlineChip, Meter, .cap) are reused
// as-is, which is the intended migration pattern: structure first, skins
// only where MUI genuinely replaces something.
function NodeCard({ node: n }) {
  return (
    <Card variant="outlined" sx={{ height: "100%" }}>
      <CardContent sx={{ px: 1.75, py: 1.5, "&:last-child": { pb: 1.5 } }}>
        <Stack spacing={1}>
          <Stack direction="row" spacing={1} sx={{ alignItems: "center" }}>
            <Typography component="span" sx={{ fontSize: 15, fontWeight: 650 }}>
              {n.node_name}
            </Typography>
            <OnlineChip node={n} />
            <Box sx={{ flexGrow: 1 }} />
            <Typography component="span" variant="body2" sx={{ color: "text.disabled", fontSize: 12 }}>
              {timeAgo(n.last_sync_at)}
            </Typography>
          </Stack>
          <Box>
            {(n.capabilities.length ? n.capabilities : ["no capabilities"]).map(
              (cap) => (
                <span
                  key={cap}
                  className={`cap readonly ${
                    n.effective_capabilities?.includes(cap) ? "" : "off"
                  }`}
                  title={
                    n.effective_capabilities?.includes(cap)
                      ? "assignable"
                      : "turned off on the Machines tab"
                  }
                >
                  {cap}
                </span>
              )
            )}
          </Box>
          {n.active_job ? (
            <Box>
              <Box className="dim" sx={{ mb: 0.5 }}>
                {n.active_job.job_type} · {n.active_job.project_name || "—"}
              </Box>
              <Meter percent={n.active_job.progress_percent} />
              <Box className="faint" sx={{ mt: 0.5, fontSize: 12 }}>
                {n.active_job.progress_message || "…"}
              </Box>
            </Box>
          ) : (
            <div className="faint">Idle</div>
          )}
          {(n.telemetry?.preflight || []).length > 0 && (
            <Box className="faint" sx={{ fontSize: 12 }}>
              ⏸ {n.telemetry.preflight.join("; ")}
            </Box>
          )}
        </Stack>
      </CardContent>
    </Card>
  );
}

function JobRow({ job, onOpenProject, children }) {
  return (
    <tr
      className={job.project_uuid ? "clickable" : ""}
      onClick={job.project_uuid ? () => onOpenProject(job.project_uuid) : undefined}
    >
      <td>
        <span className="mono faint">{shortId(job.job_uuid)}</span>
      </td>
      <td>{job.job_type}</td>
      <td className="dim">{job.project_name || "—"}</td>
      <td>
        <StatusChip status={job.status} stalled={job.stalled} />
      </td>
      {children}
    </tr>
  );
}

export default function Dashboard({ onOpenProject }) {
  const { data, error, refresh } = usePoll(api.status, 5000);
  const [actionError, setActionError] = useState(null);

  if (!data) {
    return error ? <ErrorBanner error={error} prefix="Cannot reach coordinator" /> : <div className="empty">Loading…</div>;
  }

  const act = (fn) => async () => {
    setActionError(null);
    try {
      await fn();
      await refresh();
    } catch (err) {
      setActionError(err);
    }
  };

  const deleteJob = (j) => async () => {
    if (!window.confirm(`Delete ${j.job_type} and any jobs waiting on it? This cannot be undone.`))
      return;
    await act(() => api.deleteJob(j.job_uuid))();
  };

  const attention = data.attention || [];

  return (
    <>
      <ErrorBanner error={error} prefix="Live updates interrupted" />
      <ErrorBanner error={actionError} prefix="Action failed" />

      <section className="card">
        <h2>
          Machines<span className="count">{data.nodes.length}</span>
        </h2>
        {data.nodes.length === 0 && (
          <div className="empty">
            No machines yet — install an agent (see DEPLOY.md) and it will
            appear here on its first sync.
          </div>
        )}
        <Grid container spacing={1.5}>
          {data.nodes.map((n) => (
            <Grid key={n.node_name} size={{ xs: 12, sm: 6, lg: 4 }}>
              <NodeCard node={n} />
            </Grid>
          ))}
        </Grid>
      </section>

      {attention.length > 0 && (
        <section className="card">
          <h2>
            Needs attention<span className="count">{attention.length}</span>
          </h2>
          <table>
            <thead>
              <tr>
                <th>Job</th>
                <th>Type</th>
                <th>Project</th>
                <th>Status</th>
                <th>Problem</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {attention.map((j) => (
                <JobRow key={j.job_uuid} job={j} onOpenProject={onOpenProject}>
                  <td className="dim">
                    {j.error_code && <span className="mono">{j.error_code}</span>}{" "}
                    {j.error_message}
                  </td>
                  <td onClick={(e) => e.stopPropagation()}>
                    <span className="actions">
                      <button className="btn small" onClick={act(() => api.retryJob(j.job_uuid))}>
                        Retry
                      </button>
                      <button
                        className="btn small danger"
                        onClick={act(() => api.cancelJob(j.job_uuid))}
                      >
                        Cancel
                      </button>
                      <button className="btn small danger" onClick={deleteJob(j)}>
                        Delete
                      </button>
                    </span>
                  </td>
                </JobRow>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <section className="card">
        <h2>
          Running<span className="count">{data.running.length}</span>
        </h2>
        {data.running.length === 0 ? (
          <div className="empty">Nothing running.</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Job</th>
                <th>Type</th>
                <th>Project</th>
                <th>Status</th>
                <th>Machine</th>
                <th>Progress</th>
              </tr>
            </thead>
            <tbody>
              {data.running.map((j) => (
                <JobRow key={j.job_uuid} job={j} onOpenProject={onOpenProject}>
                  <td>{j.assigned_node}</td>
                  <td style={{ minWidth: 200 }}>
                    <Meter percent={j.progress_percent} />
                    <div className="faint" style={{ fontSize: 12 }}>
                      {j.progress_message}
                    </div>
                  </td>
                </JobRow>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <h2>
          Queue<span className="count">{data.queue.length}</span>
        </h2>
        {data.queue.length === 0 ? (
          <div className="empty">Queue is empty.</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Job</th>
                <th>Type</th>
                <th>Project</th>
                <th>Status</th>
                <th>Waiting on</th>
                <th className="num">Priority</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {data.queue.map((j) => (
                <JobRow key={j.job_uuid} job={j} onOpenProject={onOpenProject}>
                  <td className="faint mono">
                    {(j.waiting_on || []).map(shortId).join(", ") || "—"}
                  </td>
                  <td className="num">{j.priority}</td>
                  <td className="dim">{timeAgo(j.created_at)}</td>
                </JobRow>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <h2>Recently finished</h2>
        {(data.recent || []).length === 0 ? (
          <div className="empty">Nothing yet.</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Job</th>
                <th>Type</th>
                <th>Project</th>
                <th>Status</th>
                <th>Machine</th>
                <th>Finished</th>
              </tr>
            </thead>
            <tbody>
              {data.recent.map((j) => (
                <JobRow key={j.job_uuid} job={j} onOpenProject={onOpenProject}>
                  <td>{j.assigned_node || "—"}</td>
                  <td className="dim">{timeAgo(j.finished_at)}</td>
                </JobRow>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
