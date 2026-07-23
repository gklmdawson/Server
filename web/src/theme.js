// MUI theme mapped 1:1 onto the Sunrise tokens in styles.css, so MUI
// components and the existing hand-rolled CSS render as one design system.
// cssVariables + colorSchemeSelector:'media' makes MUI switch light/dark on
// prefers-color-scheme — the same trigger styles.css uses — with no JS state.
import { createTheme } from "@mui/material/styles";

export const theme = createTheme({
  cssVariables: { colorSchemeSelector: "media" },
  colorSchemes: {
    light: {
      palette: {
        primary: { main: "#0077a8", contrastText: "#ffffff" }, // Cerulean (--accent)
        secondary: { main: "#113e59" },                        // Navy
        success: { main: "#006f67" },                          // Spruce (--good)
        warning: { main: "#ffd457", contrastText: "#113e59" }, // Yellow
        error: { main: "#d2342e" },                            // Ruby (--critical)
        background: { default: "#f4f7f9", paper: "#ffffff" },  // --page / --surface
        text: {
          primary: "#113e59",   // --ink
          secondary: "#4c6e75", // --ink-2
          disabled: "#a3a5a8",  // --muted
        },
        divider: "rgba(17, 62, 89, 0.12)", // --border
      },
    },
    dark: {
      palette: {
        primary: { main: "#009acb", contrastText: "#ffffff" }, // Horizon
        secondary: { main: "#c4cdd3" },
        success: { main: "#2fa597" },
        warning: { main: "#ffd457", contrastText: "#113e59" },
        error: { main: "#e5514b" },
        background: { default: "#0c1922", paper: "#16242e" },
        text: {
          primary: "#ffffff",
          secondary: "#c4cdd3",
          disabled: "#9ba6ac",
        },
        divider: "rgba(255, 255, 255, 0.12)",
      },
    },
  },
  typography: {
    fontFamily:
      '"Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif',
    // body1 is what CssBaseline puts on <body>; match styles.css exactly so
    // adding the baseline changes nothing visually.
    body1: { fontSize: 14, lineHeight: 1.45 },
    body2: { fontSize: 12.5, lineHeight: 1.45 },
    button: { textTransform: "none" }, // Sunrise buttons are sentence case
  },
  shape: { borderRadius: 10 }, // .card / .node-card radius
});
