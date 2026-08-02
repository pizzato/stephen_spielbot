# Contributing

The canonical guide is
[`CONTRIBUTING.md`](https://github.com/pizzato/stephen_spielbot/blob/main/CONTRIBUTING.md)
in the repository. This page is the short version, plus how to work on these docs.

## Development setup

You don't need the GPU cluster to work on most of the app — only the controller (web app
and tests) runs locally.

```bash
git clone https://github.com/pizzato/stephen_spielbot
cd stephen_spielbot

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt \
                      -r requirements-dev.txt \
                      -r webapp/backend/requirements.txt

cd webapp/frontend && npm install && cd -    # Node 20+
```

`make install` does the full deployment (deps, models, worker containers over SSH,
config) and is meant for a real setup with GPU workers. For contributing, the above is
usually enough.

## Running it

```bash
make web-dev        # FastAPI + Vite with hot reload — UI at http://localhost:5174
make web-build      # production build of the frontend
```

Port 8001 serves the API and the last *built* frontend; 5174 is the dev server with the
`/api` proxy.

## The CI gate

Make these pass before opening a pull request — they're exactly what
[`ci.yml`](https://github.com/pizzato/stephen_spielbot/blob/main/.github/workflows/ci.yml)
runs:

```bash
.venv/bin/python -m pytest tests/     # Python tests
make lint                             # ruff
cd webapp/frontend && npm run build   # the frontend must build
```

CI runs the suite on Python 3.11 and 3.12 with no secrets, so it behaves identically on
pull requests from forks. `make lint-fix` auto-fixes what ruff can.

## Coding guidelines

- **Keep changes surgical.** Touch only what the change needs; match the surrounding
  style rather than reformatting.
- **No new secrets in the repo.** Credentials live under `~/.config/video-generator/` and
  must never be committed. The API redacts secret values — keep it that way.
- **Mind model licensing for monetized output.** The default image, video, and audio
  models are commercial-friendly on purpose. Don't make a non-commercial model (the
  original F5-TTS weights, FLUX `[dev]`) the *default* — see
  [model licensing](tts_licensing.md).

## Working on the documentation

This site is [MkDocs Material](https://squidfunk.github.io/mkdocs-material/). Pages are
plain Markdown under `docs/`, and the navigation is the `nav:` block in `mkdocs.yml`.

```bash
make docs-serve     # live-reload preview at http://localhost:8010
make docs           # build with --strict (a broken internal link fails)
```

`make docs` is what the [Docs workflow](https://github.com/pizzato/stephen_spielbot/blob/main/.github/workflows/docs.yml)
runs on every pull request; merging to `main` deploys to GitHub Pages.

Adding a page means creating the Markdown file **and** adding it to `nav:` — `--strict`
fails on a page that isn't referenced.

## Pull requests

1. Branch off `main`.
2. Make the change; add or adjust tests where it makes sense.
3. Ensure tests, ruff, and the frontend build pass.
4. Open a PR describing **what** and **why**; link any related issue.
5. A maintainer reviews and merges once CI is green.

By contributing, you agree your contributions are licensed under the project's
[Apache-2.0 license](https://github.com/pizzato/stephen_spielbot/blob/main/LICENSE).

## Adding your channel

Making films with Stephen Spielbot? Add your channel to
[`channels.yaml`](https://github.com/pizzato/stephen_spielbot/blob/main/channels.yaml) and
open a pull request — that file is the only thing you need to edit. On merge, a GitHub
Action regenerates the list in the README and the app's **About** screen. Run
`make channels` to preview it locally.

## Reporting security issues

Please **don't** file public issues for vulnerabilities — see [Security](security.md).
