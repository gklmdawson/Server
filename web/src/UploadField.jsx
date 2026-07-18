import { useRef, useState } from "react";

// Drag-and-drop (or click) upload for the SMALL intake inputs — base data and
// the targets csv — which the operator has locally. Unlike the path fields
// (large data stays on the share, addressed by path), these upload the file
// CONTENTS to the coordinator, which stores them on the NAS uploads volume and
// returns the path the INTAKE_COPY worker reads. Bulk imagery is never uploaded.

function matchesExt(name, accept) {
  if (!accept || accept.length === 0) return true;
  const lower = name.toLowerCase();
  return accept.some((e) => lower.endsWith(e.toLowerCase()));
}

function formatSize(bytes) {
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

// items: [{ name, size, stored_path, error }]; uploader: async (File) => item.
export function UploadField({
  label,
  hint,
  accept,
  multiple = false,
  uploader,
  items,
  onItems,
  required,
}) {
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [reject, setReject] = useState(null);
  const inputRef = useRef(null);

  const take = async (fileList) => {
    const files = [...fileList];
    if (!files.length) return;
    setReject(null);
    const bad = files.find((f) => !matchesExt(f.name, accept));
    if (bad) {
      setReject(`${bad.name} is not ${(accept || []).join(" / ")}`);
      return;
    }
    setBusy(true);
    const next = multiple ? [...items] : [];
    for (const file of multiple ? files : [files[0]]) {
      try {
        const stored = await uploader(file);
        next.push({ name: stored.name, size: stored.size, stored_path: stored.stored_path });
      } catch (err) {
        next.push({ name: file.name, error: err.message || "upload failed" });
      }
    }
    onItems(next);
    setBusy(false);
  };

  const onDrop = (e) => {
    e.preventDefault();
    setOver(false);
    if (e.dataTransfer.files?.length) take(e.dataTransfer.files);
  };

  const remove = (i) => onItems(items.filter((_, idx) => idx !== i));

  return (
    <div className="field">
      <label>
        {label} {hint && <span className="hint">{hint}</span>}
      </label>
      <div
        className={`upload-drop ${over ? "drag-over" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
        onClick={() => inputRef.current?.click()}
        role="button"
        tabIndex={0}
      >
        <input
          ref={inputRef}
          type="file"
          multiple={multiple}
          accept={(accept || []).join(",")}
          style={{ display: "none" }}
          onChange={(e) => {
            take(e.target.files);
            e.target.value = "";
          }}
        />
        <span className="upload-hint">
          {busy ? "Uploading…" : `Drop ${multiple ? "file(s)" : "a file"} here or click to browse`}
        </span>
      </div>
      {reject && <div className="drop-note">{reject}</div>}
      {items.length > 0 && (
        <ul className="upload-list">
          {items.map((it, i) => (
            <li key={i} className={it.error ? "upload-item err" : "upload-item"}>
              <span className="upload-name">📄 {it.name}</span>
              {it.error ? (
                <span className="hint"> {it.error}</span>
              ) : (
                <span className="hint"> {formatSize(it.size || 0)}</span>
              )}
              <button type="button" className="btn small" onClick={() => remove(i)}>
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
      {required && items.length === 0 && (
        <input
          tabIndex={-1}
          aria-hidden="true"
          required
          value=""
          onChange={() => {}}
          style={{ opacity: 0, height: 0, width: 0, position: "absolute" }}
        />
      )}
    </div>
  );
}
