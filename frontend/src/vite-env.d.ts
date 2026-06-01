// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

// Vite injects import.meta.env at build time. Declare the shape here so
// TypeScript accepts it without requiring a working vite/client package.
interface ImportMetaEnv {
  readonly VITE_API_URL?: string
  readonly MODE: string
  readonly BASE_URL: string
  readonly PROD: boolean
  readonly DEV: boolean
  readonly SSR: boolean
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

declare module '*.png' {
  const src: string
  export default src
}

declare module '*.jpg' {
  const src: string
  export default src
}

declare module '*.svg' {
  const src: string
  export default src
}