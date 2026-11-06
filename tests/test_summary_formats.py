"""AI 요약 형식 — 기록 유형·문서 종류마다 칸을 정하면 AI 가 그 칸을 채우고 화면에 칸별로."""
import json

import pytest

from services import db, doc_summary, enrich, llm, mailbox, store, summary_formats
from tests.test_doc_summary import _docx
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


def test_defaults_and_editing():
    kinds = {(f['target'], f['kind']) for f in summary_formats.list_formats()}
    assert ('history', '회의') in kinds and ('document', '보고서') in kinds
    assert [x['name'] for x in summary_formats.get('history', '회의')] == ['안건', '결정 사항', '할 일', '참석자']
    saved = summary_formats.save('history', '동향', '- 누가: 어느 임원이\n무엇을: 발언·움직임\n시사점\n누가: 중복')
    assert saved == [{'name': '누가', 'hint': '어느 임원이'}, {'name': '무엇을', 'hint': '발언·움직임'},
                     {'name': '시사점', 'hint': ''}]
    assert summary_formats.to_text(saved) == '누가: 어느 임원이\n무엇을: 발언·움직임\n시사점'
    summary_formats.save('history', '동향', '시사점')  # 고치기
    assert summary_formats.get('history', '동향') == [{'name': '시사점', 'hint': ''}]
    with pytest.raises(ValueError, match='칸을'):
        summary_formats.save('history', '메모', '  ')
    with pytest.raises(ValueError, match='8개'):
        summary_formats.save('history', '메모', '\n'.join(f'칸{i}' for i in range(9)))
    summary_formats.delete('history', '동향')
    assert summary_formats.get('history', '동향') == [] and summary_formats.history_prompt('동향') == ''
    assert summary_formats.clean({'안건': '예산', '결정 사항': ['A안', 'B 보류'], '모르는 칸': 'x', '참석자': ' '},
                                 summary_formats.get('history', '회의')) == [
        {'name': '안건', 'text': '예산'}, {'name': '결정 사항', 'text': 'A안\nB 보류'}]


def test_history_ai_fills_sections_for_its_type(monkeypatch):
    prompts = []

    def fake_chat(system, user, temperature=0.2):
        prompts.append(user)
        return json.dumps({'title': '예산 회의', 'summary': '예산을 논의함', 'keywords': ['예산'], 'todos': [],
                           'sections': {'안건': '내년 예산', '결정 사항': '3억으로 확정', '할 일': ''}},
                          ensure_ascii=False)
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    monkeypatch.setattr(llm, 'chat', fake_chat)
    t = store.create_task('예산')
    h = store.add_history(t['id'], '예산 회의 메모', entry_type='회의')
    enrich.enrich_entry(h['id'])
    got = store.get_history(h['id'])
    assert got['sections'] == [{'name': '안건', 'text': '내년 예산'}, {'name': '결정 사항', 'text': '3억으로 확정'}]
    assert "기록 유형('회의')은 정해진 형식" in prompts[0]
    m = store.add_history(t['id'], '그냥 메모', entry_type='메모')
    enrich.enrich_entry(m['id'])
    assert store.get_history(m['id'])['sections'] == [] and '정해진 형식' not in prompts[1]  # 형식 없는 유형


def test_document_summary_fills_sections_of_chosen_type(monkeypatch):
    prompts = []

    def fake_chat(system, user, temperature=0.2):
        prompts.append(user)
        if '파일 이름:' in user:
            return json.dumps({'doc_type': '보고서', 'summary': '장비 도입 보고', 'tags': ['장비'], 'key_dates': [],
                               'changes': '', 'sections': {'목적': '장비 도입 검토', '핵심 내용': '3대 4억',
                                                           '결론·건의': ''}}, ensure_ascii=False)
        return json.dumps({'kind': 'ignore', 'reason': '참고', 'tasks': []})
    monkeypatch.setattr(llm, 'chat', fake_chat)
    mailbox.fetch_new(connect=lambda cfg: FakePOP([
        _mail('보고', 'Mon, 19 Oct 2026 09:00:00 +0900', [('장비보고.docx', _docx(['장비 3대 4억']))], '<a@x>')]))
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    from services.db import MailAttachment
    with db.SessionLocal() as s:
        att = s.query(MailAttachment).one().id
    ins = doc_summary.analyze(att)
    assert ins['sections'] == [{'name': '목적', 'text': '장비 도입 검토'}, {'name': '핵심 내용', 'text': '3대 4억'}]
    doc_prompt = next(p for p in prompts if '파일 이름:' in p)
    assert '[회의록]' in doc_prompt and '[보고서]' in doc_prompt and '"sections"' in doc_prompt
