"""Suno credit boundaries: preview first, deliberate export, resumable requests."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import suno_music


class SunoMusicTests(unittest.TestCase):
    def setUp(self):
        self.wd = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.calls = []
        self.downloaded = set()
        self.clips = [{"id": cid, "status": "complete", "title": "Test",
                       "song_url": f"https://suno.com/song/{cid}",
                       "metadata": {"model": "v6-mini", "duration": 31}}
                      for cid in ("one", "two")]
        self.enterContext(mock.patch.object(suno_music.suno, "account_usage", return_value={
            "suno_account": {"download_credits_remaining": 20, "plan": {"name": "Pro"}}}))
        self.call = self.enterContext(mock.patch.object(suno_music.suno, "call_tool", side_effect=self._call))
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = [b"exported MP3"]
        self.enterContext(mock.patch.object(suno_music.requests, "get", return_value=response))
        self.enterContext(mock.patch.object(suno_music, "transcode_to_wav",
                                           side_effect=lambda source, target: Path(target).write_bytes(b"WAV")))
        self.enterContext(mock.patch.object(suno_music, "_get_duration", return_value=31))

    def _call(self, name, args):
        self.calls.append((name, args.copy()))
        if name == "audio-create":
            return {"clip_ids": ["one", "two"], "status": "submitted"}
        if name == "audio-wait":
            return {"clips": self.clips}
        if name == "audio-download":
            if args["confirm"]:
                self.downloaded.add(args["id"])
            if args["id"] in self.downloaded:
                return {"url": "https://cdn.suno.ai/export.mp3"}
            return {"status": "requires_credit", "credits_remaining": 20}
        raise AssertionError(name)

    def generate(self, **kw):
        return suno_music.generate_suno_takes("Test", 30, work_dir=self.wd, caption="folk",
                                             lyrics="[Verse]\nhello",
                                             operation_id="test-operation", **kw)

    def test_manual_preview_creates_pair_without_any_export(self):
        result = self.generate(voice_id="singing-voice")
        self.assertEqual([t["id"] for t in result["suno_takes"]], ["one", "two"])
        self.assertNotIn("audio-download", [name for name, _ in self.calls])
        self.assertEqual(self.calls[0][1]["voice_id"], "singing-voice")
        self.assertFalse((self.wd / "background_music.wav").exists())

    def test_preview_works_with_zero_download_credits(self):
        suno_music.suno.account_usage.return_value = {"suno_account": {"download_credits_remaining": 0}}
        self.generate()
        self.assertEqual(self.downloaded, set())

    def test_one_take_export_never_exports_the_other(self):
        self.generate()
        result = suno_music.download_suno_take(self.wd, "one")
        self.assertEqual(self.downloaded, {"one"})
        self.assertEqual(len(result["takes"]), 1)
        self.assertEqual(result["clip_ids"], ["one", "two"])

    def test_explicit_selection_exports_only_the_chosen_take_and_reuses_it(self):
        self.generate()
        result = suno_music.download_suno_take(self.wd, "two")
        hist = suno_music.import_suno_takes(self.wd, result)
        self.assertEqual(self.downloaded, {"two"})
        self.assertEqual(len(hist["versions"]), 1)
        self.assertEqual(hist["versions"][0]["remote_id"], "two")
        calls = len(self.calls)
        again = suno_music.download_suno_take(self.wd, "two")
        hist = suno_music.import_suno_takes(self.wd, again)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(len(hist["versions"]), 1)
        self.assertEqual((self.wd / "background_music.wav").read_bytes(), b"WAV")

    def test_saved_request_resume_does_not_create_or_download_again(self):
        self.generate()
        suno_music.download_suno_take(self.wd, "one")
        calls = len(self.calls)
        self.generate()
        suno_music.download_suno_take(self.wd, "one")
        self.assertEqual(len(self.calls), calls)

    def test_lost_creation_response_reuses_idempotency_key(self):
        original = self._call
        first = True
        def lose(name, args):
            nonlocal first
            result = original(name, args)
            if name == "audio-create" and first:
                first = False
                raise TimeoutError("lost response")
            return result
        self.call.side_effect = lose
        with self.assertRaises(TimeoutError):
            self.generate()
        self.generate()
        creates = [a for name, a in self.calls if name == "audio-create"]
        self.assertEqual(creates[0], creates[1])

    def test_manual_retry_with_new_task_id_recovers_the_original_key(self):
        self.call.side_effect = TimeoutError('lost response')
        with self.assertRaises(TimeoutError):
            self.generate()
        original = json.loads(next(self.wd.glob('suno_requests/*/request.json')).read_text())['request']
        self.call.side_effect = self._call
        suno_music.generate_suno_takes('Test', 30, work_dir=self.wd, caption='folk',
                                       lyrics='[Verse]\nhello', operation_id='new-manual-task')
        created = next(args for name, args in self.calls if name == 'audio-create')
        self.assertEqual(created, original)
        self.assertEqual(len(list(self.wd.glob('suno_requests/*/request.json'))), 1)

    def test_ambiguous_old_submission_cannot_spend_again(self):
        self.call.side_effect = TimeoutError("lost response")
        with self.assertRaises(TimeoutError):
            self.generate()
        path = next(self.wd.glob("suno_requests/*/request.json"))
        state = json.loads(path.read_text())
        state["submitted_at"] = 0
        path.write_text(json.dumps(state))
        self.call.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "23 hours"):
            self.generate()
        self.call.assert_not_called()

    def test_export_failure_preserves_current_track_and_remote_ids(self):
        (self.wd / "background_music.wav").write_bytes(b"old")
        self.generate()
        self.call.side_effect = RuntimeError("export failed")
        with self.assertRaises(RuntimeError):
            suno_music.download_suno_take(self.wd, "two")
        self.assertEqual((self.wd / "background_music.wav").read_bytes(), b"old")
        self.assertEqual(len(suno_music.list_suno_takes(self.wd)), 2)

    def test_unauthorised_clip_id_never_calls_suno(self):
        self.generate()
        self.call.reset_mock()
        with self.assertRaises(ValueError):
            suno_music.download_suno_take(self.wd, "another-users-song")
        self.call.assert_not_called()

    def test_pending_songs_are_visible_after_wait_failure(self):
        original = self._call
        def fail_wait(name, args):
            if name == 'audio-wait':
                raise TimeoutError('Still generating')
            return original(name, args)
        self.call.side_effect = fail_wait
        with self.assertRaises(TimeoutError):
            self.generate()
        pending = suno_music.list_suno_takes(self.wd)
        self.assertEqual([t['id'] for t in pending], ['one', 'two'])
        self.assertEqual(pending[0]['status'], 'pending')
        self.assertEqual(self.downloaded, set())

    def test_retry_with_changed_parameters_cannot_create_a_second_pair(self):
        self.generate(model="v6-mini")
        with self.assertRaises(ValueError):
            self.generate(model="v6")
        self.assertEqual(sum(name == "audio-create" for name, _ in self.calls), 1)

    def test_failed_sibling_does_not_block_importing_completed_take(self):
        self.generate()
        path = next(self.wd.glob('suno_requests/*/request.json'))
        state = json.loads(path.read_text())
        state.pop('clips')
        path.write_text(json.dumps(state))
        original = self._call
        def selected_only(name, args):
            if name == 'audio-wait':
                self.assertEqual(args['ids'], ['one'])
                return {'clips': [self.clips[0]]}
            return original(name, args)
        self.call.side_effect = selected_only
        suno_music.download_suno_take(self.wd, 'one')
        previews = suno_music.list_suno_takes(self.wd)
        self.assertEqual([c['id'] for c in previews], ['one', 'two'])
        self.assertEqual(previews[1]['status'], 'pending')
        self.assertEqual(self.downloaded, {'one'})


if __name__ == "__main__":
    unittest.main()
