import { useEffect, useMemo, useState } from "react";
import { api } from "./api.js";
import { PathInput, PathLines, normalizePath } from "./FilePicker.jsx";
import { ErrorBanner } from "./ui.jsx";

// The intake form replaces data_intake.py's GUI: it collects DECISIONS only.
// The heavy work (copy to the NAS, RINEX conversion) runs as an INTAKE job on
// the machine that can see the source paths, then the selected chains follow.

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function todayDate() {
  const now = new Date();
  return `${String(now.getDate()).padStart(2, "0")}${MONTHS[now.getMonth()]}${now.getFullYear()}`;
}

function splitLines(text) {
  return text
    .split(/\r?\n/)
    .map((s) => normalizePath(s))
    .filter(Boolean);
}

const LIDAR = ["L2", "L3"];
const BASE_DATA_EXTS = [".t02", ".t04", ".t0b"];

export default function Submit({ onSubmitted }) {
  const [options, setOptions] = useState({ sensors: [], defaults: {} });
  const [form, setForm] = useState({
    root_path: "",
    client: "",
    project: "",
    date: todayDate(),
    sensor_type: "M3E",
    sources: "",
    bases: "",
    base_data_is_rinex: false,
    ecef: "",
    run_photo_chain: true,
    run_lidar_chain: false,
    gcp_path: "",
    epsg_h: "",
    epsg_v: "",
    no_targets: false,
    classify_model: "",
    priority: "100",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const [browseRoots, setBrowseRoots] = useState([]);

  useEffect(() => {
    api
      .browseRoots()
      .then((data) => setBrowseRoots(data.roots || []))
      .catch(() => {});
    api
      .intakeOptions()
      .then((opts) => {
        setOptions(opts);
        const d = opts.defaults || {};
        setForm((f) => ({
          ...f,
          root_path: f.root_path || d.root_path || "",
          epsg_h: f.epsg_h || d.epsg_h || "",
          epsg_v: f.epsg_v || d.epsg_v || "",
          classify_model:
            f.classify_model || (d.classify_models && d.classify_models[0]) || "",
        }));
      })
      .catch(() => {});
  }, []);

  const isLidar = LIDAR.includes(form.sensor_type);

  const set = (key) => (e) => {
    const value = e.target.type === "checkbox" ? e.target.checked : e.target.value;
    setForm((f) => ({ ...f, [key]: value }));
  };

  const setSensor = (e) => {
    const sensor = e.target.value;
    setForm((f) => ({
      ...f,
      sensor_type: sensor,
      // Sensible chain defaults; still editable below.
      run_photo_chain: ["M3E", "P1"].includes(sensor) || LIDAR.includes(sensor),
      run_lidar_chain: LIDAR.includes(sensor),
    }));
  };

  const payload = useMemo(() => {
    const ecefParts = splitLines(form.ecef.replaceAll(",", "\n"));
    return {
      root_path: normalizePath(form.root_path),
      client: form.client.trim(),
      project: form.project.trim(),
      date: form.date.trim(),
      sensor_type: form.sensor_type,
      source_folders: splitLines(form.sources),
      base_data_paths: splitLines(form.bases),
      base_data_is_rinex: form.base_data_is_rinex,
      base_ecef_xyz: ecefParts.length === 3 ? ecefParts.map(Number) : null,
      run_photo_chain: form.run_photo_chain,
      run_lidar_chain: isLidar && form.run_lidar_chain,
      gcp_path: normalizePath(form.gcp_path),
      epsg_h: form.epsg_h.trim(),
      epsg_v: form.epsg_v.trim(),
      no_targets: form.no_targets,
      classify_model: form.run_lidar_chain ? form.classify_model.trim() : "",
      priority: parseInt(form.priority, 10) || 100,
    };
  }, [form, isLidar]);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const resp = await api.submitIntake(payload);
      setResult(resp);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  if (result) {
    return (
      <section className="card">
        <h2>Submitted</h2>
        <div className="banner ok" style={{ marginBottom: 10 }}>
          Project <b>{result.name}</b> created with {result.jobs.length} job
          {result.jobs.length === 1 ? "" : "s"}:{" "}
          {result.jobs.map((j) => j.job_type).join(" → ")}
        </div>
        <div className="actions">
          <button
            className="btn primary"
            onClick={() => onSubmitted(result.project_uuid)}
          >
            Watch it on the project page
          </button>
          <button className="btn" onClick={() => setResult(null)}>
            Submit another
          </button>
        </div>
      </section>
    );
  }

  const models = options.defaults?.classify_models || [];

  return (
    <section className="card">
      <h2>Submit a flight</h2>
      <form className="intake" onSubmit={submit}>
        <fieldset>
          <legend>Project</legend>
          <div className="row3">
            <div className="field">
              <label>Client</label>
              <input type="text" required value={form.client} onChange={set("client")} />
            </div>
            <div className="field">
              <label>Project</label>
              <input type="text" required value={form.project} onChange={set("project")} />
            </div>
            <div className="field">
              <label>
                Flight date <span className="hint">ddMonYYYY</span>
              </label>
              <input
                type="text"
                required
                pattern="\d{2}[A-Za-z]{3}\d{4}"
                value={form.date}
                onChange={set("date")}
              />
            </div>
          </div>
          <div className="row">
            <div className="field">
              <label>Sensor</label>
              <select value={form.sensor_type} onChange={setSensor}>
                {(options.sensors.length
                  ? options.sensors
                  : ["M3E", "P1", "L2", "L3", "R3Pro", "R3ProMobile"]
                ).map((s) => (
                  <option key={s}>{s}</option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Priority</label>
              <select value={form.priority} onChange={set("priority")}>
                <option value="100">Normal</option>
                <option value="200">High</option>
                <option value="300">Rush</option>
              </select>
            </div>
          </div>
          <PathInput
            label="Projects root"
            hint="the storage share, e.g. \\192.168.35.25\3dData"
            required
            value={form.root_path}
            onChange={(v) => setForm((f) => ({ ...f, root_path: v }))}
            roots={browseRoots}
            mode="folder"
            pickerTitle="Pick the projects root folder"
          />
        </fieldset>

        <fieldset>
          <legend>Data</legend>
          <PathLines
            label="Source folder(s) — one per line"
            hint="must be reachable from the intake machine (ingest share or its local disk); drop or paste paths, or Browse"
            required
            value={form.sources}
            onChange={(v) => setForm((f) => ({ ...f, sources: v }))}
            roots={browseRoots}
            mode="folder"
            pickerTitle="Pick source folder(s)"
          />
          <PathLines
            label="Base data file(s) — one per line"
            hint=".T02/.T04, or a RINEX obs if already converted"
            value={form.bases}
            onChange={(v) => setForm((f) => ({ ...f, bases: v }))}
            roots={browseRoots}
            mode="file"
            exts={form.base_data_is_rinex ? null : BASE_DATA_EXTS}
            pickerTitle="Pick base data file(s)"
          />
          <label className="check">
            <input
              type="checkbox"
              checked={form.base_data_is_rinex}
              onChange={set("base_data_is_rinex")}
            />
            <span>
              Base data is already RINEX{" "}
              <span className="why">(skips Trimble conversion; companions are collected)</span>
            </span>
          </label>
          <div className="field">
            <label>
              Corrected base position — ECEF X, Y, Z{" "}
              <span className="hint">optional; comma-separated metres</span>
            </label>
            <input
              type="text"
              placeholder="e.g. -1878522.21, -4599428.34, 4001432.17"
              value={form.ecef}
              onChange={set("ecef")}
            />
          </div>
        </fieldset>

        <fieldset>
          <legend>Processing</legend>
          <label className="check">
            <input
              type="checkbox"
              checked={form.run_photo_chain}
              onChange={set("run_photo_chain")}
            />
            <span>
              Photo chain <span className="why">Terra PPK → Pix4Dmatic</span>
            </span>
          </label>
          <label className="check">
            <input
              type="checkbox"
              disabled={!isLidar}
              checked={isLidar && form.run_lidar_chain}
              onChange={set("run_lidar_chain")}
            />
            <span>
              LiDAR chain{" "}
              <span className="why">
                Terra reconstruction → Cyclone 3DR classification (L2/L3 only)
              </span>
            </span>
          </label>

          <div className="row3">
            <PathInput
              label="Targets / GCP csv"
              hint="TAT file"
              value={form.gcp_path}
              onChange={(v) => setForm((f) => ({ ...f, gcp_path: v }))}
              roots={browseRoots}
              mode="file"
              exts={[".csv"]}
              pickerTitle="Pick the targets / GCP csv"
            />
            <div className="field">
              <label>EPSG horizontal</label>
              <input type="text" value={form.epsg_h} onChange={set("epsg_h")} />
            </div>
            <div className="field">
              <label>EPSG vertical</label>
              <input type="text" value={form.epsg_v} onChange={set("epsg_v")} />
            </div>
          </div>

          {isLidar && form.run_lidar_chain && (
            <div className="row">
              <div className="field">
                <label>
                  3DR classification model{" "}
                  <span className="hint">blank = skip classification</span>
                </label>
                {models.length > 0 ? (
                  <select value={form.classify_model} onChange={set("classify_model")}>
                    <option value="">— skip classification —</option>
                    {models.map((m) => (
                      <option key={m}>{m}</option>
                    ))}
                  </select>
                ) : (
                  <input
                    type="text"
                    value={form.classify_model}
                    onChange={set("classify_model")}
                  />
                )}
              </div>
              <label className="check" style={{ alignSelf: "end" }}>
                <input type="checkbox" checked={form.no_targets} onChange={set("no_targets")} />
                <span>No targets</span>
              </label>
            </div>
          )}
        </fieldset>

        <ErrorBanner error={error} prefix="Submission rejected" />
        <div>
          <button className="btn primary" disabled={busy} type="submit">
            {busy ? "Submitting…" : "Queue it"}
          </button>
        </div>
      </form>
    </section>
  );
}
