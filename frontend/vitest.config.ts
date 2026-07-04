import { defineConfig } from 'vitest/config'

// Minimal unit-test config, separate from vite.config.ts (which stays
// production-build-only). jsdom gives us `document`/`EventSource`-shaped
// globals for the game-loop and runtime-stream reconnect tests.
export default defineConfig({
  test: {
    environment: 'jsdom',
  },
})
