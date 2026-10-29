"""문서별 판 이력 — 메일 첨부와 올린 파일(직접·기록)을 같은 문서끼리 묶어 판을 매긴다."""
import base64

import pytest

from services import db, documents, llm, mailbox, store
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


def test_mail_and_uploaded_versions_form_one_document():
    mailbox.fetch_new(connect=lambda cfg: FakePOP([
        _mail('예산안 송부', 'Mon, 19 Oct 2026 09:00:00 +0900', [('2027예산안_1019.docx', b'v1')], '<a@x>'),
        _mail('예산안 수정', 'Wed, 21 Oct 2026 09:00:00 +0900', [('2027예산안_1021_v3.docx', b'v3')], '<b@x>'),
        _mail('회의록', 'Wed, 21 Oct 2026 10:00:00 +0900', [('회의록.hwpx', b'h')], '<c@x>')]))
    m1, m2, m3 = sorted(m['id'] for m in mailbox.list_mails())
    t = store.create_task('2027 예산 편성')
    child = store.create_task('장비 예산', parent_id=t['id'])
    mailbox.link_task(m1, t['id'], '나')
    # 그 사이 우리가 고쳐서 올린 판 (하위 업무의 기록에 첨부)
    h = store.add_history(child['id'], '검토 의견 반영본 공유', '메모', '나')
    store.add_reference(child['id'], 'file', file_name='2027예산안_1020_v2.docx', file_bytes=b'v2', history_id=h['id'])
    store.add_reference(t['id'], 'file', file_name='참석자명단.xlsx', file_bytes=b'x')

    docs = documents.for_task(t['id'])
    budget = next(d for d in docs if d['base'] == '2027예산안')
    assert [(v['version'], v['kind'], v['filename']) for v in budget['versions']] == [
        (1, 'mail', '2027예산안_1019.docx'), (2, 'ref', '2027예산안_1020_v2.docx'), (3, 'mail', '2027예산안_1021_v3.docx')]
    assert budget['n_versions'] == 3 and budget['latest']['filename'] == '2027예산안_1021_v3.docx'
    v2 = budget['versions'][1]
    assert v2['source'] == '기록 첨부' and '검토 의견 반영본' in v2['history_title'] and v2['via_task']['id'] == child['id']
    assert v2['size'] == 2 and v2['href'].startswith('/files/')
    assert {d['base'] for d in docs} == {'2027예산안', '참석자명단'}  # 연결 안 된 회의록 메일은 없음
    assert docs[0]['base'] == '2027예산안'  # 최근에 바뀐 문서 먼저

    every = documents.search()
    assert {d['base'] for d in every} == {'2027예산안', '참석자명단', '회의록'}
    found = documents.search('예산안')
    assert [d['n_versions'] for d in found] == [3]  # 검색에 걸려도 문서의 모든 판
