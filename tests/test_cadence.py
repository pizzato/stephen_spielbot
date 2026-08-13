"""Voice cadence: measurement store, speed resolution, length planning, and
the natural-pause narration splitter (pipeline/cadence.py)."""
import wave

import pytest

from pipeline import cadence
from pipeline.llm import Scene, enforce_scene_word_caps


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_CADENCE_FILE", str(tmp_path / "voice_cadence.json"))


# ── word_count ────────────────────────────────────────────────────────────────

def test_word_count_plain():
    assert cadence.word_count("The quick brown fox jumps.") == 5


def test_word_count_ignores_pause_markers_and_punctuation():
    assert cadence.word_count("Wait [pause:2] here — now!") == 3


def test_word_count_counts_cjk_chars():
    assert cadence.word_count("你好世界") == 4
    assert cadence.word_count("hello 世界") == 3


def test_word_count_empty():
    assert cadence.word_count("") == 0
    assert cadence.word_count(None) == 0


# ── store / sampling ──────────────────────────────────────────────────────────

def test_natural_wpm_defaults_until_measured():
    wpm, measured = cadence.natural_wpm("Meredith", "openf5")
    assert wpm == cadence.DEFAULT_WPM
    assert measured is False


def test_record_sample_and_lookup():
    # 30 words in 12 s of speech = 150 wpm
    cadence.record_sample("Meredith", "openf5", words=30, seconds=12.0)
    wpm, measured = cadence.natural_wpm("Meredith", "openf5")
    assert measured is True
    assert wpm == pytest.approx(150.0, abs=0.5)


def test_record_sample_normalizes_speed_and_silence():
    # 30 words in 10 s of audio incl. 2 s spliced silence, at 1.5x speed:
    # speech = 8 s → measured 225 wpm → natural 150.
    cadence.record_sample("V", "openf5", words=30, seconds=10.0,
                          speed=1.5, silence_secs=2.0)
    wpm, _ = cadence.natural_wpm("V", "openf5")
    assert wpm == pytest.approx(150.0, abs=0.5)


def test_record_sample_rolls_average():
    cadence.record_sample("V", "openf5", words=30, seconds=12.0)  # 150
    cadence.record_sample("V", "openf5", words=32, seconds=12.0)  # 160
    wpm, _ = cadence.natural_wpm("V", "openf5")
    assert 150 < wpm < 160


def test_record_sample_rejects_junk():
    cadence.record_sample("V", "openf5", words=3, seconds=1.0)      # too short
    cadence.record_sample("V", "openf5", words=500, seconds=10.0)   # 3000 wpm
    _, measured = cadence.natural_wpm("V", "openf5")
    assert measured is False


def test_keys_are_per_voice_and_engine():
    cadence.record_sample("A", "openf5", words=30, seconds=12.0)
    assert cadence.natural_wpm("A", "chatterbox-multilingual")[1] is False
    assert cadence.natural_wpm("B", "openf5")[1] is False


def test_default_option_aliases_to_default_narrator():
    cadence.record_sample("", "openf5", words=30, seconds=12.0)
    wpm, measured = cadence.natural_wpm("Default (F5-TTS)", "openf5")
    assert measured is True and wpm == pytest.approx(150.0, abs=0.5)


def test_set_measured_overwrites():
    cadence.record_sample("V", "openf5", words=30, seconds=12.0)
    cadence.set_measured("V", "openf5", 172.4)
    wpm, _ = cadence.natural_wpm("V", "openf5")
    assert wpm == pytest.approx(172.4)


# ── speed ⇄ cadence ───────────────────────────────────────────────────────────

def test_resolve_speed_from_target_cadence():
    cadence.set_measured("V", "openf5", 150.0)
    s = cadence.resolve_voice_speed(
        {"voice": "V", "tts_engine": "openf5", "voice_cadence_wpm": 180})
    assert s == pytest.approx(1.2)


def test_resolve_speed_falls_back_to_legacy_multiplier():
    assert cadence.resolve_voice_speed({"voice_speed": 1.25}) == pytest.approx(1.25)
    assert cadence.resolve_voice_speed({}) == 1.0


