import { useRef, useState } from "react";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import FormHelperText from "@mui/material/FormHelperText";
import FormLabel from "@mui/material/FormLabel";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import ErrorOutlineIcon from "@mui/icons-material/ErrorOutlined";
import InsertDriveFileIcon from "@mui/icons-material/InsertDriveFile";
import UploadFileIcon from "@mui/icons-material/UploadFile";

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

// The dropzone: a dashed Paper-style target that lights up in the accent
// color while a file is dragged over it.
const dropSx = (over) => ({
  border: "1.5px dashed",
  borderColor: over ? "primary.main" : "divider",
  borderRadius: 1,
  px: 1.5,
  py: 1.75,
  textAlign: "center",
  cursor: "pointer",
  bgcolor: over ? "action.hover" : "background.paper",
  transition: "border-color 0.12s, background-color 0.12s",
  "&:hover": { borderColor: "primary.main" },
});

// items: [{ name, size, stored_path, error }]; uploader: async (File) => item.
// itemNote: optional (item) => string, shown as an extra tag per uploaded file
// (e.g. the detected Trimble/RINEX type of a base observation).
export function UploadField({
  label,
  hint,
  accept,
  multiple = false,
  uploader,
  items,
  onItems,
  required,
  itemNote,
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
    <Stack spacing={0.5}>
      <FormLabel required={required} sx={{ fontSize: 12, fontWeight: 600 }}>
        {label}
        {hint && (
          <Typography component="span" variant="caption" sx={{ color: "text.disabled", ml: 0.5, fontWeight: 400 }}>
            {hint}
          </Typography>
        )}
      </FormLabel>
      <Box
        sx={dropSx(over)}
        role="button"
        tabIndex={0}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
        onClick={() => inputRef.current?.click()}
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
        <Stack direction="row" spacing={1} sx={{ alignItems: "center", justifyContent: "center" }}>
          {busy ? (
            <CircularProgress size={16} />
          ) : (
            <UploadFileIcon fontSize="small" sx={{ color: "text.disabled" }} />
          )}
          <Typography variant="body2" sx={{ color: "text.disabled" }}>
            {busy
              ? "Uploading…"
              : `Drop ${multiple ? "file(s)" : "a file"} here or click to browse`}
          </Typography>
        </Stack>
      </Box>
      {reject && <FormHelperText error>{reject}</FormHelperText>}
      {items.length > 0 && (
        <Stack direction="row" sx={{ flexWrap: "wrap", gap: 0.75, pt: 0.5 }}>
          {items.map((it, i) =>
            it.error ? (
              <Tooltip key={i} title={it.error}>
                <Chip
                  size="small"
                  color="error"
                  variant="outlined"
                  icon={<ErrorOutlineIcon />}
                  label={`${it.name} — ${it.error}`}
                  onDelete={() => remove(i)}
                />
              </Tooltip>
            ) : (
              <Chip
                key={i}
                size="small"
                variant="outlined"
                icon={<InsertDriveFileIcon />}
                label={`${it.name} · ${formatSize(it.size || 0)}${itemNote ? ` · ${itemNote(it)}` : ""}`}
                onDelete={() => remove(i)}
              />
            )
          )}
        </Stack>
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
    </Stack>
  );
}
