# Contributing to Stephen Spielbot

Thanks for your interest in contributing! This is an AI video generator
(Python + FastAPI backend, React/Vite frontend, GPU workers running ComfyUI +
F5-TTS). The notes below get you set up and explain how changes land.

## Ways to contribute

- **Bugs / features** — open an issue (templates are provided).
- **Code** — small, focused pull requests are easiest to review.
- **Docs** — fixes to the README / setup docs are very welcome.

## Development setup

You don't need the full GPU cluster to work on most of the app — only the
controller (web app + tests) runs locally.

```bash
git clone https://github.com/pizzato/stephen_spielbot
cd stephen_spielbot

# Python environment (controller + tests)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt \
                      -r requirements-dev.txt \
                      -r webapp/backend/requirements.txt

# Frontend (needs a recent Node — Node 20+)
cd webapp/frontend && npm install && cd -
```

`make install` does the full setup (deps, models, worker containers over SSH,
config) and is intended for a real deployment with GPU workers — see the
[README](README.md). For contributing, the steps above are usually enough.

## Running it

```bash
make web-dev        # FastAPI + Vite with hot reload (http://localhost:8001)
make web-build      # production build of the frontend
```

> The web app has **no authentication** and is meant to run on `localhost`.
> See [SECURITY.md](SECURITY.md).

## Tests, linting & the CI gate

Please make sure these pass before opening a PR — they're exactly what CI runs
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)):

```bash
.venv/bin/python -m pytest tests/     # Python tests
make lint                             # ruff (pyflakes + syntax checks)
make lint-fix                         # auto-fix the fixable ruff findings
cd webapp/frontend && npm run build   # frontend must build
```

CI runs the test suite on Python 3.11 and 3.12, plus ruff and the frontend
build, with no secrets — so it works the same on pull requests from forks.

## Coding guidelines

- **Keep changes surgical.** Touch only what the change needs; match the
  surrounding code's style rather than reformatting.
- **No new secrets in the repo.** Credentials live under
  `~/.config/video-generator/` and must never be committed. The API redacts
  secret values; keep it that way.
- **Mind model licensing for monetized output.** The default image/video/audio
  models are commercial-friendly on purpose. Don't make a non-commercial model
  (e.g. the original F5-TTS weights, FLUX `[dev]`) the *default* — see
  [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and [`NOTICE.md`](NOTICE.md).

## Pull request process

1. Branch off `main`.
2. Make your change; add/adjust tests where it makes sense.
3. Ensure tests, `ruff`, and the frontend build pass.
4. Open a PR describing **what** and **why**; link any related issue.
5. A maintainer reviews and merges once CI is green.

By contributing, you agree your contributions are licensed under the project's
[Apache-2.0 License](LICENSE).

## Reporting security issues

Please **don't** file public issues for vulnerabilities — see
[SECURITY.md](SECURITY.md) for private reporting.
