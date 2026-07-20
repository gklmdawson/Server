import { useState } from "react";
import { api } from "./api.js";
import { usePoll } from "./usePoll.js";
import {
  ErrorBanner,
  Meter,
  StatusChip,
  localTime,
  shortId,
  timeAgo,
} from "./ui.jsx";

function JobDetail({ jobUuid }) {
  const { data, error } = usePoll(() => api.job(jobUuid), 5000, [jobUuid]);
  if (!data) return <div className="empty">{error ? error.message : "Loading…"}</div>;
  return (
    <div style={{ padding: "6px 0 10px" }}>
      {data.error_message && (
        <div className="banner error" style={{ marginBottom: 8 }}>
          {data.error_code}: {data.error_message}
        </div>
      )}
      <details>
        <summary className="dim clickable">Parameters</summary>
        <pre className="mono dim" style={{ overflowX: "auto" }}>
          {JSON.stringify(data.parameters, null, 2)}
        </pre>
      </details>
      <ul className="events">
        {(data.events || []).map((e, i) => (
          <li key={i}>
            <span className="ts">{localTime(e.ts)}</span>
            <span className="type">{e.type}</span>
            <span className="dim">{e.message}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ProjectDetail({ uuid, onBack }) {
  const { data, error, refresh } = usePoll(() => api.project(uuid), 5000, [uuid]);
  const [openJob, setOpenJob] = useState(null);
  const [actionError, setActionError] = useState(null);

  if (!data) {
    return error ? <ErrorBanner error={error} /> : <div className="empty">Loading…</div>;
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

  const deleteProject = async () => {
    if (
      !window.confirm(
        `Delete the whole "${data.name}" submission and all its jobs? This cannot be undone.`
      )
    )
      return;
    setActionError(null);
    try {
      await api.deleteProject(uuid);
      onBack();
    } catch (err) {
      setActionError(err);
    }
  };

  return (
    <section className="card">
      <h2 style={{ display: "flex", alignItems: "center" }}>
        <button className="btn small" onClick={onBack} style={{ marginRight: 10 }}>
          ← All projects
        </button>
        {data.client ? `${data.client} — ` : ""}
        {data.name}
        <span className="count">
          {data.sensor_type} · {data.date_folder}
        </span>
        <button
          className="btn small danger"
          style={{ marginLeft: "auto" }}
          onClick={deleteProject}
        >
          Delete project
        </button>
      </h2>
      <ErrorBanner error={actionError} prefix="Action failed" />
      {data.root_path && (
        <div className="faint mono" style={{ marginBottom: 8 }}>
          {data.root_path}
        </div>
      )}
      <table>
        <thead>
          <tr>
            <th>Job</th>
            <th>Type</th>
            <th>Status</th>
            <th>Machine</th>
            <th>Progress</th>
            <th>Updated</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {data.jobs.map((j) => (
            <>
              <tr
                key={j.job_uuid}
                className="clickable"
                onClick={() => setOpenJob(openJob === j.job_uuid ? null : j.job_uuid)}
              >
                <td>
                  <span className="mono faint">{shortId(j.job_uuid)}</span>
                </td>
                <td>{j.job_type}</td>
                <td>
                  <StatusChip status={j.status} />
                </td>
                <td>{j.assigned_node || "—"}</td>
                <td style={{ minWidth: 180 }}>
                  {j.status === "RUNNING" || j.progress_percent != null ? (
                    <Meter percent={j.progress_percent} />
                  ) : (
                    <span className="faint">
                      {(j.waiting_on || []).length
                        ? `waiting on ${j.waiting_on.map(shortId).join(", ")}`
                        : "—"}
                    </span>
                  )}
                </td>
                <td className="dim">
                  {timeAgo(j.finished_at || j.last_progress_at || j.created_at)}
                </td>
                <td onClick={(e) => e.stopPropagation()}>
                  <span className="actions">
                    {["FAILED", "CANCELLED", "NEEDS_ATTENTION"].includes(j.status) && (
                      <button className="btn small" onClick={act(() => api.retryJob(j.job_uuid))}>
                        Retry
                      </button>
                    )}
                    {["QUEUED", "ASSIGNED", "RUNNING", "NEEDS_ATTENTION"].includes(
                      j.status
                    ) && (
                      <button
                        className="btn small danger"
                        onClick={act(() => api.cancelJob(j.job_uuid))}
                      >
                        Cancel
                      </button>
                    )}
                    {["QUEUED", "SUCCEEDED", "FAILED", "CANCELLED", "NEEDS_ATTENTION"].includes(
                      j.status
                    ) && (
                      <button className="btn small danger" onClick={deleteJob(j)}>
                        Delete
                      </button>
                    )}
                  </span>
                </td>
              </tr>
              {openJob === j.job_uuid && (
                <tr key={`${j.job_uuid}-detail`}>
                  <td colSpan={7}>
                    <JobDetail jobUuid={j.job_uuid} />
                  </td>
                </tr>
              )}
            </>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export default function Projects({ selected, onSelect }) {
  const { data, error } = usePoll(api.projects, 8000);

  if (selected) {
    return <ProjectDetail uuid={selected} onBack={() => onSelect(null)} />;
  }
  if (!data) {
    return error ? <ErrorBanner error={error} prefix="Cannot reach coordinator" /> : <div className="empty">Loading…</div>;
  }

  return (
    <section className="card">
      <h2>
        Projects<span className="count">{data.projects.length}</span>
      </h2>
      {data.projects.length === 0 ? (
        <div className="empty">No projects yet — use Submit to create one.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Client</th>
              <th>Project</th>
              <th>Sensor</th>
              <th>Jobs</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {data.projects.map((p) => (
              <tr
                key={p.project_uuid}
                className="clickable"
                onClick={() => onSelect(p.project_uuid)}
              >
                <td>{p.client || "—"}</td>
                <td>{p.name}</td>
                <td>{p.sensor_type || "—"}</td>
                <td>
                  {Object.entries(p.job_counts || {}).map(([status, count]) => (
                    <span key={status} style={{ marginRight: 6, whiteSpace: "nowrap" }}>
                      <StatusChip status={status} />
                      <span className="dim"> ×{count}</span>
                    </span>
                  ))}
                </td>
                <td className="dim">{timeAgo(p.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
