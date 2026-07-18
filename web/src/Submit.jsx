import { useEffect, useMemo, useState } from "react";
import { api } from "./api.js";
import { PathInput, PathLines, normalizePath } from "./FilePicker.jsx";
import { UploadField } from "./UploadField.jsx";
import { ErrorBanner } from "./ui.jsx";

// The intake form replaces data_intake.py's GUI: it collects DECISIONS only.
// Large flight data stays on the share (addressed by path); the small inputs
// (base data, targets csv, base ECEF csv) are UPLOADED here. Picking a source
// folder probes it on the NAS (EXIF) to pre-fill sensor/date/EPSG — all still
// editable. The heavy work runs as INTAKE_COPY -> RINEX_CONVERT jobs.

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

function chainDefaults(sensor) {
  return {
    run_photo_chain: ["M3E", "P1"].includes(sensor) || LIDAR.includes(sensor),
    run_lidar_chain: LIDAR.includes(sensor),
  };
}

export default function Submit({ onSubmitted }) {
  const [options, setOptions] = useState({ sensors: [], defaults: {} });
  const [form, setForm] = useState({
    root_path: "",
    client: "",
    project: "",
    date: todayDate(),
    sensor_type: "M3E",
    sources: "",
    base_data_is_rinex: false,
    ecef: "",
    run_photo_chain: true,
    run_lidar_chain: false,
    epsg_h: "",
    epsg_v: "",
    no_targets: false,
    classify_model: "",
    priority: "100",
  });
  const [baseUploads, setBaseUploads] = useState([]); // [{name,size,stored_path,error}]
  const [gcpUpload, setGcpUpload] = useState([]);      // 0 or 1 item
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const [browseRoots, setBrowseRoots] = useState([]);

  // Probe (NAS helper) state.
  const [probeInfo, setProbeInfo] = useState(null);
  const [probeTarget, setProbeTarget] = useState(null); // {root, path}
  const [probing, setProbing] = useState(false);
  const [rtk, setRtk] = useState(null);
  const [rtkBusy, setRtkBusy] = useState(false);
  const [ecefBusy, setEcefBusy] = useState(false);
  const [ecefOver, setEcefOver] = useState(false);
  const [ecefErr, setEcefErr] = useState(null);

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
    setForm((f) => ({ ...f, sensor_type: sensor, ...chainDefaults(sensor) }));
  };

  // --- probe: pre-fill sensor/date/EPSG from the picked source folder ---
  const runProbe = async (root, relPath) => {
    setProbeTarget({ root, path: relPath });
    setProbing(true);
    setRtk(null);
    setProbeInfo(null);
    try {
      const p = await api.probe(root, relPath);
      setProbeInfo(p);
      setForm((f) => ({
        ...f,
        sensor_type: p.sensor || f.sensor_type,
        date: p.date || f.date,
        epsg_h: p.epsg_h || f.epsg_h,
        epsg_v: p.epsg_v || f.epsg_v,
        ...(p.sensor ? chainDefaults(p.sensor) : {}),
      }));
    } catch (err) {
      setProbeInfo({ error: err.message || "probe failed" });
    } finally {
      setProbing(false);
    }
  };

  const onSourceMeta = (root, relPaths) => {
    if (root && relPaths && relPaths.length) runProbe(root, relPaths[0]);
  };

  const checkRtk = async () => {
    if (!probeTarget) return;
    setRtkBusy(true);
    setRtk(null);
    try {
      const p = await api.probe(probeTarget.root, probeTarget.path, true);
      setRtk(p.rtk || { error: "no result" });
    } catch (err) {
      setRtk({ error: err.message || "scan failed" });
    } finally {
      setRtkBusy(false);
    }
  };

  // --- base ECEF csv: drop a file, parse X/Y/Z into the field ---
  const takeEcefFile = async (file) => {
    if (!file) return;
    setEcefBusy(true);
    setEcefErr(null);
    try {
      const r = await api.parseEcefFile(file);
      const [x, y, z] = r.ecef;
      setForm((f) => ({ ...f, ecef: `${x}, ${y}, ${z}` }));
    } catch (err) {
      setEcefErr(err.message || "could not parse ECEF csv");
    } finally {
      setEcefBusy(false);
    }
  };

  const payload = useMemo(() => {
    const ecefParts = splitLines(form.ecef.replaceAll(",", "\n"));
    const gcp = gcpUpload[0]?.stored_path || "";
    return {
      root_path: normalizePath(form.root_path),
      client: form.client.trim(),
      project: form.project.trim(),
      date: form.date.trim(),
      sensor_type: form.sensor_type,
      source_folders: splitLines(form.sources),
      base_data_paths: baseUploads.filter((u) => u.stored_path).map((u) => u.stored_path),
      base_data_is_rinex: form.base_data_is_rinex,
      base_ecef_xyz: ecefParts.length === 3 ? ecefParts.map(Number) : null,
      run_photo_chain: form.run_photo_chain,
      run_lidar_chain: isLidar && form.run_lidar_chain,
      gcp_path: gcp,
      epsg_h: form.epsg_h.trim(),
      epsg_v: form.epsg_v.trim(),
      no_targets: form.no_targets,
      classify_model: form.run_lidar_chain ? form.classify_model.trim() : "",
      priority: parseInt(form.priority, 10) || 100,
    };
  }, [form, isLidar, baseUploads, gcpUpload]);

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
            hint="the flight data on the share/card; picking one auto-detects sensor, date & EPSG below"
            required
            value={form.sources}
            onChange={(v) => setForm((f) => ({ ...f, sources: v }))}
            roots={browseRoots}
            mode="folder"
            pickerTitle="Pick source folder(s)"
            onPickMeta={onSourceMeta}
          />
          {(probing || probeInfo) && (
            <div className={`banner ${probeInfo?.error ? "error" : "ok"} probe-banner`}>
              {probing && "Reading flight images on the NAS…"}
              {probeInfo?.error && `Auto-detect unavailable: ${probeInfo.error}`}
              {probeInfo && !probeInfo.error && (
                <>
                  Auto-detected{" "}
                  <b>{probeInfo.sensor || "unknown sensor"}</b>
                  {probeInfo.exif_model ? ` (${probeInfo.exif_model})` : ""}
                  {probeInfo.date ? `, ${probeInfo.date}` : ""}
                  {probeInfo.epsg_h
                    ? `, EPSG ${probeInfo.epsg_h}/${probeInfo.epsg_v || "?"}`
                    : ", no EPSG (enter manually)"}{" "}
                  from {probeInfo.image_count || 0} image
                  {probeInfo.image_count === 1 ? "" : "s"}. Everything below is editable.
                  {"  "}
                  <button
                    type="button"
                    className="btn small"
                    disabled={rtkBusy}
                    onClick={checkRtk}
                  >
                    {rtkBusy ? "Scanning…" : "Check RTK coverage"}
                  </button>
                  {rtk && !rtk.error && rtk.fixed_pct != null && (
                    <span className="hint">
                      {" "}
                      RTK fixed on {rtk.fixed_pct.toFixed(0)}% of {rtk.total_photos} photos
                    </span>
                  )}
                  {rtk?.error && <span className="hint"> RTK scan: {rtk.error}</span>}
                </>
              )}
            </div>
          )}

          <UploadField
            label="Base data file(s)"
            hint=".T02/.T04, or a RINEX obs if already converted — uploaded to the NAS"
            accept={form.base_data_is_rinex ? null : BASE_DATA_EXTS}
            multiple
            uploader={api.uploadIntakeFile}
            items={baseUploads}
            onItems={setBaseUploads}
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
          <div
            className={`field ecef-drop ${ecefOver ? "drag-over" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setEcefOver(true);
            }}
            onDragLeave={() => setEcefOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setEcefOver(false);
              if (e.dataTransfer.files?.length) takeEcefFile(e.dataTransfer.files[0]);
            }}
          >
            <label>
              Corrected base position — ECEF X, Y, Z{" "}
              <span className="hint">
                optional; type metres or drop a Point ID,X,Y,Z csv
              </span>
            </label>
            <input
              type="text"
              placeholder="e.g. -1878522.21, -4599428.34, 4001432.17"
              value={form.ecef}
              onChange={set("ecef")}
            />
            {ecefBusy && <div className="drop-note">Parsing ECEF csv…</div>}
            {ecefErr && <div className="drop-note">{ecefErr}</div>}
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
            <UploadField
              label="Targets / GCP csv"
              hint="TAT file — uploaded to the NAS"
              accept={[".csv"]}
              uploader={api.uploadIntakeFile}
              items={gcpUpload}
              onItems={setGcpUpload}
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
