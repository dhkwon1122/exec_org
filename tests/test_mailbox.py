import json
from email.message import EmailMessage

import pytest

from services import db, llm, mailbox, schedules, store


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


SCHEDULE_HTML = """<p>원장님 일정입니다.</p><table><tr><th>일자</th><th>시간</th><th>일정</th><th>장소</th></tr>
<tr><td>10/27(화)</td><td>09:00~10:00</td><td>주간 임원회의</td><td>본관</td></tr>
<tr><td>10/28(수)</td><td>14:00</td><td>AI센터 보고</td><td>원장실</td></tr></table>"""


def _mail(subject, sender, html=None, text=None, date='Fri, 23 Oct 2026 17:10:00 +0900', mid='<1@x>') -> bytes:
    m = EmailMessage()
    m['Subject'], m['From'], m['Date'], m['Message-ID'] = subject, sender, date, mid
    m.set_content(text or 'plain')
    if html:
        m.add_alternative(html, subtype='html')
    return m.as_bytes()


class FakePOP:
    def __init__(self, mails):
        self.mails, self.deleted, self.quit_called = mails, [], False

    def uidl(self):
        return b'+OK', [f'{i + 1} uid-{i + 1}'.encode() for i in range(len(self.mails))], 0

    def retr(self, n):
        return b'+OK', self.mails[n - 1].split(b'\n'), 0

    def dele(self, n):
        self.deleted.append(n)

    def quit(self):
        self.quit_called = True


def test_rules_auto_apply_schedule_and_task_and_ignore():
    parent = store.create_task('비서실 요청 처리')
    mailbox.save_rule({'field': 'sender', 'pattern': 'secretary@', 'action': 'schedule', 'auto': True})
    mailbox.save_rule({'field': 'subject', 'pattern': '[자료요청]', 'action': 'task', 'auto': True,
                       'parent_task_id': parent['id']})
    mailbox.save_rule({'field': 'subject', 'pattern': '뉴스레터', 'action': 'ignore'})
    pop = FakePOP([
        _mail('원장님 주간 일정', '비서실 <secretary@x.com>', SCHEDULE_HTML, mid='<a@x>'),
        _mail('[자료요청] 3분기 실적 자료 제출', '기획팀 <plan@x.com>', text='금요일까지 실적 자료 부탁드립니다', mid='<b@x>'),
        _mail('주간 뉴스레터', 'news@x.com', text='소식', mid='<c@x>'),
    ])
    r = mailbox.fetch_new(connect=lambda cfg: pop)
    assert r == {'new': 3, 'skipped': 0, 'processed': 3} and pop.quit_called and pop.deleted == []
    by_subject = {m['subject']: m for m in mailbox.list_mails()}
    sch = by_subject['원장님 주간 일정']
    assert (sch['status'], sch['kind']) == ('auto', 'schedule') and '새 일정 2건' in sch['reason']
    assert [e['title'] for e in schedules.list_range('2026-10-27', '2026-10-28')] == ['주간 임원회의', 'AI센터 보고']
    task = by_subject['[자료요청] 3분기 실적 자료 제출']
    assert task['status'] == 'auto' and len(task['task_ids']) == 1
    made = store.get_task(task['task_ids'][0])
    assert made['parent_id'] == parent['id'] and made['title'] == '[자료요청] 3분기 실적 자료 제출'
    hist = store.list_history(made['id'])
    assert any(h['entry_type'] == '요청' and '금요일까지 실적 자료' in h['content'] for h in hist)
    assert by_subject['주간 뉴스레터']['status'] == 'ignored'
    # 다시 가져와도 같은 메일은 또 처리하지 않는다
    assert mailbox.fetch_new(connect=lambda cfg: pop)['new'] == 0


