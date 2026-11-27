"""원장님 일정의 보고 기록 — 보고 내용·피드백을 '보고' 이력으로, 관련 업무·보고 단계와 함께."""
import pytest

from services import db, reporting, schedule_records, schedules, store


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))


def test_record_with_task_marks_director_stage():
    e = schedules.save({'date': '2026-11-27', 'start_time': '14:00', 'title': '2027 사업계획 보고', 'category': '보고'})
    t = reporting.set_target(store.create_task('2027 사업계획')['id'], '대표이사')
    h = schedule_records.add(e['id'], '사업계획 초안 설명', '예산 10% 줄여서 다시 보고', t['id'], '담당자',
                             mark_stage=True)
    assert h['entry_type'] == '보고' and h['task_id'] == t['id']
    assert h['title'] == '원장님 보고: 2027 사업계획 보고' and h['occurred_at'].startswith('2026-11-27 14:00')
    assert '보고 내용:\n사업계획 초안 설명' in h['content'] and '피드백·지시:\n예산 10%' in h['content']
    p = reporting.progress(store.get_task(t['id']))
    assert p['reached'] == {'원장': '2026-11-27'}
    assert [x['entry_type'] for x in store.list_history(t['id'])].count('보고') == 1  # 단계 표시로 기록이 겹치지 않음
    assert [x['id'] for x in schedule_records.for_schedule(e['id'])] == [h['id']]
    assert schedule_records.counts([e['id'], 999]) == {e['id']: 1}


def test_record_without_task_and_validation():
    store.create_person('김상무')
    e = schedules.save({'date': '2026-11-27', 'title': '현안 보고', 'attendees': '김상무'})
    h = schedule_records.add(e['id'], feedback='다음 주 다시')  # 업무 없이 — 참석한 임원에 연결
    assert h['task_id'] is None and h['occurred_at'].startswith('2026-11-27 09:00')
    assert [p['name'] for p in store.get_history(h['id'])['people']] == ['김상무']
    lone = schedules.save({'date': '2026-11-27', 'title': '혼자 보고'})
    with pytest.raises(ValueError, match='관련 업무'):
        schedule_records.add(lone['id'], '내용')
    plain = store.create_task('보고 업무 아님')
    schedule_records.add(e['id'], '내용', task_id=plain['id'], mark_stage=True)
    assert store.get_task(plain['id'])['report_log'] == {}  # 보고 업무가 아니면 단계는 건드리지 않음
    with pytest.raises(ValueError):
        schedule_records.add(e['id'], ' ', ' ')
    with pytest.raises(ValueError):
        schedule_records.add(999, '내용')
    with pytest.raises(ValueError):
        schedule_records.add(e['id'], '내용', task_id=999)


def test_people_schedule_does_not_mark_director_stage():
    e = schedules.save({'date': '2026-11-27', 'title': '피플팀장 보고'}, owner='people')
    t = reporting.set_target(store.create_task('인사 제도')['id'], '원장')
    h = schedule_records.add(e['id'], '내용', task_id=t['id'], mark_stage=True)
    assert h['title'].startswith('피플팀장 보고:') and store.get_task(t['id'])['report_log'] == {}
