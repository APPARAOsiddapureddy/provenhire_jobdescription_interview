# packaging/

Distribution packaging for the `proven-hire` CLI.

- **npm** — NOT yet published. The CLI lives in the `cli/` workspace package
  (`@proven-hire/cli`, marked `private`) and currently runs only from a
  repo checkout: `pnpm build && pnpm proven-hire <command>`. Publishing to
  the npm registry is deferred to a later WP.
- **pip** — a thin Python wrapper so `pipx run proven-hire` works for agent-only
  self-hosters. Deferred to a later WP.
