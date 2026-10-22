import io
import json
import zipfile
from datetime import datetime

import openpyxl
import pytest

from services import db, doc_summary, doc_text, doc_versions, llm, mailbox
from services.db import InboundMail, MailAttachment
from tests.test_doc_versions import _mail
from tests.test_mailbox import FakePOP

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


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


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


def _docx(paras: list[str], table: list[list[str]] | None = None) -> bytes:
    body = ''.join(f'<w:p><w:r><w:t>{p}</w:t></w:r></w:p>' for p in paras)
    if table:
        body += '<w:tbl>' + ''.join(
            '<w:tr>' + ''.join(f'<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>' for c in row) + '</w:tr>'
            for row in table) + '</w:tbl>'
    return _zip({'word/document.xml': f'<w:document {W}><w:body>{body}</w:body></w:document>'})


def _xlsx(rows: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '예산'
    for r in rows:
        ws.append(r)
    hidden = wb.create_sheet('숨김')
    hidden.append(['비밀'])
    hidden.sheet_state = 'hidden'
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _pptx(slides: list[list[str]]) -> bytes:
    files = {}
    for i, lines in enumerate(slides, 1):
        ps = ''.join(f'<a:p><a:r><a:t>{t}</a:t></a:r></a:p>' for t in lines)
        files[f'ppt/slides/slide{i}.xml'] = f'<p:sld xmlns:p="urn:p" {A}><p:txBody>{ps}</p:txBody></p:sld>'
    return _zip(files)


def test_extract_docx_with_table():
    text, note = doc_text.extract('보고서.docx', _docx(['연구 성과 보고', '결론: 예산 증액 필요'],
                                                      [['항목', '금액'], ['인건비', '3억']]))
    assert note == ''
    assert '연구 성과 보고' in text and '항목 | 금액' in text and '인건비 | 3억' in text


def test_extract_xlsx_skips_hidden_sheet_and_formats_cells():
    text, note = doc_text.extract('예산.xlsx', _xlsx([['항목', '금액', '날짜'], ['장비', 1200000.0,
                                                                            datetime(2026, 11, 3)], [None, None]]))
    assert note == ''
    assert '[시트: 예산]' in text and '장비 | 1200000 | 2026-11-03' in text
    assert '비밀' not in text


def test_extract_pptx_in_slide_order():
    data = _pptx([[f'슬라이드{i}'] for i in range(1, 12)])
    text, _ = doc_text.extract('발표.pptx', data)
    assert text.index('슬라이드2') < text.index('슬라이드10')
    assert '[슬라이드 11]' in text


def test_extract_unsupported_and_broken():
    assert doc_text.extract('예전.doc', b'x')[1].startswith('예전 형식(.doc)')
    assert doc_text.extract('사진.png', b'x')[0] == ''
    text, note = doc_text.extract('깨짐.docx', b'not a zip')
    assert text == '' and '열지 못했습니다' in note
    assert '글자가 없습니다' in doc_text.extract('빈.docx', _docx([]))[1]


def _fetch(mails, monkeypatch=None):
    """메일 받기는 LLM 없이(분류에 LLM 을 부르지 않게), 그 뒤 monkeypatch 가 있으면 LLM 을 켠다."""
    mailbox.fetch_new(connect=lambda cfg: FakePOP(mails))
    if monkeypatch is not None:
        monkeypatch.setattr(llm, 'is_configured', lambda: True)
    

def _att(name):
    with db.SessionLocal() as s:
        return s.query(MailAttachment).filter_by(filename=name).one()


def test_fetch_reads_text_so_documents_are_searchable():
    _fetch([_mail('예산 자료', 'Mon, 19 Oct 2026 09:00:00 +0900',
                  [('예산안_1019.docx', _docx(['반도체 장비 예산안']))], '<a@x>')])
    a = _att('예산안_1019.docx')
    assert '반도체 장비 예산안' in a.text and a.text_at is not None and a.analyzed_at is None
    found = doc_summary.search('장비 예산안')
    assert [d['filename'] for d in found] == ['예산안_1019.docx']
    assert '장비' in found[0]['snippet']


def test_analyze_mail_stores_summary_tags_and_version_changes(monkeypatch):
    prompts = []

    def fake_chat(system, user, temperature=0.2):
        prompts.append(user)
        return json.dumps({'doc_type': '계획서', 'summary': '장비 예산 계획.', 'tags': ['예산', '#장비', '예산'],
                           'key_dates': [{'date': '2026-11-03', 'what': '제출 기한'}, {'date': '11월', 'what': 'x'}],
                           'changes': '금액이 3억에서 4억으로 늘었다.'}, ensure_ascii=False)

    monkeypatch.setattr(llm, 'chat', fake_chat)
    _fetch([_mail('예산', 'Mon, 19 Oct 2026 09:00:00 +0900', [('예산안_1019.docx', _docx(['총액 3억']))], '<a@x>'),
            _mail('예산 수정', 'Tue, 20 Oct 2026 09:00:00 +0900',
                  [('예산안_1020_r1.docx', _docx(['총액 4억'])), ('사진.png', b'PNG')], '<b@x>')], monkeypatch)
    first, second = _att('예산안_1019.docx'), _att('예산안_1020_r1.docx')

    r = doc_summary.analyze_mail(first.mail_id)
    assert r == {'done': 1, 'skipped': []}
    ins = doc_summary.get_insight(first.id)
    assert ins['doc_type'] == '계획서' and ins['tags'] == ['예산', '장비']
    assert ins['key_dates'] == [{'date': '2026-11-03', 'what': '제출 기한'}]
    assert ins['changes'] == ''  # 첫 판은 비교할 앞 판이 없다
    assert '달라진 줄' not in prompts[0]

    r = doc_summary.analyze_mail(second.mail_id)
    assert r['done'] == 1 and r['skipped'][0][0] == '사진.png'
    assert '- 총액 3억' in prompts[1] and '+ 총액 4억' in prompts[1]
    assert '지금 쓰고 있는 태그: 예산, 장비' in prompts[1]
    assert doc_summary.get_insight(second.id)['changes'] == '금액이 3억에서 4억으로 늘었다.'
    assert dict(doc_summary.known_tags()) == {'예산': 2, '장비': 2}

    # 이미 요약한 것은 다시 묻지 않고, force 면 다시 묻는다
    doc_summary.analyze(second.id)
    assert len(prompts) == 2
    doc_summary.analyze(second.id, force=True)
    assert len(prompts) == 3
    # 판 목록에도 요약이 실린다
    groups = doc_versions.for_mails([second.mail_id])
    doc = next(g for g in groups if g['doc_key'] == '예산안.docx')
    assert doc['files'][0]['insight']['summary'] == '장비 예산 계획.'


def test_same_file_resent_copies_summary_without_llm(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, 'chat', lambda *a, **k: calls.append(1) or json.dumps(
        {'doc_type': '보고서', 'summary': '요약', 'tags': ['보고'], 'key_dates': [], 'changes': ''}))
    data = _docx(['같은 내용'])
    _fetch([_mail('1', 'Mon, 19 Oct 2026 09:00:00 +0900', [('보고.docx', data)], '<a@x>'),
            _mail('2', 'Tue, 20 Oct 2026 09:00:00 +0900', [('보고 (1).docx', data)], '<b@x>')], monkeypatch)
    a, b = _att('보고.docx'), _att('보고 (1).docx')
    doc_summary.analyze(a.id)
    assert doc_summary.analyze(b.id)['summary'] == '요약'
    assert len(calls) == 1


def test_unreadable_attachment_reports_reason(monkeypatch):
    _fetch([_mail('스캔', 'Mon, 19 Oct 2026 09:00:00 +0900', [('빈.docx', _docx([]))], '<a@x>')], monkeypatch)
    a = _att('빈.docx')
    with pytest.raises(ValueError, match='글자가 없습니다'):
        doc_summary.analyze(a.id)
    assert doc_summary.analyze_mail(a.mail_id)['skipped'] == [('빈.docx', '글자가 없습니다.')]


def test_docs_action_recommended_and_keeps_mail_status(monkeypatch):
    monkeypatch.setattr(llm, 'chat', lambda *a, **k: json.dumps(
        {'doc_type': '보고서', 'summary': '요약', 'tags': [], 'key_dates': [], 'changes': ''}))
    _fetch([_mail('자료', 'Mon, 19 Oct 2026 09:00:00 +0900', [('보고.docx', _docx(['내용']))], '<a@x>')], monkeypatch)
    mid = _att('보고.docx').mail_id
    with db.SessionLocal() as s:
        before = s.get(InboundMail, mid).status
    m = mailbox.get_mail(mid)
    assert dict(mailbox.recommend_actions(m))['docs'] == '아직 요약하지 않은 첨부 문서 1개'
    assert mailbox.run_actions(mid, ['docs'], '나') == {'docs': 1}
    with db.SessionLocal() as s:
        assert s.get(InboundMail, mid).status == before
    assert 'docs' not in dict(mailbox.recommend_actions(mailbox.get_mail(mid)))
