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
                          'place': '미국 산호세', 'deputy': '박민수 상무'}, person_ids=[kim['id'], lee['id']], by='권동혁')
    assert [(r['person_name'], r['kind']) for r in trip] == [('김영수', '해외출장'), ('이영희', '해외출장')]
    half = absences.save({'kind': '휴가', 'start_date': '2026-10-22', 'half': '오후'}, person_ids=[kim['id']])[0]
    assert (half['end_date'], absences.days(half), absences.days(trip[0])) == ('2026-10-22', 0.5, 5)
    rows = absences.list_range('2026-10-01', '2026-10-31')
    assert [(r['person_name'], r['start_date']) for r in rows] == [
        ('김영수', '2026-10-22'), ('김영수', '2026-10-26'), ('이영희', '2026-10-26')]
    assert [r['person_name'] for r in absences.on_day('2026-10-28', rows)] == ['김영수', '이영희']
    assert absences.list_range('2026-10-01', '2026-10-31', kinds=['휴가'])[0]['id'] == half['id']
    assert len(absences.list_range('2026-10-01', '2026-10-31', query='산호세')) == 2
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
    # 여러 날짜면 반차는 무시
    r = absences.save({'start_date': '2026-10-26', 'end_date': '2026-10-27', 'half': '오전'}, person_ids=[kim['id']])[0]
    assert r['half'] == ''
    today = date.today()
    past = absences.save({'start_date': (today - timedelta(days=30)).isoformat()}, person_ids=[kim['id']])[0]
    soon = absences.save({'start_date': (today + timedelta(days=3)).isoformat()}, person_ids=[kim['id']])[0]
    ids = [x['id'] for x in absences.for_person(kim['id'])]
    assert ids.index(soon['id']) < ids.index(past['id'])  # 앞으로의 부재가 먼저
    store.delete_person(kim['id'])
    assert absences.list_range('2000-01-01', '2100-01-01') == []  # 임원을 지우면 부재도 지워짐
