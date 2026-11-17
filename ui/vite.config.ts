import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// Optional: COLUMNTRACE_DATA_DIR (set in the git-ignored ui/.env.local) lets the dev server
// read one extra folder, so a private lineage file can be opened with
//   http://localhost:5173/?data=/@fs/<absolute path to lineage.json>
// without copying it into the project tree.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const extra = env.COLUMNTRACE_DATA_DIR
  return {
    plugins: [react()],
    base: './',
    server: { fs: { allow: [process.cwd(), ...(extra ? [extra] : [])] } },
    test: { environment: 'node', include: ['src/**/*.test.ts'] },
  }
})
