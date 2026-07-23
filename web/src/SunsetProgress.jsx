// A sunset that sets as the flight entry is completed. The sun's height and the
// sky's warmth are driven purely by how many required fields are filled
// (0 -> dusk, all -> sun on the horizon), so each field the operator completes
// animates the sun a step lower via CSS transitions. The scene is drawn from
// the Sunrise brand tokens (Navy -> Cedar -> Yellow) — no image asset, so it
// stays self-contained and themes/animates cheaply. Swap the `.warm` gradient
// for a background-image if a real photo is ever preferred.

export function SunsetProgress({ filled, total }) {
  const p = total ? Math.max(0, Math.min(1, filled / total)) : 0;
  const done = filled >= total && total > 0;
  const remaining = Math.max(0, total - filled);
  const label = done
    ? `All ${total} required details in — ready to queue.`
    : `${filled} of ${total} required details in, ${remaining} to go.`;

  return (
    <div className="sunset" style={{ "--p": p }} role="img" aria-label={label}>
      <div className="warm" />
      <div className="sun" />
      <div className="ground" />
      <div className="caption">
        <span className="lead">
          {done ? "The sun's down — ready to queue" : "Getting your flight ready"}
        </span>
        <span className="sub">{label}</span>
      </div>
      <div className="track">
        <span />
      </div>
    </div>
  );
}
