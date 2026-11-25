"""Local durable submission fence tests (no remote service calls)."""
from copy import deepcopy
import hashlib
import json

import pytest

from pku_sync import catalog_snapshots as catalog
from pku_sync.config import Settings
from pku_sync.panel.assignment_workflow import _requirements, PreparationError


ASSIGNMENT = {'content_id': '_2_1', 'source_id': 'pku:assignment:_2_1',
              'title': '正式作业', 'source': 'content-tree'}


def setup(monkeypatch, tmp_path):
    from pku_sync.panel.submission_permit import SubmissionPermit

    settings = Settings(_env_file=None, data_dir=tmp_path, pku_username='student')
    assignment = deepcopy(ASSIGNMENT)
    metadata = {'course_id': '_1_1', 'content_id': '_2_1', 'draft_id': 'one', 'state': 'prepared',
                'portal_account_sha256': 'a' * 64, 'sha256': 'b' * 64,
                'requirements_sha256': _requirements(assignment)}
    saved = []
    monkeypatch.setattr(catalog, 'current', lambda *args: {'revision': 1, 'snapshot': {
        'sections': {'assignments': {'items': [assignment]}}}})

    def permit(meta=metadata):
        return SubmissionPermit(settings, meta, assignment, lambda: saved.append(deepcopy(meta)))

    return permit, metadata, saved


def test_unavailable_catalog_prevents_permit_acquisition(monkeypatch, tmp_path):
    permit, metadata, saved = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(catalog, 'current', lambda *args: (_ for _ in ()).throw(catalog.CatalogUnavailable('offline')))
    with pytest.raises(PreparationError, match='快照不可用'):
        with permit():
            pytest.fail('unavailable catalog must never allow portal POST')
    assert not saved
    assert 'submission_fence' not in metadata


def test_old_local_assignment_requirements_cannot_obtain_a_permit(monkeypatch, tmp_path):
    permit, metadata, saved = setup(monkeypatch, tmp_path)
    metadata['requirements_sha256'] = 'c' * 64
    with pytest.raises(PreparationError, match='要求与预览不一致'):
        with permit():
            pytest.fail('old requirements must never reach portal POST')
    assert not saved
    assert 'submission_fence' not in metadata


def test_prepost_failure_releases_only_unsent_operation(monkeypatch, tmp_path):
    permit, metadata, saved = setup(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        with permit() as fence:
            assert metadata['submission_fence']['state'] == 'acquired'
            assert metadata['submission_fence']['protocol'] == 1
            assert not fence.armed
            raise ValueError('file failed preview hash before POST')
    assert metadata['submission_fence']['state'] == 'aborted'


def test_armed_exit_retains_verification_pending_and_blocks_reentry(monkeypatch, tmp_path):
    permit, metadata, _ = setup(monkeypatch, tmp_path)
    with permit() as fence:
        fence.arm()
        assert fence.armed
    assert metadata['submission_fence']['state'] == 'verification_pending'
    with pytest.raises(PreparationError, match='禁止再次提交'):
        with permit():
            pytest.fail('pending draft without receipt must block duplicate POST')


def test_verified_local_receipt_completes_and_recovers_without_portal(monkeypatch, tmp_path):
    permit, metadata, _ = setup(monkeypatch, tmp_path)
    html = '<div>复查提交历史记录</div>'
    receipt = {'course_id': '_1_1', 'content_id': '_2_1', 'portal_verified': True,
               'verified_at': '2026-10-04T00:00:00+00:00', 'review_history_html': html,
               'review_history_sha256': hashlib.sha256(html.encode()).hexdigest()}
    with permit() as fence:
        fence.arm()
        metadata.update(state='receipt_verified', receipt=receipt)
        fence.complete(receipt)
    assert metadata['submission_fence']['state'] == 'submitted'
    assert metadata['submission_fence']['receipt']['review_history_sha256'] == receipt['review_history_sha256']
    with permit() as recovered:
        assert recovered.recovered_receipt == receipt
        assert metadata['submission_fence']['state'] == 'submitted'


def test_altered_local_receipt_does_not_complete_pending_operation(monkeypatch, tmp_path):
    permit, metadata, _ = setup(monkeypatch, tmp_path)
    with permit() as fence:
        fence.arm()
    metadata.update(state='receipt_verified', receipt={
        'portal_verified': True, 'course_id': '_1_1', 'content_id': '_2_1',
        'verified_at': '2026-10-04T00:00:00+00:00', 'review_history_html': 'changed',
        'review_history_sha256': 'd' * 64})
    with pytest.raises(PreparationError):
        permit().reconcile()
    assert metadata['submission_fence']['state'] == 'verification_pending'


def test_reconcile_aborts_unarmed_crash_and_blocks_armed_pending(monkeypatch, tmp_path):
    permit, metadata, _ = setup(monkeypatch, tmp_path)
    permit().__enter__()  # Simulated crash after acquire, before arm or __exit__.
    assert metadata['submission_fence']['state'] == 'acquired'
    metadata['state'] = 'verification_pending'
    assert permit().reconcile()['state'] == 'aborted'
    assert metadata['submission_fence']['state'] == 'aborted'
    assert metadata['state'] == 'aborted'

    permit2, metadata2, _ = setup(monkeypatch, tmp_path)
    with permit2() as fence:
        fence.arm()
    metadata2['state'] = 'verification_pending'
    with pytest.raises(PreparationError):
        permit2().reconcile()
    assert metadata2['submission_fence']['state'] == 'verification_pending'

    metadata2['submission_fence'] = {'revision': 1}
    with pytest.raises(PreparationError, match='没有可验证的本地状态'):
        permit2().reconcile()


def panel_setup(monkeypatch, tmp_path):
    from pku_sync.panel.submission_permit import SubmissionPermit
    from tests.test_panel_assignment_workflow import _client, _file_draft

    clients = [_client(tmp_path / name, monkeypatch, fence_factory=SubmissionPermit) for name in ('windows', 'other-device')]
    rows = json.loads((tmp_path / 'windows/course/assignments/index.json').read_text('utf-8'))
    monkeypatch.setattr(catalog, 'current', lambda *args: {'revision': 1, 'snapshot': {
        'sections': {'assignments': {'items': rows}}}})
    drafts = [_file_draft(client) for client in clients]
    return clients, drafts


def test_unknown_post_blocks_second_attempt_until_verified(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from pku_sync.panel import assignment_workflow
    from pku_sync.submit import SubmissionForm

    clients, drafts = panel_setup(monkeypatch, tmp_path)
    posts, logins = [], []

    class Campus:
        def post(self, *args, **kwargs):
            posts.append('POST')
            return SimpleNamespace(status_code=200, json=lambda: {})

        def get(self, *args, **kwargs):
            return SimpleNamespace(status_code=200, text='<div>unknown submission</div>',
                url='https://course.pku.edu.cn/webapps/assignment/uploadAssignment?course_id=_103987_1&content_id=_1714169_1')

        def close(self):
            pass

    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: logins.append('login') or Campus())
    monkeypatch.setattr(assignment_workflow, 'fetch_form', lambda *a, **kw: SubmissionForm('_103987_1', '_1714169_1', {}))
    client, (base, draft, body) = clients[0], drafts[0]
    first = client.post(base + '/submit', json=body)
    assert first.status_code == 400
    second = client.post(base + '/submit', json=body)
    assert second.status_code == 400
    assert posts == ['POST'] and logins == ['login']
    meta = json.loads((tmp_path / 'windows/submission-drafts' / draft['draft_id'] / 'draft.json').read_text('utf-8'))
    assert meta['submission_fence']['state'] == 'verification_pending'


def test_account_switch_during_arm_stops_actual_post(monkeypatch, tmp_path):
    from pku_sync.panel import assignment_workflow
    from pku_sync.panel.submission_permit import SubmissionPermit
    from pku_sync.submit import SubmissionForm

    clients, drafts = panel_setup(monkeypatch, tmp_path)

    class Campus:
        def post(self, *args, **kwargs):
            pytest.fail('account changed during arm must never POST')

        def close(self):
            pass

    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: Campus())
    monkeypatch.setattr(assignment_workflow, 'fetch_form', lambda *a, **kw: SubmissionForm('_103987_1', '_1714169_1', {}))
    orig_arm = SubmissionPermit.arm

    def switch_on_arm(self):
        orig_arm(self)
        clients[0].app.state.recording_manager.settings.pku_username = 'another-student'

    monkeypatch.setattr(SubmissionPermit, 'arm', switch_on_arm)
    base, draft, body = drafts[0]
    result = clients[0].post(base + '/submit', json=body)
    assert result.status_code == 400 and '账号' in result.json()['detail']
    meta = json.loads((tmp_path / 'windows/submission-drafts' / draft['draft_id'] / 'draft.json').read_text('utf-8'))
    assert meta['submission_fence']['state'] == 'verification_pending'


