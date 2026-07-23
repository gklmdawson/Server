import sunLogo from "./assets/sun-logo.png";

// The Submit tab's sky. Rendered as a fixed, full-viewport background layer
// behind the whole page (no white margin anywhere), with the form floating on
// top as a centered column. The sun (the company logo) rises at the bottom-left
// when nothing is filled, arcs over the top around the midpoint, and sets at the
// bottom-right when every required field is in — driven by --p (fraction filled)
// and --arc (its parabolic height), set here from the form's completion. The
// logo glows via a filter halo that brightens toward noon and turns slowly for
// life. Sky colors are the Sunrise brand tokens (Cerulean day, Cedar/Yellow
// dawn & dusk). Decorative — completion status is conveyed in text by the
// caption beside the heading.

export function SunsetProgress({ p, arc }) {
  return (
    <div className="submit-sky" style={{ "--p": p, "--arc": arc }} aria-hidden="true">
      <div className="day" />
      <img className="sun" src={sunLogo} alt="" />
      <div className="ground" />
      <div className="sunset-track">
        <span />
      </div>
    </div>
  );
}
