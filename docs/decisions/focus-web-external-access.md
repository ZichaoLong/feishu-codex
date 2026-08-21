# Focus Web External Access for Self-Hosted Deployments

Document role: synchronized English peer. Canonical Chinese: `docs/decisions/focus-web-external-access.zh-CN.md`.

> Status: accepted. Focus Web remains loopback-only by default. Only a complete,
> explicit trusted-proxy configuration enables the external audience defined
> here. The Gateway never listens on a non-loopback address in either mode.

## 1. Problem

A deployer needs to reach a Focus Gateway that still listens on one fixed
loopback port from a phone or another machine. The HTTPS proxy may run beside
Focus, or B may terminate HTTPS and use a protected persistent SSH tunnel to
the loopback Gateway on A. This cannot be implemented by exposing Codex
app-server, opening the Gateway listener, or trusting browser-supplied headers.

## 2. Deployment and authority

- `web_host` is always loopback; trusted-proxy mode requires an explicit fixed,
  nonzero `web_port`.
- Each instance accepts exactly one canonical HTTPS external origin with a
  non-loopback host, never `localhost`, a `.localhost` name, a loopback IP, a
  wildcard, a path-routed instance, or a list of candidate authorities. The
  host may be canonical DNS, IPv4, or a compressed IPv6 literal.
  Focus session cookies are named by instance, but external authority remains
  divided by external host rather than port. Concurrently accessed instances
  therefore require distinct DNS names or IP literals, not only distinct ports
  on one host.
- The proxy-to-Gateway hop is same-host loopback or an encrypted SSH tunnel
  whose exposed endpoints both remain loopback. Gateway does not infer proxy
  identity from source IP or interface placement.
- The deployer chooses the proxy product, external network, and user
  authentication. The proxy/network owns TLS and certificates plus boundaries
  such as Basic Auth, OIDC, mTLS, or a private-network ACL. Focus assumes no
  particular product.
- app-server remains a Focus-owned loopback/capability-token backend; browsers
  connect only to Gateway.

External authority comes only from validated canonical config, exact `Host` /
HTTPS `Origin`, and a Focus-specific proxy proof. `request.remote`, a
localhost source, `Forwarded`, every `X-Forwarded-*` field, and proxy-vendor
headers never participate in admission or cookie decisions.

## 3. Proxy proof, label, and trust domain

The deployer generates an independent 32-byte random URL-safe proof for each
Focus instance. Its raw value remains only in the proxy's secret manager; Focus
config stores only the 64-character lowercase SHA-256 verifier. The raw proof
never enters runtime discovery, URLs, cookies, HTTP/WebSocket responses,
events, logs, or a durable runtime store.

The proxy may inject admission headers only after its own authentication/ACL.
It first removes client-supplied instances of the proof and label headers, then
injects the exact proof and one nonempty, length-bounded opaque proxy label on
every HTTP request and WebSocket upgrade. The browser cannot self-assert the
label.

This narrow wire has exactly two headers. `X-Focus-Trusted-Proxy-Proof` is the
43-character URL-safe no-padding encoding of the 32-byte random proof.
`X-Focus-Trusted-Proxy-Identity` is only the opaque label above and must match
`[A-Za-z0-9][A-Za-z0-9._:@/+~-]{0,127}`. The historical word `Identity` in its
name does not promote that label into a Focus identity fact.

Focus proves only that the configured proxy made a statement about that opaque
label. The label is not a Focus-verified real name, email, device identity,
administrator role, per-user ACL, or complete audit fact. Every admitted
subject in one proxy trust domain is a fully trusted collaborator of that Focus
instance. A deployer needing isolation uses separate instances, OS accounts, or
containers rather than adding a cosmetic ACL around the label.

## 4. Session audience and browser admission

Local bootstrap remains local-only. External first authentication reuses the
existing `POST /api/client/register`. Request admission issues a temporary
external session only when exact external `Host`, exact HTTPS `Origin`, the
constant-time proof check, and a bounded label all succeed. It sets the
`Secure; HttpOnly; SameSite=Strict; Path=/` cookie only after the handler
succeeds; handler failure revokes the temporary session immediately.

Session audience stores only the local/external boundary, exact external
origin, and opaque proxy label. Every later external HTTP request and WebSocket
upgrade repeats proof and label validation and must exactly match that session
audience. Sessions cannot cross local/external, origin, or label boundaries.
The proof is not a writer, interaction-response, backend, or control-plane
credential. A registered Web document still follows the existing RuntimeLoop,
document, and root-operation admission.

The external page URL is the bookmarkable configured origin and contains no
fragment token. `focusctl web open` and runtime discovery continue to publish
only the local loopback endpoint; neither publishes the external origin, proof,
or label. If a live session expires or the service restarts, this contract only
guarantees that an explicit page reload or reopening the bookmark can register
again through the proxy. It does not promise unattended silent refresh and
never replays a mutation, steer, approval, or another request whose effect may
already have occurred.

