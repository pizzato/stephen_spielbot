---
name: Bug report
about: Something broke — a render failed, the UI misbehaved, a publish didn't happen
title: ''
labels: ''
assignees: ''

---

**Describe the bug**
A clear and concise description of what the bug is.

**To Reproduce**
Steps to reproduce the behavior:
1. Go to '...'
2. Click on '....'
3. See error

**Expected behavior**
A clear and concise description of what you expected to happen.

**Setup (please complete what applies):**
 - Controller OS: [e.g. macOS 15, Ubuntu 24.04]
 - Workers: [e.g. single machine / remote hosts; GPU model(s)]
 - LLM backend: [Claude API / local vLLM / other]
 - Browser (for UI bugs): [e.g. Chrome 130]

**Logs**
The backend log is at `~/.local/share/video-generator/logs/app.log` — paste the
relevant lines (strip anything private). For worker issues,
`docker logs <container>` on the worker host is the place to look.

**Additional context**
Add any other context about the problem here (screenshots welcome).
