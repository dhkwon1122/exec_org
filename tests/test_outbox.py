import json

import pytest
import requests

from services import absences, db, doc_versions, mailer, outbox, schedules, store


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    for k, v in {'MAIL_API_URL': 'https://mail.example/', 'MAIL_TOKEN': 'tok', 'MAIL_SYSTEM_ID': 'sys',
                 'MAIL_FROM': 'exec@example.com'}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv('MAIL_USER_ID', raising=False)
    yield


class Resp:
    def __init__(self, code=200):
        self.status_code = code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f'{self.status_code} error')


def test_payload_matches_people_in_sait_api(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, 'post', lambda url, **kw: calls.append((url, kw)) or Resp())
    t = store.create_task('보고서', owner='김철수')
    r = outbox.send('홍길동 <hong@x.com>, kim@x.com', '[업무 공유] 보고서', '본문\n둘째 줄', table_html='<table></table>',
                    cc='lee@x.com; kim@x.com', related_kind='task', related_id=t['id'], by='권동혁')
    url, kw = calls[0]
    assert url == 'https://mail.example/mails/send?userId=people.sait'
    assert kw['headers']['Authorization'] == 'Bearer tok' and kw['headers']['System-ID'] == 'sys'
    assert kw['proxies'] == {'http': None, 'https': None}
    body = json.loads(kw['data'])
    assert body['sender'] == {'emailAddress': 'exec@example.com'} and body['contentType'] == 'html'
    assert body['recipients'] == [{'emailAddress': 'hong@x.com', 'recipientType': 'TO'},
                                  {'emailAddress': 'kim@x.com', 'recipientType': 'TO'},
                                  {'emailAddress': 'lee@x.com', 'recipientType': 'CC'}]  # 받는 사람과 겹치는 참조는 뺌
    assert '본문<br>둘째 줄' in body['contents'] and '<table></table>' in body['contents']
    assert r['status'] == 'sent' and outbox.list_sent(related_kind='task', related_id=t['id'])[0]['id'] == r['id']


def test_attachments_go_multipart(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(requests, 'post', lambda url, **kw: calls.append(kw) or Resp())
    t = store.create_task('보고서')
    ref = store.add_reference(t['id'], 'file', title='자료', file_name='자료_v2.pptx', file_bytes=b'PPT')
    doc_versions.save(_mail_id(), [('평가_r1.xlsx', b'XLS')])
    att = doc_versions.for_mails([1])[0]['files'][0]
    r = outbox.send('a@x.com', '첨부', 'x', attachments=[f"ref:{ref['id']}", f"att:{att['id']}"])
    kw = calls[0]
    assert kw['files'] == [('attachments', ('자료_v2.pptx', b'PPT')), ('attachments', ('평가_r1.xlsx', b'XLS'))]
    assert kw['data'][0][0] == 'mail' and 'Content-Type' not in kw['headers']
    assert r['attachments'] == '자료_v2.pptx, 평가_r1.xlsx'


def _mail_id():
    from services.db import InboundMail
    with db.SessionLocal() as s:
        m = InboundMail(uidl='u1', subject='평가 자료', sender='김팀장 <kim@x.com>', mail_date='2026-10-21', body='본문')
        s.add(m)
        s.commit()
        return m.id


def test_failures_are_recorded_and_validation(monkeypatch):
    monkeypatch.setattr(requests, 'post', lambda url, **kw: Resp(500))
    with pytest.raises(mailer.MailError):
        outbox.send('a@x.com', '제목', '본문')
    assert outbox.list_sent()[0]['status'] == 'failed' and '500' in outbox.list_sent()[0]['error']
    with pytest.raises(ValueError):
        outbox.send('not-an-address', '제목', '본문')
    with pytest.raises(ValueError):
        outbox.send('', '제목', '본문')  # 받는 사람도 기본값도 없음
    with pytest.raises(ValueError):
        outbox.send('a@x.com', ' ', '본문')
    monkeypatch.delenv('MAIL_TOKEN')
    assert not mailer.is_configured()
    with pytest.raises(mailer.MailError):
        outbox.send('', '제목', '본문', default_to='me@x.com')  # 받는 사람을 비우면 나에게 — 설정이 없으면 실패
    assert outbox.list_sent()[0]['to'] == 'me@x.com'


def test_drafts():
    t = store.create_task('2026 임원 인사', owner='김철수, 이영희', description='승진 심사부터 발령까지')
    store.create_todo('심사 자료 준비', task_id=t['id'], due_date='2026-10-24')
    store.add_history(t['id'], '원장님 보고 완료', '회의', title='심사 일정 보고')
    d = outbox.draft_task(t['id'])
    assert d['subject'] == '[업무 공유] 2026 임원 인사' and '담당: 김철수, 이영희' in d['text']
    assert '☐ 심사 자료 준비 (~2026-10-24)' in d['text'] and '심사 일정 보고' in d['text']
    schedules.save({'date': '2026-10-21', 'start_time': '09:00', 'title': '주간 임원회의', 'location': '본관'})
    d = outbox.draft_schedule('2026-10-19', '2026-10-25')
    assert '주간 임원회의' in d['table_html'] and '10/21(수)' in d['table_html'] and d['related_kind'] == 'schedule'
    kim = store.create_person('김영수', position='부사장')
    absences.save({'kind': '출장', 'start_date': '2026-10-22', 'end_date': '2026-10-23', 'place': '부산'},
                  person_ids=[kim['id']])
    d = outbox.draft_absence('2026-10-19', '2026-10-25')
    assert '김영수 부사장' in d['table_html'] and '10/22 ~ 10/23' in d['table_html']
    mid = _mail_id()
    d = outbox.draft_reply(mid)
    assert (d['subject'], d['to']) == ('RE: 평가 자료', 'kim@x.com') and '> 본문' in d['text']


def test_parse_addrs():
    assert outbox.parse_addrs('a@x.com; 홍길동 <b@y.co.kr>\nc@z, a@x.com') == (['a@x.com', 'b@y.co.kr'], ['c@z'])
    assert outbox.parse_addrs(['kim@x.com', '']) == (['kim@x.com'], [])  # 빈 '직접 입력' 칸이 붙어도
    assert outbox.parse_addrs('"김팀장" <kim@x.com>,') == (['kim@x.com'], [])
