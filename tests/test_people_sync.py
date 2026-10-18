import pytest
from sqlalchemy import create_engine, inspect, text

from services import db, people_sync, store, users


COLS = ['researcher_id', 'name', 'department', 'org_code', 'position', 'job_function', 'knox_id',
        'employment_status', 'valid_year', 'valid_month', 'is_current']
ORDER_COLS = ['researcher_id', 'order_date', 'order_name', 'order_dep', 'order_cl', 'order_assignment']


def _text_table(path, name, cols, rows):
    """Researcher-board load_to_db 처럼 전 컬럼 TEXT 로 테이블을 만든다."""
    with create_engine(f'sqlite:///{path}').begin() as c:
        c.execute(text(f'DROP TABLE IF EXISTS {name}'))
        c.execute(text(f'CREATE TABLE {name} (' + ', '.join(f'{col} TEXT' for col in cols) + ')'))
        if rows:
            c.execute(text(f"INSERT INTO {name} VALUES ({', '.join(':' + col for col in cols)})"),
                      [dict(zip(cols, r)) for r in rows])


def _researchers(path, rows):
    _text_table(path, 'researchers', COLS, rows)


ROWS = [
    ('E001', '김영수', '전략기획실', 'D01', '부사장', '', 'ys.kim', '재직', '2026', '9', 'Y'),
    ('E002', '이민정', 'AI센터', 'D02', '상무', '', 'mj.lee', '재직', '2026', '9', 'Y'),
    ('E003', '박철호', '경영지원', 'D03', '사장', '', 'ch.park', '재직', '2026', '9', 'Y'),
    ('E004', '김지훈', '소자연구소', 'D04', '상무', '', 'jh.kim', '재직', '2026', '9', 'Y'),
    ('E005', '김태현', '반도체연구소', 'D05', '상무', '', 'th.kim', '재직', '2026', '9', 'Y'),
    ('E006', '한수진', '소재연구소', 'D06', 'Master', '', 'sj.han', '재직', '2026', '9', 'Y'),
    ('E007', '오민석', 'AI센터', 'D02', ' FELLOW', '', 'ms.oh', '재직', '2026', '9', 'Y'),  # 대소문자·공백 달라도
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
        ('김지훈', '상무', '소자연구소'), ('김태현', '상무', '반도체연구소'), ('이민정', '상무', 'AI센터'),
        ('한수진', 'Master', '소재연구소'), ('오민석', 'Fellow', 'AI센터')]


def test_sync_adds_with_title_aliases_and_preserves_local_fields(env):
    r = people_sync.sync_people()
    assert r['total'] == 7 and len(r['added']) == 7 and r['missing'] == []
    by_name = {p['name']: p for p in store.list_people()}
    assert by_name['김영수']['aliases'] == ['김부사장', '김 부사장']
    assert by_name['이민정']['aliases'] == ['이상무', '이 상무']
    assert by_name['김지훈']['aliases'] == []  # 김상무가 둘이라 모호 → 별칭 안 만듦
    assert by_name['한수진']['aliases'] == ['한마스터', '한 마스터', '한 Master']
    assert by_name['오민석']['aliases'] == ['오펠로우', '오 펠로우', '오펠로', '오 펠로', '오 Fellow']
    assert store.detect_people('한 마스터님 의견') == [by_name['한수진']['id']]
    assert by_name['김영수']['source_id'] == 'E001'
    # 원문 자동 연결이 바로 된다
    assert store.detect_people('김부사장 보고 일정') == [by_name['김영수']['id']]

    # 로컬에서 고친 별칭·메모는 다음 불러오기에서 유지, 승진·부서 이동은 반영
    store.update_person(by_name['김영수']['id'], aliases='김부사장, 전략실장', note='해외 경험 많음')
    _researchers(env, [('E001', '김영수', '미래전략실', 'D09', '사장', '', '', '재직', '2026', '10', 'Y')]
                 + [r for r in ROWS if r[0] != 'E001'])
    r = people_sync.sync_people()
    assert r['added'] == [] and r['updated'] == ['김영수 (직위, 소속)'] and r['unchanged'] == 6
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
    monkeypatch.setenv('EXEC_ORG_POSITIONS', 'fellow')  # 설정 쪽 대소문자도 무시, 표기는 설정값
    assert [(e['name'], e['position']) for e in people_sync.fetch_executives()] == [('오민석', 'fellow')]


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


