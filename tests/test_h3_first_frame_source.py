"""H3 Image scene openings: inherited settings, durable routing and references."""
import json
from types import SimpleNamespace
from unittest import mock

import pytest
from PIL import Image

import app
import resume_generation as rg
import webapp.backend.main as backend
import worker_agent
from pipeline import comfyui, engines
from pipeline.llm import Scene
from pipeline.orchestrator import DurableStore, job_id_from_work_dir, task_id
from pipeline.scene_video import ensure_first_frame
from test_styles import TempConfigCase, _style


class FirstFrameSourceSettingsTests(TempConfigCase):
    def test_inherited_source_persists_and_render_job_stamps_it(self):
        self.write_config({'styles': [_style('A', image_engine='qwen-image-2.1-turbo'),
                                     _style('H3', first_frame_source='h3', h3_image_size='4k'),
                                     {'name': 'Child', 'parent': 'H3'}],
                           'default_style': 'A'})
        cfg = app.load_config()
        app.save_config(cfg)
        cfg = app.load_config()
        self.assertEqual(app.scene_frame_engine(cfg, 'Child')['family'], 'h3-image')
        self.assertNotIn('first_frame_source', next(s for s in cfg['styles'] if s['name'] == 'Child'))
        self.assertNotIn('h3_image_size', next(s for s in cfg['styles'] if s['name'] == 'Child'))
        self.assertEqual(app.scene_frame_engine(cfg, 'Child')['h3_image_size'], '4k')
        self.assertEqual(app.scene_frame_engine({**cfg, 'h3_image_size': 'video'}, 'Child')['h3_image_size'], 'video')
        self.assertEqual(app.scene_frame_engine(cfg, 'A')['key'], 'qwen-image-2.1-turbo')
        # Stamped source wins if the style changes while a job is paused.
        self.assertEqual(app.scene_frame_engine({**cfg, 'first_frame_source': 'image'}, 'Child')['family'], 'flux2')
        wd = self.output_dir / 'h3-source-job'
        wd.mkdir()
        jid = job_id_from_work_dir(wd)
        store = DurableStore.default()
        try:
            store.create_or_update_job(jid, wd, 'H3 source', config={'style_name': 'Child'})
            store.upsert_scenes(jid, [{'id': 1, 'title': 'One', 'image_prompt': 'a frame',
                                       'video_prompt': 'a move', 'narration': 'words'}])
        finally:
            store.close()
        with mock.patch.object(app, '_launch_generation_job', return_value={}):
            backend.start_generation(backend.GenerateBody(job_id=jid, work_dir=str(wd), video_title='H3 source'))
        jc = json.loads((wd / 'job_config.json').read_text())
        self.assertEqual(jc['first_frame_source'], 'h3')
        self.assertEqual(jc['h3_image_size'], '4k')
        self.assertEqual(engines.resolve(cfg, jc['image_engine'])['family'], 'flux2')
        store = DurableStore.default()
        try:
            store.ensure_generation_plan(jid, wd, 'H3 source',
                                         [Scene(id=1, title='One', image_prompt='a frame', video_prompt='v', narration='words')],
                                         {**jc, 'vid_width': 832, 'vid_height': 480})
            payload = store.get_task(task_id(jid, 'scene', 1, 'image')).payload
            video_payload = store.get_task(task_id(jid, 'scene', 1, 'video')).payload
        finally:
            store.close()
        self.assertEqual(payload['first_frame_source'], 'h3')
        self.assertEqual(payload['h3_image_size'], '4k')
        self.assertEqual(video_payload['h3_image_size'], '4k')
        self.assertEqual(video_payload['first_frame_source'], 'h3')

    def test_h3_character_binding_and_no_frame_opt_out(self):
        cfg = app._ensure_styles({'styles': [_style('S', first_frame_source='h3')], 'default_style': 'S'})
        wd = self.output_dir / 'frame'
        wd.mkdir()
        chars = app._write_script_characters(wd, [{'name': 'Ana', 'description': 'a woman'}])
        cid = chars[0]['id']
        d = app._script_characters_dir(wd)
        d.mkdir(exist_ok=True)
        Image.new('RGB', (32, 32), 'white').save(d / f'{cid}.png')
        chars[0]['ref_image'] = f'{cid}.png'
        app._write_script_characters(wd, chars)
        scene = Scene(id=1, title='Ana', image_prompt='Ana waits.', video_prompt='v', narration='',
                      metadata_extra={'cast': ['Ana'], 'mode': 'silent'})
        engine = app.scene_frame_engine(cfg, 'S')
        with mock.patch('pipeline.scene_video.generate_with_engine') as gen:
            ensure_first_frame(scene, wd, 832, 480, comfy_url='http://worker', image_engine=engine, cfg=cfg)
        self.assertIn('Ana appears EXACTLY as the character in <Picture 1>', gen.call_args.args[1])
        self.assertEqual(gen.call_args.kwargs['reference_images'], [d / f'{cid}.png'])
        scene.metadata_extra['no_first_frame'] = True
        with mock.patch.object(rg, 'generate_with_engine') as gen:
            self.assertIsNone(rg.ensure_opening_frame(scene, wd, cfg, comfy_url='http://worker',
                                                      vid_width=832, vid_height=480, style_name='S'))
        gen.assert_not_called()