def test_unmatched_mail_gets_ai_recommendation_then_review(monkeypatch):
    def fake_chat(system, user, temperature=0.2):
        if '임원조직 담당자의 메일' in system:
            return json.dumps({'kind': 'both', 'reason': '일정 안내와 자료 요청이 함께 있음',
                               'tasks': [{'title': '보고 자료 준비하기', 'due': '2026-10-27', 'assignee': ''},
                                         {'title': 'bad', 'due': 'x'}]})
        return json.dumps({'events': [{'date': '2026-10-28', 'start_time': '14:00', 'title': 'AI센터 보고'}]})
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    monkeypatch.setattr(llm, 'chat', fake_chat)
    pop = FakePOP([_mail('보고 일정 및 자료 요청', '김팀장 <kim@x.com>',
                         text='10/28 14:00 원장님께 AI센터 보고가 있습니다. 월요일까지 자료 준비 부탁드립니다.')])
    mailbox.fetch_new(connect=lambda cfg: pop)
    m = mailbox.list_mails(['review'])[0]
    assert m['kind'] == 'both' and m['schedule_import_id'] and m['suggestion']['tasks'][1]['due'] == ''
    assert schedules.list_range('2026-10-01', '2026-10-31') == []  # 추천만 — 아직 반영 안 함
    imp = schedules.get_import(m['schedule_import_id'])
    r = mailbox.apply_review(m['id'], m['suggestion']['tasks'][:1], None, imp['events'], by='권동혁')
    assert r['tasks'] == 1 and r['added'] == 1
    m = mailbox.get_mail(m['id'])
    assert m['status'] == 'done' and m['handled_by'] == '권동혁'
    assert store.get_task(m['task_ids'][0])['due_date'] == '2026-10-27'
    mailbox.reopen(m['id'])
    assert mailbox.get_mail(m['id'])['status'] == 'review'


def test_without_llm_guess_and_old_mail_skipped(monkeypatch):
    monkeypatch.setenv('MAIL_SINCE_DAYS', '30')
    monkeypatch.setenv('MAIL_MAX_PER_RUN', '2')
    pop = FakePOP([
        _mail('아주 옛 메일', 'a@x', text='x', mid='<0@x>'),  # 최대 건수 밖 → 건너뜀
        _mail('옛 메일', 'a@x', text='x', date='Mon, 01 Jan 2024 09:00:00 +0900', mid='<1@x>'),
        _mail('원장님 일정', 'b@x', SCHEDULE_HTML, date=None or 'Fri, 23 Oct 2026 17:10:00 +0900', mid='<2@x>'),
    ])
    monkeypatch.setattr(mailbox, 'date', type('D', (), {'today': staticmethod(lambda: __import__('datetime').date(2026, 10, 25)),
                                                      'fromisoformat': staticmethod(__import__('datetime').date.fromisoformat)}))
    r = mailbox.fetch_new(connect=lambda cfg: pop)
    assert r == {'new': 1, 'skipped': 2, 'processed': 1}
    m = mailbox.list_mails()[0]
    assert (m['subject'], m['kind'], m['status']) == ('원장님 일정', 'schedule', 'review')  # 일정표 → 일정 추천
    assert len(mailbox.list_mails()) == 1  # 건너뛴 메일은 목록에 안 보임


def test_rule_crud_and_matching():
    r = mailbox.save_rule({'field': 'sender', 'pattern': 'Secretary', 'action': 'schedule', 'auto': False})
    assert r['name'] == "보낸 사람에 'Secretary'" and not r['auto']
    assert mailbox.match_rule('x', '비서 <secretary@x.com>', '')['id'] == r['id']  # 대소문자 무시
    ign = mailbox.save_rule({'field': 'body', 'pattern': '수신거부', 'action': 'ignore', 'auto': False})
    assert ign['auto']  # 무시는 늘 바로
    with pytest.raises(ValueError):
        mailbox.save_rule({'field': 'sender', 'pattern': ' ', 'action': 'task'})
    mailbox.delete_rule(r['id'])
    assert mailbox.match_rule('x', 'secretary@x.com', '') is None


def test_run_once_records_failure(monkeypatch):
    monkeypatch.setattr(mailbox, 'fetch_new', lambda: (_ for _ in ()).throw(OSError('connection refused')))
    st = mailbox.run_once()
    assert not st['ok'] and 'connection refused' in st['error'] and mailbox.last_run()['ok'] is False


