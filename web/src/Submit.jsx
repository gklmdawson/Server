import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api.js";
import { PathInput, PathLines, normalizePath } from "./FilePicker.jsx";
import { UploadField } from "./UploadField.jsx";
import { ErrorBanner } from "./ui.jsx";

// The intake form replaces data_intake.py's GUI: it collects DECISIONS only.
// Large flight data stays on the share (addressed by path); the small inputs
// (base data, targets csv, base ECEF csv) are UPLOADED here. Picking a source
// folder probes it on the NAS (EXIF) to pre-fill sensor/date/EPSG — those
// auto-filled fields LOCK (🔒) until explicitly unlocked, so a stray click
// can't silently change a detected value. The heavy work runs as
// INTAKE_COPY -> RINEX_CONVERT jobs.

function splitLines(text) {
  return text
    .split(/\r?\n/)
    .map((s) => normalizePath(s))
    .filter(Boolean);
}

const LIDAR = ["L2", "L3"];
const DEFAULT_ROOT_PATH = "\\\\192.168.35.25\\3dData";

// Trimble raw base files are T0x (.t02/.t04/.t0b…); anything else the operator
// drops is treated as already-RINEX (matches data_intake.py's all() rule).
const isTrimble = (name) => /\.t0\w$/i.test(name || "");

function chainDefaults(sensor) {
  return {
    run_photo_chain: ["M3E", "P1"].includes(sensor) || LIDAR.includes(sensor),
    run_lidar_chain: LIDAR.includes(sensor),
  };
}

// Small padlock shown beside auto-filled fields; clicking it re-enables edits.
function Unlock({ onClick }) {
  return (
    <button
      type="button"
      className="btn small unlock"
      title="Auto-filled — click to unlock and edit"
      aria-label="Unlock auto-filled field"
      onClick={onClick}
    >
      🔒
    </button>
  );
}

