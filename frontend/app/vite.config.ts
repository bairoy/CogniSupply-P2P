import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The repo keeps ONE .env at its root (see .env.example) -- backend services
// and the frontend read the same file. Vite's root is frontend/app, so without
// this it would look for frontend/app/.env and VITE_MAPBOX_TOKEN would silently
// come back undefined. Only VITE_-prefixed keys are ever exposed to the bundle,
// so DATABASE_URL/JWT_SECRET/API keys in that file stay server-side.
const repoRoot = fileURLToPath(new URL("../../", import.meta.url));

export default defineConfig({
  plugins: [react()],
  envDir: repoRoot,
  server: { port: 5173, strictPort: false },
});
