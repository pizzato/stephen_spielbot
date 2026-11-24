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
        self.cfg = {'name': 'Folk Films', 'music_engine': 'suno', 'suno_model': 'v6', 'suno_voice_id': 'voice-one'}
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
            result = backend._do_song_generate(self.wd, operation_id='manual', engine_override='suno')
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

    def test_normal_generation_ignores_a_saved_suno_default(self):
        pool = mock.MagicMock()
        pool.acquire.return_value = 'http://worker'
        with mock.patch.object(suno_music, 'generate_suno_takes', side_effect=AssertionError('No Suno')), \
             mock.patch.object(backend.gapp, '_preview_worker_urls', return_value=['http://worker']), \
             mock.patch('pipeline.worker_pool.WorkerPool', return_value=pool), \
             mock.patch('pipeline.comfyui.generate_music',
                        side_effect=lambda title, secs, out, *a, **kw: Path(out).write_bytes(b'minimax')) as generate, \
             mock.patch('pipeline.assembler._get_duration', return_value=40), \
             mock.patch.object(backend.story_mode, 'expand_song_sound', return_value='folk'):
            backend._do_song_generate(self.wd)
        self.assertEqual(generate.call_args.kwargs['music_engine'], 'minimax-music3')
        self.assertEqual(backend._selected_music_engine(self.wd), 'minimax-music3')
        self.assertEqual(json.loads((self.wd / 'song.json').read_text())['music_engine'], 'minimax-music3')

    def test_song_action_forwards_requested_provider_and_defaults_to_minimax(self):
        for requested in ({}, {'music_engine': 'suno'}):
            with self.subTest(requested=requested), \
                 mock.patch.object(backend.gapp, 'OUTPUT_DIR', self.wd), \
                 mock.patch.object(backend.threading, 'Thread') as thread:
                result = backend.song_generate(backend.SongGenerateBody(work_dir=str(self.wd), **requested))
            args = thread.call_args.kwargs['args']
            self.assertEqual(args[0], result['task_id'])
            self.assertEqual(args[-1], requested.get('music_engine', 'minimax-music3'))

    def test_generation_worker_uses_action_choice(self):
        options = backend.SunoGenerationOptions(model='v6-wild', duration=None, voice_id='voice-two')
        with mock.patch.object(backend, '_do_song_generate', return_value={'ok': True}) as generate:
            backend._run_song_generate_task('explicit-suno', self.wd, music_engine='suno', suno_options=options)
        generate.assert_called_once_with(self.wd, 0.0, operation_id='explicit-suno', engine_override='suno',
                                         suno_options=options)

    def test_song_request_options_override_style_defaults_for_this_request(self):
        options = backend.SunoGenerationOptions(
            title='New title', caption='rock', lyrics='Different words', voice_id='', model='', duration=None,
            vocal_gender='female', max_mode=True, controls={'weirdness': 0.75, 'variety': 0})
        with mock.patch.object(suno_music, 'generate_suno_takes', return_value={'suno_takes': []}) as generate:
            backend._do_song_generate(self.wd, engine_override='suno', suno_options=options)
        self.assertEqual(generate.call_args.kwargs, {
            'title': 'New title', 'caption': 'rock', 'lyrics': 'Different words', 'voice_id': '', 'model': '',
            'duration': None, 'vocal_gender': 'female', 'max_mode': True, 'controls': {'weirdness': 0.75, 'variety': 0},
            'work_dir': self.wd, 'operation_id': None, 'style_name': 'Folk Films'})
        self.assertEqual(json.loads((self.wd / 'song.json').read_text())['caption'], 'folk')

    def test_suno_sound_at_limit_keeps_explicit_text_when_vocalist_wont_fit(self):
        path = self.wd / 'song.json'
        data = json.loads(path.read_text())
        data['vocalist'] = 'A warm alto'
        path.write_text(json.dumps(data))
        caption = 'x' * 1000
        options = backend.SunoGenerationOptions(caption=caption, voice_id='')
        with mock.patch.object(suno_music, 'generate_suno_takes', return_value={'suno_takes': []}) as generate:
            backend._do_song_generate(self.wd, engine_override='suno', suno_options=options)
        self.assertEqual(generate.call_args.kwargs['caption'], caption)

    def test_invalid_provider_is_rejected_by_both_actions(self):
        from pydantic import ValidationError
        for body_type in (backend.SongGenerateBody, backend.MusicRegenBody):
            with self.subTest(body_type=body_type), self.assertRaises(ValidationError):
                body_type(work_dir=str(self.wd), music_engine='unknown')

    def test_selected_track_provider_follows_history_not_next_generation(self):
        source = self.wd / 'take.wav'
        source.write_bytes(b'wave')
        local = music_history.record(self.wd, source, provider='comfy', model='minimax-music3')['selected']
        remote = music_history.record(self.wd, source, provider='suno', remote_id='remote')['selected']
        music_history.record(self.wd, source, voice='A', source_id=remote)
        self.assertEqual(backend._selected_music_engine(self.wd), 'suno')
        music_history.select(self.wd, local)
        self.assertEqual(backend._selected_music_engine(self.wd), 'minimax-music3')

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
            backend._do_song_generate(self.wd, add_seconds=10, engine_override='suno')

    def test_normal_generate_longer_does_not_resing_an_imported_suno_track(self):
        track = self.wd / 'background_music.wav'
        track.write_bytes(b'suno')
        music_history.record(self.wd, track, provider='suno', remote_id='remote')
        with self.assertRaisesRegex(ValueError, 'continuation'):
            backend._do_song_generate(self.wd, add_seconds=10)

    def test_status_exposes_balances_without_credentials(self):
        with mock.patch.object(suno, 'is_connected', return_value=True), \
             mock.patch.object(suno, 'account_usage', return_value={
                 'allowed_models': ['v6-mini'], 'features': ['controls.advanced'], 'suno_account': {
                     'plan': {'name': 'Free'}, 'credits_remaining': 735,
                     'download_credits_remaining': 5}}):
            result = backend.suno_status()
        self.assertEqual(result['download_credits_remaining'], 5)
        self.assertEqual(result['allowed_models'], ['v6-mini'])
        self.assertTrue(result['advanced_controls'])
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
        options = backend.SunoGenerationOptions(voice_id='voice-two', duration=45, controls={'style_influence': 0})
        with mock.patch.object(backend, '_film_job_config', return_value=self.cfg), \
             mock.patch.object(backend, '_film_checkpoint'), \
             mock.patch.object(backend, '_record_film_task_activity'), \
             mock.patch('pipeline.assembler._get_duration', return_value=40), \
             mock.patch.object(suno_music, 'generate_suno_takes') as generate, \
             mock.patch.object(suno_music, 'list_suno_takes', return_value=[{'id': 'a'}]), \
             mock.patch.object(backend.gapp, '_preview_worker_urls', side_effect=AssertionError('No GPU')), \
             mock.patch.object(backend.gapp, 'on_remix', side_effect=AssertionError('No remux')):
            backend._run_music_regen('suno-preview-test', self.wd, 'folk', music_engine='suno', suno_options=options)
        self.assertEqual(backend._film_tasks['suno-preview-test']['status'], 'done')
        self.assertNotIn('take_count', generate.call_args.kwargs)
        self.assertEqual(generate.call_args.kwargs['voice_id'], 'voice-two')
        self.assertEqual(generate.call_args.kwargs['duration'], 45)
        self.assertEqual(generate.call_args.kwargs['controls'], {'style_influence': 0})
        self.assertEqual(generate.call_args.kwargs['style_name'], 'Folk Films')
        self.assertFalse((self.wd / 'background_music.wav').exists())

    def test_normal_remix_ignores_old_suno_job_config(self):
        (self.wd / 'combined.mp4').write_bytes(b'video')
        pool = mock.MagicMock()
        with mock.patch.object(backend, '_film_job_config', return_value=self.cfg), \
             mock.patch.object(backend, '_film_checkpoint'), \
             mock.patch.object(backend, '_record_film_task_activity'), \
             mock.patch.object(backend, '_acquire_render_worker', return_value='http://worker'), \
             mock.patch.object(backend, '_mix_volumes', return_value=(1, 1, 1)), \
             mock.patch.object(backend, '_maybe_burn_subtitles'), \
             mock.patch.object(backend, '_maybe_burn_first_frame_cover'), \
             mock.patch.object(backend, '_maybe_fade_ending'), \
             mock.patch.object(backend, '_maybe_apply_title_cards'), \
             mock.patch.object(backend.gapp, '_preview_worker_urls', return_value=['http://worker']), \
             mock.patch('pipeline.worker_pool.WorkerPool', return_value=pool), \
             mock.patch('pipeline.assembler._get_duration', return_value=40), \
             mock.patch.object(backend.story_mode, 'expand_song_sound', return_value='folk'), \
             mock.patch('pipeline.comfyui.generate_music',
                        side_effect=lambda title, secs, out, *a, **kw: Path(out).write_bytes(b'minimax')) as generate, \
             mock.patch.object(suno_music, 'generate_suno_takes', side_effect=AssertionError('No Suno')), \
             mock.patch.object(backend.gapp, 'on_remix', return_value=(str(self.wd / 'final.mp4'), '')):
            backend._run_music_regen('normal-remix-test', self.wd, 'folk')
        self.assertEqual(backend._film_tasks['normal-remix-test']['status'], 'done')
        self.assertEqual(generate.call_args.kwargs['music_engine'], 'minimax-music3')


if __name__ == '__main__':
    unittest.main()
