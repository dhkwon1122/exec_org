"""예약 메일: 실제 네트워크 없이 시간·범위·동시 실행·실패 이력을 검증한다."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import threading

import pytest
import requests

from services import db, morning_digest as digest, outbox, store
from services.db import HistoryEntry, Task


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    db.configure('sqlite:///' + str(tmp_path / 'digest.db'))
    monkeypatch.setenv('TZ', 'Asia/Seoul')
    monkeypatch.setenv('MORNING_DIGEST_WORKER', '0')
    for k, v in {
        'MAIL_API_URL': 'https://mail.example',
        'MAIL_TOKEN': 'fake',
        'MAIL_SYSTEM_ID': 'fake',
        'MAIL_FROM': 'from@example.com',
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(
        requests, 'post', lambda *a, **kw: pytest.fail('실제 메일 API 호출 금지')
    )


def config(**values):
    return dict(
        recipients='a@example.com, b@example.com',
        enabled=True,
        send_time='07:00',
        timezone='Asia/Seoul',
        weekdays_only=False,
        app_url='https://work.example',
        **values,
    )


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def test_defaults_and_validation(monkeypatch):
    assert not digest.settings()['enabled']
    assert digest.settings()['send_time'] == '07:00'
    for field, value in [
        ('recipients', 'bad-address'),
        ('send_time', '7:00'),
        ('send_time', '25:00'),
        ('timezone', 'unknown'),
        ('app_url', 'javascript:alert(1)'),
        ('app_url', 'https://u:p@example.com'),
        ('app_url', 'https://example.com?q=1'),
    ]:
        values = config()
        values[field] = value
        with pytest.raises(ValueError):
            digest.save(values)
    values = config()
    values['recipients'] = ''
    with pytest.raises(ValueError):
        digest.save(values)
    monkeypatch.delenv('MAIL_TOKEN')
    with pytest.raises(ValueError, match='메일 API'):
        digest.save(config())
    values = config()
    values['enabled'] = False
    assert not digest.save(values, '담당자')['enabled']
    assert digest.settings()['updated_by'] == '담당자'
    with pytest.raises(ValueError):
        digest.preview(config={**values, 'app_url': 'javascript:alert(1)'})


def test_yesterday_progress_today_work_all_owners_and_escape():
    t = store.create_task(
        '오늘 마감 <script>alert(1)</script>', owner='다른 담당', due_date='2026-11-16'
    )
    store.create_task(
        '오늘 시작 겸 지난 마감', start_date='2026-11-16', due_date='2026-11-14'
    )
    store.create_task('지난 마감 미완료', due_date='2026-11-13')
    store.create_task('다음 주 업무', due_date='2026-11-23')
    store.create_task('기간 밖 업무', due_date='2026-11-24')
    completed = store.create_task('전날 완료한 업무')
    deleted = store.create_task('휴지통 업무', due_date='2026-11-16')
    old = store.add_history(
        t['id'], '나중에 입력한 이전 회의', occurred_at='2026-11-01'
    )
    late = store.add_history(
        t['id'], '전날 입력이지만 미래 일자', occurred_at='2026-12-01'
    )
    yesterday = store.add_history(t['id'], '전날 회의 내용', occurred_at='2026-11-15')
    hidden = store.add_history(t['id'], '휴지통 기록', occurred_at='2026-11-15')
    with db.transaction() as s:
        for h in s.query(HistoryEntry).all():
            h.created_at = datetime(2026, 10, 1)
            h.updated_at = datetime(2026, 10, 1)
        s.get(HistoryEntry, old['id']).updated_at = datetime(2026, 11, 15, 23, 59)
        s.get(HistoryEntry, late['id']).created_at = datetime(2026, 11, 15)
        s.get(HistoryEntry, hidden['id']).deleted_at = datetime(2026, 11, 15)
        done = s.get(Task, completed['id'])
        done.status = '완료'
        done.done_at = datetime(2026, 11, 15, 23, 59)
        s.get(Task, deleted['id']).deleted_at = datetime(2026, 11, 15)
    message = digest.preview('2026-11-16', config())
    text, html = message['text'], message['table_html']
    for title in (
        '다른 담당',
        '오늘 시작 겸 지난 마감',
        '지난 마감 미완료',
        '다음 주 업무',
        '전날 완료한 업무',
        '나중에 입력한 이전 회의',
        '전날 입력이지만 미래 일자',
        '전날 회의 내용',
        '기록 일자 2026-11-01',
    ):
        assert title in text
    for title in ('휴지통 업무', '휴지통 기록', '기간 밖 업무'):
        assert title not in text
    assert text.count('오늘 시작 겸 지난 마감') == 1
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert f'https://work.example/history/{yesterday["id"]}' in html
    assert f'https://work.example/task/{t["id"]}' in html


def test_truncation_is_explicit(monkeypatch):
    monkeypatch.setattr(digest, 'LIMIT', 2)
    for i in range(3):
        store.create_task(f'오늘{i}', due_date='2026-11-16')
    text = digest.preview('2026-11-16', config())['text']
    assert '표시 한도 2건' in text and '오늘2' not in text


def test_timezone_window_handles_dst(monkeypatch):
    monkeypatch.setenv('TZ', 'UTC')
    begin, end = digest._wall_window(date(2026, 11, 1), 'America/New_York')
    assert (end - begin).total_seconds() == 25 * 3600
    assert begin == datetime(2026, 11, 1, 4)


def test_due_boundary_restart_catchup_weekends_and_next_day(monkeypatch):
    digest.save(config())
    sent = []
    monkeypatch.setattr(
        outbox, 'send', lambda *a, **kw: sent.append((a, kw)) or {'id': len(sent)}
    )
    assert not digest.dispatch_due(utc('2026-11-15T21:59:59'))  # 한국 월요일 06:59
    assert digest.dispatch_due(utc('2026-11-15T22:00:00'))
    assert not digest.dispatch_due(utc('2026-11-16T09:00:00'))  # 재시작해도 당일 1회
    assert digest.dispatch_due(utc('2026-11-17T03:00:00'))  # 당일 12시 복구 시 당일분
    assert len(sent) == 2 and sent[0][0][0] == 'a@example.com, b@example.com'
    assert sent[0][1]['related_kind'] == 'morning_digest'
    values = config()
    values['weekdays_only'] = True
    digest.save(values)
    assert not digest.dispatch_due(utc('2026-11-21T00:00:00'))  # 토요일
    assert digest.deliveries()[0]['status'] == 'sent'
    with pytest.raises(ValueError):
        digest.dispatch_due(datetime(2026, 11, 18, 7))


def test_multiple_workers_claim_only_one_attempt(monkeypatch):
    digest.save(config())
    barrier = threading.Barrier(2)
    original = digest.settings
    local = threading.local()

    def synchronized_settings():
        values = original()
        if not getattr(local, 'loaded', False):
            local.loaded = True
            barrier.wait(timeout=5)
        return values

    monkeypatch.setattr(digest, 'settings', synchronized_settings)
    sent = []
    monkeypatch.setattr(outbox, 'send', lambda *a, **kw: sent.append(a) or {'id': 1})
    with ThreadPoolExecutor(2) as executor:
        results = list(
            executor.map(digest.dispatch_due, [utc('2026-11-15T22:00:00')] * 2)
        )
    assert sorted(results) == [False, True] and len(sent) == 1


def test_failure_records_outbox_and_does_not_retry(monkeypatch):
    digest.save(config())
    calls = []

    def fail(*a, **kw):
        calls.append(1)
        raise requests.exceptions.Timeout('response timeout')

    monkeypatch.setattr(requests, 'post', fail)
    assert digest.dispatch_due(utc('2026-11-15T22:00:00'))
    assert not digest.dispatch_due(utc('2026-11-16T03:00:00'))
    assert len(calls) == 1
    row = digest.deliveries()[0]
    assert row['status'] == 'failed' and row['outbound_id']
    assert outbox.list_sent()[0]['status'] == 'failed'


def test_settings_changed_during_generation_prevents_send(monkeypatch):
    digest.save(config())
    original = digest.preview

    def change(day, values):
        values = {**values, 'enabled': False}
        digest.save(values)
        return original(day, values)

    monkeypatch.setattr(digest, 'preview', change)
    monkeypatch.setattr(
        outbox, 'send', lambda *a, **kw: pytest.fail('설정 변경 후 발송 금지')
    )
    assert digest.dispatch_due(utc('2026-11-15T22:00:00'))
    assert digest.deliveries()[0]['status'] == 'failed'