@pytest.mark.parametrize('size,expected', [('video', (832, 480)), ('4k', (3840, 2215))])
def test_worker_scene_image_uses_h3_and_extends_lease(tmp_path, size, expected):
    task = SimpleNamespace(id='image-task', job_id='job', payload={
        'work_dir': str(tmp_path), 'scene_id': 1, 'first_frame_source': 'h3',
        'h3_image_size': size, 'image_prompt': 'An empty room.', 'width': 832, 'height': 480})
    store = mock.Mock()
    store.skip_task_if_artifact_exists.return_value = False
    with mock.patch.object(worker_agent, 'generate_with_engine') as gen, \
         mock.patch.object(app, 'load_config', return_value={}):
        worker_agent._execute_scene_image(store, task, 'http://worker')
    assert gen.call_args.args[0]['family'] == 'h3-image'
    store.heartbeat_task.assert_called_once_with(task.id, lease_seconds=14400)

    assert gen.call_args.args[0]['h3_image_size'] == size
    assert store.record_artifact.call_args.kwargs['width'] == expected[0]
    assert store.record_artifact.call_args.kwargs['height'] == expected[1]


def test_old_h3_setting_now_resolves_image_at_video_size():
    engine = engines.resolve_first_frame({'first_frame_source': 'h3'})
    assert engine['key'] == 'minimax-h3-image'
    assert engine['h3_image_size'] == 'video'
    assert app.DEFAULT_CFG['default_first_frame_source'] == 'image'
    assert app._norm_h3_image_size('4K') == '4k'
    assert app._norm_h3_image_size('invalid') == 'video'


def test_h3_scene_only_bundle_is_downloadable_but_not_a_general_image_choice():
    engine = engines.get('minimax-h3-image')
    assert len(engine['models']) == 3
    assert app._norm_engine(engine['key'], 'generate') == engines.DEFAULT_ENGINE
    assert app._norm_engine(engine['key'], 'edit') == engines.DEFAULT_ENGINE
    public = next(e for e in engines.public_list() if e['key'] == engine['key'])
    assert public['scene_only'] is True
    assert public['license_note']


@pytest.mark.parametrize('missing', [*engines.get('minimax-h3-image')['extra_probes'], 'node', 'version', None])
def test_h3_readiness_needs_companions_native_node_and_current_worker(missing):
    url = 'http://worker:8188'
    with mock.patch.object(app, 'load_config', return_value={'comfy_workers': [url]}), \
         mock.patch('pipeline.worker_pool.queue_depth', return_value=0), \
         mock.patch.object(comfyui, 'engine_model_present', side_effect=lambda u, p: p != missing), \
         mock.patch.object(comfyui, 'comfy_node_exists', side_effect=lambda u, n: not (missing == 'node' and n == 'MiniMaxH3ImageConditioning')), \
         mock.patch.object(comfyui, 'comfyui_version', return_value=(0, 42, 5) if missing == 'version' else (0, 45, 0)):
        result = backend.list_engines()
    assert result['availability']['minimax-h3-image'] is (missing is None)
    if missing in ('node', 'version'):
        assert result['worker_updates']['minimax-h3-image'] == url