The authentication-required surface must reload the whole document as its
explicit recovery action; it must not call ordinary load again inside a stale
document whose identity registration already completed. The same surface
honestly directs external users to reload/reopen the bookmark and local or SSH
local-forward users to run `focusctl [--instance <name>] web open` and use the
new URL. It neither guesses deployment mode from the hostname nor turns reload
into effect replay.

## 5. Capacity, revocation, and failure boundary

The proxy owns auth-attempt, request, connection, and WebSocket
rate/concurrency limits at the unauthenticated edge. Gateway retains its
existing request-body, upload, WebSocket-message, and event-queue hard limits,
and applies code-defined fixed caps plus `429` to proof-driven external session
and socket issuance: at most 128 external sessions per process, at most eight
WebSockets per external session, and a new external socket only while the
Gateway's current total socket count is below 128. Focus does not add per-user
token buckets, timers, configurable rate-limit schema, or durable counters.

The service captures one trusted-proxy config snapshot at startup. Rotation or
revocation replaces the verifier and performs a planned restart. That revokes
process-local sessions and may cause brief unavailability; zero-downtime dual
credential slots, live reload, watchers, and recovery state are not added.

If the tunnel or proxy is unavailable, external access fails directly while
Focus runtime and local Web neither fall back to plaintext networking nor open
the listener. If B's tunnel reaches the wrong instance, that instance's
different proof verifier must reject the request before any session or handler
effect.

## 6. Explicit non-goals

- non-loopback binding or a Focus-owned direct TLS listener
- plaintext public HTTP or an external audience without verifiable TLS
- device bearers, durable pairing, silent device refresh, or long-lived browser
  credentials
- Focus-native passwords/PINs, per-user ACLs, administrator roles, or user
  identity auditing
- source-IP trust, arbitrary forwarded-header trust, or proxy-vendor special
  cases
- new app-server, thread, turn, writer, approval, or interaction authority

## 7. Minimal deployment checklist

1. Generate one independent proof and verifier for the instance. This
   cross-platform Python command prints the raw proof and its SHA-256 only to
   the current terminal. Put the raw value in the proxy's secret management and
   the verifier in `codex.yaml`; never put the raw value in Focus config, a URL,
   or the repository:

   ```text
   python -c "import hashlib,secrets; p=secrets.token_urlsafe(32); print('proof='+p); print('sha256='+hashlib.sha256(p.encode('ascii')).hexdigest())"
   ```

2. Configure `web_enabled: true`, a loopback `web_host`, a fixed nonzero
   `web_port`, the exact `web_trusted_proxy_origin` (a canonical DNS, IPv4, or
   compressed-IPv6 HTTPS origin), and
   `web_trusted_proxy_proof_sha256: <verifier>`, then restart that instance.
   `focusctl web open` remains local-only and does not publish the external
   URL.
3. The HTTPS proxy authenticates/authorizes first. It removes client-supplied
   `X-Focus-Trusted-Proxy-Proof` and `X-Focus-Trusted-Proxy-Identity`, then
   injects the secret proof and one fixed bounded opaque label on every HTTP
   request and WebSocket upgrade. It preserves the browser's external `Host`
   and `Origin`, forwards to the instance loopback port, and never substitutes
   forwarded headers for the two Focus headers.
4. A same-host proxy connects directly to loopback. When the proxy is on B and
   Focus is on A, a service manager maintains a host-key-pinned B-loopback →
   SSH → A-loopback tunnel and B's proxy connects only to that B-loopback
   endpoint. Neither host exposes the Gateway port.
5. Bookmark the external HTTPS origin. The proxy/private network owns durable
   login, simple password/PIN, OIDC, membership, and user revocation; Focus does
   not copy those device or ACL facts. If the page reports session loss,
   explicitly reload it without obtaining another Focus bootstrap token.

## 8. Deployment checks and diagnostics

- After changing trusted-proxy config or its verifier, restart the target
  instance and run `focusctl --instance <name> service status`. Confirm that
  the runtime is available and the Web Gateway is listening on the configured
  fixed loopback port. This status surface deliberately does not show the
  external origin, proof, or label.
- From outside, open only the configured HTTPS origin. Do not send the loopback
  bootstrap URL produced by `focusctl web open` through the proxy, and do not
  expect that command to publish the external URL.
- `400 Invalid external Host header` means the request `Host` did not exactly
  match the loaded external authority. `403 Invalid trusted proxy
  proof/identity` or a cross-origin rejection means that the proof, label, or
  browser `Origin` failed admission. If an established page later receives
  `401`, explicitly reload the whole document as required by section 4.
- Diagnose in this order: the proxy upstream targets the correct instance's
  fixed loopback port; external `Host` remains exact; a browser-supplied
  `Origin` is forwarded unchanged; client-supplied Focus headers are removed;
  both Focus headers are reinjected on every HTTP request and WebSocket
  upgrade; and the raw proof's SHA-256 matches the Focus verifier.
  `Forwarded` / `X-Forwarded-*` cannot repair any admission failure.
- Do not enable diagnostics that record request headers, proxy secrets, or
  complete upstream requests. If proof diagnosis is unavoidable, compare only
  verifiers. After any suspected disclosure, rotate the proof, replace the
  verifier, and perform a planned restart of the target instance.
