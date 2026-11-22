"""The UI can preview Suno without replacing audio or spending exports."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import webapp.backend.main as backend
from pipeline import music_history, suno, suno_music


class SunoBackendTests(unittest.TestCase):
    def setUp(self):
        self.wd = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.wd / 'song.json').write_text(json.dumps({
            'title': 'Test', 'seconds': 40, 'lyrics': '[Verse]\nhello', 'caption': 'folk'}))
        self.cfg = {'music_engine': 'suno', 'suno_model': 'v6', 'suno_voice_id': 'voice-one'}
        self.enterContext(mock.patch.object(backend.gapp, 'load_config', return_value=self.cfg))
        self.enterContext(mock.patch.object(backend.gapp, 'style_settings', return_value=self.cfg))
        self.enterContext(mock.patch.object(backend, '_persist_activity_log_locked'))

    def test_manual_generation_returns_remote_pair_leaving_current_audio_unchanged(self):
        current = self.wd / 'background_music.wav'
        current.write_bytes(b'old')
        remote = [{'id': 'a', 'song_url': 'https://suno.com/song/a'}, {'id': 'b'}]
        with mock.patch.object(suno_music, 'generate_suno_takes', return_value={
                'clip_ids': ['a', 'b'], 'takes': [], 'suno_takes': remote}) as generate, \
             mock.patch.object(suno_music, 'import_suno_takes') as imports, \
             mock.patch.object(backend.gapp, '_preview_worker_urls', side_effect=AssertionError('No GPU')):
            result = backend._do_song_generate(self.wd, operation_id='manual')
        self.assertEqual(result['imported_count'], 0)
        self.assertEqual(result['suno_takes'], remote)
        self.assertEqual(current.read_bytes(), b'old')
        imports.assert_not_called()
        self.assertNotIn('take_count', generate.call_args.kwargs)
        self.assertEqual(generate.call_args.kwargs['voice_id'], 'voice-one')

    def test_automatic_generation_never_calls_suno_even_with_suno_style_and_override(self):
        def local(title, secs, out, *args, **kw):
            self.assertEqual(kw['music_engine'], 'minimax-music3')
            Path(out).write_bytes(b'minimax')
        pool = mock.MagicMock()
        pool.acquire.return_value = 'http://worker'
        with mock.patch.object(suno_music, 'generate_suno_takes', side_effect=AssertionError('No Suno')), \
             mock.patch.object(backend.gapp, '_preview_worker_urls', return_value=['http://worker']), \
             mock.patch('pipeline.worker_pool.WorkerPool', return_value=pool), \
             mock.patch('pipeline.comfyui.generate_music', side_effect=local), \
             mock.patch('pipeline.assembler._get_duration', return_value=40), \
             mock.patch.object(backend.story_mode, 'expand_song_sound', return_value='folk'):
            backend._do_song_generate(self.wd, automatic=True, engine_override='suno')
        self.assertEqual((self.wd / 'background_music.wav').read_bytes(), b'minimax')

    def test_import_of_existing_take_is_a_local_reselection(self):
        audio = self.wd / 'take.wav'
        audio.write_bytes(b'chosen')
        music_history.record(self.wd, audio, provider='suno', remote_id='chosen',
                             model='v6', singing_voice_id='voice-one')
        with mock.patch.object(suno_music, 'download_suno_take', side_effect=AssertionError('No export')), \
             mock.patch.object(suno_music, 'request_for_take', return_value={
                 'lyrics': 'original words', 'style': 'original sound'}), \
             mock.patch('pipeline.assembler._get_duration', return_value=42):
            result = backend._do_suno_import(self.wd, 'chosen')
        self.assertEqual(result['imported_count'], 1)
        self.assertEqual(len(result['versions']), 1)
        self.assertEqual((self.wd / 'background_music.wav').read_bytes(), b'chosen')
        song = json.loads((self.wd / 'song.json').read_text())
        self.assertEqual(song['suno_voice_id'], 'voice-one')
        self.assertEqual(song['duration'], 42)
        self.assertEqual(song['seconds'], 42)
        self.assertEqual(song['lyrics'], 'original words')
        self.assertEqual(song['caption'], 'original sound')

    def test_oauth_credentials_cannot_be_served_as_media(self):
        token = self.wd / 'suno_token.json'
        token.write_text('{"access_token":"secret"}')
        with mock.patch.object(suno, '_TOKEN_PATH', token), \
             mock.patch.object(backend.gapp, 'OUTPUT_DIR', self.wd):
            with self.assertRaises(backend.HTTPException) as exc:
                backend.serve_file(str(token))
        self.assertEqual(exc.exception.status_code, 404)

    def test_explicit_import_records_only_selected_remote_take(self):
        audio = self.wd / 'take.wav'
        audio.write_bytes(b'chosen')
        with mock.patch.object(suno_music, 'download_suno_take', return_value={
                'clip_ids': ['a', 'b'], 'request': {'style': 'folk'},
                'takes': [{'path': str(audio), 'remote_id': 'b', 'model': 'v6', 'voice_id': 'voice-one'}]}) as download, \
             mock.patch('pipeline.assembler._get_duration', return_value=42):
            result = backend._do_suno_import(self.wd, 'b')
        download.assert_called_once_with(self.wd, 'b')
        self.assertEqual([v['remote_id'] for v in result['versions']], ['b'])

    def test_suno_continuation_does_not_silently_regenerate(self):
        with self.assertRaisesRegex(ValueError, 'continuation'):
            backend._do_song_generate(self.wd, add_seconds=10)

    def test_status_exposes_balances_without_credentials(self):
        with mock.patch.object(suno, 'is_connected', return_value=True), \
             mock.patch.object(suno, 'account_usage', return_value={
                 'allowed_models': ['v6-mini'], 'suno_account': {
                     'plan': {'name': 'Free'}, 'credits_remaining': 735,
                     'download_credits_remaining': 5}}):
            result = backend.suno_status()
        self.assertEqual(result['download_credits_remaining'], 5)
        self.assertEqual(result['allowed_models'], ['v6-mini'])
        self.assertTrue(result['connected'])

    def test_voice_list_paginates_both_collections_and_deduplicates(self):
        pages = [{'voices': [{'id': 'a', 'name': 'A'}], 'next_cursor': 'next'},
                 {'voices': [{'id': 'b', 'name': 'B'}]},
                 {'voices': [{'id': 'a', 'name': 'A'}]}]
        with mock.patch.object(suno, 'is_connected', return_value=True), \
             mock.patch.object(suno, 'call_tool', side_effect=pages) as call:
            result = backend.suno_voices()
        self.assertEqual([v['id'] for v in result['voices']], ['a', 'b'])
        self.assertEqual(call.call_args_list[1].args[1]['cursor'], 'next')
        self.assertEqual(call.call_args_list[-1].args[1]['collection'], 'loved')

    def test_postrender_suno_regen_previews_without_remux_or_worker(self):
        (self.wd / 'combined.mp4').write_bytes(b'video')
        with mock.patch.object(backend, '_film_job_config', return_value=self.cfg), \
             mock.patch.object(backend, '_film_checkpoint'), \
             mock.patch.object(backend, '_record_film_task_activity'), \
             mock.patch('pipeline.assembler._get_duration', return_value=40), \
             mock.patch.object(suno_music, 'generate_suno_takes') as generate, \
             mock.patch.object(suno_music, 'list_suno_takes', return_value=[{'id': 'a'}]), \
             mock.patch.object(backend.gapp, '_preview_worker_urls', side_effect=AssertionError('No GPU')), \
             mock.patch.object(backend.gapp, 'on_remix', side_effect=AssertionError('No remux')):
            backend._run_music_regen('suno-preview-test', self.wd, 'folk')
        self.assertEqual(backend._film_tasks['suno-preview-test']['status'], 'done')
        self.assertNotIn('take_count', generate.call_args.kwargs)
        self.assertFalse((self.wd / 'background_music.wav').exists())


if __name__ == '__main__':
    unittest.main()
