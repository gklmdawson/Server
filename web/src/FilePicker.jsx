import { useEffect, useRef, useState } from "react";
import { api } from "./api.js";

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
export function FilePicker({ roots, mode, exts, multi, title, onPick, onClose }) {
  const [rootLabel, setRootLabel] = useState(roots.length === 1 ? roots[0].label : "");
  const [path, setPath] = useState("");
  const [listing, setListing] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(() => new Set());
  const dialogRef = useRef(null);

  useEffect(() => {
    dialogRef.current?.showModal();
  }, []);

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
  }, [rootLabel, path]);

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
    onPick([...selected].map(joined));
    onClose();
  };

  const chooseCurrent = () => {
    onPick([listing.display_path]);
    onClose();
  };

  const crumbs = path ? path.split("/") : [];
  const selectable = (entry) => (mode === "folder" ? entry.dir : !entry.dir);
  const visible = (listing?.entries || []).filter(
    (e) => e.dir || (mode === "file" && matchesExt(e.name, exts))
  );

  return (
    <dialog className="picker" ref={dialogRef} onClose={onClose}>
      <header>
        <b>{title}</b>
        {exts?.length > 0 && mode === "file" && (
          <span className="hint"> {exts.join(" / ")} only</span>
        )}
        <span style={{ flex: 1 }} />
        <button type="button" className="btn small" onClick={onClose}>
          Close
        </button>
      </header>

      {roots.length > 1 && (
        <div className="picker-roots">
          {roots.map((r) => (
            <button
              key={r.label}
              type="button"
              className={`btn small ${r.label === rootLabel ? "primary" : ""}`}
              onClick={() => {
                setRootLabel(r.label);
                setPath("");
              }}
            >
              {r.label}
            </button>
          ))}
        </div>
      )}

      {rootLabel && (
        <div className="picker-crumbs">
          <button type="button" onClick={() => setPath("")}>
            {roots.find((r) => r.label === rootLabel)?.label || rootLabel}
          </button>
          {crumbs.map((part, i) => (
            <button
              key={i}
              type="button"
              onClick={() => setPath(crumbs.slice(0, i + 1).join("/"))}
            >
              {part}
            </button>
          ))}
        </div>
      )}

      <div className="picker-list">
        {!rootLabel && <div className="empty">Pick a location above.</div>}
        {error && <div className="banner error">{error.message}</div>}
        {rootLabel && !listing && !error && <div className="empty">Loading…</div>}
        {listing && listing.parent !== null && (
          <div className="picker-row" onClick={() => setPath(listing.parent)}>
            <span className="picker-icon">↩</span>
            <span>..</span>
          </div>
        )}
        {listing &&
          visible.map((entry) => (
            <div
              key={entry.name}
              className={`picker-row ${selected.has(entry.name) ? "selected" : ""}`}
              onClick={() =>
                selectable(entry) ? toggle(entry.name) : entry.dir && enter(entry.name)
              }
            >
              {selectable(entry) && (
                <input
                  type="checkbox"
                  checked={selected.has(entry.name)}
                  onChange={() => toggle(entry.name)}
                  onClick={(e) => e.stopPropagation()}
                />
              )}
              <span className="picker-icon">{entry.dir ? "📁" : "📄"}</span>
              <span className="picker-name">{entry.name}</span>
              {entry.dir ? (
                <button
                  type="button"
                  className="btn small"
                  onClick={(e) => {
                    e.stopPropagation();
                    enter(entry.name);
                  }}
                >
                  Open
                </button>
              ) : (
                <span className="hint">{formatSize(entry.size)}</span>
              )}
            </div>
          ))}
        {listing && visible.length === 0 && listing.parent === null && (
          <div className="empty">Nothing here.</div>
        )}
        {listing?.truncated && (
          <div className="banner error">Folder too large — showing the first entries only.</div>
        )}
      </div>

      <footer className="actions">
        {mode === "folder" && listing && (
          <button type="button" className="btn" onClick={chooseCurrent}>
            Use this folder
          </button>
        )}
        <span style={{ flex: 1 }} />
        <button type="button" className="btn" onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          className="btn primary"
          disabled={selected.size === 0}
          onClick={confirmSelected}
        >
          {multi ? `Add selected (${selected.size})` : "Choose"}
        </button>
      </footer>
    </dialog>
  );
}

function BrowseButton({ show, onClick }) {
  if (!show) return null;
  return (
    <button type="button" className="btn small browse" onClick={onClick}>
      Browse…
    </button>
  );
}

function DropNote({ note }) {
  if (!note) return null;
  return <div className="drop-note">{note}</div>;
}

// Single-path input (root_path, gcp_path).
export function PathInput({ label, hint, value, onChange, required, roots, mode, exts, pickerTitle }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState(null);
  const { over, props } = useDropProps((text) => onChange(text.split(/\r?\n/)[0] || ""), setNote);

  return (
    <div className="field">
      <label>
        {label} {hint && <span className="hint">{hint}</span>}
      </label>
      <div className={`path-input ${over ? "drag-over" : ""}`} {...props}>
        <input
          type="text"
          required={required}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onBlur={() => onChange(normalizePath(value))}
        />
        <BrowseButton show={roots.length > 0} onClick={() => setOpen(true)} />
      </div>
      <DropNote note={note} />
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
    </div>
  );
}

// Multi-path textarea, one path per line (source folders, base data files).
export function PathLines({ label, hint, value, onChange, required, roots, mode, exts, pickerTitle }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState(null);
  const append = (text) => {
    const existing = value.trim();
    onChange(existing ? `${existing}\n${text}` : text);
  };
  const { over, props } = useDropProps(append, setNote);

  return (
    <div className="field">
      <label>
        {label} {hint && <span className="hint">{hint}</span>}
      </label>
      <div className={`path-input ${over ? "drag-over" : ""}`} {...props}>
        <textarea
          required={required}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onBlur={() => onChange(normalizeLines(value))}
        />
        <BrowseButton show={roots.length > 0} onClick={() => setOpen(true)} />
      </div>
      <DropNote note={note} />
      {open && (
        <FilePicker
          roots={roots}
          mode={mode}
          exts={exts}
          multi={true}
          title={pickerTitle || label}
          onPick={(paths) => append(paths.join("\n"))}
          onClose={() => setOpen(false)}
        />
      )}
    </div>
  );
}
