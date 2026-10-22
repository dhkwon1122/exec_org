from datetime import date, timedelta

import pytest

from services import absences, db, store


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    yield


def test_register_list_and_overlap():
    kim = store.create_person('김영수', position='부사장')
    lee = store.create_person('이영희', position='상무')
    trip = absences.save({'kind': '해외출장', 'start_date': '2026-10-26', 'end_date': '2026-10-30',
                          'place': '미국 산호세', 'content': '반도체 학회 참석', 'note': '연락처 010-0000-0000'},
                         person_ids=[kim['id'], lee['id']], by='권동혁')
    assert [(r['person_name'], r['kind']) for r in trip] == [('김영수', '해외출장'), ('이영희', '해외출장')]
    assert (trip[0]['place'], trip[0]['content'], trip[0]['time_range']) == ('미국 산호세', '반도체 학회 참석', '')
    half = absences.save({'kind': '휴가', 'start_date': '2026-10-22', 'time_range': '14시-18시'},
                         person_ids=[kim['id']])[0]
    assert (half['end_date'], half['time_range'], absences.days(trip[0])) == ('2026-10-22', '14:00~18:00', 5)
    assert absences.norm_time_range('오후') == '오후' and absences.norm_time_range('9:30') == '09:30~'
    rows = absences.list_range('2026-10-01', '2026-10-31')
    assert [(r['person_name'], r['start_date']) for r in rows] == [
        ('김영수', '2026-10-22'), ('김영수', '2026-10-26'), ('이영희', '2026-10-26')]
    assert [r['person_name'] for r in absences.on_day('2026-10-28', rows)] == ['김영수', '이영희']
    assert absences.list_range('2026-10-01', '2026-10-31', kinds=['휴가'])[0]['id'] == half['id']
    assert len(absences.list_range('2026-10-01', '2026-10-31', query='산호세')) == 2
    assert len(absences.list_range('2026-10-01', '2026-10-31', query='학회')) == 2
    assert [r['id'] for r in absences.overlaps(kim['id'], '2026-10-29', '2026-11-02')] == [trip[0]['id']]
    assert absences.overlaps(kim['id'], '2026-10-29', '2026-11-02', exclude=trip[0]['id']) == []
    # 고치기·지우기
    absences.save({'kind': '출장', 'start_date': '2026-10-27', 'end_date': '2026-10-28'}, aid=trip[1]['id'])
    assert absences.get(trip[1]['id'])['start_date'] == '2026-10-27'
    absences.delete(half['id'])
    assert absences.get(half['id']) is None


def test_validation_and_person_view():
    kim = store.create_person('김영수')
    with pytest.raises(ValueError):
        absences.save({'start_date': '2026-10-26', 'end_date': '2026-10-20'}, person_ids=[kim['id']])
    with pytest.raises(ValueError):
        absences.save({'start_date': '2026-10-26'}, person_ids=[])
    with pytest.raises(ValueError):
        absences.save({'start_date': ''}, person_ids=[kim['id']])
    today = date.today()
    past = absences.save({'start_date': (today - timedelta(days=30)).isoformat()}, person_ids=[kim['id']])[0]
    soon = absences.save({'start_date': (today + timedelta(days=3)).isoformat()}, person_ids=[kim['id']])[0]
    ids = [x['id'] for x in absences.for_person(kim['id'])]
    assert ids.index(soon['id']) < ids.index(past['id'])  # 앞으로의 부재가 먼저
    store.delete_person(kim['id'])
    assert absences.list_range('2000-01-01', '2100-01-01') == []  # 임원을 지우면 부재도 지워짐


def test_rule_extract_keeps_deputy_out_and_reads_time_and_place():
    store.create_person('이영희', position='상무')
    store.create_person('박민수', position='상무')
    rows, _ = absences.extract('이영희 상무 11/9~11/10 휴가 (대결: 박민수 상무)', '2026-10-21')
    assert [(r['person'], r['start_date'], r['end_date'], r['note']) for r in rows] == [
        ('이영희', '2026-11-09', '2026-11-10', '대결: 박민수 상무')]  # 대결자는 부재가 아니라 메모로
    rows, _ = absences.extract('이영희 상무 10/27 14:00~16:00 외부 일정 (고객사 방문)', '2026-10-21')
    assert [(r['kind'], r['time_range'], r['place']) for r in rows] == [('외부 일정', '14:00~16:00', '고객사 방문')]
    rows, _ = absences.extract('이영희 상무 10/27 오후 반차', '2026-10-21')
    assert rows[0]['time_range'] == '오후'


def test_old_half_and_deputy_move(tmp_path):
    from sqlalchemy import create_engine, text
    path = tmp_path / 'old.db'
    db.configure('sqlite:///' + str(path))
    kim = store.create_person('김영수')
    a = absences.save({'start_date': '2026-10-22', 'note': '연락처 010'}, person_ids=[kim['id']])[0]
    with create_engine(f'sqlite:///{path}').begin() as c:  # 칸 정리 전에 남은 값
        c.execute(text("UPDATE absences SET half='오전', deputy='박민수 상무' WHERE id=:i"), {'i': a['id']})
    db.configure('sqlite:///' + str(path))
    got = absences.get(a['id'])
    assert (got['time_range'], got['note']) == ('오전', '연락처 010\n대결: 박민수 상무')
