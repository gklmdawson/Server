import { useEffect, useMemo, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import AlertTitle from "@mui/material/AlertTitle";
import Autocomplete from "@mui/material/Autocomplete";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Collapse from "@mui/material/Collapse";
import FormControlLabel from "@mui/material/FormControlLabel";
import FormHelperText from "@mui/material/FormHelperText";
import FormLabel from "@mui/material/FormLabel";
import Grid from "@mui/material/Grid";
import IconButton from "@mui/material/IconButton";
import InputAdornment from "@mui/material/InputAdornment";
import LinearProgress from "@mui/material/LinearProgress";
import Stack from "@mui/material/Stack";
import Step from "@mui/material/Step";
import StepLabel from "@mui/material/StepLabel";
import Stepper from "@mui/material/Stepper";
import Switch from "@mui/material/Switch";
import TextField from "@mui/material/TextField";
import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import LockIcon from "@mui/icons-material/Lock";
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

// Padlock adornment for auto-filled fields; clicking it re-enables edits.
function UnlockAdornment({ onClick }) {
  return (
    <InputAdornment position="end">
      <Tooltip title="Auto-filled — click to unlock and edit">
        <IconButton size="small" edge="end" aria-label="Unlock auto-filled field" onClick={onClick}>
          <LockIcon fontSize="inherit" />
        </IconButton>
      </Tooltip>
    </InputAdornment>
  );
}

// Standalone padlock for controls that can't host an adornment (the sensor
// select's arrow occupies the end slot).
function UnlockButton({ onClick }) {
  return (
    <Tooltip title="Auto-filled — click to unlock and edit">
      <IconButton size="small" aria-label="Unlock auto-filled field" sx={{ mt: 0.75 }} onClick={onClick}>
        <LockIcon fontSize="inherit" />
      </IconButton>
    </Tooltip>
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
        <Alert severity="success">
          <AlertTitle>
            Project <b>{result.name}</b> created with {result.jobs.length} job
            {result.jobs.length === 1 ? "" : "s"}
          </AlertTitle>
          The chain runs left to right as each machine finishes its step.
        </Alert>
        <Stepper activeStep={-1} alternativeLabel sx={{ my: 2 }}>
          {result.jobs.map((j, i) => (
            <Step key={i}>
              <StepLabel>{j.job_type}</StepLabel>
            </Step>
          ))}
        </Stepper>
        <Stack direction="row" spacing={1}>
          <Button variant="contained" onClick={() => onSubmitted(result.project_uuid)}>
            Watch it on the project page
          </Button>
          <Button variant="outlined" onClick={() => setResult(null)}>
            Submit another
          </Button>
        </Stack>
      </section>
    );
  }

  const models = options.defaults?.classify_models || [];
  const showEcefInput = ecefManual || form.ecef.trim() !== "" || locked.ecef;

  const probeAlert = () => {
    if (probing) {
      return (
        <Alert severity="info" icon={<CircularProgress size={18} />}>
          Reading flight images on the NAS…
          <LinearProgress sx={{ mt: 1 }} />
        </Alert>
      );
    }
    if (probeInfo?.error) {
      return <Alert severity="warning">Auto-detect unavailable: {probeInfo.error}</Alert>;
    }
    if (!probeInfo) return null;
    return (
      <Alert
        severity="success"
        action={
          <Button color="inherit" size="small" loading={rtkBusy} onClick={checkRtk}>
            {rtkBusy ? "Scanning…" : "Check RTK coverage"}
          </Button>
        }
      >
        Auto-detected <b>{probeInfo.sensor || "unknown sensor"}</b>
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
        {probeInfo.image_count === 1 ? "" : "s"}. Auto-filled fields are locked — click the
        padlock to edit.
        {rtk && !rtk.error && rtk.fixed_pct != null && (
          <Chip
            size="small"
            sx={{ ml: 1 }}
            label={`RTK fixed on ${rtk.fixed_pct.toFixed(0)}% of ${rtk.total_photos} photos`}
          />
        )}
        {rtk?.error && (
          <Typography component="span" variant="caption" sx={{ ml: 1 }}>
            RTK scan: {rtk.error}
          </Typography>
        )}
      </Alert>
    );
  };

  return (
    <section className="card">
      <h2>Submit a flight</h2>
      <form className="intake" onSubmit={submit}>
        {/* The flight folder is the true starting point: probing it fills
            sensor/date/EPSG (and picks chain defaults) for everything below,
            so it leads the form and gets the brand-yellow hero CTA. */}
        <fieldset className="hero-step">
          <legend>1 · Flight data — start here</legend>
          <PathLines
            label="Source folder(s) — one per line"
            hint="the flight data on the share/card; picking one auto-fills sensor, date & EPSG below"
            required
            value={form.sources}
            onChange={(v) => setForm((f) => ({ ...f, sources: v }))}
            roots={browseRoots}
            mode="folder"
            pickerTitle="Pick source folder(s)"
            onPickMeta={onSourceMeta}
            browseHero
            browseLabel="📂 Pick flight folder(s)…"
          />
          <Collapse in={probing || !!probeInfo}>{probeAlert()}</Collapse>
        </fieldset>

        <fieldset>
          <legend>2 · Project</legend>
          <Grid container spacing={1.5}>
            <Grid size={{ xs: 12, sm: 4 }}>
              <TextField
                fullWidth
                size="small"
                label="Client"
                required
                value={form.client}
                onChange={set("client")}
              />
            </Grid>
            <Grid size={{ xs: 12, sm: 4 }}>
              <TextField
                fullWidth
                size="small"
                label="Project"
                required
                value={form.project}
                onChange={set("project")}
              />
            </Grid>
            <Grid size={{ xs: 12, sm: 4 }}>
              <TextField
                fullWidth
                size="small"
                label="Flight date"
                helperText="ddMonYYYY"
                required
                placeholder="e.g. 10Jul2026"
                value={form.date}
                onChange={set("date")}
                slotProps={{
                  htmlInput: { pattern: "\\d{2}[A-Za-z]{3}\\d{4}" },
                  input: {
                    readOnly: !!locked.date,
                    endAdornment: locked.date && <UnlockAdornment onClick={() => unlock("date")} />,
                  },
                }}
              />
            </Grid>
            <Grid size={{ xs: 12, sm: 6 }}>
              <Stack direction="row" spacing={0.5} sx={{ alignItems: "flex-start" }}>
                <TextField
                  fullWidth
                  size="small"
                  select
                  label="Sensor"
                  helperText="auto-detected from the source folder"
                  required
                  value={form.sensor_type}
                  onChange={setSensor}
                  disabled={!!locked.sensor_type}
                  slotProps={{ select: { native: true } }}
                >
                  <option value="">— select sensor —</option>
                  {(options.sensors.length
                    ? options.sensors
                    : ["M3E", "P1", "L2", "L3", "R3Pro", "R3ProMobile"]
                  ).map((s) => (
                    <option key={s}>{s}</option>
                  ))}
                </TextField>
                {locked.sensor_type && <UnlockButton onClick={() => unlock("sensor_type")} />}
              </Stack>
            </Grid>
            <Grid size={{ xs: 12, sm: 6 }}>
              <Stack spacing={0.5}>
                <FormLabel sx={{ fontSize: 12, fontWeight: 600 }}>Priority</FormLabel>
                <ToggleButtonGroup
                  exclusive
                  size="small"
                  color="primary"
                  value={form.priority}
                  onChange={(e, v) => v != null && setForm((f) => ({ ...f, priority: v }))}
                >
                  <ToggleButton value="100">Normal</ToggleButton>
                  <ToggleButton value="200">High</ToggleButton>
                  <ToggleButton value="300">Rush</ToggleButton>
                </ToggleButtonGroup>
              </Stack>
            </Grid>
          </Grid>
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
          <legend>3 · Base station</legend>
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
            <Alert severity={baseMixed ? "warning" : "info"} sx={{ py: 0 }}>
              {baseMixed
                ? "Mixed Trimble + RINEX files — treated as Trimble; conversion will run on all of them."
                : baseIsRinex
                ? "RINEX base data detected — Trimble conversion is skipped; companion files are collected."
                : "Trimble base data detected — it will be converted to RINEX."}
            </Alert>
          )}
          <Box
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
              <TextField
                fullWidth
                size="small"
                label="Corrected base position — ECEF X, Y, Z"
                placeholder="e.g. -1878522.21, -4599428.34, 4001432.17"
                helperText={ecefErr || "optional; metres, from a Point ID,X,Y,Z csv or typed"}
                error={!!ecefErr}
                value={form.ecef}
                onChange={set("ecef")}
                slotProps={{
                  input: {
                    readOnly: !!locked.ecef,
                    endAdornment: (
                      <InputAdornment position="end">
                        {ecefBusy && <CircularProgress size={16} sx={{ mr: 1 }} />}
                        {locked.ecef ? (
                          <UnlockAdornment
                            onClick={() => {
                              unlock("ecef");
                              setEcefManual(true);
                            }}
                          />
                        ) : (
                          <Button
                            size="small"
                            disabled={ecefBusy}
                            onClick={() => ecefFileRef.current?.click()}
                          >
                            Browse…
                          </Button>
                        )}
                      </InputAdornment>
                    ),
                  },
                }}
              />
            ) : (
              <Stack spacing={0.5}>
                <FormLabel sx={{ fontSize: 12, fontWeight: 600 }}>
                  Corrected base position — ECEF X, Y, Z{" "}
                  <Typography component="span" variant="caption" sx={{ color: "text.disabled", fontWeight: 400 }}>
                    optional; metres, from a Point ID,X,Y,Z csv or typed
                  </Typography>
                </FormLabel>
                <Box
                  role="button"
                  tabIndex={0}
                  onClick={() => ecefFileRef.current?.click()}
                  sx={{
                    border: "1.5px dashed",
                    borderColor: ecefOver ? "primary.main" : "divider",
                    borderRadius: 1,
                    px: 1.5,
                    py: 1.75,
                    textAlign: "center",
                    cursor: "pointer",
                    bgcolor: ecefOver ? "action.hover" : "background.paper",
                    "&:hover": { borderColor: "primary.main" },
                  }}
                >
                  <Stack spacing={1} sx={{ alignItems: "center" }}>
                    <Typography variant="body2" sx={{ color: "text.disabled" }}>
                      {ecefBusy
                        ? "Parsing ECEF csv…"
                        : "Drop a Point ID,X,Y,Z csv here or click to browse"}
                    </Typography>
                    <Button
                      size="small"
                      variant="outlined"
                      onClick={(e) => {
                        e.stopPropagation();
                        setEcefManual(true);
                      }}
                    >
                      Type it manually
                    </Button>
                  </Stack>
                </Box>
                {ecefErr && <FormHelperText error>{ecefErr}</FormHelperText>}
              </Stack>
            )}
          </Box>
        </fieldset>

        <fieldset>
          <legend>4 · Processing</legend>
          <Stack spacing={0}>
            <FormControlLabel
              control={
                <Switch
                  size="small"
                  checked={form.run_photo_chain}
                  onChange={set("run_photo_chain")}
                />
              }
              label={
                <span>
                  Photo chain <span className="why">Terra PPK → Pix4Dmatic</span>
                </span>
              }
            />
            <Tooltip title={isLidar ? "" : "LiDAR chain requires an L2/L3 sensor"} placement="bottom-start">
              <span>
                <FormControlLabel
                  control={
                    <Switch
                      size="small"
                      disabled={!isLidar}
                      checked={isLidar && form.run_lidar_chain}
                      onChange={set("run_lidar_chain")}
                    />
                  }
                  label={
                    <span>
                      LiDAR chain{" "}
                      <span className="why">
                        Terra reconstruction → Cyclone 3DR classification (L2/L3 only)
                      </span>
                    </span>
                  }
                />
              </span>
            </Tooltip>
          </Stack>

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
              <Alert
                severity={tltInfo.error ? "error" : "info"}
                icon={tltInfo.busy ? <CircularProgress size={18} /> : undefined}
                sx={{ py: 0, mt: 0.5 }}
              >
                {tltInfo.busy && "Reading targets csv…"}
                {tltInfo.error && `Targets csv: ${tltInfo.error}`}
                {tltInfo.tlt_count != null &&
                  `${tltInfo.total_rows} point${tltInfo.total_rows === 1 ? "" : "s"}: ` +
                    `${tltInfo.tlt_count} TLT → SINGLE_TLT.csv (LiDAR), ` +
                    `${tltInfo.tat_count} TAT+TLT → TAT.csv (Pix4D). ` +
                    "Both saved to the project folder at intake."}
              </Alert>
            )}
          </div>

          <Grid container spacing={1.5}>
            <Grid size={{ xs: 12, sm: 6 }}>
              <TextField
                fullWidth
                size="small"
                label="EPSG horizontal"
                helperText={epsgName(form.epsg_h) || " "}
                value={form.epsg_h}
                onChange={set("epsg_h")}
                slotProps={{
                  input: {
                    readOnly: !!locked.epsg_h,
                    endAdornment: locked.epsg_h && (
                      <UnlockAdornment onClick={() => unlock("epsg_h")} />
                    ),
                  },
                }}
              />
            </Grid>
            <Grid size={{ xs: 12, sm: 6 }}>
              <TextField
                fullWidth
                size="small"
                label="EPSG vertical"
                helperText={epsgName(form.epsg_v) || " "}
                value={form.epsg_v}
                onChange={set("epsg_v")}
                slotProps={{
                  input: {
                    readOnly: !!locked.epsg_v,
                    endAdornment: locked.epsg_v && (
                      <UnlockAdornment onClick={() => unlock("epsg_v")} />
                    ),
                  },
                }}
              />
            </Grid>
          </Grid>

          {isLidar && form.run_lidar_chain && (
            <Grid container spacing={1.5}>
              <Grid size={{ xs: 12, sm: 6 }}>
                <Autocomplete
                  freeSolo
                  size="small"
                  options={models}
                  inputValue={form.classify_model}
                  onInputChange={(e, v) => setForm((f) => ({ ...f, classify_model: v }))}
                  renderInput={(params) => (
                    <TextField
                      {...params}
                      label="3DR classification model"
                      helperText="blank = skip classification (the default)"
                    />
                  )}
                />
              </Grid>
              <Grid size={{ xs: 12, sm: 6 }} sx={{ alignSelf: "center" }}>
                <FormControlLabel
                  control={
                    <Checkbox
                      size="small"
                      checked={form.no_targets}
                      onChange={set("no_targets")}
                    />
                  }
                  label="No targets"
                />
              </Grid>
            </Grid>
          )}
        </fieldset>

        <ErrorBanner error={error} prefix="Submission rejected" />
        <div>
          <Button variant="contained" size="large" type="submit" loading={busy}>
            Queue it
          </Button>
        </div>
      </form>
    </section>
  );
}
