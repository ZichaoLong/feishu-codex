# Focus Web

Focus Web is the browser frontend bundled with each running Focus instance. It
is derived from kimi-web and retains its Vue design system, responsive shell,
Markdown, KaTeX, Mermaid, Shiki, tool, and diff presentation code. Focus-owned
transport and runtime logic lives under `src/focus/`.

Runtime and deployment users do not need Node.js, npm, fnm, or nvm. Release
builds are committed under `bot/web_assets/dist` and packaged with the Python
application.

## Development

The following is only the local frontend-development setup used on this
machine. It is not a deployment prerequisite. This machine uses `fnm` to
select Node.js; non-interactive shells must initialize it explicitly:

```bash
export PATH="$HOME/.local/share/fnm:$PATH"
eval "$(fnm env --shell bash)"
```

Install the lockfile-pinned frontend dependencies and run checks from this
directory:

```bash
npm ci
npm run typecheck
npm test
npm run check:style
npm run check:kimi-provenance -- --upstream /path/to/kimi-code
npm run build
npm run check:notices
```

`npm run build` writes the packaged static assets to
`../bot/web_assets/dist`.  It also regenerates the browser-delivery third-party
notices from the rendered Rollup module graph and `package-lock.json`.

## Development Server

Start a Focus service with Web enabled, then obtain a fresh bootstrap URL:

```bash
focusctl web open --no-browser
```

Use the Gateway origin printed by that command as the Vite proxy target:

```bash
FOCUS_WEB_GATEWAY_URL=http://127.0.0.1:<gateway-port> npm run dev
```

Open the Vite origin and preserve the printed fragment, for example:

```text
http://127.0.0.1:5175/#token=<bootstrap-token>
```

Vite proxies same-origin `/api` HTTP and WebSocket traffic to the selected
Focus Gateway and rewrites `Origin` accordingly.

## Architecture Boundary

The browser never connects directly to Codex app-server:

```text
Vue components
  -> Focus view state and actions
     -> FocusWebApi
        -> loopback Focus Web Gateway
           -> RuntimeLoop / ownership / runtime leases
              -> Codex app-server adapter
```

HTTP snapshots are authoritative. WebSocket events carry process-local
`runtime_epoch` and `revision` invalidations; a gap causes a snapshot reload.
The event stream is not a durable replay log.

Web supports multiple observing and realtime-contributing documents; writer
leases remain only for operations that explicitly require serialization.
Prompt/steer, model and effort selection, approval and structured input, compact/review,
goals, lifecycle controls, bounded history, attachments, and read-only Tasks
from collaboration items in parent history are connected
through Focus. File browsing, terminal, side chat, child-tree observation or
recovery, and direct input to parent-owned Codex child threads remain disabled
by contract.

The Gateway remains loopback-only. SSH local forwarding remains local mode;
access through a non-loopback external browser origin requires the configured
trusted HTTPS reverse-proxy contract. See
[Focus Web External Access](../docs/decisions/focus-web-external-access.md).

## Upstream

See `UPSTREAM.md` for the imported kimi-web commit and the provenance, license,
and notice rules. `provenance/kimi-web-files.json` records every Kimi-derived
source file and reviewed local modification; run the explicit provenance check
after changing declared derived code. It compares against the recorded import
only and does not require Focus to track new Kimi commits, minimize the diff, or
merge later Kimi features. The check is intentionally separate from the release
build because it verifies Git objects from a local Kimi checkout.

## Third-Party Notices

The release build writes these stable files into `bot/web_assets/dist`:

- `/THIRD_PARTY_NOTICES.html` — readable browser page
- `/THIRD_PARTY_NOTICES.md` — plain-text source notice
- `/THIRD_PARTY_SBOM.json` — machine-readable component inventory

They are intentionally served with `Cache-Control: no-store`: filenames stay
stable for legal discoverability while generated content follows the locked
bundle. `npm run check:notices` verifies their lockfile digest, Kimi provenance,
required font/icon license texts, and the source/package copies used by wheels.
