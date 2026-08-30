# SIGGRAPH Asia 2026 AI Film Frontiers technical report

This folder contains the first LaTeX draft for *The Cycle*.

## Build

Use a current ACM article-template installation and run:

```sh
latexmk -pdf technical_report.tex
```

The report intentionally uses `\documentclass[sigconf]{acmart}` as required by the challenge. The organizers limit the report to four pages. Re-check the page count after every substantive edit.

## Author checks before submission

- Confirm whether `Independent Researcher, Sydney, Australia` is the desired affiliation and add the preferred email/ORCID if appropriate.
- Add the Linklings submission ID and organizer-supplied DOI/ISBN/copyright metadata if the submission system provides them.
- Confirm the production-time estimate requested by FilmFreeway; the project files record outputs and versions but not reliable human hours.
- Confirm the copyright paragraph accurately describes every asset in the final submitted cut.
- Keep the machine-generated disclosure and the explicit `MiniMax H3` and `MiniMax-Music3` credits. Check the current model terms before submission.
- Replace the current film export with a submission-ready file. The inspected cut is H.264 with stereo audio, but it is 1280x704 and has no burned-in subtitles; FilmFreeway currently asks for 1080p and burned-in subtitles.
- The challenge page lists a four-page maximum and an August 23, 2026 deadline. Verify both again immediately before uploading.

## Evidence used for this draft

- Final film: 241.664 s, 1280x704, 25 fps, H.264 + stereo AAC.
- 48 final scenes, each tied to a contiguous window of the 240.919 s master song.
- FLUX.2 Klein first frames and MiniMax H3 Ref2VA Turbo final scene renders.
- MiniMax-Music3 soundtrack; two original generations and three optional seed-vc re-voicings retained, with original generation 2 selected in the inspected cut.
- 97 retained scene-image versions and eight retained alternate video files across four scenes.
- Canonical `The Ape` character record and portrait used in the recurring ape sequence.
