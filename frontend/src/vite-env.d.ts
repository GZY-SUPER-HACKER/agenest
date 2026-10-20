/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** API origin, only needed when the frontend is served by something other than the backend. */
  readonly VITE_API_BASE?: string;
  /** Dev-only: where the Vite dev server proxies /api. */
  readonly VITE_API_ORIGIN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
