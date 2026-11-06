"""메일 스레드 — RE:/FW: 로 이어진 메일 묶기, 한 통을 업무에 걸면 스레드 전체가 같이 걸림."""
from email.message import EmailMessage

import pytest

from services import db, llm, mail_threads, mailbox, store
from services.db import OutboundMail
from tests.test_mailbox import FakePOP


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
    yield


def _mail(subject, mid, date='23', irt='', refs='', text='본문') -> bytes:
    m = EmailMessage()
    m['Subject'], m['From'], m['Date'], m['Message-ID'] = subject, '김 <kim@x.com>', \
        f'Fri, {date} Oct 2026 17:10:00 +0900', mid
    if irt:
        m['In-Reply-To'] = irt
    if refs:
        m['References'] = refs
    m.set_content(text)
    return m.as_bytes()


def _fetch(mails):
    mailbox.fetch_new(connect=lambda cfg: FakePOP(mails), process_now=False)


def _by_subject():
    return {m['subject']: m for m in mailbox.list_mails()}


def test_headers_and_subject_group_threads():
    _fetch([_mail('예산 협의', '<A@x>', '20'),
            _mail('RE: 예산 협의', '<b@x>', '21', irt='<A@x>', refs='<A@x>'),
            _mail('FW: RE: 예산  협의', '<c@x>', '22'),                           # 머리글 없는 전달 — 제목으로
            _mail('Re: 다른 제목', '<d@x>', '22', refs='<a@X> <b@x>'),            # 제목이 바뀐 답장 — 머리글로
            _mail('예산 협의', '<e@x>', '23'),                                     # 머리말 없는 같은 제목은 새 스레드
            _mail('주간 보고', '<f@x>', '23')])
    rows = _by_subject()
    first = min((x for x in rows.values() if x['subject'] == '예산 협의'), key=lambda x: x['id'])
    key = first['thread_key']
    assert key == 'mid:<a@x>'
    assert {rows[s]['thread_key'] for s in ('RE: 예산 협의', 'FW: RE: 예산  협의', 'Re: 다른 제목')} == {key}
    assert rows['주간 보고']['thread_key'] != key
    others = [x for x in mailbox.list_mails() if x['subject'] == '예산 협의' and x['id'] != first['id']]
    assert others[0]['thread_key'] == 'mid:<e@x>'
    assert sorted(len(g) for g in mail_threads.group(mailbox.list_mails())) == [1, 1, 4]
    assert mail_threads.sizes([key]) == {key: 4}


def test_reply_arriving_before_original_joins_its_thread():
    reply = _mail('RE: 회의 일정', '<r@x>', '21', irt='<orig@x>', refs='<orig@x>')
    _fetch([reply])
    _fetch([reply, _mail('회의 일정', '<orig@x>', '20')])
    keys = {m['thread_key'] for m in mailbox.list_mails()}
    assert keys == {'mid:<orig@x>'}


def test_link_spreads_to_thread_and_new_mail_inherits():
    first = [_mail('자료 요청', '<a@x>', '20'), _mail('RE: 자료 요청', '<b@x>', '21', irt='<a@x>', refs='<a@x>'),
             _mail('관계없음', '<z@x>', '21')]
    _fetch(first)
    m = _by_subject()
    t = store.create_task('자료 정리')
    assert mailbox.link_task(m['자료 요청']['id'], t['id'], '나') is True
    m = _by_subject()
    assert m['RE: 자료 요청']['task_ids'] == [t['id']] and m['관계없음']['task_ids'] == []
    def mails_logged():
        return len([h for h in store.list_history(t['id']) if h['entry_type'] == '요청'])
    assert mails_logged() == 2  # 두 메일 원문 모두 기록으로
    # 나중에 온 같은 스레드의 메일도 자동으로
    _fetch(first + [_mail('RE: RE: 자료 요청', '<c@x>', '22', irt='<b@x>', refs='<a@x> <b@x>')])
    assert _by_subject()['RE: RE: 자료 요청']['task_ids'] == [t['id']]
    assert mails_logged() == 3
    assert mailbox.link_task(m['RE: 자료 요청']['id'], t['id'], '나') is False  # 이미 걸림 — 기록을 또 남기지 않음
    assert mails_logged() == 3
    # 연결 끊기는 스레드 전체
    assert mailbox.unlink_task(m['자료 요청']['id'], t['id']) == 3
    assert all(x['task_ids'] == [] for x in mailbox.list_mails())


def test_create_task_from_mail_spreads_and_thread_view_includes_reply_sent():
    _fetch([_mail('보고서 작성', '<a@x>', '20'), _mail('RE: 보고서 작성', '<b@x>', '21', irt='<a@x>')])
    m = _by_subject()
    tid = mailbox.create_task_from_mail(m['RE: 보고서 작성']['id'], '보고서', None, '나')
    assert _by_subject()['보고서 작성']['task_ids'] == [tid]
    with db.SessionLocal() as s:
        s.add(OutboundMail(to_addrs='kim@x.com', subject='RE: 보고서 작성', body_html='<p>보냈습니다</p>',
                           related_kind='mail', related_id=str(m['보고서 작성']['id'])))
        s.commit()
    th = mail_threads.thread(m['보고서 작성']['id'])
    assert [x['dir'] for x in th] == ['in', 'in', 'out']
    assert th[2]['snippet'].strip() == '보냈습니다'


def test_old_mails_get_threads_by_subject():
    with db.SessionLocal() as s:
        s.add(db.InboundMail(uidl='u1', subject='협조 요청', mail_date='2026-10-01', status='done'))
        s.add(db.InboundMail(uidl='u2', subject='회신: 협조 요청', mail_date='2026-10-02', status='review'))
        s.add(db.InboundMail(uidl='u3', subject='RE: 협조 요청', mail_date='2026-01-01', status='review'))  # 너무 멂
        s.commit()
    assert mail_threads.ensure_all() == 3
    k = {m['subject']: m['thread_key'] for m in mailbox.list_mails()}
    assert k['회신: 협조 요청'] == k['협조 요청'] != k['RE: 협조 요청']
