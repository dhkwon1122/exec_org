"""리더급 — team_refer 직책자 불러오기(services/leader_sync.py), 목록·엑셀·메일 첨부(services/leaders.py)."""
import io

import pytest
from openpyxl import load_workbook

from services import db, leader_sync, leaders, outbox, people_sync, profile, store, users
from tests.test_people_sync import COLS


def _text_table(path, name, cols, rows):
    """전 컬럼 TEXT 표 (2026_salary_grade 처럼 숫자로 시작하는 이름도 되게 따옴표로)."""
    from sqlalchemy import create_engine, text
    with create_engine(f'sqlite:///{path}').begin() as c:
        c.execute(text(f'DROP TABLE IF EXISTS {name}'))
        c.execute(text(f'CREATE TABLE {name} (' + ', '.join(f'"{col}" TEXT' for col in cols) + ')'))
        for r in rows:
            c.execute(text(f"INSERT INTO {name} VALUES ({', '.join(f':p{i}' for i in range(len(cols)))})"),
                      {f'p{i}': v for i, v in enumerate(r)})

TEAM_COLS = ['org_name_wd', 'work_type', 'dep_1st_name', 'dep_2nd_name', 'dep_3rd_name', 'dep_code', 'dep_id',
             'upper_dep_id', 'team_layer', 'researcher_id', 'name', 'assignment_name', 'deleted',
             'valid_year', 'valid_month', 'valid_day']
PEOPLE = [
    ('E001', '김영수', '전략기획실', 'D01', '부사장', '', '', '재직', '2026', '9', 'Y', '1968', '1995-03-01', '2023-01-01'),
    ('L001', '박팀장', 'AI팀', 'D11', 'CL4', '', '', '재직', '2026', '9', 'Y', '1978', '2005-01-01', '2020-03-01'),
    ('L002', '정그룹', '반도체연구소', 'D12', 'CL4', '', '', '재직', '2026', '9', 'Y', '1975', '2003-01-01', '2019-03-01'),
    ('L003', '최예전', '소재팀', 'D13', 'CL3', '', '', '재직', '2026', '9', 'Y', '1980', '2008-01-01', '2021-03-01'),
    ('L004', '한직접', '경영지원', 'D14', 'CL3', '', '', '재직', '2026', '9', 'Y', '1982', '2010-01-01', '2022-03-01'),
]


def _team(path, rows):
    _text_table(path, 'team_refer', TEAM_COLS, rows)


TEAM = [  # 조직 행 — 조직장급만 사번이 있다
    ('AI팀', 'R&D', 'AI센터', 'AI팀', '', '1010', 'D-AI', 'D-C', '2', 'L001', '박팀장', '팀장', '', '2026', '09', '30'),
    ('AI센터', 'R&D', 'AI센터', '', '', '1000', 'D-C', '', '1', 'E001', '김영수', '센터장', '', '2026', '09', '30'),
    ('반도체연구소', 'R&D', '반도체연구소', '', '', '2000', 'D-S', '', '1', 'L002', '정그룹', '그룹장', '', '2026', '09', '30'),
    ('반도체1팀', 'R&D', '반도체연구소', '반도체1팀', '', '2010', 'D-S1', 'D-S', '2', 'L002', '정그룹', '팀장', '', '2026', '09', '30'),
    ('소재팀', 'R&D', '소재연구소', '소재팀', '', '3010', 'D-M', '', '2', 'L003', '최예전', '팀장', '', '2026', '06', '30'),
    ('소재팀', 'R&D', '소재연구소', '소재팀', '', '3010', 'D-M', '', '2', '', '', '', 'Y', '2026', '09', '30'),  # 없어짐
    ('일반파트', 'R&D', 'AI센터', 'AI팀', '일반파트', '1011', 'D-P', 'D-AI', '3', '', '', '', '', '2026', '09', '30'),
]


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    db.configure('sqlite:///' + str(tmp_path / 'app.db'))
    pdb = tmp_path / 'people.db'
    _text_table(pdb, 'researchers', COLS, PEOPLE)
    _team(pdb, TEAM)
    _text_table(pdb, 'evaluations', ['researcher_id', '2026_salary_grade', '2025_second_half_grade', 'is_current'],
                [('L001', '가', 'EM', 'Y'), ('L002', '', 'ES', 'Y')])
    _text_table(pdb, 'leadership', ['researcher_id', 'year', 'evaluator_group', '미래통찰', '성과창출', '몰입촉진',
                                    '인재육성', '자기관리', '저해행동'],
                [('L001', '2025', '타인평균', '4.0', '3.8', '3.6', '4.2', '4.0', '1.2'),
                 ('L001', '2025', '본인', '4.5', '4.5', '4.5', '4.5', '4.5', '1.0'),
                 ('L001', '2024', '타인평균', '3.5', '3.5', '3.5', '3.5', '3.5', '1.5')])
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{pdb}')
    monkeypatch.delenv('USER_DATABASE_URL', raising=False)
    monkeypatch.delenv('EXEC_ORG_POSITIONS', raising=False)
    users.reset()
    yield pdb
    users.reset()