def test_resolve_speed_clamped():
    cadence.set_measured("V", "openf5", 150.0)
    s = cadence.resolve_voice_speed(
        {"voice": "V", "tts_engine": "openf5", "voice_cadence_wpm": 60})
    assert s == pytest.approx(0.4)  # 60/150, above the 0.3 floor
    s = cadence.resolve_voice_speed(
        {"voice": "V", "tts_engine": "openf5", "voice_cadence_wpm": 400})
    assert s == cadence.SPEED_MAX


def test_effective_wpm_prefers_target_then_scales_legacy():
    cadence.set_measured("V", "openf5", 140.0)
    wpm, _ = cadence.effective_wpm(
        {"voice": "V", "tts_engine": "openf5", "voice_cadence_wpm": 170})
    assert wpm == 170
    wpm, _ = cadence.effective_wpm(
        {"voice": "V", "tts_engine": "openf5", "voice_speed": 1.5})
    assert wpm == pytest.approx(210.0)


# ── planning ──────────────────────────────────────────────────────────────────

def test_plan_script_two_minutes_at_150():
    p = cadence.plan_script(2.0, 150.0)
    assert p["n_scenes"] == 10          # 120 s / 12 s target
    assert p["scene_words_target"] == 30
    assert p["scene_words_min"] == 25   # 10 s
    assert p["scene_words_max"] == 37   # 15 s
    assert p["words_total"] == 300


def test_plan_script_sentence_pause_eats_word_room():
    p = cadence.plan_script(2.0, 150.0, sentence_pause=1.0)
    assert p["scene_words_max"] == 35   # (15-1)s of speech
    assert p["scene_words_target"] < 30


def test_plan_script_scales_with_cadence():
    slow = cadence.plan_script(2.0, 100.0)
    fast = cadence.plan_script(2.0, 200.0)
    assert slow["n_scenes"] == fast["n_scenes"] == 10
    assert fast["words_total"] == 2 * slow["words_total"]


def test_plan_for_scenes_pins_count():
    p = cadence.plan_for_scenes(7, 150.0)
    assert p["n_scenes"] == 7
    assert p["words_total"] == 7 * p["scene_words_target"]


def test_minutes_for_scenes_uses_legacy_scene_length():
    assert cadence.minutes_for_scenes(6) == pytest.approx(0.9)  # 6 × 9 s


def test_plan_clamps_absurd_minutes():
    assert cadence.plan_script(10_000, 150.0)["n_scenes"] <= 200


# ── an explicit scene count ───────────────────────────────────────────────────

def test_scene_count_divides_the_length_into_longer_scenes():
    # 4 min as 10 scenes is 24 s each — twice the contract's 12 s — and each
    # scene carries twice the narration.
    auto = cadence.plan_script(4.0, 150.0)
    pinned = cadence.plan_script(4.0, 150.0, n_scenes=10,
                                 scene_ceiling=cadence.LTX_SCENE_CEIL_SECS)
    assert auto["n_scenes"] == 20
    assert pinned["n_scenes"] == 10
    assert pinned["scene_secs_target"] == pytest.approx(24.0)
    assert pinned["scene_words_target"] == 2 * auto["scene_words_target"]
    assert pinned["minutes"] == pytest.approx(4.0)   # same film, fewer cuts


def test_scene_count_divides_the_length_into_shorter_scenes():
    p = cadence.plan_script(2.0, 150.0, n_scenes=20)
    assert p["scene_secs_target"] == pytest.approx(6.0)
    assert p["scene_secs_max"] < cadence.SCENE_MAX_SECS
    assert p["minutes"] == pytest.approx(2.0)


def test_length_gives_way_when_a_scene_cannot_stretch_that_far():
    # 3 scenes of a 10-minute film would be 200 s each; H3 holds 12 s, so the
    # count stands and the film comes out at what it adds up to.
    p = cadence.plan_script(10.0, 150.0, n_scenes=3, scene_ceiling=12.0)
    assert p["n_scenes"] == 3
    assert p["scene_secs_target"] == pytest.approx(12.0)
    assert p["minutes"] == pytest.approx(0.6)


def test_a_scene_is_never_squeezed_below_the_floor():
    p = cadence.plan_script(0.5, 150.0, n_scenes=20)
    assert p["scene_secs_target"] == pytest.approx(cadence.SCENE_FLOOR_SECS)
    assert p["minutes"] == pytest.approx(20 * cadence.SCENE_FLOOR_SECS / 60.0, abs=0.01)


