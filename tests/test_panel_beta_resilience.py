"""Beta regressions: isolated catalog failures, local reading and MCP setup."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pku_sync.config import Settings
from pku_sync.mcp_tools import LocalCourseTools
from pku_sync.panel import notion_home, notion_workspace, recordings_api
from pku_sync.panel.student_ui import PANEL_COPY
from pku_sync.pipeline import collect_jobs


def seed(root, name='Networks', course_id='healthy'):
    folder = root / name
    (folder / 'recordings').mkdir(parents=True)
    (folder / 'course.json').write_text(json.dumps({'course_id': course_id, 'name': name, 'code': name}), 'utf8')
    (folder / 'recordings/index.json').write_text(json.dumps([
        {'course_id': course_id, 'title': '第1讲 TCP', 'recorded_at': '2026-10-01 08:00:00'}]), 'utf8')
    job = next(job for job in collect_jobs(root, course_id) if job.course_dir == folder)
    job.directory.mkdir(parents=True, exist_ok=True)
    (job.directory / 'notes.md').write_text(
        '## 课堂重点\n### TCP 可靠传输\n- 确认与重传\n  - 滑动窗口\n'
        '## 作业与考试口头线索\n- 下周复习拥塞控制\n', 'utf8')
    return folder, job


def api(root):
    settings = Settings(
        _env_file=None,
        data_dir=root,
        notion_token='fixture-notion',
        openai_base_url='https://api.deepseek.com/v1',
        openai_api_key='test-key',
        transcription_backend='openai',
        pku_username='fixture-student',
    )
    app = FastAPI()
    recordings_api.add_recording_routes(app, SimpleNamespace(settings=settings))
    return TestClient(app), settings


def batch_fixture(root):
    from pku_sync import catalog_snapshots
    from pku_sync.models import Course
    items = []
    for course_id in ('net', 'math'):
        folder, _ = seed(root, course_id, course_id)
        (folder / 'recordings/index.json').write_text(json.dumps([
            {'course_id': course_id, 'title': '第1-2节', 'recorded_at': '2026-10-01 08:00:00'},
            {'course_id': course_id, 'title': '第3-5节', 'recorded_at': '2026-10-02 08:00:00'}]), 'utf8')
        items.extend({'course_id': course_id, 'recording_id': recordings_api.recording_id(job)}
                     for job in collect_jobs(root, course_id))
        catalog_snapshots.save_cache(root, {'revision': 1, 'snapshot': catalog_snapshots.build_snapshot(
            Course(course_id=course_id, name=course_id),
            [job.recording for job in collect_jobs(root, course_id)], [], [], [])})
    settings = Settings(
        _env_file=None,
        data_dir=root,
        pku_username='fixture-student',
        notion_token='',
        openai_base_url='https://api.deepseek.com/v1',
        openai_api_key='test-key',
        transcription_backend='openai',
    )
    manager = recordings_api.RecordingWorkManager(settings)
    app = FastAPI()
    recordings_api.add_recording_routes(app, manager)
    return TestClient(app), manager, items


def wait_for_batch(manager):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        snapshot = manager.state().get('batch_organize', {})
        if snapshot.get('state') != 'running':
            return snapshot
        time.sleep(.01)
    pytest.fail('Batch worker did not finish')


@pytest.mark.parametrize('model,points', [
    ('qwen3.5-flash', (1, 2)), ('qwen3.7-plus', (2, 4)),
    ('qwen3.8-max', (10, 18)), ('qwen3.8-max-0902', (10, 18))])
def test_batch_organize_preview_estimates(tmp_path, model, points):
    client, manager, _ = batch_fixture(tmp_path)
    manager.settings.notes_model = model
    jobs = list(collect_jobs(tmp_path, 'net'))
    jobs[0].directory.mkdir(parents=True, exist_ok=True)
    (jobs[0].directory / 'duration.json').write_text('{"duration_seconds": 3601}', 'utf8')
    (jobs[0].directory / 'transcript.json').write_text('{"segments": []}', 'utf8')
    payload = client.get('/api/campus/batch-organize/preview').json()
    assert payload['notes_model'] == model
    assert len(payload['courses']) == 2
    by_id = {lesson['id']: lesson for course in payload['courses'] for lesson in course['lessons']}
    cached = by_id[recordings_api.recording_id(jobs[0])]
    assert cached['duration_seconds'] == cached['estimated_duration_seconds'] == 3601
    assert cached['estimated_transcribe_minutes'] == 0
    assert cached['duration_is_estimated'] is False
    assert cached['transcript_available'] and cached['needs_organize']
    unknown = by_id[recordings_api.recording_id(jobs[1])]
    assert unknown['estimated_duration_seconds'] == 8100
    assert unknown['estimated_transcribe_minutes'] == 135
    assert unknown['duration_is_estimated'] and unknown['eligible']
    for lesson in by_id.values():
        assert (lesson['estimated_points_min'], lesson['estimated_points_max']) == (0, 0)
    (jobs[0].directory / 'transcript.json').unlink()
    payload = client.get('/api/campus/batch-organize/preview').json()
    known = next(lesson for course in payload['courses'] for lesson in course['lessons']
                 if lesson['id'] == recordings_api.recording_id(jobs[0]))
    assert known['estimated_transcribe_minutes'] == 61
    assert not known['duration_is_estimated']


def test_batch_preview_duration_fallback_and_readonly(monkeypatch, tmp_path):
    from pku_sync.panel import campus_catalog
    course = {'id': 'net', 'title': 'Networks', 'read_only': True, 'read_only_message': '只读',
              'lessons': [{'id': str(i), 'title': title} for i, title in enumerate(
                  ('无课时', '第0-2节', '第3-1节', '第2-4节'))]}
    monkeypatch.setattr(campus_catalog, 'campus_courses', lambda _: {'courses': [{'id': 'net'}]})
    monkeypatch.setattr(campus_catalog, 'campus_course', lambda *args: course)
    result = recordings_api.batch_organize_preview(Settings(_env_file=None, data_dir=tmp_path))
    lessons = result['courses'][0]['lessons']
    assert [lesson['estimated_transcribe_minutes'] for lesson in lessons] == [90, 90, 90, 135]
    assert all(not lesson['eligible'] and not lesson['needs_organize'] for lesson in lessons)
    course['read_only'] = False
    course['lessons'][0]['unavailable_reason'] = '录像不可用'
    course['lessons'][1]['notes_available'] = True
    lessons = recordings_api.batch_organize_preview(Settings(_env_file=None, data_dir=tmp_path))['courses'][0]['lessons']
    assert not lessons[0]['eligible'] and not lessons[0]['needs_organize']
    assert lessons[1]['eligible'] and not lessons[1]['needs_organize']


def test_batch_organize_parallel_courses_and_progress(tmp_path, monkeypatch):
    client, manager, items = batch_fixture(tmp_path)
    rendezvous = threading.Barrier(2)
    release = threading.Event()
    both_started = threading.Event()
    calls, active, maximum = [], set(), []
    lock = threading.Lock()

    def start(child, key, course_id, **kwargs):
        assert child._writes is manager._writes
        assert kwargs == {'direct_oss': True, 'regenerate': False, 'notes_strategy': 'transcript'}
        def action():
            with lock:
                assert course_id not in active
                first = not any(course == course_id for course, _ in calls)
                active.add(course_id)
                calls.append((course_id, key))
                maximum.append(len(active))
            try:
                if first:
                    rendezvous.wait(5)
                    both_started.set()
                    assert release.wait(5)
                child._stage('正在生成笔记')
                return '/notes/' + key
            finally:
                with lock:
                    active.remove(course_id)
        return child._start('process', key, action, course_id=course_id)

    monkeypatch.setattr(recordings_api.RecordingWorkManager, 'start_campus_process', start)
    try:
        response = client.post('/api/campus/batch-organize', json={
            'items': items + items[:1], 'notes_strategy': 'transcript', 'max_parallel_courses': 2})
        assert response.status_code == 200
        assert response.json()['progress_total'] == 4  # duplicate excluded
        assert both_started.wait(5)
        snapshot = manager.state()['batch_organize']
        assert snapshot['state'] == 'running' and snapshot['active_courses'] == 2
        assert sum(row['state'] == 'running' for row in snapshot['items']) == 2
        snapshot['items'][0]['title'] = 'cannot mutate manager'
        assert manager.state()['batch_organize']['items'][0]['title'] != 'cannot mutate manager'
        assert client.post('/api/campus/batch-organize', json={'items': items}).status_code == 400
        with pytest.raises(ValueError, match='这门课'):
            manager._start('process', 'conflict', lambda: '', course_id='net')
    finally:
        release.set()
    result = wait_for_batch(manager)
    assert result['state'] == 'done'
    assert result['progress_done'] == result['progress_total'] == 4
    assert result['progress_remaining'] == 0 and result['progress_percent'] == 100
    assert maximum and max(maximum) == 2
    assert all(row['state'] == 'done' and row['result_url'] for row in result['items'])
    for course_id in ('net', 'math'):
        assert [key for course, key in calls if course == course_id] == [
            item['recording_id'] for item in items if item['course_id'] == course_id]
    assert manager._writes.owners == {} and manager._batch_children == {}


@pytest.mark.parametrize('outcome', ['cancel', 'account-change', 'partial', 'failed'])
def test_batch_organize_cancellation_and_failures(tmp_path, monkeypatch, outcome):
    client, manager, items = batch_fixture(tmp_path)
    started, release = threading.Event(), threading.Event()
    calls = []

    def start(child, key, course_id, **kwargs):
        def action():
            calls.append(key)
            started.set()
            assert release.wait(5)
            child._stage('正在整理')  # real owner/cancellation boundary
            if outcome == 'failed' or outcome == 'partial' and course_id == 'net':
                raise ValueError('fixture failure; cached transcript retained')
            return '/notes/' + key
        return child._start('process', key, action, course_id=course_id)

    monkeypatch.setattr(recordings_api.RecordingWorkManager, 'start_campus_process', start)
    body = recordings_api.BatchOrganizeRequest(items=items, max_parallel_courses=1)
    try:
        manager.start_batch_organize(body)
        assert started.wait(5)
        if outcome == 'cancel':
            assert client.post('/api/campus/batch-organize/cancel').status_code == 200
            assert all(child._cancel_sync.is_set() for child in manager._batch_children.values())
        elif outcome == 'account-change':
            manager.settings.pku_username = 'other-student'
            assert 'batch_organize' not in manager.state()
            assert client.post('/api/campus/batch-organize/cancel').status_code == 400
    finally:
        release.set()
    if outcome == 'account-change':
        deadline = time.monotonic() + 5
        while manager._batch_organize['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        assert manager._batch_organize['state'] == 'cancelled'
        manager.settings.pku_username = 'fixture-student'
    result = wait_for_batch(manager)
    assert result['state'] == {'cancel': 'cancelled', 'account-change': 'cancelled',
                               'partial': 'partial', 'failed': 'failed'}[outcome]
    assert result['progress_remaining'] == 0 and result['active_courses'] == 0
    if outcome in ('cancel', 'account-change'):
        assert len(calls) == 1
        assert all(row['state'] == 'cancelled' for row in result['items'])
    assert manager._writes.owners == {}


def test_batch_organize_validation(tmp_path):
    client, manager, items = batch_fixture(tmp_path)
    assert client.post('/api/campus/batch-organize', json={'items': []}).status_code == 400
    for value in (0, 7):
        assert client.post('/api/campus/batch-organize', json={
            'items': items, 'max_parallel_courses': value}).status_code == 422
    assert client.post('/api/campus/batch-organize', json={
        'items': [{'course_id': 'other', 'recording_id': items[0]['recording_id']}]}).status_code == 400
    manager.settings.openai_api_key = ''
    response = client.post('/api/campus/batch-organize', json={'items': items})
    assert response.status_code == 400 and '模型配置' in response.json()['detail']


def test_corrupted_course_does_not_poison_healthy_courses(tmp_path, monkeypatch):
    seed(tmp_path)
    broken, _ = seed(tmp_path, 'Unprojected', 'unprojected')
    damaged, _ = seed(tmp_path, 'Corrupted', 'corrupted')
    (damaged / 'course.json').write_text('{broken', 'utf8')
    for section in ('materials', 'assignments', 'announcements'):
        (broken / section).mkdir()
        (broken / section / 'index.json').write_text('{broken', 'utf8')
    # A valid JSON scalar must degrade just as safely as invalid JSON.
    (damaged / 'recordings/index.json').write_text('null', 'utf8')
    client, settings = api(tmp_path)
    assert client.get('/api/campus/courses').status_code == 200
    assert client.get('/api/campus/courses/healthy').json()['title'] == 'Networks'
    assert client.get('/api/campus/courses/healthy/summary-preview').status_code == 200
    assert client.get('/api/campus/courses/unprojected').status_code == 200
    assert LocalCourseTools(tmp_path).get_course_summary('healthy')['summarized_lessons'] == 1

    workspace = {'schema': 2, 'active': True, 'id': 'home', 'parent_id': 'parent',
                 'courses': {'unprojected': {'id': 'old', 'lectures': {'retained': {}}}}}
    notion_workspace.manifest_path(tmp_path).write_text(json.dumps(workspace), 'utf8')
    from pku_sync import catalog_snapshots
    monkeypatch.setattr(catalog_snapshots, 'activate_account', lambda _: None)
    monkeypatch.setattr(catalog_snapshots, 'cached', lambda *args: None)
    # Only projection eligibility needs a snapshot; catalog endpoints retain real local reads.
    monkeypatch.setattr(notion_workspace, '_bind_history', lambda *args: ({'lessons': []}, None))
    monkeypatch.setattr(notion_workspace, '_verify_home', lambda *args: None)
    monkeypatch.setattr(notion_workspace, '_verify_historical_course', lambda *args: None)
    monkeypatch.setattr(notion_workspace, '_classify_course_children',
                        lambda *args: ({'每节课': {'id': 'center'}}, []))
    observed = []

    class Notion:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get_page(self, key): return {'id': key}
        def list_children(self, key): return []
        def list_child_pages(self, key):
            return [{'id': 'center', 'title': '每节课'}] if key == 'course-good' else []

    def course_page(client, home, row, **kwargs):
        observed.append(row['id'])
        if row['id'] == 'unprojected':
            raise ValueError('Course projection missing')
        return {'id': 'course-good'}

    monkeypatch.setattr(notion_workspace, 'NotionClient', lambda _: Notion())
    monkeypatch.setattr(notion_workspace, '_course_page', course_page)
    monkeypatch.setattr(notion_workspace, 'campus_courses', lambda _: {'courses': [
        {'id': 'unprojected'}, {'id': 'healthy'}]})
    monkeypatch.setattr(notion_workspace, 'campus_course', lambda *args: {'lessons': []})
    monkeypatch.setattr(catalog_snapshots, 'cached', lambda *args: {'projection_fixture': True})
    monkeypatch.setattr(notion_home, 'available_parents', lambda _: [{'id': 'parent'}])
    with pytest.raises(ValueError, match='projection missing'):
        notion_workspace.read_projection(settings)
    response = client.get('/api/campus/notion/parents')
    assert response.status_code == 200, response.text
    result = response.json()['workspace']
    assert result['courses']['healthy']['centers']['每节课']['id'] == 'center'
    assert result['courses']['unprojected'] == workspace['courses']['unprojected']
    assert observed[-2:] == ['unprojected', 'healthy']


def test_in_app_note_and_summary_preview_endpoints(tmp_path):
    _, job = seed(tmp_path)
    client, _ = api(tmp_path)
    key = recordings_api.recording_id(job)
    note = client.get(f'/api/campus/courses/healthy/recordings/{key}/note-preview')
    assert note.status_code == 200
    assert note.json()['available'] is True
    assert note.json()['topics'] == ['TCP 可靠传输']
    assert note.json()['clues'] == ['下周复习拥塞控制']
    assert '滑动窗口' in note.json()['markdown']
    summary = client.get('/api/campus/courses/healthy/summary-preview').json()
    assert summary['total_lessons'] == summary['summarized_lessons'] == 1
    assert 'TCP 可靠传输' in summary['markdown']
    assert client.get(f'/api/campus/courses/other/recordings/{key}/note-preview').status_code == 404
    assert client.get('/api/campus/courses/missing/summary-preview').status_code == 404
    (job.directory / 'notes.md').unlink()
    assert client.get(f'/api/campus/courses/healthy/recordings/{key}/note-preview').json()['available'] is False


def test_catalog_note_topics_are_bounded_and_tolerate_unreadable_notes(tmp_path, monkeypatch):
    from pku_sync.panel.campus_catalog import campus_course
    _, job = seed(tmp_path)
    notes = job.directory / 'notes.md'
    notes.write_text('## 课堂重点\n### TCP\n### 滑动窗口\n### 拥塞控制\n', 'utf8')
    assert campus_course(tmp_path, 'healthy')['lessons'][0]['note_topic'] == 'TCP · 滑动窗口'
    read_text = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == notes:
            raise OSError('note temporarily unavailable')
        return read_text(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_text', unreadable)
        assert campus_course(tmp_path, 'healthy')['lessons'][0]['note_topic'] == ''
    notes.unlink()
    assert campus_course(tmp_path, 'healthy')['lessons'][0]['note_topic'] == ''


def test_mcp_semantic_tools_and_panel_hub(tmp_path):
    folder, job = seed(tmp_path)
    tools = LocalCourseTools(tmp_path)
    for query in ('healthy', 'Networks', 'network'):
        assert tools.get_course_summary(query)['summarized_lessons'] == 1
    assert tools.get_course_summary()['courses'][0]['total_lessons'] == 1
    assert tools.get_lecture_notes('healthy', '2026-10-01')[0]['title'] == job.recording.title
    assert tools.get_lecture_notes('healthy', 'absent') == []
    assert tools.search_course_knowledge('TCP')[0]['source_type'] == 'notes'
    for section in ('assignments', 'announcements'):
        (folder / section).mkdir()
        (folder / section / 'index.json').write_text(json.dumps([
            {'title': 'TCP deadline', 'instructions': 'TCP exercises', 'private_url': 'do-not-expose'}]), 'utf8')
    (job.directory / 'transcript.json').write_text(json.dumps({'segments': [{'text': 'TCP lecture'}]}), 'utf8')
    hits = tools.search_course_knowledge('tcp')
    assert {row['source_type'] for row in hits} == {'notes', 'assignments', 'announcements', 'transcript'}
    assert 'do-not-expose' not in json.dumps(hits)
    assert tools.search_course_knowledge(' ') == []
    client, _ = api(tmp_path)
    status = client.get('/api/mcp/status').json()
    assert status['ok'] and status['courses_count'] == status['notes_count'] == 1
    assert {host['id'] for host in status['hosts']} == {'codex', 'claude', 'antigravity', 'oh-my-pi', 'opencode', 'deepseek-dsh'}
    assert '\\' not in status['launcher_path'] + status['python_path']
    for host in status['hosts']:
        assert tmp_path.as_posix() in host['config_snippet']
        if host['id'] != 'codex':
            json.loads(host['config_snippet'])
    import tomllib
    codex = next(host for host in status['hosts'] if host['id'] == 'codex')
    assert tomllib.loads(codex['config_snippet'])['mcp_servers']['pku-sync']['env']['DATA_DIR'] == tmp_path.as_posix()


@pytest.mark.parametrize('metadata', ['catalog', 'recording', 'missing', 'malformed', 'scalar'])
def test_knowledge_search_lecture_identity_and_source_priority(tmp_path, metadata):
    folder = tmp_path / '计算机网络'
    recordings = folder / 'recordings'
    recordings.mkdir(parents=True)
    (folder / 'course.json').write_text(json.dumps({'course_id': 'net', 'name': '计算机网络'}), 'utf8')
    entry = {'course_id': 'net', 'title': '第三讲：可靠传输', 'recorded_at': '2026-10-08 08:00:00'}
    (recordings / 'index.json').write_text(json.dumps([entry] if metadata == 'catalog' else []), 'utf8')
    directory = collect_jobs(tmp_path, 'net')[0].directory if metadata == 'catalog' else recordings / 'opaque-id'
    directory.mkdir(parents=True)
    if metadata in {'recording', 'malformed', 'scalar'}:
        value = json.dumps(entry) if metadata == 'recording' else '{broken' if metadata == 'malformed' else 'null'
        (directory / 'recording.json').write_text(value, 'utf8')
    text = '滑动窗口 可靠传输 TCP'
    (directory / 'notes.md').write_text(text, 'utf8')
    (directory / 'transcript.json').write_text(json.dumps({'segments': [{'text': text}]}), 'utf8')
    for section in ('assignments', 'announcements'):
        (folder / section).mkdir()
        (folder / section / 'index.json').write_text(json.dumps([{'instructions': text}]), 'utf8')
    tools = LocalCourseTools(tmp_path)
    hits = tools.search_course_knowledge('滑动窗口，可靠传输；tcp', 'net')
    assert [hit['source_type'] for hit in hits] == ['notes', 'assignments', 'announcements', 'transcript']
    for hit in (hits[0], hits[-1]):
        assert hit['title'] == (entry['title'] if metadata in {'catalog', 'recording'} else directory.name)
        if metadata in {'catalog', 'recording'}:
            assert hit['date'] == '2026-10-08'
        else:
            assert 'date' not in hit
    # Content relevance still takes priority over the source-type tie breaker.
    (directory / 'transcript.json').write_text(json.dumps({'text': text + ' ' + text}), 'utf8')
    assert tools.search_course_knowledge('TCP')[0]['source_type'] == 'transcript'


@pytest.mark.parametrize('separator', [' ', '\n', ',', '，', '、', '。', ';', '；', ':', '：', '!', '！', '?', '？', '/', '|'])
def test_knowledge_search_splits_chinese_query_punctuation(tmp_path, separator):
    _, job = seed(tmp_path)
    (job.directory / 'notes.md').write_text('滑动窗口和可靠传输', 'utf8')
    tools = LocalCourseTools(tmp_path)
    hits = tools.search_course_knowledge(separator.join(['滑动窗口', '可靠传输']))
    assert len(hits) == 1 and hits[0]['source_type'] == 'notes'
    assert hits[0]['title'] == job.recording.title
    assert tools.search_course_knowledge(separator * 2) == []


def test_knowledge_search_source_priority_across_courses(tmp_path):
    first, first_job = seed(tmp_path, 'Alpha', 'alpha')
    second, second_job = seed(tmp_path, 'Beta', 'beta')
    for job in (first_job, second_job):
        (job.directory / 'notes.md').write_text('可靠传输', 'utf8')
    (first_job.directory / 'transcript.json').write_text(json.dumps({'text': '可靠传输'}), 'utf8')
    for folder, section in ((first, 'assignments'), (second, 'announcements')):
        (folder / section).mkdir()
        (folder / section / 'index.json').write_text(json.dumps([{'text': '可靠传输'}]), 'utf8')
    hits = LocalCourseTools(tmp_path).search_course_knowledge('可靠传输')
    assert [(hit['source_type'], hit['course']) for hit in hits] == [
        ('notes', 'Alpha'), ('notes', 'Beta'), ('assignments', 'Alpha'),
        ('announcements', 'Beta'), ('transcript', 'Alpha')]


def test_retired_match_slides_route_returns_410(tmp_path):
    _, job = seed(tmp_path)
    client, _ = api(tmp_path)
    key = recordings_api.recording_id(job)
    assert client.post(f'/api/campus/courses/healthy/recordings/{key}/match-slides').status_code == 410


def test_keyframe_matching_without_material_index_is_noop(tmp_path, monkeypatch):
    from pku_sync import lecture_source_download as sources
    _, job = seed(tmp_path)
    job.video.write_bytes(b'dummy video')
    def unexpected(*args, **kwargs):
        pytest.fail('no materials must not trigger extraction or network access')
    monkeypatch.setattr(sources, 'extract_keyframes', unexpected)
    monkeypatch.setattr('pku_sync.auth.get_session', unexpected)
    assert sources.match_materials_by_keyframes(job, recordings_api.recording_id(job)) == []


@pytest.mark.parametrize('index_shape', ['dict', 'list'])
def test_note_visuals_serve_board_and_render_slide_by_catalog_title(tmp_path, index_shape):
    from test_slide_alignment import _write_pdf, _deck_rectangles
    import cv2
    import numpy as np
    folder, job = seed(tmp_path)
    frames = job.directory / 'keyframes'
    frames.mkdir()
    # A dark board deliberately scores below the publication cutoff.
    board = np.full((180, 320, 3), (38, 65, 42), dtype=np.uint8)
    cv2.putText(board, 'CIA', (25, 80), cv2.FONT_HERSHEY_SIMPLEX, 1, (230, 240, 230), 2)
    (frames / 'board.png').write_bytes(cv2.imencode('.png', board)[1].tobytes())
    rows = [{'file': 'board.png', 'timestamp': 755}]
    (frames / 'index.json').write_text(json.dumps({'keyframes': rows} if index_shape == 'dict' else rows), 'utf8')
    title = '01-信息安全引论与CIA三元组.pdf'
    materials = folder / 'materials'
    materials.mkdir()
    _write_pdf(materials / 'download.pdf', _deck_rectangles(2))
    (materials / 'index.json').write_text(json.dumps([{'title': title, 'files': ['download.pdf']}]), 'utf8')
    (frames / 'slide-alignment.json').write_text(json.dumps({'anchors': [
        {'start': 700, 'end': 800, 'title': title, 'page': 2}]}), 'utf8')
    (job.directory / 'notes.md').write_text('## 课堂重点\n### CIA [12:00–14:00]\n- 威胁矩阵\n## 复习建议\n- 复习', 'utf8')
    client, _ = api(tmp_path)
    key = recordings_api.recording_id(job)
    base = f'/api/campus/courses/healthy/recordings/{key}'
    response = client.get(base + '/note-preview')
    assert response.status_code == 200, response.text
    visual, = response.json()['visuals']
    assert visual['heading'] == 'CIA [12:00–14:00]'
    assert visual['time'] == '12:35' and visual['timestamp'] == 755
    assert visual['slide_title'] == title and visual['slide_page'] == 2
    assert client.get(visual['frame_url']).content == (frames / 'board.png').read_bytes()
    slide = client.get(visual['slide_url'])
    assert slide.status_code == 200 and slide.headers['content-type'] == 'image/jpeg'
    assert cv2.imdecode(np.frombuffer(slide.content, dtype=np.uint8), cv2.IMREAD_COLOR).shape[0] > 100
    # Second request is served from the generated cache, even offline.
    (materials / 'download.pdf').unlink()
    assert client.get(visual['slide_url']).content == slide.content
    assert client.get(base + '/slide-page', params={'file': title, 'page': 0}).status_code == 400
    assert client.get(base + '/slide-page', params={'file': title, 'page': 999}).status_code == 404
    assert client.get(base + '/frames/index.json').status_code == 400
    assert client.get(base + '/frames/missing.png').status_code == 404
    assert client.get(base + '/frames/..%5Cboard.png').status_code == 400
    assert client.get(visual['frame_url'].replace('/healthy/', '/other/')).status_code == 404
    assert client.get(visual['slide_url'].replace('/healthy/', '/other/')).status_code == 404
    outside = tmp_path / 'outside.pdf'
    _write_pdf(outside, _deck_rectangles(1))
    assert client.get(base + '/slide-page', params={'file': str(outside)}).status_code == 404
    # Pre-rendered slide pages work without the source PDF as well.
    from pku_sync.panel.note_images import _slide_cache
    cache = Path(str(_slide_cache(job, title, 6)) + '.png')
    cache.write_bytes((frames / 'board.png').read_bytes())
    assert client.get(base + '/slide-page', params={'file': title, 'page': 6}).content == cache.read_bytes()


def test_visual_sections_order_windows_matches_and_malformed_cache(tmp_path, monkeypatch):
    from pku_sync.panel import note_images
    _, job = seed(tmp_path)
    frames = job.directory / 'keyframes'
    frames.mkdir()
    for name in ('a.jpg', 'b.png', 'c.jpg'):
        (frames / name).write_bytes(b'fixture')
    rows = [{'file': 'c.jpg', 'timestamp': 120}, {'file': 'b.png', 'timestamp': 30},
            {'file': 'a.jpg', 'timestamp': 10}, {'file': '../private.jpg', 'timestamp': 5}]
    (frames / 'index.json').write_text(json.dumps({'keyframes': rows}), 'utf8')
    monkeypatch.setattr(note_images, '_visual_score', lambda p: 5 if p.name == 'b.png' else 1)
    build = lambda text: note_images.build_note_visual_sections(job, 'healthy', 'recording', text)
    items = build('### First\n### Second\n## 复习建议')
    assert [v['timestamp'] for v in items] == [10, 30]
    items = build('### First [00:00-01:00]\n### Second [02:00–03:00]')
    assert [v['timestamp'] for v in items] == [30, 120]
    assert build('### Empty [05:00–06:00]') == []
    (frames / 'keyframe-matches.json').write_text(json.dumps({'matches': [
        {'frame_file': 'b.png', 'title': 'Deck.pdf', 'page': 3}]}), 'utf8')
    item, = build('### First [00:00-01:00]')
    assert item['slide_title'] == 'Deck.pdf' and item['slide_page'] == 3 and not item['slide_url']
    # Exact saved note sections take precedence over chronological distribution.
    text = '### First\n### Second'
    monkeypatch.setattr(note_images, '_saved_visual_windows', lambda *args: [
        {'markdown': '### First', 'start': 100, 'end': 180},
        {'markdown': '### Second', 'start': 0, 'end': 60}])
    assert [v['timestamp'] for v in build(text)] == [120, 30]
    for malformed in ('{bad', 'null', '{"keyframes": null}'):
        (frames / 'index.json').write_text(malformed, 'utf8')
        assert build(text) == []


def test_direct_slide_anchor_and_supplemental_frame_preview(tmp_path):
    from pku_sync.panel.note_images import build_note_visual_sections, _slide_cache
    _, job = seed(tmp_path)
    frames = job.directory / 'keyframes'
    frames.mkdir()
    supplemental = job.directory / 'note-frames'
    supplemental.mkdir()
    (supplemental / 'board.webp').write_bytes(b'cached supplemental frame')
    (frames / 'index.json').write_text(json.dumps({'keyframes': [
        {'file': 'board.webp', 'timestamp': 12, 'anchor': {'title': 'Slides.pdf', 'page': 6}}]}), 'utf8')
    image = Path(str(_slide_cache(job, 'Slides.pdf', 6)) + '.png')
    image.parent.mkdir()
    image.write_bytes(b'pre-rendered slide')
    key = recordings_api.recording_id(job)
    # A single level-two heading is itself a content section.
    item, = build_note_visual_sections(job, 'healthy', key, '## 课堂重点\n课堂内容')
    assert item['heading'] == '课堂重点' and item['slide_page'] == 6 and item['slide_url']
    client, _ = api(tmp_path)
    assert client.get(item['frame_url']).content == (supplemental / 'board.webp').read_bytes()
    assert client.get(item['slide_url']).content == image.read_bytes()
    (frames / 'slide-alignment.json').write_text('null', 'utf8')
    (frames / 'keyframe-matches.json').write_text('{"matches":null}', 'utf8')
    assert build_note_visual_sections(job, 'healthy', key, '## Topic')[0]['slide_title'] == 'Slides.pdf'


