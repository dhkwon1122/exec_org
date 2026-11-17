"""'추적' 업무 — 정기 메일 모음이 업무에 자동 연결되고, 업데이트 간격·늦음을 계산한다."""
from datetime import date

import pytest

from services import db, llm, mail_series, mailbox, store, tracking
from tests.test_mail_series import _fetch, _raw


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


def test_tracking_is_an_active_status():
    assert store.TRACKING in store.STATUSES and store.TRACKING in store.ACTIVE_STATUSES
    t = store.create_task('AI센터 주간보고', status=store.TRACKING)
    assert [x['id'] for x in tracking.tracking_tasks()] == [t['id']]


def test_series_with_new_tracking_task_links_past_and_new_mails(monkeypatch):
    _fetch(_raw('[주간보고] 1주차', 'r@x.com', '과제 A 30%', 'Mon, 2 Nov 2026', '<1@x>'),
           _raw('[주간보고] 2주차', 'r@x.com', '과제 A 60%', 'Mon, 9 Nov 2026', '<2@x>'))
    s = mail_series.save({'name': 'AI센터 주간보고', 'subject_pattern': '[주간보고]', 'file_away': True,
                          'new_task': True}, by='담당자')
    task = store.get_task(s['task_id'])
    assert task['status'] == store.TRACKING and task['title'] == 'AI센터 주간보고'
    hist = [h for h in store.list_history(task['id']) if h['entry_type'] == '요청']
    assert sorted(h['occurred_at'][:10] for h in hist) == ['2026-11-02', '2026-11-09']  # 메일 날짜로 기록
    assert all(m['status'] == 'done' and task['id'] in m['task_ids'] for m in mailbox.list_mails())  # 연결 + 처리 완료
    assert tracking.rules(task['id'])[0]['id'] == s['id']

    monkeypatch.setattr(mailbox, 'process', lambda mid: pytest.fail('추적 메일은 AI 처리하지 않음'))
    _fetch(_raw('[주간보고] 3주차', 'r@x.com', '과제 A 90%', 'Mon, 16 Nov 2026', '<3@x>'))
    newest = mail_series.mails(s['id'])[0][0]
    assert newest['status'] == 'done' and task['id'] in mailbox.get_mail(newest['id'])['task_ids']
    st = tracking.status(task['id'], today=date(2026, 11, 17))
    assert st['count'] == 3 and st['last'] == '2026-11-16' and st['days_since'] == 1
    assert st['interval'] == 7 and st['expected'] == '2026-11-23' and not st['late']
    assert tracking.status(task['id'], today=date(2026, 11, 30))['late']  # 2주 동안 안 옴


def _mail_records(tid):
    return [h for h in store.list_history(tid) if h['entry_type'] == '요청']


def test_attach_existing_task_and_validation():
    t = store.create_task('월간 실적', status=store.TRACKING)
    _fetch(_raw('월간 실적 (10월)', 'kpi@x.com', '10월', 'Mon, 2 Nov 2026', '<1@x>'))
    s = mail_series.save({'name': '월간', 'subject_pattern': '월간 실적', 'task_id': str(t['id'])})
    assert s['task_id'] == t['id'] and len(_mail_records(t['id'])) == 1
    mail_series.save({'name': '월간', 'subject_pattern': '월간 실적', 'task_id': t['id']}, s['id'])
    assert len(_mail_records(t['id'])) == 1  # 다시 저장해도 기록이 겹치지 않는다
    with pytest.raises(ValueError, match='업무'):
        mail_series.save({'name': '없는 업무', 'subject_pattern': 'x', 'task_id': 99999})


def test_status_without_enough_updates():
    t = store.create_task('새 추적', status=store.TRACKING)
    assert tracking.status(t['id'])['count'] == 0
    store.add_history(t['id'], '첫 자료', '메모', occurred_at='2026-11-02 09:00')
    st = tracking.status(t['id'], today=date(2026, 11, 3))
    assert st['count'] == 1 and st['interval'] is None and not st['late']