def test_scene_count_defaults_to_the_contract_ceiling():
    # With no engine ceiling given, a scene stretches only to the 15 s contract.
    p = cadence.plan_script(10.0, 150.0, n_scenes=4)
    assert p["scene_secs_target"] == pytest.approx(cadence.SCENE_MAX_SECS)


def test_engine_scene_ceiling_follows_the_engine():
    assert cadence.engine_scene_ceiling("ltx25") == cadence.LTX_SCENE_CEIL_SECS
    assert cadence.engine_scene_ceiling("minimax-h3") == 12.0
    assert cadence.engine_scene_ceiling("minimax-h3", chained=True) > 20.0


# ── splitting ─────────────────────────────────────────────────────────────────

def test_split_short_text_untouched():
    assert cadence.split_narration("A short line.", 20) == ["A short line."]


def test_split_at_sentence_boundaries():
    text = ("The city fell in a single night. "
            "Its people fled across the frozen river. "
            "Nothing was ever rebuilt on the old foundations.")
    pieces = cadence.split_narration(text, 8)
    assert pieces == [
        "The city fell in a single night.",
        "Its people fled across the frozen river.",
        "Nothing was ever rebuilt on the old foundations.",
    ]


def test_split_long_sentence_at_natural_pauses():
    text = ("The expedition pressed on through the storm, dragging their sledges "
            "over ridged ice, until the coastline finally appeared ahead.")
    pieces = cadence.split_narration(text, 10)
    assert len(pieces) >= 2
    for p in pieces:
        assert cadence.word_count(p) <= 10


def test_split_packs_sentences_up_to_cap():
    text = "One two three. Four five six. Seven eight nine."
    assert cadence.split_narration(text, 7) == [
        "One two three. Four five six.", "Seven eight nine."]


def test_split_unbreakable_clause_kept_whole():
    text = "one two three four five six seven eight nine ten eleven twelve"
    assert cadence.split_narration(text, 5) == [text]


def test_split_merges_runt_tail():
    text = "Alpha beta gamma delta epsilon. Zeta eta theta iota kappa. Tail."
    pieces = cadence.split_narration(text, 6, min_words=3)
    assert pieces[-1].endswith("Tail.")
    assert len(pieces) == 2  # the 1-word tail merged into its neighbour


# ── scene-level enforcement (pipeline/llm.py) ────────────────────────────────

def _scene(i, narration, **kw):
    return Scene(id=i, title=f"S{i}", image_prompt=f"img{i}",
                 video_prompt=f"vid{i}", narration=narration, **kw)


def test_enforce_splits_and_renumbers():
    plan = cadence.plan_script(1.0, 150.0)
    long = ("The fleet slipped out of the harbor before dawn, its lanterns dark, "
            "its decks silent, while the entire city slept on unaware. "
            "By the time the sun rose over the headland the ships were gone, "
            "and only the empty moorings remained to tell the story.")
    scenes = [_scene(1, "A short opening line for the video."),
              _scene(2, long),
              _scene(3, "A short closing line for the video.")]
    out = enforce_scene_word_caps(scenes, plan)
    assert len(out) > 3
    assert [s.id for s in out] == list(range(1, len(out) + 1))
    for s in out:
        assert cadence.word_count(s.narration) <= plan["scene_words_max"]
    # split pieces inherit the source scene's visuals
    mid = [s for s in out if s.image_prompt == "img2"]
    assert len(mid) >= 2 and all(s.video_prompt == "vid2" for s in mid)


def test_enforce_skips_dialogue_and_overrides():
    plan = cadence.plan_script(1.0, 150.0)
    long = " ".join(["word"] * 60)
    dialogue = _scene(1, long, mode="dialogue",
                      lines=[{"speaker": "A", "text": "hi"}])
    override = _scene(2, long, metadata_extra={"tts_text": "spoken override"})
    out = enforce_scene_word_caps([dialogue, override], plan)
    assert len(out) == 2


def test_enforce_noop_without_plan():
    scenes = [_scene(1, " ".join(["word"] * 60))]
    assert enforce_scene_word_caps(scenes, None) == scenes


# ── wav measurement ───────────────────────────────────────────────────────────

def test_wav_seconds(tmp_path):
    p = tmp_path / "t.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * 24000 * 3)  # 3 s
    assert cadence.wav_seconds(p) == pytest.approx(3.0)
    assert cadence.wav_seconds(tmp_path / "missing.wav") == 0.0
