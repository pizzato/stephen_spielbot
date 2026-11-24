"""Official Suno generation/export, with resumable requests on the controller."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import requests

from pipeline import music_history, suno
from pipeline.assembler import _get_duration, transcode_to_wav


def _save(path: Path, value: dict) -> None:
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2))
    os.replace(tmp, path)


def _check(result: dict) -> dict:
    if result.get('error') or result.get('status') in {'error', 'failed', 'refused'}:
        raise RuntimeError(f"Suno: {result.get('error') or result.get('reason') or result.get('message') or result['status']}")
    return result


def _wait(ids: list[str], deadline: float) -> list[dict]:
    while time.monotonic() < deadline:
        result = _check(suno.call_tool('audio-wait', {'ids': ids, 'until': 'complete'}))
        clips = result.get('clips') or ([result] if result.get('id') else [])
        by_id = {c['id']: _check(c) for c in clips if c.get('id')}
        if all(by_id.get(cid, {}).get('status') == 'complete' for cid in ids):
            return [by_id[cid] for cid in ids]
        time.sleep(2)
    raise TimeoutError('Suno is still generating. Retry this request to resume the same songs.')


def _download(clip_id: str, target: Path, state: dict, state_path: Path,
              deadline: float) -> None:
    """Export through the official credit-gated tool, never preview URLs."""
    confirmed = state.setdefault('download_confirmed', [])
    while time.monotonic() < deadline:
        response = _check(suno.call_tool('audio-download', {
            'id': clip_id, 'format': 'mp3', 'confirm': False}))
        if response.get('status') == 'requires_credit':
            if clip_id in confirmed:
                raise RuntimeError('Suno download confirmation is unresolved. Check the song in Suno before retrying its export.')
            if float(response.get('credits_remaining') or 0) < 1:
                raise RuntimeError('No Suno download credits remain. The generated songs are saved in Suno.')
            # Persist before spending, including when the response is lost. A
            # later retry preflights again and never blindly repeats a charge.
            confirmed.append(clip_id)
            _save(state_path, state)
            response = _check(suno.call_tool('audio-download', {
                'id': clip_id, 'format': 'mp3', 'confirm': True}))
        url = response.get('url')
        if url:
            if not isinstance(url, str) or not url.startswith('https://'):
                raise RuntimeError('Suno returned an invalid download URL.')
            source = target.with_suffix('.mp3.part')
            staged = target.with_suffix('.staging.wav')
            try:
                with requests.get(url, stream=True, timeout=(15, 120)) as download:
                    download.raise_for_status()
                    size = 0
                    with source.open('wb') as out:
                        for chunk in download.iter_content(1024 * 1024):
                            size += len(chunk)
                            if size > 512 * 1024 * 1024:
                                raise RuntimeError('Suno audio export exceeds 512 MB.')
                            out.write(chunk)
                transcode_to_wav(source, staged)
                if _get_duration(staged) <= 0:
                    raise RuntimeError('Suno returned an empty audio export.')
                staged.replace(target)
            finally:
                source.unlink(missing_ok=True)
                staged.unlink(missing_ok=True)
            return
        if response.get('status') != 'processing':
            raise RuntimeError('Suno did not return an audio download URL.')
        time.sleep(2)
    raise TimeoutError('Suno export is still processing. Retry to resume the existing songs.')


def generate_suno_takes(title: str, duration: float | None, *, work_dir: Path,
                        caption: str | None = None, lyrics: str | None = None,
                        model: str = '', voice_id: str = '',
                        vocal_gender: str | None = None, max_mode: bool = False,
                        negative_style: str = '', controls: dict | None = None,
                        operation_id: str | None = None) -> dict:
    """Generate two remote previews, without exporting either audio file.

    Reusing operation_id resumes the same request after transport failures.
    Each deliberate new generation uses a fresh ID.
    """
    operation_id = operation_id or str(uuid.uuid4())
    key = str(uuid.uuid5(uuid.NAMESPACE_URL, 'spielbot:suno:' + operation_id))
    folder = Path(work_dir) / 'suno_requests' / key
    state_path = folder / 'request.json'
    has_lyrics = bool(lyrics and lyrics.strip())
    request = {'title': title, 'style': caption or 'cinematic instrumental',
               'instrumental': not has_lyrics,
               'idempotency_key': key}
    if duration is not None and 15 <= round(duration) <= 360:
        request.update(duration=round(duration), duration_guidance=True)
    if has_lyrics:
        request['lyrics'] = lyrics
        if voice_id:
            request['voice_id'] = voice_id
        elif vocal_gender:
            request['vocal_gender'] = vocal_gender
    if model:
        request['model'] = model
    if max_mode:
        request['max_mode'] = True
    if negative_style:
        request['negative_style'] = negative_style
    controls = {name: value for name, value in (controls or {}).items() if value is not None}
    if not request.get('voice_id'):
        controls.pop('audio_influence', None)
    if controls:
        request['controls'] = controls
    # A manual retry after losing audio-create's response must recover the
    # original key, even though the UI's new background task has a new ID.
    if not state_path.exists():
        params = {k: v for k, v in request.items() if k != 'idempotency_key'}
        for pending_path in (Path(work_dir) / 'suno_requests').glob('*/request.json'):
            pending = json.loads(pending_path.read_text())
            original = {k: v for k, v in pending['request'].items() if k != 'idempotency_key'}
            if not pending.get('clip_ids') and original == params:
                folder, state_path = pending_path.parent, pending_path
                request = pending['request']
                break
    folder.mkdir(parents=True, exist_ok=True)
    fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    with (folder / 'request.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if state and state.get('fingerprint') != fingerprint:
            raise ValueError('This Suno request already exists with different settings. Start a new generation.')
        if not state:
            usage = suno.account_usage()
            state = {'request': request, 'fingerprint': fingerprint, 'submitted_at': time.time(),
                     'plan_at_generation': (usage.get('suno_account') or {}).get('plan')}
            _save(state_path, state)
        if not state.get('clip_ids'):
            # Suno guarantees key retention for at least 24 hours only. An
            # ambiguous older submission needs reconciliation, not a new charge.
            if time.time() - state['submitted_at'] > 23 * 3600:
                raise RuntimeError('This Suno submission is over 23 hours old without saved song IDs. Check the Suno library before starting another generation.')
            created = _check(suno.call_tool('audio-create', state['request']))
            ids = created.get('clip_ids') or ([created['id']] if created.get('id') else [])
            if not ids:
                raise RuntimeError('Suno did not return the expected song IDs. Retry to resume this request.')
            state['clip_ids'] = ids
            _save(state_path, state)
        deadline = time.monotonic() + 20 * 60
        if not state.get('clips') or any(c.get('status') != 'complete' for c in state['clips']):
            state['clips'] = _wait(state['clip_ids'], deadline)
            _save(state_path, state)
        return {'clip_ids': state['clip_ids'], 'operation_id': operation_id,
                'suno_takes': _public_takes(state)}


def _public_takes(state: dict) -> list[dict]:
    known = {c['id']: c for c in state.get('clips', [])}
    clips = [known.get(cid, {'id': cid, 'status': 'pending'}) for cid in state.get('clip_ids', [])]
    return [{'id': c['id'], 'title': c.get('title') or state['request'].get('title', ''),
             'status': c.get('status'), 'song_url': c.get('song_url') or f'https://suno.com/song/{c["id"]}',
             'model': (c.get('metadata') or {}).get('model') or state['request'].get('model', ''),
             'duration': (c.get('metadata') or {}).get('duration'),
             'voice_id': state['request'].get('voice_id', '')}
            for c in clips if c.get('id')]


def list_suno_takes(work_dir: Path) -> list[dict]:
    takes = []
    for path in sorted((Path(work_dir) / 'suno_requests').glob('*/request.json')):
        state = json.loads(path.read_text())
        takes.extend(_public_takes(state))
    imported = {v.get('remote_id'): v for v in music_history.history(work_dir)['versions']
                if v.get('provider') == 'suno'}
    for take in takes:
        take['imported'] = take['id'] in imported
    return takes


def request_for_take(work_dir: Path, clip_id: str) -> dict:
    """Saved words and sound that belong to a selected remote take."""
    for path in (Path(work_dir) / 'suno_requests').glob('*/request.json'):
        state = json.loads(path.read_text())
        if clip_id in state.get('clip_ids', []):
            return state['request']
    return {}


def download_suno_take(work_dir: Path, clip_id: str) -> dict:
    """An explicit Use-this-take action authorizes one official export."""
    wd = Path(work_dir)
    for path in (wd / 'suno_requests').glob('*/request.json'):
        with path.with_name('request.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads(path.read_text())
            if clip_id not in state.get('clip_ids', []):
                continue
            known = {c['id']: c for c in state.get('clips', [])}
            if known.get(clip_id, {}).get('status') != 'complete':
                # A failed sibling must not block the usable take the user chose.
                known[clip_id] = _wait([clip_id], time.monotonic() + 20 * 60)[0]
                state['clips'] = list(known.values())
                _save(path, state)
            target = path.parent / f'take_{state["clip_ids"].index(clip_id) + 1}.wav'
            if not target.exists():
                _download(clip_id, target, state, path, time.monotonic() + 20 * 60)
            clip = next(c for c in state['clips'] if c['id'] == clip_id)
            take = {'path': str(target), 'remote_id': clip_id,
                    'model': (clip.get('metadata') or {}).get('model') or state['request'].get('model', ''),
                    'voice_id': state['request'].get('voice_id', '')}
            return {'clip_ids': state['clip_ids'], 'takes': [take], 'request': state['request']}
    raise ValueError('This Suno take does not belong to this film.')


def import_suno_takes(work_dir: Path, result: dict, caption: str = '') -> dict:
    """Keep all downloaded takes, select the first; repeat imports are harmless."""
    wd = Path(work_dir)
    music_history.seed_if_empty(wd, wd / 'background_music.wav', caption)
    first = None
    for i, take in enumerate(result['takes']):
        hist = music_history.history(wd)
        match = next((v for v in hist['versions']
                      if v.get('provider') == 'suno' and v.get('remote_id') == take['remote_id']), None)
        if match is None:
            hist = music_history.record(
                wd, Path(take['path']), f'Suno take {i + 1} · {caption}',
                provider='suno', remote_id=take['remote_id'],
                model=take.get('model', ''), singing_voice_id=take.get('voice_id', ''))
            vid = hist['selected']
        else:
            vid = match['id']
        if first is None:
            first = vid
    if first is not None:
        music_history.select(wd, first)
    return music_history.history(wd)