def _keys(plan, kind=None):
    return [i['key'] for i in plan['items'] if kind is None or i['kind'] == kind]


def test_plan_only_proposes_and_apply_takes_selected(env):
    people_sync.sync_people()
    by_name = {p['name']: p for p in store.list_people()}
    _researchers(env, [('E001', '김영수', '미래전략실', 'D09', '사장', '', '', '재직', '2026', '10', 'Y'),
                       ('E010', '신임원', '경영지원', 'D03', '상무', '', '', '재직', '2026', '10', 'Y')]
                 + [r for r in ROWS if r[0] != 'E001'])
    plan = people_sync.plan_sync()
    assert sorted(_keys(plan)) == ['add:E010', 'set:E001:org', 'set:E001:position']
    labels = {i['key']: i['label'] for i in plan['items']}
    assert labels['set:E001:position'] == '김영수 직위: 부사장 → 사장'
    assert store.get_person(by_name['김영수']['id'])['position'] == '부사장'  # 제안만으로는 안 바뀐다

    # 직위만 덮어쓰고 소속은 유지, 새 임원은 추가하지 않음
    r = people_sync.apply_sync(plan, ['set:E001:position'])
    assert r['updated'] == ['김영수 (직위)'] and r['kept'] == ['김영수 소속'] and r['added'] == []
    p = store.get_person(by_name['김영수']['id'])
    assert (p['position'], p['org']) == ('사장', '전략기획실')

    # 다음 제안: 유지한 소속은 '지난번에 유지함'으로 기본 해제, 추가 제안은 여전히 나온다
    plan = people_sync.plan_sync()
    kept = [i for i in plan['items'] if i['kind'] == 'set']
    assert len(kept) == 1 and kept[0]['kept'] and '지난번에 유지함' in kept[0]['label']
    assert people_sync.default_keys(plan) == ['add:E010']

    # People in SAIT 값이 또 바뀌면 다시 기본 선택
    _researchers(env, [('E001', '김영수', '신사업실', 'D09', '사장', '', '', '재직', '2026', '11', 'Y')]
                 + [r for r in ROWS if r[0] != 'E001'])
    plan = people_sync.plan_sync()
    assert 'set:E001:org' in people_sync.default_keys(plan)


def test_link_is_a_proposal_and_its_changes_need_it(env):
    mine = store.create_person('이민정', '전무', aliases='이상무')
    plan = people_sync.plan_sync()
    assert {'link:E002', 'set:E002:position', 'set:E002:org'} <= set(_keys(plan))
    # 연결을 고르지 않으면 변경도 반영되지 않는다
    people_sync.apply_sync(plan, ['set:E002:position'])
    assert store.get_person(mine['id'])['source_id'] == ''
    plan = people_sync.plan_sync()
    r = people_sync.apply_sync(plan, ['link:E002', 'set:E002:position'])
    p = store.get_person(mine['id'])
    assert r['linked'] == ['이민정'] and (p['source_id'], p['position'], p['org']) == ('E002', '상무', '')


def test_apply_skips_stale_proposals():
    plan = people_sync.plan_sync()
    people_sync.apply_sync(plan, _keys(plan, 'add'))
    r = people_sync.apply_sync(plan, _keys(plan, 'add'))  # 같은 제안을 두 번 반영해도 중복 추가 없음
    assert r['added'] == [] and len(r['skipped']) == 7
    assert len(store.list_people()) == 7


