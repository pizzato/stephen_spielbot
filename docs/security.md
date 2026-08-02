# Security

!!! danger "The web app has no authentication"
    The web UI and its API (FastAPI, default port **8001**) ship with **no login, no API
    key, and no per-user access control**. Anyone who can reach the port has full
    control: read and change settings, post to connected YouTube and X accounts, and
    start or stop worker machines.

This is acceptable **only** because the app is meant to run as a single-user tool bound
to `127.0.0.1`. Treat reaching the port as equivalent to holding an admin session.

## Safe deployment

- **Keep it on localhost.** Don't bind the server to `0.0.0.0` or a public interface, and
  don't put it behind a reverse proxy reachable from an untrusted network — unless you
  add your own authentication in front of it.
- **`make tailscale` exposes the app to every device on your tailnet**, over tailnet-only
  HTTPS and still with no app-level auth. Only use it on a tailnet you fully trust. The
  target deliberately uses `tailscale serve`, not `tailscale funnel` — **never** expose
  this app to the public internet.
- **Credentials never leave the machine in cleartext over the API.** Stored secrets
  (`claude_api_key`, `grok_api_key`, `openai_api_key`, `hf_token`, `x_client_secret`) are
  redacted from `GET /api/config`; the UI shows a "saved — leave blank to keep"
  placeholder.
- **Secrets live outside the repo**, under `~/.config/video-generator/` — `config.yaml`,
  `client_secrets.json`, `*_token.json`. Never commit them.

## Publishing safeguards

Two independent gates stand between a finished render and a public post:

- **Publish approval** — with *Require approval before publishing* on
  ([Settings → Automation](manual/settings.md#publishing-schedule)), finished films are
  held until you approve them in [Films](manual/films.md).
- **Default privacy** — new YouTube uploads default to `private`. Raise it deliberately.

Automation can be turned all the way up to hands-free; it is off by default for a reason.
Read [Settings → Automation](manual/settings.md#automation) before enabling it on a
channel you care about.

## Content Credentials

Published videos can be signed with [C2PA](https://c2pa.org) provenance declaring them
AI-generated. It needs `c2patool` installed (`brew install c2patool`) and is skipped
silently otherwise. With no certificate configured, a local self-signed one is generated
automatically — readable everywhere, though validators show "issued by an unknown
source". See [Settings → Content Credentials](manual/settings.md#content-credentials-c2pa).

## Reporting a vulnerability

Please report security issues **privately** via GitHub's *Report a vulnerability*
(Security → Advisories) on the
[repository](https://github.com/pizzato/stephen_spielbot/security/advisories), rather than
opening a public issue. Reports are acknowledged and fixed before public disclosure.

The canonical policy is
[`SECURITY.md`](https://github.com/pizzato/stephen_spielbot/blob/main/SECURITY.md).