def test_recovery_route_finds_receipt_after_restart_and_never_opens_portal(monkeypatch, tmp_path):
    from pku_sync.panel import assignment_workflow

    clients, drafts = panel_setup(monkeypatch, tmp_path)
    client, (_, draft, _) = clients[0], drafts[0]
    path = tmp_path / 'windows/submission-drafts' / draft['draft_id'] / 'draft.json'
    metadata = json.loads(path.read_text('utf-8'))
    html = '<div>复查提交历史记录</div>'
    receipt = {'portal_verified': True, 'course_id': metadata['course_id'], 'content_id': metadata['content_id'],
               'verified_at': '2026-10-04T00:00:00+00:00', 'review_history_html': html,
               'review_history_sha256': hashlib.sha256(html.encode()).hexdigest()}
    metadata.update(state='receipt_verified', receipt=receipt,
                    submission_fence={'protocol': 1, 'revision': 1, 'permit_id': 'permit', 'state': 'verification_pending'})
    path.write_text(json.dumps(metadata), 'utf-8')
    monkeypatch.setattr(assignment_workflow, 'get_session', lambda **kw: pytest.fail('recovery must not open portal'))
    listing = client.get('/api/campus/courses/_103987_1/submission-receipts').json()
    assert listing['items'][0]['state'] == 'receipt_verified'
    recovery = listing['items'][0]['recovery_url']
    assert client.post(recovery).status_code == 200
    saved = json.loads(path.read_text('utf-8'))
    assert saved['state'] == 'submitted'
    assert saved['submission_fence']['state'] == 'submitted'
    assert client.get('/api/campus/submission-drafts/' + draft['draft_id'] + '/receipt').status_code == 200


def test_recovery_route_rejects_other_account_before_reconcile(monkeypatch, tmp_path):
    from pku_sync.panel.submission_permit import SubmissionPermit

    clients, drafts = panel_setup(monkeypatch, tmp_path)
    client = clients[0]
    client.app.state.recording_manager.settings.pku_username = 'another-student'
    monkeypatch.setattr(SubmissionPermit, 'reconcile', lambda *a, **kw: pytest.fail('wrong account must not reconcile'))
    response = client.post('/api/campus/submission-drafts/' + drafts[0][1]['draft_id'] + '/recover')
    assert response.status_code == 409 and '账号' in response.json()['detail']