def test_concurrent_fetch_is_safe(monkeypatch):
    import fcntl
    pop = FakePOP([_mail('a', 'x@x', text='1', mid='<a@x>')])
    monkeypatch.setattr(mailbox, '_connect', lambda cfg: pop)
    # 다른 프로세스가 가져오는 중이면 기다리지 않고 busy
    with open(db.DATA_DIR + '/.mail_fetch.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert mailbox.run_once()['busy'] is True
    assert mailbox.run_once()['new'] == 1
    # 같은 메일이 이미 들어가 있어도(다른 프로세스가 먼저 넣음) 오류 없이 건너뜀
    from services.db import InboundMail
    with db.SessionLocal() as s:
        s.query(InboundMail).delete()
        s.commit()
    known = mailbox.fetch_new.__globals__['select']

    def stale_select(*a, **k):  # 이미 아는 uidl 목록을 비워서 '처음 보는 메일'로 착각하게
        if a and a[0] is InboundMail.uidl:
            return known(InboundMail.uidl).where(InboundMail.id < 0)
        return known(*a, **k)
    with db.SessionLocal() as s:
        s.add(InboundMail(uidl='uid-1', status='review'))
        s.commit()
    monkeypatch.setitem(mailbox.fetch_new.__globals__, 'select', stale_select)
    assert mailbox.fetch_new(connect=lambda cfg: pop)['new'] == 0


def _tasks():
    a = store.create_task('2026 임원 평가', tags='평가, MBO', description='임원종합평가·MBO 결과 취합과 보고')
    b = store.create_task('조직개편 준비', tags='조직')
    c = store.create_task('끝난 업무: 임원 평가 시스템 교체')
    store.update_task(c['id'], status='완료')
    return a, b


def test_parent_suggested_by_keywords():
    a, b = _tasks()
    pop = FakePOP([_mail('[요청] 임원 평가 MBO 결과 제출', '인사팀 <hr@x.com>',
                         text='이번 주까지 임원 MBO 평가 결과를 제출해 주세요.', mid='<p1@x>')])
    mailbox.fetch_new(connect=lambda cfg: pop)
    sug = mailbox.list_mails()[0]['suggestion']
    assert sug['parent_task_id'] == a['id'] and '평가' in sug['parent_reason']
    assert [c['id'] for c in sug['parent_candidates']][0] == a['id']
    assert all(c['title'] != '끝난 업무: 임원 평가 시스템 교체' for c in sug['parent_candidates'])  # 완료 업무 제외


def test_parent_from_same_thread_beats_keywords():
    a, b = _tasks()
    first = FakePOP([_mail('조직도 수정 건', 'kim@x.com', text='조직도 수정 부탁', mid='<t1@x>')])
    mailbox.fetch_new(connect=lambda cfg: first)
    m = mailbox.list_mails()[0]
    mailbox.apply_review(m['id'], [{'title': '조직도 수정하기'}], b['id'], None, by='나')
    reply = FakePOP([_mail('조직도 수정 건', 'kim@x.com', text='', mid='<t1@x>'),
                     _mail('RE: 조직도 수정 건', 'kim@x.com', text='임원 평가 일정도 같이 반영해 주세요', mid='<t2@x>')])
    mailbox.fetch_new(connect=lambda cfg: reply)
    sug = mailbox.list_mails()[0]['suggestion']
    assert sug['parent_task_id'] == b['id'] and sug['parent_reason'].startswith('같은 메일 스레드')


def test_parent_picked_by_ai_and_bad_answer_ignored(monkeypatch):
    a, b = _tasks()
    answers = iter([json.dumps({'kind': 'task', 'reason': '요청', 'tasks': [{'title': '개편안 검토하기'}]}),
                    json.dumps({'parent_id': b['id'], 'reason': '조직 개편 관련 요청'}),
                    json.dumps({'kind': 'task', 'reason': '요청', 'tasks': [{'title': '평가 자료 보내기'}]}),
                    json.dumps({'parent_id': 9999, 'reason': '없는 번호'})])
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    monkeypatch.setattr(llm, 'chat', lambda *a, **k: next(answers))
    pop = FakePOP([_mail('조직 개편안 검토 요청', 'x@x', text='조직 개편안 검토 부탁', mid='<a1@x>'),
                   _mail('MBO 평가 자료', 'y@x', text='임원 평가 MBO 자료', mid='<a2@x>')])
    mailbox.fetch_new(connect=lambda cfg: pop)
    by_subject = {m['subject']: m['suggestion'] for m in mailbox.list_mails()}
    assert by_subject['조직 개편안 검토 요청']['parent_task_id'] == b['id']
    assert by_subject['조직 개편안 검토 요청']['parent_reason'] == 'AI: 조직 개편 관련 요청'
    # AI 가 목록에 없는 번호를 주면 버리고 낱말 점수로
    assert by_subject['MBO 평가 자료']['parent_task_id'] == a['id']


def test_no_parent_when_nothing_matches():
    _tasks()
    pop = FakePOP([_mail('점심 메뉴', 'x@x', text='오늘 점심은 김밥', mid='<n@x>')])
    mailbox.fetch_new(connect=lambda cfg: pop)
    sug = mailbox.list_mails()[0]['suggestion']
    assert sug['parent_task_id'] is None and sug['parent_candidates'] == []
