"""업무 참고자료 탭에 모이는 자료 — 하위 업무·함께 걸친 기록·메일 첨부(업무를 만든 메일 포함), 전체 참고자료의 메일 첨부."""
import pytest

from services import db, doc_versions, llm, mailbox, store
from tests.test_doc_versions import _mail
from tests.test_mailbox import FakePOP


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    monkeypatch.setattr(db, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(mailbox, '_STATUS_FILE', str(tmp_path / 'st.json'))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    monkeypatch.setattr(llm, 'is_configured', lambda: False)
    for k, v in {'MAIL_POP3_HOST': 'pop.example', 'MAIL_POP3_USER': 'me', 'MAIL_SINCE_DAYS': '0'}.items():
        monkeypatch.setenv(k, v)
    yield


def test_task_scope_includes_all_descendants():
    a = store.create_task('상위')
    b = store.create_task('하위', parent_id=a['id'])
    c = store.create_task('손자', parent_id=b['id'])
    other = store.create_task('다른 업무')
    assert store.task_scope(a['id']) == [a['id'], b['id'], c['id']]
    assert store.task_scope(a['id'], include_children=False) == [a['id']]
    assert other['id'] not in store.task_scope(a['id'])


def test_references_for_task_collects_child_and_shared_history_refs():
    a = store.create_task('상위')
    b = store.create_task('하위', parent_id=a['id'])
    x = store.create_task('옆 업무')
    store.add_reference(a['id'], 'link', title='직접', url='https://a.example')
    store.add_reference(b['id'], 'note', title='하위 메모', note='하위 업무 메모')
    h = store.add_history(x['id'], '두 업무에 걸친 회의', '회의', '나', also_task_ids=[a['id']])
    store.add_reference(x['id'], 'link', title='회의 자료', url='https://x.example', history_id=h['id'])
    store.add_reference(x['id'], 'link', title='옆 업무만', url='https://only.example')
    refs = {r['title']: r for r in store.references_for_task(a['id'])}
    assert set(refs) == {'직접', '하위 메모', '회의 자료'}
    assert (refs['직접']['via'], refs['직접']['source']) == ('self', '직접 등록')
    assert refs['하위 메모']['via'] == 'child' and refs['하위 메모']['via_task']['title'] == '하위'
    assert (refs['회의 자료']['via'], refs['회의 자료']['source']) == ('shared', '기록 첨부')
    assert refs['회의 자료']['via_task']['id'] == x['id']
    assert {r['title'] for r in store.references_for_task(a['id'], include_children=False)} == {'직접', '회의 자료'}
    counts = store.reference_history_counts(a['id'], [r['id'] for r in refs.values()])
    assert counts[refs['회의 자료']['id']] == 1
    assert '두 업무에 걸친 회의' in store.history_titles([h['id']])[h['id']]


def _fetch(mails):
    mailbox.fetch_new(connect=lambda cfg: FakePOP(mails))


def test_mail_attachments_from_child_and_source_mail():
    _fetch([_mail('예산 자료', 'Mon, 19 Oct 2026 09:00:00 +0900', [('예산안_1019.docx', b'v1')], '<a@x>'),
            _mail('예산 수정', 'Tue, 20 Oct 2026 09:00:00 +0900', [('예산안_1020_r1.docx', b'v2')], '<b@x>'),
            _mail('회의록', 'Wed, 21 Oct 2026 09:00:00 +0900', [('회의록.hwpx', b'h')], '<c@x>')])
    m1, m2, m3 = sorted(m['id'] for m in mailbox.list_mails())
    parent = store.create_task('예산 편성')
    child_id = mailbox.create_task_from_mail(m2, '예산 수정 반영', parent['id'], '나')  # 하위 업무를 만든 메일
    mailbox.link_task(m1, parent['id'], '나')
    groups = {g['doc_key']: g for g in doc_versions.for_task(parent['id'])}
    assert set(groups) == {'예산안.docx'}
    assert {f['filename'] for f in groups['예산안.docx']['files']} == {'예산안_1019.docx', '예산안_1020_r1.docx'}
    assert groups['예산안.docx']['via_tasks'] == []  # 이 업무에 걸린 메일도 있음
    only_child = doc_versions.for_task(parent['id'], include_children=False)
    assert [f['filename'] for f in only_child[0]['files']] == ['예산안_1019.docx']
    mailbox.link_task(m3, child_id, '나')
    g = {x['doc_key']: x for x in doc_versions.for_task(parent['id'])}['회의록.hwpx']
    assert g['via_tasks'] == [{'id': child_id, 'title': '예산 수정 반영'}]


def test_latest_documents_for_references_page():
    _fetch([_mail('예산 자료', 'Mon, 19 Oct 2026 09:00:00 +0900', [('예산안_1019.docx', b'v1')], '<a@x>'),
            _mail('예산 수정', 'Tue, 20 Oct 2026 09:00:00 +0900', [('예산안_1020_r1.docx', b'v2')], '<b@x>'),
            _mail('회의록', 'Wed, 21 Oct 2026 09:00:00 +0900', [('회의록.hwpx', b'h')], '<c@x>')])
    t = store.create_task('예산 편성')
    m1 = min(m['id'] for m in mailbox.list_mails())
    mailbox.link_task(m1, t['id'], '나')
    docs = doc_versions.latest_documents()
    assert [d['file']['filename'] for d in docs] == ['회의록.hwpx', '예산안_1020_r1.docx']  # 문서마다 최신 판 하나
    budget = docs[1]
    assert budget['n_versions'] == 2 and budget['tasks'] == [{'id': t['id'], 'title': '예산 편성'}]
    assert [d['file']['filename'] for d in doc_versions.latest_documents('예산안')] == ['예산안_1020_r1.docx']
    assert doc_versions.latest_documents('없는말') == []
