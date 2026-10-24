"""H3 scene openings: real frame-zero extraction, references and persisted routing."""
import json
import shutil
import subprocess
from types import SimpleNamespace
from unittest import mock

import pytest
from PIL import Image

import app
import resume_generation as rg
import webapp.backend.main as backend
import worker_agent
from pipeline import assembler, comfyui, engines
from pipeline.llm import Scene
from pipeline.orchestrator import DurableStore, job_id_from_work_dir, task_id
from pipeline.scene_video import ensure_first_frame
from test_styles import TempConfigCase, _style


@pytest.fixture
def two_colour_clip(tmp_path):
    """A distinct first and last half catches accidentally extracting the tail."""
    clip = tmp_path / 'source.mp4'
    subprocess.run([
        assembler._FFMPEG, '-y', '-loglevel', 'error',
        '-f', 'lavfi', '-i', 'color=red:s=64x64:r=24:d=0.5',
        '-f', 'lavfi', '-i', 'color=blue:s=64x64:r=24:d=0.5',
        '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clip),
    ], check=True, capture_output=True)
    return clip


@pytest.mark.parametrize('use_refs', [False, True])
def test_h3_extracts_frame_zero_at_requested_size(tmp_path, two_colour_clip, use_refs):
    captured = {}

    def submit(workflow, path, **kw):
        captured.update(workflow=workflow, clip=path, **kw)
        shutil.copy2(two_colour_clip, path)
        return path

    engine = engines.resolve_first_frame({'first_frame_source': 'h3'})
    out = tmp_path / 'opening.png'
    with mock.patch.object(comfyui, '_h3_submit', side_effect=submit), \
         mock.patch.object(comfyui, '_upload_image', return_value='portrait.png') as upload, \
         mock.patch.object(comfyui, 'generate_scene_image') as image_model:
        comfyui.generate_with_engine(
            engine, 'Ana from <Picture 1> waits at the window.', out,
            width=1000, height=568, seed=7,
            reference_images=[tmp_path / 'portrait.png'] if use_refs else [],
            comfy_url='http://worker:8188')
    with Image.open(out) as im:
        assert im.size == (1000, 568)
        red, green, blue = im.getpixel((500, 284))
        assert red > 240 and green < 10 and blue < 10
    assert not captured['clip'].exists()  # intermediate video is temporary
    assert captured['length'] == 22
    wf = captured['workflow']
    assert wf['9']['inputs']['steps'] == 4
    assert wf['10']['inputs']['noise_seed'] == 7
    assert (wf['7']['inputs']['width'], wf['7']['inputs']['height']) == (992, 544)
    assert wf['7']['inputs']['length'] == 22
    assert '{{' not in json.dumps(wf)
    if use_refs:
        assert wf['7']['class_type'] == 'MiniMaxH3ReferenceToVideo'
        assert wf['7']['inputs']['ref_images.ref_image_0']
        assert wf['17']['inputs']['strength'] == 0.6
        upload.assert_called_once()
    else:
        assert wf['7']['class_type'] == 'MiniMaxH3ImageToVideo'
        assert 'first_frame' not in wf['7']['inputs']
        assert not any(n['class_type'] == 'LoadImage' for n in wf.values())
        upload.assert_not_called()
    image_model.assert_not_called()


def test_failed_h3_request_preserves_existing_image(tmp_path):
    out = tmp_path / 'opening.png'
    out.write_bytes(b'previous selected frame')
    engine = engines.resolve_first_frame({'first_frame_source': 'h3'})
    with mock.patch.object(comfyui, '_h3_submit', side_effect=RuntimeError('missing model')), \
         pytest.raises(RuntimeError, match='missing model'):
        comfyui.generate_with_engine(engine, 'p', out, width=832, height=480)
    assert out.read_bytes() == b'previous selected frame'


class FirstFrameSourceSettingsTests(TempConfigCase):
    def test_inherited_source_persists_and_render_job_stamps_it(self):
        self.write_config({'styles': [_style('A'),
                                     _style('H3', first_frame_source='h3'),
                                     {'name': 'Child', 'parent': 'H3'}],
                           'default_style': 'A'})
        cfg = app.load_config()
        app.save_config(cfg)
        cfg = app.load_config()
        self.assertEqual(app.scene_frame_engine(cfg, 'Child')['family'], 'h3-first-frame')
        self.assertNotIn('first_frame_source', next(s for s in cfg['styles'] if s['name'] == 'Child'))
        self.assertEqual(app.scene_frame_engine(cfg, 'A')['family'], 'flux2')
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
        self.assertEqual(engines.resolve(cfg, jc['image_engine'])['family'], 'flux2')
        store = DurableStore.default()
        try:
            store.ensure_generation_plan(jid, wd, 'H3 source',
                                         [Scene(id=1, title='One', image_prompt='a frame', video_prompt='v', narration='words')],
                                         {**jc, 'vid_width': 832, 'vid_height': 480})
            payload = store.get_task(task_id(jid, 'scene', 1, 'image')).payload
        finally:
            store.close()
        self.assertEqual(payload['first_frame_source'], 'h3')

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


def test_worker_scene_image_uses_h3_and_extends_lease(tmp_path):
    task = SimpleNamespace(id='image-task', job_id='job', payload={
        'work_dir': str(tmp_path), 'scene_id': 1, 'first_frame_source': 'h3',
        'image_prompt': 'An empty room.', 'width': 832, 'height': 480})
    store = mock.Mock()
    store.skip_task_if_artifact_exists.return_value = False
    with mock.patch.object(worker_agent, 'generate_with_engine') as gen, \
         mock.patch.object(app, 'load_config', return_value={}):
        worker_agent._execute_scene_image(store, task, 'http://worker')
    assert gen.call_args.args[0]['family'] == 'h3-first-frame'
    store.heartbeat_task.assert_called_once_with(task.id, lease_seconds=14400)