HISTORY = [  # 월별 스냅샷 — 김영수: CL4 → 상무 → 부사장, 한과거: 상무였다가 지금은 목록에 없음
    ('E001', '김영수', '전략기획실', 'D01', 'CL4', '', '', '재직', '2022', '1', ''),
    ('E001', '김영수', '전략기획실', 'D01', 'CL4', '', '', '재직', '2022', '2', ''),
    ('E001', '김영수', 'AI센터', 'D02', '상무', '', '', '재직', '2023', '3', ''),
    ('E001', '김영수', 'AI센터', 'D02', '상무', '', '', '재직', '2024', '3', ''),
    ('E001', '김영수', '전략기획실', 'D01', '부사장', '', '', '재직', '2025', '3', ''),
    ('P001', '한과거', '소재연구소', 'D06', '상무', '', '', '재직', '2021', '6', ''),
    ('P001', '한과거', '소재연구소', 'D06', 'Corporate VP', '', '', '재직', '2022', '1', ''),
    ('P001', '한과거', '경영지원', 'D03', '부사장', '', '', '재직', '2023', '1', ''),
]
ORDERS = [
    ('E001', '2023-03-01', '임원 선임', 'AI센터', '상무', '센터장'),
    ('E001', '2025-03-01', '승진', '전략기획실', '부사장', '실장'),
    ('P001', '2023-12-31', '퇴임', '경영지원', '부사장', ''),
]


def test_past_executives_and_career_accumulate(env):
    _text_table(env, 'researchers_history', COLS, HISTORY)
    _text_table(env, 'hr_orders', ORDER_COLS, ORDERS)
    plan = people_sync.plan_sync()
    past = {i['name']: i for i in plan['items'] if i['kind'] == 'past'}
    # 이력에서 마지막 임원 시점 값, researchers 에 지난 시점(N)으로 남은 정퇴임도 과거 임원
    assert past['한과거']['label'] == '한과거 · 부사장 · 경영지원 (2023-01까지)'
    assert '정퇴임' in past and plan['past_total'] == 2
    assert '김영수' not in past  # 현재 임원은 과거 임원으로 중복 제안하지 않는다

    r = people_sync.apply_sync(plan, people_sync.default_keys(plan))
    assert sorted(r['past_added']) == ['정퇴임', '한과거'] and len(r['added']) == 7
    by_name = {p['name']: p for p in store.list_people(include_inactive=True)}
    assert by_name['한과거']['active'] is False  # 추적 중지 상태 — 목록·자동 연결에서 빠짐
    assert all(p['name'] != '한과거' for p in store.list_people())

    kim = store.list_career(by_name['김영수']['id'])
    assert [(c['date'], c['kind'], c['position'], c['org'], c['title']) for c in kim] == [
        ('2025-03-01', '발령', '부사장', '전략기획실', '승진'),
        ('2025-03', '직급·소속', '부사장', '전략기획실', ''),
        ('2023-03-01', '발령', '상무', 'AI센터', '임원 선임'),
        ('2023-03', '직급·소속', '상무', 'AI센터', ''),
        ('2022-01', '직급·소속', 'CL4', '전략기획실', '')]  # 바뀐 달만 (2022-02, 2024-03 는 같은 값)
    assert [c['title'] for c in store.list_career(by_name['한과거']['id']) if c['kind'] == '발령'] == ['퇴임']

    # 다시 불러와도 중복 없음, 원천에서 지워져도 남고, 새 이력은 더 쌓인다
    _text_table(env, 'researchers_history', COLS, HISTORY[-3:] + [
        ('E001', '김영수', '미래전략실', 'D09', '부사장', '', '', '재직', '2026', '3', '')])
    _text_table(env, 'hr_orders', ORDER_COLS, [])
    plan = people_sync.plan_sync()
    assert plan['career_new'] == 1 and plan['items'] == []
    r = people_sync.apply_sync(plan, [])
    assert r['career_added'] == 1
    kim = store.list_career(by_name['김영수']['id'])
    assert kim[0]['date'] == '2026-03' and len(kim) == 6


def test_career_for_manually_linked_and_missing_tables(env):
    mine = store.create_person('김영수', '부사장')
    people_sync.sync_people()  # history·orders 테이블이 없는 예전 적재본이어도 동작
    assert store.get_person(mine['id'])['source_id'] == 'E001' and store.list_career(mine['id']) == []
    _text_table(env, 'hr_orders', ORDER_COLS, ORDERS)
    plan = people_sync.plan_sync()
    assert plan['career_new'] == 2  # 이미 연결된 김영수(E001)의 발령 2건
    people_sync.apply_sync(plan, [])
    assert len(store.list_career(mine['id'])) == 2
    store.delete_person(mine['id'])  # 임원을 지우면 경력 이력도 같이
    with db.SessionLocal() as s:
        assert s.query(db.PersonCareer).count() == 0
