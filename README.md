# Posttrain

A self-hosted, read-only NiceGUI dashboard for the local transformer post-training run. The visual reference is [tempmonitor](https://github.com/candlelightner/tempmonitor/tree/feat/initial-monitor).

The dashboard refreshes every 30 seconds. It shows optimizer and epoch progress, ETA, training loss with an EMA-20 overlay, complete validation results, pre-clipping gradient norms, the learning-rate schedule, update speed and token throughput, GPU and host resources, checkpoint retention, and run configuration. Charts support hover, zoom, update ranges, time/step axes, and optional baseline or logarithmic gradient views.

Training history is reconstructed from the supervisor's allowlisted numeric events. Resource observations begin when this dashboard starts collecting and persist in its private runtime directory. They are not reconstructed for earlier hours. The website reads aggregate metadata only; it does not read chat datasets, private training logs, message text, weights, or optimizer/RNG payloads, and it has no training-control endpoints.

## Local service

- URL: `http://127.0.0.1:8766`
- Intended public URL: `https://posttrain.dirigent.uk`
- Login: shared password only; no administrator account or username.
- Runtime: isolated Python 3.11 environment with NiceGUI 3.19.0.
- Service: `posttrain-dashboard.service` in the `dirigent` user manager.
- Limits: 512 MiB memory and half a CPU core; GPU acceleration is not used.
- User lingering is enabled so the services survive logout.

```sh
systemctl --user status posttrain-dashboard.service
systemctl --user restart posttrain-dashboard.service
```

`run-dashboard` is an alternative foreground launcher. It loads `.env` as literal values, without shell evaluation. The private `.env` has mode 0600 and contains a salted password hash and random session secret. Runtime files have private permissions. Keep those files local and out of Git.

Authentication protects the NiceGUI HTTP and websocket paths. Sessions expire after 12 hours and are revoked on logout; restarting the app requires logging in again. Public-host cookies are Secure and HttpOnly. Login attempts are rate-limited and modifying requests require a valid origin. `/healthz` reveals only whether the web service is responding.

## Cloudflare Tunnel

The official `cloudflared` 2026.10.7 binary is installed locally in `bin/`, with its release SHA-256 verified. Its user service is installed but waits for a private connector token.

In the Cloudflare dashboard, create or select a remotely managed tunnel and add a published application route:

| Setting | Value |
| --- | --- |
| Public hostname | `posttrain.dirigent.uk` |
| Service type | HTTP |
| Service URL | `http://127.0.0.1:8766` |
| Connector location | This VM |
| HTTP Host Header override | Leave unset |

On this VM, run:

```sh
/home/dirigent/posttrain-dashboard/connect-tunnel
```

Paste the connector token into its hidden prompt. It saves the token to `runtime/tunnel.token` with mode 0600 and enables the outbound tunnel service. Tokens never appear in process arguments or application source.

```sh
systemctl --user status posttrain-tunnel.service
```

The origin binds to loopback, so the connector needs to run on this VM. Cloudflare provides public HTTPS and forwards HTTP to the local origin. Websocket forwarding must remain enabled. The dashboard already supplies its own password gate; no separate Cloudflare Access account is required for this setup. Cloudflare's [published application setup](https://developers.cloudflare.com/tunnel/get-started/) documents the route fields.

Public reachability is verified after the account-side route and connector are configured.

## Development and checks

`requirements.lock` pins runtime dependencies; `requirements-dev.lock` adds pytest and Playwright. The active training environment is separate and unchanged.

```sh
.venv/bin/python -m pytest -q tests
```

Browser verification uses the locally downloaded Chromium headless shell with GPU acceleration disabled. Screenshots and QA artifacts stay in ignored `artifacts/`. The local browser dependency cache, credentials, runtime observations, binaries, and environments are all ignored by Git.
