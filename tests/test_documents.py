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


def _versions(d):
    return [(v['version'], v['filename']) for v in d['versions']]


def test_merge_documents_with_slightly_different_names_and_split_back():
    first = [_mail('예산', 'Mon, 19 Oct 2026 09:00:00 +0900', [('2027예산안_1019.docx', b'v1')], '<a@x>'),
             _mail('예산 수정', 'Wed, 21 Oct 2026 09:00:00 +0900', [('2027년 예산(안)_1021.docx', b'v2')], '<b@x>')]
    mailbox.fetch_new(connect=lambda cfg: FakePOP(first))
    t = store.create_task('2027 예산 편성')
    for m in mailbox.list_mails():
        mailbox.link_task(m['id'], t['id'], '나')
    store.add_reference(t['id'], 'file', file_name='2027년 예산(안)_1022_v3.docx', file_bytes=b'v3')
    docs = documents.for_task(t['id'])
    assert len(docs) == 2  # 이름이 조금 달라 따로 묶였다
    a = next(d for d in docs if d['base'] == '2027예산안')
    b = next(d for d in docs if d['key'] != a['key'])

    assert documents.merge(b['key'], a['key']) == 2
    docs = documents.for_task(t['id'])
    assert len(docs) == 1 and _versions(docs[0]) == [
        (1, '2027예산안_1019.docx'), (2, '2027년 예산(안)_1021.docx'), (3, '2027년 예산(안)_1022_v3.docx')]
    assert {d['key'] for d in documents.catalog()} == {a['key']}

    # 앞으로 같은 이름 규칙으로 오는 파일도 합친 문서로
    store.add_reference(t['id'], 'file', file_name='2027년 예산(안)_1025.docx', file_bytes=b'v4')
    mailbox.fetch_new(connect=lambda cfg: FakePOP(first + [  # 앞의 두 통은 이미 받은 것
        _mail('예산 최종', 'Mon, 26 Oct 2026 09:00:00 +0900', [('2027년 예산(안)_1026_최종.docx', b'v5')], '<c@x>')]))
    for m in mailbox.list_mails():
        mailbox.link_task(m['id'], t['id'], '나')
    docs = documents.for_task(t['id'])
    assert len(docs) == 1 and docs[0]['n_versions'] == 5

    # 잘못 묶인 판 하나를 떼어 낸다 — 그 판의 원래 이름이 기본 이름
    v1 = docs[0]['versions'][0]
    assert documents.split_suggestion(docs[0]['key'], v1) == '2027예산안 2'
    v2 = docs[0]['versions'][1]
    name = documents.split_suggestion(docs[0]['key'], v2)
    assert name == '2027년 예산 안'
    new = documents.split(docs[0]['key'], [v2['id']], name)
    docs = documents.for_task(t['id'])
    assert len(docs) == 2
    split_doc = next(d for d in docs if d['key'] == new)
    assert _versions(split_doc) == [(1, '2027년 예산(안)_1021.docx')]
    # 그 이름 규칙의 합침도 풀려 다음 파일은 떼어 낸 문서로
    store.add_reference(t['id'], 'file', file_name='2027년 예산(안)_1030.docx', file_bytes=b'v6')
    split_doc = next(d for d in documents.for_task(t['id']) if d['key'] == new)
    assert split_doc['n_versions'] == 2


def test_merge_and_split_guard_rails():
    t = store.create_task('보고')
    store.add_reference(t['id'], 'file', file_name='보고서.docx', file_bytes=b'a')
    store.add_reference(t['id'], 'file', file_name='결과 보고서.docx', file_bytes=b'b')
    a, b = sorted(d['key'] for d in documents.for_task(t['id']))
    with pytest.raises(ValueError):
        documents.merge(a, a)
    documents.merge(a, b)
    with pytest.raises(ValueError, match='같은 문서'):
        documents.merge(b, a)  # a 는 이미 b 로 합쳐졌다
    d = documents.for_task(t['id'])[0]
    with pytest.raises(ValueError, match='같은 이름'):
        documents.split(d['key'], [d['versions'][0]['id']], '보고서')
    with pytest.raises(ValueError, match='이름'):
        documents.split(d['key'], [d['versions'][0]['id']], ' ')
    # 합친 쪽을 다시 다른 문서로 합쳐도 처음 이름 규칙이 따라간다
    store.add_reference(t['id'], 'file', file_name='최종 보고.docx', file_bytes=b'c')
    c = next(x['key'] for x in documents.for_task(t['id']) if x['key'] not in (a, b))
    documents.merge(b, c)
    store.add_reference(t['id'], 'file', file_name='보고서_v2.docx', file_bytes=b'd')
    docs = documents.for_task(t['id'])
    assert len(docs) == 1 and docs[0]['key'] == c and docs[0]['n_versions'] == 4


@pytest.mark.parametrize('resent', [b'old', b'new'])
def test_resending_an_uploaded_version_keeps_the_latest_content_and_insight(resent):
    from datetime import datetime
    from services.db import Reference
    t = store.create_task('보고서 검토')
    refs = [store.add_reference(t['id'], 'file', file_name='보고서.docx', file_bytes=content)
            for content in (b'old', b'new', resent)]
    with db.SessionLocal() as s:
        for i, ref in enumerate(refs):
            r = s.get(Reference, ref['id'])
            r.created_at = r.analyzed_at = datetime(2026, 10, 19 + i, 9)
            r.summary = '최신 내용' if i == 1 or (i == 2 and resent == b'new') else '이전 내용'
        s.commit()
    doc = documents.for_task(t['id'])[0]
    assert doc['n_versions'] == 2 and doc['latest']['latest'] and doc['latest']['version'] == 2
    assert doc['latest']['ref_id'] == refs[2 if resent == b'new' else 1]['id']
    assert doc['insight']['summary'] == '최신 내용'
    assert doc['updated'] == '2026-10-21'  # 재전송 수신일은 문서 활동 이력에 남는다
    assert documents.search()[0]['latest']['id'] == doc['latest']['id']


def test_old_mail_resend_does_not_replace_a_newer_uploaded_version():
    mailbox.fetch_new(connect=lambda cfg: FakePOP([
        _mail('원본', 'Mon, 19 Oct 2026 09:00:00 +0900', [('보고서.docx', b'old')], '<old@x>'),
        _mail('원본 재전송', 'Wed, 21 Oct 2026 09:00:00 +0900', [('보고서.docx', b'old')], '<resend@x>')]),
        process_now=False)
    t = store.create_task('검토')
    for m in mailbox.list_mails():
        mailbox.link_task(m['id'], t['id'], '나')
    ref = store.add_reference(t['id'], 'file', file_name='보고서_1020.docx', file_bytes=b'new')
    doc = documents.for_task(t['id'])[0]
    assert doc['latest']['kind'] == 'ref' and doc['latest']['ref_id'] == ref['id']
    assert doc['latest']['version'] == 2 and doc['n_versions'] == 2

