#!/usr/bin/env bash
# InfiniteTalk spike — measured single generation. Throwaway; see README.md.
#
# Runs generate_infinitetalk.py on one portrait + one audio wav while sampling GPU
# memory and wall-clock, then ffprobes the output. Prints a metrics summary so you
# can judge VRAM / max length / quality on real hardware instead of estimates.
#
#   bash run_spike.sh --image portrait.png --audio test_60s.wav
#   bash run_spike.sh --image portrait.png --audio test_60s.wav --size infinitetalk-720
#   bash run_spike.sh --image portrait.png --audio test_60s.wav --frames 3000 --lowvram --fp8
set -euo pipefail

SPIKE_ROOT="${SPIKE_ROOT:-$HOME/infinitetalk_spike}"
REPO_DIR="${REPO_DIR:-$SPIKE_ROOT/InfiniteTalk}"

IMAGE=""
AUDIO=""
SIZE="infinitetalk-480"      # or infinitetalk-720
FRAMES="1000"               # ~40s default; ~25 fps -> 1500≈60s, 3000≈120s
STEPS="40"
PROMPT="A person speaks directly to the camera, natural facial expression, subtle head movement, steady framing, soft studio lighting."
EXTRA=()

usage() { grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --image)   IMAGE="$2"; shift 2 ;;
    --audio)   AUDIO="$2"; shift 2 ;;
    --size)    SIZE="$2"; shift 2 ;;
    --frames)  FRAMES="$2"; shift 2 ;;
    --steps)   STEPS="$2"; shift 2 ;;
    --prompt)  PROMPT="$2"; shift 2 ;;
    --lowvram) EXTRA+=(--num_persistent_param_in_dit 0); shift ;;
    --fp8)     EXTRA+=(--quant fp8); shift ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1"; usage ;;
  esac
done

[ -n "$IMAGE" ] && [ -f "$IMAGE" ] || { echo "ERROR: --image <portrait.png> required and must exist"; exit 1; }
[ -n "$AUDIO" ] && [ -f "$AUDIO" ] || { echo "ERROR: --audio <speech.wav> required and must exist"; exit 1; }
[ -f "$REPO_DIR/generate_infinitetalk.py" ] || { echo "ERROR: repo not found at $REPO_DIR (run setup.sh?)"; exit 1; }

IMAGE="$(cd "$(dirname "$IMAGE")" && pwd)/$(basename "$IMAGE")"   # absolutise
AUDIO="$(cd "$(dirname "$AUDIO")" && pwd)/$(basename "$AUDIO")"

WORK="$(pwd)/infinitetalk_spike_out"
mkdir -p "$WORK"
JSON="$WORK/input.json"
VRAMLOG="$WORK/vram.csv"
SAVE="$WORK/infinitetalk_res"
: > "$VRAMLOG"

# --- build the input json (schema per examples/single_example_image.json) ----
python3 - "$IMAGE" "$AUDIO" "$PROMPT" "$JSON" <<'PY'
import json, sys
img, aud, prompt, out = sys.argv[1:5]
json.dump({"prompt": prompt, "cond_video": img, "cond_audio": {"person1": aud}},
          open(out, "w"), indent=2)
PY

AUDIO_DUR="$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$AUDIO" 2>/dev/null || echo '?')"

echo "── InfiniteTalk spike ─────────────────────────────────────"
echo " repo   : $REPO_DIR"
echo " image  : $IMAGE"
echo " audio  : $AUDIO  (${AUDIO_DUR}s)"
echo " size   : $SIZE   frames=$FRAMES  steps=$STEPS  extra=${EXTRA[*]:-none}"
echo "───────────────────────────────────────────────────────────"

# --- GPU memory sampler (1 Hz, peak taken afterwards) ------------------------
( while true; do
    nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits >> "$VRAMLOG" 2>/dev/null || true
    sleep 1
  done ) &
SAMPLER=$!
cleanup() { kill "$SAMPLER" 2>/dev/null || true; }
trap cleanup EXIT

# --- run ---------------------------------------------------------------------
cd "$REPO_DIR"
START=$SECONDS
set +e
python generate_infinitetalk.py \
  --ckpt_dir weights/Wan2.1-I2V-14B-480P \
  --wav2vec_dir weights/chinese-wav2vec2-base \
  --infinitetalk_dir weights/InfiniteTalk/single/infinitetalk.safetensors \
  --input_json "$JSON" \
  --size "$SIZE" \
  --sample_steps "$STEPS" \
  --max_frame_num "$FRAMES" \
  --mode streaming \
  --motion_frame 9 \
  "${EXTRA[@]}" \
  --save_file "$SAVE"
RC=$?
set -e
ELAPSED=$((SECONDS - START))
cleanup
trap - EXIT

# --- collect metrics ---------------------------------------------------------
PEAK="$(sort -n "$VRAMLOG" 2>/dev/null | tail -1 || echo '?')"
OUT="$(ls -t "$SAVE"*.mp4 "$WORK"/*.mp4 2>/dev/null | head -1 || true)"
if [ -n "$OUT" ] && [ -f "$OUT" ]; then
  OUT_DUR="$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$OUT" 2>/dev/null || echo '?')"
  OUT_WH="$(ffprobe -v quiet -select_streams v:0 -show_entries stream=width,height -of csv=s=x:p=0 "$OUT" 2>/dev/null || echo '?')"
  OUT_FPS="$(ffprobe -v quiet -select_streams v:0 -show_entries stream=avg_frame_rate -of csv=p=0 "$OUT" 2>/dev/null || echo '?')"
else
  OUT="(none produced)"; OUT_DUR="-"; OUT_WH="-"; OUT_FPS="-"
fi

MM=$((ELAPSED / 60)); SS=$((ELAPSED % 60))
echo
echo "── InfiniteTalk spike result ──────────────────────────────"
printf " requested : streaming, max_frame_num=%s, %s, %s steps %s\n" "$FRAMES" "$SIZE" "$STEPS" "${EXTRA[*]:+(${EXTRA[*]})}"
printf " input     : audio %ss\n" "$AUDIO_DUR"
printf " OUTPUT    : %ss  %s  %sfps  -> %s\n" "$OUT_DUR" "$OUT_WH" "$OUT_FPS" "$OUT"
printf " peak VRAM : %s MiB\n" "$PEAK"
printf " wall-clock: %ss  (%dm%02ds)   exit=%s\n" "$ELAPSED" "$MM" "$SS" "$RC"
echo "───────────────────────────────────────────────────────────"
if [ "$RC" -ne 0 ]; then
  echo " exit != 0 — if it's a torch/flash-attn/arch error, that's the arm64 finding (see README)."
  echo "            if it OOM'd, re-run with --lowvram and/or --fp8, or a smaller --frames."
fi
echo " Now WATCH $OUT: lip-sync accuracy? identity stable? colour drift after ~60s?"
echo " Re-run with --frames 1500 (~60s) and --frames 3000 (~120s) to find the length ceiling."