def test_fetch_leaders_current_non_exec_holders():
    got = {x['name']: x for x in leader_sync.fetch_leaders()}
    assert set(got) == {'박팀장', '정그룹'}  # 임원(김영수)·없어진 조직의 직책자(최예전) 빼고
    assert got['정그룹']['assignment'] == '그룹장, 팀장' and got['정그룹']['org'] == '반도체연구소, 반도체1팀'
    assert got['박팀장']['org_path'] == 'AI센터 > AI팀' and got['박팀장']['position'] == 'CL4'
    assert got['박팀장']['hire_date'] == '2005-01-01'


def test_sync_adds_updates_former_and_brings_evals_leadership(env):
    r = leader_sync.sync_leaders()
    assert sorted(r['added']) == ['박팀장', '정그룹'] and r['eval_changed'] == 3 and r['leadership_changed'] == 3
    rows = {x['name']: x for x in leaders.list_leaders()}
    assert set(rows) == {'박팀장', '정그룹'}
    park = rows['박팀장']
    assert park['assignment'] == '팀장' and park['eval'].startswith('2026 연봉등급 가')
    assert park['leadership'] == '2025 3.92'  # 저해행동 뺀 다섯 역량 평균
    assert store.list_people(role='exec') == []  # 임원 목록엔 안 나옴
    assert [p['name'] for p in store.list_people(role='all')] and store.detect_people('박팀장 면담') == [park['id']]
    kinds, years = profile.evaluation_table(park['id'], defaults=False)
    assert kinds == ['연봉등급', '하반기업적'] and years[0] == {'year': 2026, '연봉등급': '가'}
    lead = leaders.leadership(park['id'])
    assert [(x['year'], x['group']) for x in lead] == [(2025, '본인'), (2025, '타인평균'), (2024, '타인평균')]

    # 직접 추가한 리더(사번 있음)는 빈 칸만 채우고, 직책자에서 빠진 불러온 리더는 지난 리더로
    leaders.add_leader('한직접', '파트장', '경영지원팀', 'L004')
    with pytest.raises(ValueError):
        leaders.add_leader('중복', source_id='L004')
    _team(env, [t for t in TEAM if t[9] != 'L002'])
    r = leader_sync.sync_leaders()
    assert r['former'] == ['정그룹'] and r['added'] == []
    assert {x['name'] for x in leaders.list_leaders()} == {'박팀장', '한직접'}
    allrows = {x['name']: x for x in leaders.list_leaders(include_former=True)}
    assert allrows['정그룹']['former'] and allrows['한직접']['position'] == 'CL3'
    assert allrows['한직접']['assignment'] == '파트장' and allrows['한직접']['hr_state'] == ''
    # 다시 직책자가 되면 지금 리더로
    _team(env, TEAM)
    assert leader_sync.sync_leaders()['added'] == []
    assert not {x['name']: x for x in leaders.list_leaders()}['정그룹']['former']


def test_exec_sync_leaves_leaders_alone_and_promotes(env):
    leader_sync.sync_leaders()
    people_sync.sync_people()  # 임원 불러오기가 리더를 '퇴직·전출'로 바꾸지 않는다
    park = next(p for p in store.list_people(role='leader'))
    assert park['hr_state'] == 'current' and park['role'] == 'leader'
    assert [p['name'] for p in store.list_people(role='exec')] == ['김영수']
    # 리더가 임원이 되면 임원으로 옮긴다
    from sqlalchemy import create_engine, text
    with create_engine(f'sqlite:///{env}').begin() as c:
        c.execute(text("UPDATE researchers SET position='상무' WHERE researcher_id='L001'"))
    r = people_sync.sync_people()
    assert '박팀장 (리더 → 임원)' in r['updated']
    assert {p['name'] for p in store.list_people(role='exec')} == {'김영수', '박팀장'}


def test_export_xlsx_and_mail_attachment(env, monkeypatch):
    leader_sync.sync_leaders()
    ids = [x['id'] for x in leaders.list_leaders()]
    store.add_history(None, '면담 — 차기 과제 논의', '동향', author='나', title='박팀장 면담', person_ids=[ids[0]])
    wb = load_workbook(io.BytesIO(leaders.export_xlsx(ids)))
    assert wb.sheetnames == ['기본 정보', '경력·발령', '평가', '리더십진단', '최근 기록']
    basic = list(wb['기본 정보'].values)
    assert basic[0][:3] == ('이름', '사번', '직책') and len(basic) == 3
    assert len(list(wb['리더십진단'].values)) == 4 and list(wb['최근 기록'].values)[1][3] == '박팀장 면담'
    draft = leaders.draft_mail(ids)
    assert draft['attach_value'] == [f"leaders:{ids[0]}.{ids[1]}"] and '2명' in draft['subject']
    files = outbox._load_attachments(draft['attach_value'])
    assert files[0][0].endswith('_2명.xlsx') and files[0][1][:2] == b'PK'
    with pytest.raises(ValueError):
        leaders.export_xlsx([])
