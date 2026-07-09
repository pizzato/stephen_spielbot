# Security Policy

## The web app has no authentication

Stephen Spielbot's web UI and its API (FastAPI, default port **8001**) ship with
**no login, no API key, and no per-user access control**. Anyone who can reach
the port has full control: read/change settings, post to connected YouTube/X
accounts, and start/stop worker machines.

This is acceptable **only** because the app is meant to run as a single-user
tool bound to `127.0.0.1` (localhost). Treat reaching the port as equivalent to
having an admin session.

### Safe deployment

- **Keep it on localhost.** Do not bind the server to `0.0.0.0` or a public
  interface, and do not put it behind a reverse proxy that is reachable from an
  untrusted network, unless you add your own authentication in front of it.
- **`make tailscale` exposes the app to every device on your tailnet** (over
  tailnet-only HTTPS, still with **no app-level auth**). Only use it on a
  tailnet you fully trust. The target deliberately uses `tailscale serve`, not
  `tailscale funnel` — **never** expose this app to the public internet.
- **Credentials never leave the machine in cleartext over the API.** Stored
  secrets (`claude_api_key`, `grok_api_key`, `hf_token`, `x_client_secret`) are redacted from
  `GET /api/config`; the UI shows a "saved — leave blank to keep" placeholder.
- **Secrets live outside the repo**, under `~/.config/video-generator/`
  (`config.yaml`, `client_secrets.json`, `*_token.json`). Never commit them.

## Reporting a vulnerability

Please report security issues privately via GitHub's **Report a vulnerability**
(Security → Advisories) on this repository, rather than opening a public issue.
We'll acknowledge and work on a fix before any public disclosure.
