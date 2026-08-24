#!/usr/bin/env python3
"""A/B the H3 acted-scene prompt experiments (pipeline/h3_prompt_experiments).

Builds every variant of one complex scene — the bundled two-hander by default,
or a real scene's metadata via --scene-json — writes each prompt to a file,
and prints a comparison table (word counts + advisory lint). With --render it
also shoots one real Ref2VA take PER VARIANT on a ComfyUI worker, same seed
and size, so the takes differ only by the prompt idea under test.

Compare prompts only (no GPU, no config needed):

    .venv/bin/python scripts/h3_prompt_ab.py --out /tmp/h3_ab

Render the A/B takes (uses the live config's references for --style-name;
fixture speakers must be mapped onto two of that style's characters):

    .venv/bin/python scripts/h3_prompt_ab.py --out /tmp/h3_ab \
        --render --style-name "My Style" --cast "MARA=Alice,ELLIS=Bob" \
        --seed 424242 --size 832x480 --only baseline,schema-labels,native-full

The resolution-vs-adherence experiment (community: guidance holds better at
352–416p than 768p) is a render axis, not a prompt one: render the SAME
variant twice, e.g. --size 832x480 and --size 736x416, and compare.

Renders queue directly on the worker — run it when the fleet is idle so it
doesn't fight the app's own renders for the GPU.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import h3_prompt_experiments as exp          # noqa: E402
from pipeline import performance as perf                   # noqa: E402


def rename_cast(meta: dict, mapping: dict[str, str]) -> dict:
    """Fixture speaker names -> a real style's character names, everywhere:
    cast, line speakers, and prose mentions in setting/beats/direction."""
    if not mapping:
        return meta
    out = json.loads(json.dumps(meta))
    out["cast"] = [mapping.get(c, c) for c in out.get("cast") or []]
    for line in out.get("lines") or []:
        line["speaker"] = mapping.get(line["speaker"], line["speaker"])
    def sub(text):
        for old, new in mapping.items():
            text = re.sub(rf"\b{re.escape(old)}\b", new, text, flags=re.IGNORECASE)
        return text
    for key in ("setting", "direction"):
        if out.get(key):
            out[key] = sub(out[key])
    for beat in out.get("beats") or []:
        beat["action"] = sub(beat["action"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="h3_prompt_ab", help="output directory")
    ap.add_argument("--scene-json", help="scene meta JSON instead of the bundled "
                    "fixture ({meta, style_note, picture_names, audio_names})")
    ap.add_argument("--only", help="comma-separated variant subset "
                    f"(of: {', '.join(exp.VARIANTS)})")
    ap.add_argument("--render", action="store_true",
                    help="also render one take per variant on a worker")
    ap.add_argument("--style-name", default="",
                    help="style whose portraits/voices the render uses")
    ap.add_argument("--cast", default="", help='fixture->style character map, '
                    'e.g. "MARA=Alice,ELLIS=Bob"')
    ap.add_argument("--worker", default="", help="ComfyUI URL (default: first "
                    "configured comfy worker)")
    ap.add_argument("--size", default="832x480", help="render WxH")
    ap.add_argument("--seed", type=int, default=424242,
                    help="fixed seed shared by every variant")
    args = ap.parse_args()

    if args.scene_json:
        scene = json.loads(Path(args.scene_json).read_text())
    else:
        scene = exp.demo_scene()
    meta = scene["meta"]

    mapping = dict(kv.split("=", 1) for kv in args.cast.split(",") if "=" in kv)
    if mapping:
        meta = rename_cast(meta, mapping)
        scene["picture_names"] = [
            {**p, "name": mapping.get(p.get("name"), p.get("name")),
             **({"character": mapping.get(p["character"], p["character"])}
                if p.get("character") else {})}
            if isinstance(p, dict) else mapping.get(p, p)
            for p in scene["picture_names"]]
        scene["audio_names"] = [mapping.get(n, n) for n in scene["audio_names"]]

    variants = exp.build_variants(meta, style_note=scene.get("style_note", ""),
                                  picture_names=scene.get("picture_names"),
                                  audio_names=scene.get("audio_names"))
    if args.only:
        keep = [v.strip() for v in args.only.split(",")]
        unknown = [v for v in keep if v not in variants]
        if unknown:
            ap.error(f"unknown variant(s): {', '.join(unknown)}")
        variants = {k: v for k, v in variants.items() if k in keep}

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, prompt in variants.items():
        (out_dir / f"{name}.txt").write_text(prompt + "\n")
        findings = exp.lint_prompt(prompt, meta)
        rows.append((name, len(prompt.split()),
                     exp.description_word_count(prompt), findings))

    width = max(len(r[0]) for r in rows)
    print(f"{'variant':<{width}}  words  description  lint")
    for name, words, desc, findings in rows:
        print(f"{name:<{width}}  {words:>5}  {desc:>11}  {len(findings)}")
        for f in findings:
            print(f"{'':<{width}}         · {f}")
    print(f"\nPrompts written to {out_dir}/")

    if not args.render:
        return 0

    # ── Render one take per variant, same references / seed / size ──────────
    from app import load_config, resolve_performance_references, style_settings
    from pipeline import engines as _engines
    from pipeline.comfyui import generate_video_h3_ref

    cfg = load_config()
    run_cfg = {**cfg, **style_settings(cfg, args.style_name)}
    worker = args.worker or next(iter(cfg.get("comfy_workers") or []), "")
    if not worker:
        print("No ComfyUI worker configured and none given via --worker",
              file=sys.stderr)
        return 2
    width_px, height_px = (int(x) for x in args.size.lower().split("x"))

    refs = resolve_performance_references(meta, run_cfg, out_dir,
                                          args.style_name or "")
    if not refs["pictures"]:
        print("No character portraits resolved — map the fixture speakers "
              "onto the style's characters with --cast", file=sys.stderr)
        return 2
    # The prompts above cite the fixture's reference layout; the render must
    # cite what actually resolved, so rebuild each variant with the real slots.
    variants = exp.build_variants(meta, style_note=scene.get("style_note", ""),
                                  picture_names=refs["pictures"],
                                  audio_names=[a["name"] for a in refs["audios"]])
    if args.only:
        variants = {k: v for k, v in variants.items() if k in keep}
    engine = _engines.resolve_reference(run_cfg, run_cfg.get("reference_engine"))
    seconds = perf.render_seconds(meta)
    print(f"\nRendering {len(variants)} take(s) on {worker} — "
          f"{engine.get('label')}, {width_px}x{height_px}, "
          f"{seconds:.1f}s, seed {args.seed}")
    for name, prompt in variants.items():
        (out_dir / f"{name}.txt").write_text(prompt + "\n")
        out = out_dir / f"{name}_{width_px}x{height_px}.mp4"
        if out.exists():
            print(f"  {name}: exists — skipping")
            continue
        print(f"  {name}: rendering → {out.name}")
        generate_video_h3_ref(
            engine, prompt, [Path(p["path"]) for p in refs["pictures"]], out,
            ref_audios=[Path(a["path"]) for a in refs["audios"]],
            width=width_px, height=height_px, seed=args.seed,
            duration_seconds=seconds, comfy_url=worker)
    print(f"\nTakes written to {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
