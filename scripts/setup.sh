#!/usr/bin/env bash
set -euo pipefail

# Proven Hire Job Description Interview dev setup — installs JS + Python deps.
echo "Installing JS workspace deps (pnpm)…"
pnpm install

echo "Syncing Python agent deps (uv)…"
( cd apps/agent && uv sync )

# Build BEFORE init: `pnpm proven-hire` runs cli/dist/index.js, which only
# exists after `pnpm build` — on a fresh clone init-first fails.
echo "Done. Next: pnpm build && pnpm proven-hire init && pnpm test (then: pnpm dev)"
