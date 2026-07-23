import { useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Breadcrumbs from "@mui/material/Breadcrumbs";
import Button from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Link from "@mui/material/Link";
import List from "@mui/material/List";
import ListItemButton from "@mui/material/ListItemButton";
import ListItemIcon from "@mui/material/ListItemIcon";
import ListItemText from "@mui/material/ListItemText";
import Skeleton from "@mui/material/Skeleton";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";
import Typography from "@mui/material/Typography";
import ArrowUpwardIcon from "@mui/icons-material/ArrowUpward";
import EjectIcon from "@mui/icons-material/Eject";
import FolderIcon from "@mui/icons-material/Folder";
import InsertDriveFileIcon from "@mui/icons-material/InsertDriveFile";
import RefreshIcon from "@mui/icons-material/Refresh";
import { api } from "./api.js";
import { useContainerRestart } from "./useContainerRestart.js";

// Path fields with a server-side file explorer behind them. The browser can
// never see WHERE a locally dropped file lives (only its name/bytes), so
// picking real NAS paths goes through the coordinator's /browse API — it
// lists the share it can see and hands back the UNC form agents use.
// Dropping/pasting path TEXT (including Explorer's quoted "Copy as path"
// strings and file:// URIs) is normalized straight into the field.

export function normalizePath(raw) {
  let s = (raw || "").trim();
  if (s.length >= 2 &&
      ((s.startsWith('"') && s.endsWith('"')) ||
       (s.startsWith("'") && s.endsWith("'")))) {
    s = s.slice(1, -1).trim();
  }
  if (/^file:\/\//i.test(s)) {
    let rest = decodeURIComponent(s.replace(/^file:\/\//i, ""));
    if (/^\/[A-Za-z]:/.test(rest)) rest = rest.slice(1);      // /D:/x -> D:/x
    else if (rest && !rest.startsWith("/")) rest = "\\\\" + rest; // host/share -> UNC
    s = rest.replaceAll("/", "\\");
  }
  return s;
}

function normalizeLines(text) {
  return text
    .split(/\r?\n/)
    .map(normalizePath)
    .filter((line, i, arr) => line || i === arr.length - 1)
    .join("\n");
}

function droppedText(e) {
  const dt = e.dataTransfer;
  const uris = dt.getData("text/uri-list");
  const text = uris || dt.getData("text/plain") || dt.getData("text") || "";
  return text
    .split(/\r?\n/)
    .filter((l) => l.trim() && !l.startsWith("#"))
    .map(normalizePath)
    .join("\n");
}

function useDropProps(applyText, setNote) {
  const [over, setOver] = useState(false);
  return {
    over,
    props: {
      onDragOver: (e) => {
        e.preventDefault();
        setOver(true);
      },
      onDragLeave: () => setOver(false),
      onDrop: (e) => {
        e.preventDefault();
        setOver(false);
        const text = droppedText(e);
        if (text) {
          setNote(null);
          applyText(text);
        } else if (e.dataTransfer.files?.length) {
          setNote(
            "Browsers hide where dropped files live — use Browse to pick them on the share."
          );
        }
      },
    },
  };
}

function formatSize(bytes) {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

function matchesExt(name, exts) {
  if (!exts || exts.length === 0) return true;
  const lower = name.toLowerCase();
  return exts.some((e) => lower.endsWith(e.toLowerCase()));
}

// mode: "file" | "folder"; multi: allow picking several at once.
// onPickMeta(rootLabel, [relPaths]) fires alongside onPick so callers can, e.g.,
// probe a picked source folder (which needs the root + relative path, not the
// display UNC path onPick hands back).
export function FilePicker({ roots, mode, exts, multi, title, onPick, onPickMeta, onClose }) {
  const [rootLabel, setRootLabel] = useState(roots.length === 1 ? roots[0].label : "");
  const [path, setPath] = useState("");
  const [listing, setListing] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(() => new Set());
  const [reloadKey, setReloadKey] = useState(0);
  const [ejecting, setEjecting] = useState("");   // device name in flight
  const [ejectMsg, setEjectMsg] = useState(null); // {ok, text}
  const {
    restarting,
    message: restartMsg,
    restart,
  } = useContainerRestart(() => setReloadKey((k) => k + 1));

  const rootIsEjectable = !!roots.find((r) => r.label === rootLabel)?.ejectable;
  const rootIsRestartable = !!roots.find((r) => r.label === rootLabel)?.restartable;

  useEffect(() => {
    if (!rootLabel) return;
    let stale = false;
    setError(null);
    setListing(null);
    api
      .browse(rootLabel, path)
      .then((data) => !stale && setListing(data))
      .catch((err) => !stale && setError(err));
    setSelected(new Set());
    return () => {
      stale = true;
    };
  }, [rootLabel, path, reloadKey]);

  const ejectDevice = async (device) => {
    setEjecting(device);
    setEjectMsg(null);
    try {
      const r = await api.eject(rootLabel, device);
      setEjectMsg({ ok: true, text: r.message || `${device} ejected — safe to remove.` });
      setReloadKey((k) => k + 1); // the device drops off once unmounted
    } catch (err) {
      setEjectMsg({ ok: false, text: err.message || "eject failed" });
    } finally {
      setEjecting("");
    }
  };

  const enter = (name) => setPath(path ? `${path}/${name}` : name);

  const toggle = (name) => {
    setSelected((prev) => {
      const next = new Set(multi ? prev : []);
      if (prev.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const joined = (name) => `${listing.display_path}${listing.sep}${name}`;

  const confirmSelected = () => {
    const names = [...selected];
    onPick(names.map(joined));
    onPickMeta?.(rootLabel, names.map((name) => (path ? `${path}/${name}` : name)));
    onClose();
  };

  const chooseCurrent = () => {
    onPick([listing.display_path]);
    onPickMeta?.(rootLabel, [path]);
    onClose();
  };

  const crumbs = path ? path.split("/") : [];
  const selectable = (entry) => (mode === "folder" ? entry.dir : !entry.dir);
  const visible = (listing?.entries || []).filter(
    (e) => e.dir || (mode === "file" && matchesExt(e.name, exts))
  );
  // Empty media root: the Rescan button moves into the empty state (the
  // stuck-user moment); the breadcrumb copy only shows alongside entries
  // (the "second card isn't appearing" case) so it's never duplicated.
  const topLevelEmpty = !!listing && visible.length === 0 && listing.parent === null;

  const rescanButton = (
    <Button
      size="small"
      variant="outlined"
      startIcon={<RefreshIcon />}
      loading={restarting}
      loadingPosition="start"
      disabled={!!ejecting}
      title="Restart the NAS containers so a freshly plugged card shows up"
      onClick={restart}
    >
      {restarting ? "Restarting… (waiting for the server)" : "Rescan cards"}
    </Button>
  );

  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="sm">
      <DialogTitle sx={{ pb: 1 }}>
        {title}
        {exts?.length > 0 && mode === "file" && (
          <Typography component="span" variant="body2" sx={{ color: "text.disabled", ml: 1 }}>
            {exts.join(" / ")} only
          </Typography>
        )}
      </DialogTitle>
      <DialogContent sx={{ pb: 1 }}>
        <Stack spacing={1}>
          {roots.length > 1 && (
            <ToggleButtonGroup
              exclusive
              size="small"
              color="primary"
              value={rootLabel}
              onChange={(e, v) => {
                if (v == null) return;
                setRootLabel(v);
                setPath("");
              }}
            >
              {roots.map((r) => (
                <ToggleButton key={r.label} value={r.label}>
                  {r.label}
                </ToggleButton>
              ))}
            </ToggleButtonGroup>
          )}

          {rootLabel && (
            <Stack direction="row" spacing={1} sx={{ alignItems: "center" }}>
              <Breadcrumbs sx={{ flex: 1, minWidth: 0 }} itemsAfterCollapse={2}>
                <Link component="button" type="button" underline="hover" onClick={() => setPath("")}>
                  {roots.find((r) => r.label === rootLabel)?.label || rootLabel}
                </Link>
                {crumbs.map((part, i) =>
                  i === crumbs.length - 1 ? (
                    <Typography key={i} sx={{ color: "text.primary" }}>{part}</Typography>
                  ) : (
                    <Link
                      key={i}
                      component="button"
                      type="button"
                      underline="hover"
                      onClick={() => setPath(crumbs.slice(0, i + 1).join("/"))}
                    >
                      {part}
                    </Link>
                  )
                )}
              </Breadcrumbs>
              {rootIsRestartable && !topLevelEmpty && rescanButton}
            </Stack>
          )}

          {ejectMsg && (
            <Alert severity={ejectMsg.ok ? "success" : "error"}>{ejectMsg.text}</Alert>
          )}
          {restartMsg && (
            <Alert severity={restartMsg.ok ? "success" : "error"}>{restartMsg.text}</Alert>
          )}

          {roots.length === 0 && (
            <Alert severity="info">
              No browse locations are configured on the coordinator, so there's
              nothing to list here. Paste or type the folder path into the field
              instead — or add <code>browse_roots</code> to the coordinator config
              (and set the admin token via ⚙ in the header if it requires one) to
              turn Browse on.
            </Alert>
          )}

          {roots.length > 0 && (
          <Box
            sx={{
              border: 1,
              borderColor: "divider",
              borderRadius: 1,
              maxHeight: "46vh",
              overflowY: "auto",
            }}
          >
            {!rootLabel && <div className="empty" style={{ padding: 10 }}>Pick a location above.</div>}
            {error && <Alert severity="error">{error.message}</Alert>}
            {rootLabel && !listing && !error && (
              <Box sx={{ px: 1.5, py: 1 }}>
                <Skeleton height={28} />
                <Skeleton height={28} width="80%" />
                <Skeleton height={28} width="60%" />
              </Box>
            )}
            <List dense disablePadding>
              {listing && listing.parent !== null && (
                <ListItemButton divider onClick={() => setPath(listing.parent)}>
                  <ListItemIcon sx={{ minWidth: 34 }}>
                    <ArrowUpwardIcon fontSize="small" />
                  </ListItemIcon>
                  <ListItemText primary=".." />
                </ListItemButton>
              )}
              {listing &&
                visible.map((entry) => (
                  <ListItemButton
                    key={entry.name}
                    divider
                    selected={selected.has(entry.name)}
                    onClick={() =>
                      selectable(entry) ? toggle(entry.name) : entry.dir && enter(entry.name)
                    }
                  >
                    {selectable(entry) && (
                      <Checkbox
                        edge="start"
                        size="small"
                        checked={selected.has(entry.name)}
                        tabIndex={-1}
                        sx={{ py: 0, mr: 0.5 }}
                        onChange={() => toggle(entry.name)}
                        onClick={(e) => e.stopPropagation()}
                      />
                    )}
                    <ListItemIcon sx={{ minWidth: 34 }}>
                      {entry.dir ? (
                        <FolderIcon fontSize="small" color="primary" />
                      ) : (
                        <InsertDriveFileIcon fontSize="small" sx={{ color: "text.disabled" }} />
                      )}
                    </ListItemIcon>
                    <ListItemText
                      primary={entry.name}
                      slotProps={{ primary: { noWrap: true } }}
                    />
                    {rootIsEjectable && path === "" && entry.dir && (
                      <Button
                        size="small"
                        variant="outlined"
                        startIcon={<EjectIcon />}
                        loading={ejecting === entry.name}
                        loadingPosition="start"
                        disabled={!!ejecting && ejecting !== entry.name}
                        title="Safely unmount this card on the NAS"
                        sx={{ ml: 1, flexShrink: 0 }}
                        onClick={(e) => {
                          e.stopPropagation();
                          ejectDevice(entry.name);
                        }}
                      >
                        {ejecting === entry.name ? "Ejecting…" : "Eject"}
                      </Button>
                    )}
                    {entry.dir ? (
                      <Button
                        size="small"
                        sx={{ ml: 1, flexShrink: 0 }}
                        onClick={(e) => {
                          e.stopPropagation();
                          enter(entry.name);
                        }}
                      >
                        Open
                      </Button>
                    ) : (
                      <Typography variant="caption" sx={{ color: "text.disabled", ml: 1, flexShrink: 0 }}>
                        {formatSize(entry.size)}
                      </Typography>
                    )}
                  </ListItemButton>
                ))}
            </List>
            {topLevelEmpty && (
              <Box sx={{ p: 2, textAlign: "center" }}>
                {rootIsRestartable ? (
                  <Stack spacing={1.5} sx={{ alignItems: "center" }}>
                    <div className="empty">
                      No card mounted. Plug the card into the NAS — if it still doesn't show:
                    </div>
                    {rescanButton}
                  </Stack>
                ) : (
                  <div className="empty">Nothing here.</div>
                )}
              </Box>
            )}
          </Box>
          )}
          {listing?.truncated && (
            <Alert severity="warning">Folder too large — showing the first entries only.</Alert>
          )}
        </Stack>
      </DialogContent>
      <DialogActions>
        {mode === "folder" && listing && (
          <Button variant="outlined" sx={{ mr: "auto" }} onClick={chooseCurrent}>
            Use this folder
          </Button>
        )}
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="contained" disabled={selected.size === 0} onClick={confirmSelected}>
          {multi ? `Add selected (${selected.size})` : "Choose"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

// `hero` renders the Sunrise Yellow-on-Navy CTA variant (theme warning
// color, matching the topbar strip) for the one field a form wants the
// user to start from. Always rendered — even when no browse roots are
// configured — so the primary "pick a folder" action never silently
// vanishes; the picker itself explains when there's nothing to browse.
function BrowseButton({ onClick, hero, label }) {
  return (
    <Button
      variant={hero ? "contained" : "outlined"}
      color={hero ? "warning" : "primary"}
      size={hero ? "medium" : "small"}
      sx={{ flexShrink: 0, mt: hero ? 0 : 0.25, whiteSpace: "nowrap" }}
      onClick={onClick}
    >
      {label || "Browse…"}
    </Button>
  );
}

// Shared visuals for the drop-target state and the "browsers hide paths" note.
const dragOverSx = (over) =>
  over
    ? {
        "& .MuiOutlinedInput-notchedOutline": {
          borderStyle: "dashed",
          borderWidth: 2,
          borderColor: "primary.main",
        },
      }
    : {};

function DropNote({ note }) {
  if (!note) return null;
  return (
    <Typography variant="caption" sx={{ color: "error.main" }}>
      {note}
    </Typography>
  );
}

// Single-path input (root_path, gcp_path).
export function PathInput({ label, hint, value, onChange, required, roots, mode, exts, pickerTitle }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState(null);
  const { over, props } = useDropProps((text) => onChange(text.split(/\r?\n/)[0] || ""), setNote);

  return (
    <Box {...props}>
      <Stack direction="row" spacing={1} sx={{ alignItems: "flex-start" }}>
        <TextField
          fullWidth
          size="small"
          label={label}
          helperText={note ? <DropNote note={note} /> : hint}
          required={required}
          value={value}
          sx={dragOverSx(over)}
          onChange={(e) => onChange(e.target.value)}
          onBlur={() => onChange(normalizePath(value))}
        />
        <BrowseButton onClick={() => setOpen(true)} />
      </Stack>
      {open && (
        <FilePicker
          roots={roots}
          mode={mode}
          exts={exts}
          multi={false}
          title={pickerTitle || label}
          onPick={(paths) => onChange(paths[0] || "")}
          onClose={() => setOpen(false)}
        />
      )}
    </Box>
  );
}

// Multi-path field, one path per line (source folders, base data files).
// TextField multiline grows with its content natively, so every added path
// stays visible without scrolling.
export function PathLines({ label, hint, value, onChange, required, roots, mode, exts, pickerTitle, onPickMeta, browseHero, browseLabel, readOnly }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState(null);
  const append = (text) => {
    const existing = value.trim();
    onChange(existing ? `${existing}\n${text}` : text);
  };
  const { over, props } = useDropProps(append, setNote);

  return (
    <Box {...props}>
      <Stack direction="row" spacing={1} sx={{ alignItems: "flex-start" }}>
        <TextField
          fullWidth
          size="small"
          multiline
          minRows={1}
          label={label}
          helperText={note ? <DropNote note={note} /> : hint}
          required={required}
          value={value}
          sx={dragOverSx(over)}
          slotProps={readOnly ? { input: { readOnly: true } } : undefined}
          onChange={(e) => onChange(e.target.value)}
          onBlur={() => (readOnly ? undefined : onChange(normalizeLines(value)))}
        />
        <BrowseButton
          onClick={() => setOpen(true)}
          hero={browseHero}
          label={browseLabel}
        />
      </Stack>
      {open && (
        <FilePicker
          roots={roots}
          mode={mode}
          exts={exts}
          multi={true}
          title={pickerTitle || label}
          onPick={(paths) => append(paths.join("\n"))}
          onPickMeta={onPickMeta}
          onClose={() => setOpen(false)}
        />
      )}
    </Box>
  );
}
