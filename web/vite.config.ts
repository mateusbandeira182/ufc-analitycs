import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    // Em desenvolvimento, encaminha as chamadas da API para o backend FastAPI.
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: true,
    coverage: {
      provider: "v8",
      reportsDirectory: "./coverage",
      // Restringe o agregado ao código-fonte; o bundle de produção (dist/) não
      // deve poluir a cobertura.
      include: ["src/**"],
      exclude: [
        "dist/**",
        "src/api/types.ts",
        "src/main.tsx",
        "src/mocks/**",
        "src/test/**",
        "**/*.config.*",
        "**/*.d.ts",
        "**/*.test.{ts,tsx}",
      ],
    },
  },
});
