// The Submit page's sky. Rendered as an absolute background layer filling the
// whole card, with the form content floating above on frosted panels. The sun
// rises at the bottom-left when nothing is filled, arcs over the top around the
// midpoint, and sets at the bottom-right when every required field is in —
// driven by --p (fraction filled) and --arc (its parabolic height), both set on
// the .submit-card by Submit.jsx and inherited here. Colors are the Sunrise
// brand tokens (Cerulean day, Cedar/Yellow dawn & dusk); swap the gradients for
// a photo layer if a real image is ever preferred. Decorative — the completion
// status is conveyed in text by the caption beside the heading.

export function SunsetProgress() {
  return (
    <>
      <div className="sunset-bg" aria-hidden="true">
        <div className="day" />
        <div className="sun" />
        <div className="ground" />
      </div>
      <div className="sunset-track" aria-hidden="true">
        <span />
      </div>
    </>
  );
}