export default function Submit({ onSubmitted }) {
  const [options, setOptions] = useState({ sensors: [], defaults: {}, epsg_names: {} });
  const [form, setForm] = useState({
    root_path: DEFAULT_ROOT_PATH,
    client: "",
    project: "",
    date: "",
    sensor_type: "",
    sources: "",
    ecef: "",
    run_photo_chain: true,
    run_lidar_chain: false,
    epsg_h: "",
    epsg_v: "",
    no_targets: false,
    classify_model: "", // blank = skip classification (the default)
    priority: "100",
  });
  const [baseUploads, setBaseUploads] = useState([]); // [{name,size,stored_path,error}]
  const [gcpUpload, setGcpUpload] = useState([]);      // 0 or 1 item
  const [tltInfo, setTltInfo] = useState(null);        // TLT extraction preview
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const [browseRoots, setBrowseRoots] = useState([]);

  // Auto-fill locking: probe/csv-filled fields lock; once the operator unlocks
  // one, later probes leave it alone (their manual value wins from then on).
  const [locked, setLocked] = useState({});
  const [unlockedByUser, setUnlockedByUser] = useState({});
  const unlock = (key) => {
    setLocked((l) => ({ ...l, [key]: false }));
    setUnlockedByUser((u) => ({ ...u, [key]: true }));
  };

  // Probe (NAS helper) state.
  const [probeInfo, setProbeInfo] = useState(null);
  const [probeTarget, setProbeTarget] = useState(null); // {root, path}
  const [probing, setProbing] = useState(false);
  const [rtk, setRtk] = useState(null);
  const [rtkBusy, setRtkBusy] = useState(false);
  const [ecefBusy, setEcefBusy] = useState(false);
  const [ecefOver, setEcefOver] = useState(false);
  const [ecefErr, setEcefErr] = useState(null);
  const [ecefManual, setEcefManual] = useState(false);
  const ecefFileRef = useRef(null);

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
        // Config-default EPSG counts as auto-filled too, so it locks the same
        // way probe values do.
        const filled = {};
        setForm((f) => {
          if (!f.epsg_h && d.epsg_h) filled.epsg_h = true;
          if (!f.epsg_v && d.epsg_v) filled.epsg_v = true;
          return {
            ...f,
            // Config may override the built-in default; a value the operator
            // has already typed always wins.
            root_path:
              f.root_path && f.root_path !== DEFAULT_ROOT_PATH
                ? f.root_path
                : d.root_path || DEFAULT_ROOT_PATH,
            epsg_h: f.epsg_h || d.epsg_h || "",
            epsg_v: f.epsg_v || d.epsg_v || "",
          };
        });
        setLocked((l) => ({ ...l, ...filled }));
      })
      .catch(() => {});
  }, []);

  const isLidar = LIDAR.includes(form.sensor_type);
  const epsgNames = options.epsg_names || {};
  const epsgName = (code) => epsgNames[String(code || "").trim()] || "";

  const set = (key) => (e) => {
    const value = e.target.type === "checkbox" ? e.target.checked : e.target.value;
    setForm((f) => ({ ...f, [key]: value }));
  };

  // Base data: Trimble vs RINEX is decided by what was dropped, not a checkbox.
  const storedBase = baseUploads.filter((u) => u.stored_path);
  const baseIsRinex = storedBase.length > 0 && storedBase.every((u) => !isTrimble(u.name));
  const baseMixed =
    storedBase.some((u) => isTrimble(u.name)) && storedBase.some((u) => !isTrimble(u.name));

  // Targets csv: after uploading the all-points csv, preview how it splits by
  // point type. Intake writes SINGLE_TLT.csv (TLT only, for LiDAR) and TAT.csv
  // (TAT + TLT, for Pix4D) into the project folder; this just confirms counts.
  const onGcpItems = (items) => {
    setGcpUpload(items);
    const stored = items.find((it) => it.stored_path)?.stored_path;
    if (!stored) {
      setTltInfo(null);
      return;
    }
    setTltInfo({ busy: true });
    api
      .targetsSummary(stored)
      .then((r) => setTltInfo(r))
      .catch((err) => setTltInfo({ error: err.message || "could not read targets csv" }));
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
      const filled = {};
      setForm((f) => {
        const take = (key, val) => {
          if (val && !unlockedByUser[key]) {
            filled[key] = true;
            return val;
          }
          return f[key];
        };
        const next = {
          ...f,
          sensor_type: take("sensor_type", p.sensor),
          date: take("date", p.date),
          epsg_h: take("epsg_h", p.epsg_h),
          epsg_v: take("epsg_v", p.epsg_v),
        };
        if (filled.sensor_type) Object.assign(next, chainDefaults(p.sensor));
        return next;
      });
      setLocked((l) => ({ ...l, ...filled }));
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
      setLocked((l) => ({ ...l, ecef: true }));
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
      base_data_is_rinex: baseIsRinex,
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
  }, [form, isLidar, baseUploads, baseIsRinex, gcpUpload]);

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
  const showEcefInput = ecefManual || form.ecef.trim() !== "" || locked.ecef;

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
              <div className="lock-row">
                <input
                  type="text"
                  required
                  pattern="\d{2}[A-Za-z]{3}\d{4}"
                  placeholder="e.g. 10Jul2026"
                  value={form.date}
                  readOnly={!!locked.date}
                  className={locked.date ? "locked" : ""}
                  onChange={set("date")}
                />
                {locked.date && <Unlock onClick={() => unlock("date")} />}
              </div>
            </div>
          </div>
          <div className="row">
            <div className="field">
              <label>
                Sensor <span className="hint">auto-detected from the source folder</span>
              </label>
              <div className="lock-row">
                <select
                  required
                  value={form.sensor_type}
                  onChange={setSensor}
                  disabled={!!locked.sensor_type}
                  className={locked.sensor_type ? "locked" : ""}
                >
                  <option value="">— select sensor —</option>
                  {(options.sensors.length
                    ? options.sensors
                    : ["M3E", "P1", "L2", "L3", "R3Pro", "R3ProMobile"]
                  ).map((s) => (
                    <option key={s}>{s}</option>
                  ))}
                </select>
                {locked.sensor_type && <Unlock onClick={() => unlock("sensor_type")} />}
              </div>
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
                    ? `, EPSG ${probeInfo.epsg_h}${
                        epsgName(probeInfo.epsg_h) ? ` (${epsgName(probeInfo.epsg_h)})` : ""
                      }/${probeInfo.epsg_v || "?"}${
                        epsgName(probeInfo.epsg_v) ? ` (${epsgName(probeInfo.epsg_v)})` : ""
                      }`
                    : ", no EPSG (enter manually)"}{" "}
                  from {probeInfo.image_count || 0} image
                  {probeInfo.image_count === 1 ? "" : "s"}. Auto-filled fields are
                  locked — click 🔒 to edit.
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
            hint="Trimble .T0x or RINEX obs — type is auto-detected; uploaded to the NAS"
            multiple
            uploader={api.uploadIntakeFile}
            items={baseUploads}
            onItems={setBaseUploads}
            itemNote={(it) => (isTrimble(it.name) ? "Trimble" : "RINEX")}
          />
          {storedBase.length > 0 && (
            <div className="tlt-note">
              {baseMixed
                ? "Mixed Trimble + RINEX files — treated as Trimble; conversion will run on all of them."
                : baseIsRinex
                ? "RINEX base data detected — Trimble conversion is skipped; companion files are collected."
                : "Trimble base data detected — it will be converted to RINEX."}
            </div>
          )}
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
                optional; metres, from a Point ID,X,Y,Z csv or typed
              </span>
            </label>
            <input
              ref={ecefFileRef}
              type="file"
              accept=".csv"
              style={{ display: "none" }}
              onChange={(e) => {
                if (e.target.files?.length) takeEcefFile(e.target.files[0]);
                e.target.value = "";
              }}
            />
            {showEcefInput ? (
              <div className="ecef-input-row lock-row">
                <input
                  type="text"
                  placeholder="e.g. -1878522.21, -4599428.34, 4001432.17"
                  value={form.ecef}
                  readOnly={!!locked.ecef}
                  className={locked.ecef ? "locked" : ""}
                  onChange={set("ecef")}
                />
                {locked.ecef ? (
                  <Unlock
                    onClick={() => {
                      unlock("ecef");
                      setEcefManual(true);
                    }}
                  />
                ) : (
                  <button
                    type="button"
                    className="btn small"
                    disabled={ecefBusy}
                    onClick={() => ecefFileRef.current?.click()}
                  >
                    Browse…
                  </button>
                )}
              </div>
            ) : (
              <div
                className="upload-drop ecef-zone"
                onClick={() => ecefFileRef.current?.click()}
                role="button"
                tabIndex={0}
              >
                <span className="upload-hint">
                  {ecefBusy
                    ? "Parsing ECEF csv…"
                    : "Drop a Point ID,X,Y,Z csv here or click to browse"}
                </span>
                <button
                  type="button"
                  className="btn small"
                  onClick={(e) => {
                    e.stopPropagation();
                    setEcefManual(true);
                  }}
                >
                  Type it manually
                </button>
              </div>
            )}
            {ecefBusy && showEcefInput && <div className="drop-note">Parsing ECEF csv…</div>}
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

          <div>
            <UploadField
              label="Targets / GCP csv"
              hint="all points (TAT, TLT, misc) — split at intake"
              accept={[".csv"]}
              uploader={api.uploadIntakeFile}
              items={gcpUpload}
              onItems={onGcpItems}
            />
            {tltInfo && (
              <div className={`tlt-note ${tltInfo.error ? "tlt-err" : ""}`}>
                {tltInfo.busy && "Reading targets csv…"}
                {tltInfo.error && `Targets csv: ${tltInfo.error}`}
                {tltInfo.tlt_count != null &&
                  `${tltInfo.total_rows} point${tltInfo.total_rows === 1 ? "" : "s"}: ` +
                    `${tltInfo.tlt_count} TLT → SINGLE_TLT.csv (LiDAR), ` +
                    `${tltInfo.tat_count} TAT+TLT → TAT.csv (Pix4D). ` +
                    "Both saved to the project folder at intake."}
              </div>
            )}
          </div>

          <div className="row">
            <div className="field">
              <label>EPSG horizontal</label>
              <div className="lock-row">
                <input
                  type="text"
                  value={form.epsg_h}
                  readOnly={!!locked.epsg_h}
                  className={locked.epsg_h ? "locked" : ""}
                  onChange={set("epsg_h")}
                />
                {locked.epsg_h && <Unlock onClick={() => unlock("epsg_h")} />}
              </div>
              {epsgName(form.epsg_h) && (
                <span className="hint epsg-name">{epsgName(form.epsg_h)}</span>
              )}
            </div>
            <div className="field">
              <label>EPSG vertical</label>
              <div className="lock-row">
                <input
                  type="text"
                  value={form.epsg_v}
                  readOnly={!!locked.epsg_v}
                  className={locked.epsg_v ? "locked" : ""}
                  onChange={set("epsg_v")}
                />
                {locked.epsg_v && <Unlock onClick={() => unlock("epsg_v")} />}
              </div>
              {epsgName(form.epsg_v) && (
                <span className="hint epsg-name">{epsgName(form.epsg_v)}</span>
              )}
            </div>
          </div>

          {isLidar && form.run_lidar_chain && (
            <div className="row">
              <div className="field">
                <label>
                  3DR classification model{" "}
                  <span className="hint">defaults to skip</span>
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
