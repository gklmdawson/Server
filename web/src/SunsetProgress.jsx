// The Submit tab's sky. Rendered as a fixed, full-viewport background layer
// behind the whole page (no white margin anywhere), with the form floating on
// top as a centered column. The sun rises at the bottom-left when nothing is
// filled, arcs over the top around the midpoint, and sets at the bottom-right
// when every required field is in — driven by --p (fraction filled) and --arc
// (its parabolic height), set here from the form's completion. Colors are the
// Sunrise brand tokens (Cerulean day, Cedar/Yellow dawn & dusk); swap the
// gradients for a photo layer if a real image is ever preferred. Decorative —
// the completion status is conveyed in text beside the heading.

export function SunsetProgress({ p, arc }) {
  return (
    <div className="submit-sky" style={{ "--p": p, "--arc": arc }} aria-hidden="true">
      <div className="day" />
      <div className="sun" />
      <div className="ground" />
      <div className="sunset-track">
        <span />
      </div>
    </div>
  );
}
