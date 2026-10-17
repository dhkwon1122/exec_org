import pytest
from sqlalchemy import create_engine, inspect, text

from services import db, people_sync, store, users


def _researchers(path, rows):
    """Researcher-board load_to_db 처럼 전 컬럼 TEXT 로 researchers 테이블을 만든다."""
    cols = ['researcher_id', 'name', 'department', 'org_code', 'position', 'job_function', 'knox_id',
            'employment_status', 'valid_year', 'valid_month', 'is_current']
    with create_engine(f'sqlite:///{path}').begin() as c:
        c.execute(text('DROP TABLE IF EXISTS researchers'))
        c.execute(text('CREATE TABLE researchers (' + ', '.join(f'{col} TEXT' for col in cols) + ')'))
        c.execute(text(f"INSERT INTO researchers VALUES ({', '.join(':' + col for col in cols)})"),
                  [dict(zip(cols, r)) for r in rows])


ROWS = [
    ('E001', '김영수', '전략기획실', 'D01', '부사장', '', 'ys.kim', '재직', '2026', '9', 'Y'),
    ('E002', '이민정', 'AI센터', 'D02', '상무', '', 'mj.lee', '재직', '2026', '9', 'Y'),
    ('E003', '박철호', '경영지원', 'D03', '사장', '', 'ch.park', '재직', '2026', '9', 'Y'),
    ('E004', '김지훈', '소자연구소', 'D04', '상무', '', 'jh.kim', '재직', '2026', '9', 'Y'),
    ('E005', '김태현', '반도체연구소', 'D05', '상무', '', 'th.kim', '재직', '2026', '9', 'Y'),
    ('R100', '최연구', 'AI센터', 'D02', 'CL3', '', 'yg.choi', '재직', '2026', '9', 'Y'),  # 임원 아님
    ('E009', '정퇴임', '전략기획실', 'D01', '상무', '', 'tr.jung', '재직', '2026', '6', 'N'),  # 과거 시점
]


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    db.configure('sqlite:///' + str(tmp_path / 'app.db'))
    people_db = tmp_path / 'people.db'
    _researchers(people_db, ROWS)
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{people_db}')
    monkeypatch.delenv('USER_DATABASE_URL', raising=False)
    monkeypatch.delenv('EXEC_ORG_POSITIONS', raising=False)
    users.reset()
    yield people_db
    users.reset()


def test_fetch_only_current_executives():
    execs = people_sync.fetch_executives()
    assert [(e['name'], e['position'], e['org']) for e in execs] == [
        ('박철호', '사장', '경영지원'), ('김영수', '부사장', '전략기획실'),
        ('김지훈', '상무', '소자연구소'), ('김태현', '상무', '반도체연구소'), ('이민정', '상무', 'AI센터')]


def test_sync_adds_with_title_aliases_and_preserves_local_fields(env):
    r = people_sync.sync_people()
    assert r['total'] == 5 and len(r['added']) == 5 and r['missing'] == []
    by_name = {p['name']: p for p in store.list_people()}
    assert by_name['김영수']['aliases'] == ['김부사장', '김 부사장']
    assert by_name['이민정']['aliases'] == ['이상무', '이 상무']
    assert by_name['김지훈']['aliases'] == []  # 김상무가 둘이라 모호 → 별칭 안 만듦
    assert by_name['김영수']['source_id'] == 'E001'
    # 원문 자동 연결이 바로 된다
    assert store.detect_people('김부사장 보고 일정') == [by_name['김영수']['id']]

    # 로컬에서 고친 별칭·메모는 다음 불러오기에서 유지, 승진·부서 이동은 반영
    store.update_person(by_name['김영수']['id'], aliases='김부사장, 전략실장', note='해외 경험 많음')
    _researchers(env, [('E001', '김영수', '미래전략실', 'D09', '사장', '', '', '재직', '2026', '10', 'Y')]
                 + [r for r in ROWS if r[0] != 'E001'])
    r = people_sync.sync_people()
    assert r['added'] == [] and r['updated'] == ['김영수 (position, org)'] and r['unchanged'] == 4
    p = store.get_person(by_name['김영수']['id'])
    assert (p['position'], p['org'], p['aliases'], p['note']) == ('사장', '미래전략실', ['김부사장', '전략실장'],
                                                                  '해외 경험 많음')


def test_sync_links_manually_added_person_by_unique_name():
    mine = store.create_person('이민정', '상무', aliases='이상무')
    h = store.add_history(None, '이상무 동향', '동향', person_ids=[mine['id']])
    store.create_person('김태현', '상무')
    store.create_person('김태현', '전무')  # 같은 이름 둘 → 어느 쪽인지 모르므로 연결 안 함
    r = people_sync.sync_people()
    assert r['linked'] == ['이민정'] and '김태현' in r['added']
    p = store.get_person(mine['id'])
    assert p['source_id'] == 'E002' and p['org'] == 'AI센터'
    assert store.get_history(h['id'])['people'][0]['id'] == mine['id']  # 기존 기록 연결 유지


def test_sync_reports_executives_no_longer_listed(env):
    people_sync.sync_people()
    _researchers(env, [r for r in ROWS if r[0] != 'E002'])
    r = people_sync.sync_people()
    assert r['missing'] == ['이민정 상무']
    assert any(p['name'] == '이민정' and p['active'] for p in store.list_people())  # 자동으로 지우거나 멈추지 않음


def test_positions_configurable(monkeypatch):
    monkeypatch.setenv('EXEC_ORG_POSITIONS', '사장,부사장')
    assert [e['name'] for e in people_sync.fetch_executives()] == ['박철호', '김영수']


def test_sync_errors(monkeypatch, tmp_path):
    monkeypatch.delenv('DATABASE_URL')
    assert not people_sync.is_available()
    with pytest.raises(people_sync.SyncError):
        people_sync.sync_people()
    empty = tmp_path / 'empty.db'
    create_engine(f'sqlite:///{empty}').connect().close()
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{empty}')
    with pytest.raises(people_sync.SyncError, match='researchers'):
        people_sync.sync_people()


def test_people_in_sait_db_is_read_only(env):
    eng = create_engine(f'sqlite:///{env}')
    before = set(inspect(eng).get_table_names())
    people_sync.sync_people()
    assert set(inspect(eng).get_table_names()) == before
    with eng.connect() as c:
        assert c.execute(text('SELECT COUNT(*) FROM researchers')).scalar() == len(ROWS)
