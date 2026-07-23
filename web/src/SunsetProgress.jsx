// A day's sun arc that tracks how complete the flight entry is: the sun rises
// at the bottom-left when nothing is filled, arcs up to the top around the
// halfway mark, and sets at the bottom-right when every required field is in.
// Two values drive it: --p (0..1, fraction filled) moves the sun horizontally
// and the progress bar; --arc (its parabolic height, peaking at the midpoint)
// moves it vertically and fades the bright daytime sky in over the warm
// dawn/dusk base. Colors are the Sunrise brand tokens (Navy/Cerulean for day,
// Cedar/Yellow for the horizons) — no image asset, so it themes and animates
// cheaply; swap the `.day`/base gradients for a photo layer if ever preferred.

export function SunsetProgress({ filled, total }) {
  const p = total ? Math.max(0, Math.min(1, filled / total)) : 0;
  const arc = 4 * p * (1 - p); // 0 at the horizons, 1 at the top of the sky
  const done = filled >= total && total > 0;
  const remaining = Math.max(0, total - filled);

  let lead = "Getting your flight ready";
  if (filled === 0) lead = "Sunrise — start with your flight folder";
  else if (done) lead = "The sun's set — ready to queue";

  const sub = done
    ? `all ${total} required details in`
    : `${filled} of ${total} required details in, ${remaining} to go`;

  return (
    <div
      className="sunset"
      style={{ "--p": p, "--arc": arc }}
      role="img"
      aria-label={`${filled} of ${total} required fields complete`}
    >
      <div className="day" />
      <div className="sun" />
      <div className="ground" />
      <div className="caption">
        <span className="lead">{lead}</span>
        <span className="sub">{sub}</span>
      </div>
      <div className="track">
        <span />
      </div>
    </div>
  );
}
