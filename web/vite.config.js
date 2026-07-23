import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: `npm run dev` serves the UI on :5173 and proxies API calls to a
// locally running coordinator. Production never needs Node — `npm run build`
// emits static files into web/dist, which the coordinator serves itself.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: process.env.COORDINATOR_URL || "http://127.0.0.1:8443",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // Split MUI/emotion into their own vendor chunk: it dwarfs our app
        // code, changes far less often, and caches across app-only deploys.
        manualChunks: {
          mui: ["@mui/material", "@mui/icons-material", "@emotion/react", "@emotion/styled"],
        },
      },
    },
  },
});
