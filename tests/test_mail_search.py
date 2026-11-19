"""메일 검색 — 받은 메일 화면의 검색 칸, 이력 검색의 '받은 메일' 결과."""
import json

import pytest

from services import db, llm, mailbox
from tests.test_mailbox import FakePOP, _mail


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    monkeypatch.setattr(db, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(mailbox, '_STATUS_FILE', str(tmp_path / 'st.json'))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    monkeypatch.setattr(llm, 'is_configured', lambda: False)
    for k, v in {'MAIL_POP3_HOST': 'pop.example', 'MAIL_POP3_USER': 'me', 'MAIL_SINCE_DAYS': '0',
                 'MAIL_MAX_PER_RUN': '50'}.items():
        monkeypatch.setenv(k, v)
    mailbox.fetch_new(connect=lambda cfg: FakePOP([
        _mail('예산 편성 요청', '김팀장 <kim@x.com>', text='2027 예산 편성 자료를 금요일까지 보내 주세요.', mid='<1@x>'),
        _mail('출장 안내', '박과장 <park@x.com>', text='다음 주 판교 출장 일정입니다.', mid='<2@x>',
              date='Mon, 2 Nov 2026 09:00:00 +0900'),
        _mail('회의록 공유', 'lee@x.com', text='예산 관련 회의 내용 공유드립니다.', mid='<3@x>')]))


def test_search_subject_sender_body_all_statuses():
    by_subject = {m['subject']: m for m in mailbox.list_mails()}
    mailbox.complete(by_subject['회의록 공유']['id'], '담당자')
    assert {m['subject'] for m in mailbox.list_mails(query='예산')} == {'예산 편성 요청', '회의록 공유'}  # 제목·본문, 처리 완료 포함
    assert [m['subject'] for m in mailbox.list_mails(query='박과장')] == ['출장 안내']  # 보낸 사람
    assert [m['subject'] for m in mailbox.list_mails(query='예산 금요일')] == ['예산 편성 요청']  # 낱말 모두
    assert [m['subject'] for m in mailbox.list_mails(query='"판교 출장"')] == ['출장 안내']
    assert mailbox.list_mails(['review'], query='회의') == []  # 상태 조건과 함께도
    assert [m['subject'] for m in mailbox.list_mails(query='출장', date_from='2026-11-01', date_to='2026-11-02')] \
        == ['출장 안내']
    assert mailbox.list_mails(query='출장', date_from='2026-11-03') == []


def test_snippet_centers_on_term():
    text = '앞부분 ' * 40 + '핵심 낱말 여기' + ' 뒷부분' * 40
    snip = mailbox.snippet(text, ['핵심'], width=60)
    assert '핵심' in snip and snip.startswith('…') and snip.endswith('…')
    assert mailbox.snippet('짧은 본문', ['없음']) == '짧은 본문'


def test_history_search_page_shows_mail_results():
    import app  # noqa: F401 — 페이지 등록
    from pages import search
    out = search.search('예산', [], None, None, None, None, False, False, False, 1)
    text = json.dumps(out, default=lambda o: o.to_plurality_json() if hasattr(o, 'to_plurality_json') else str(o),
                      ensure_ascii=False)
    assert '받은 메일 2건' in text and '예산 편성 요청' in text and '/inbox?mail=' in text
    assert out[1].startswith('메일 2건')  # 기록이 없어도 메일만으로 결과
