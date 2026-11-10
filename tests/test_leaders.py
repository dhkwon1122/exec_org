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
    ('M001', '서마스터', 'AI팀', 'AI팀', 'R. Master', '', 'sm.seo', '재직', '2026', '9', 'Y', '1979', '2006-01-01', ''),
]


def _team(path, rows):
    _text_table(path, 'team_refer', TEAM_COLS, rows)


TEAM = [  # 조직 행 — 조직장급만 사번이 있다
    ('AI팀', 'R&D', 'AI센터', 'AI팀', '', '1010', 'D-AI', 'D-C', '2', '', '', '', '', '2026', '03', '31'),
    ('AI팀', 'R&D', 'AI센터', 'AI팀', '', '1010', 'D-AI', 'D-C', '2', 'L001', '박팀장', '팀장', '', '2026', '06', '30'),
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
    _text_table(pdb, 'tasks', ['researcher_id', 'task_name', 'the_task_name', 'start_date', 'end_date', 'input_rate'],
                [('L001', '비전과제', '', '2026-01-01', '2027-12-31', '30'),
                 ('L001', '언어과제', '언어과제(신)', '2026-01-01', '', '70'),
                 ('L001', '끝난과제', '', '2024-01-01', '2025-01-01', '100')])
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{pdb}')
    monkeypatch.delenv('USER_DATABASE_URL', raising=False)
    monkeypatch.delenv('EXEC_ORG_POSITIONS', raising=False)
    users.reset()
    yield pdb
    users.reset()


def test_fetch_leaders_current_holders_and_rmaster():
    got = {x['name']: x for x in leader_sync.fetch_leaders()}
    assert set(got) == {'김영수', '박팀장', '정그룹', '서마스터'}  # 없어진 조직의 직책자(최예전) 빼고
    assert got['김영수']['exec'] and not got['박팀장']['exec']
    st = got['박팀장']['status']
    assert (st['Platform'], st['그룹'], st['과제/파트명'], st['부서명'], st['CL'], st['구분']) == \
        ('AI센터', 'AI팀', '', 'AI팀', 'CL4', 'R&D')
    # 직책명 '팀장' 이라 2단계 조직이어도 플랫폼장/팀장 칸 (직책명이 단계보다 먼저)
    assert (st['직책구분'], st['보직장'], st['플랫폼장/팀장'], st['그룹장/PL'], st['PM/파트장']) == ('보직장', 'O', 'O', '', '')
    assert st['과제명'] == '언어과제(신), 비전과제' and st['보직 수행시작일'] == '2026-06-30'
    assert got['김영수']['status']['직책구분'] == '임원/Master' and got['김영수']['status']['플랫폼장/팀장'] == 'O'
    sm = got['서마스터']['status']  # 보직 없는 R.Master — 자기 소속(org_code)의 조직으로
    assert (sm['직책구분'], sm['보직장'], sm['Platform'], sm['그룹'], sm['Knox ID']) == ('R.Master', '', 'AI센터', 'AI팀', 'sm.seo')
    assert got['정그룹']['status']['플랫폼장/팀장'] == 'O' and got['정그룹']['status']['그룹장/PL'] == 'O'
    assert got['정그룹']['assignment'] == '그룹장, 팀장' and got['정그룹']['org'] == '반도체연구소, 반도체1팀'
    assert got['박팀장']['org_path'] == 'AI센터 > AI팀' and got['박팀장']['position'] == 'CL4'
    assert got['박팀장']['hire_date'] == '2005-01-01'


def test_sync_adds_updates_former_and_brings_evals_leadership(env):
    r = leader_sync.sync_leaders()
    assert sorted(r['added']) == ['박팀장', '서마스터', '정그룹'] and r['eval_changed'] == 3 and r['leadership_changed'] == 3
    assert r['skipped_exec'] == ['김영수']  # 임원 불러오기 전이라 사람 행이 없음
    rows = {x['name']: x for x in leaders.list_leaders()}
    assert set(rows) == {'박팀장', '정그룹', '서마스터'}
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
    assert {x['name'] for x in leaders.list_leaders()} == {'박팀장', '한직접', '서마스터'}
    allrows = {x['name']: x for x in leaders.list_leaders(include_former=True)}
    assert allrows['정그룹']['former'] and allrows['한직접']['position'] == 'CL3'
    assert allrows['한직접']['assignment'] == '파트장' and allrows['한직접']['hr_state'] == ''
    # 다시 직책자가 되면 지금 리더로
    _team(env, TEAM)
    assert leader_sync.sync_leaders()['added'] == []
    assert not {x['name']: x for x in leaders.list_leaders()}['정그룹']['former']


def test_status_rows_execs_and_manual_edits_survive_sync(env):
    people_sync.sync_people()
    leader_sync.sync_leaders()
    leaders.add_leader('무사번', '파트장', '테스트파트')
    rows = {r['성명']: r for r in leaders.list_status()}
    assert list(rows) == ['김영수', '서마스터', '박팀장', '정그룹', '무사번']  # 직책구분 순 → 조직 순
    assert rows['김영수']['직책구분'] == '임원/Master' and rows['김영수']['Platform'] == 'AI센터'
    assert rows['무사번']['직책구분'] == '일반' and rows['무사번']['부서명'] == '테스트파트'
    assert list(leaders.list_status()[0]) [:17] == leaders.STATUS_COLUMNS
    park = rows['박팀장']['id']
    leaders.save_status(park, '과제구분', '국책')
    leaders.save_status(park, 'CL', 'CL5')
    leader_sync.sync_leaders()  # 다시 불러와도 고친 칸은 그대로
    r = {x['성명']: x for x in leaders.list_status()}['박팀장']
    assert (r['과제구분'], r['CL'], r['edited']) == ('국책', 'CL5', ['CL', '과제구분'])
    leaders.save_status(park, 'CL', 'CL4')  # 불러온 값과 같아지면 고친 표시가 지워짐
    assert {x['성명']: x for x in leaders.list_status()}['박팀장']['edited'] == ['과제구분']
    assert [x['성명'] for x in leaders.list_status('국책')] == ['박팀장']
    with pytest.raises(ValueError):
        leaders.save_status(park, '없는칸', 'x')


def test_exec_sync_leaves_leaders_alone_and_promotes(env):
    leader_sync.sync_leaders()
    people_sync.sync_people()  # 임원 불러오기가 리더를 '퇴직·전출'로 바꾸지 않는다
    park = next(p for p in store.list_people(role='leader') if p['name'] == '박팀장')
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
    ids = [x['id'] for x in leaders.list_leaders() if x['name'] != '서마스터']
    store.add_history(None, '면담 — 차기 과제 논의', '동향', author='나', title='박팀장 면담', person_ids=[ids[0]])
    wb = load_workbook(io.BytesIO(leaders.export_xlsx(ids)))
    assert wb.sheetnames == ['리더급 현황', '기본 정보', '경력·발령', '평가', '리더십진단', '최근 기록']
    status = list(wb['리더급 현황'].values)
    assert list(status[0]) == leaders.STATUS_COLUMNS and len(status) == 3
    basic = list(wb['기본 정보'].values)
    assert basic[0][:3] == ('이름', '사번', '직책') and len(basic) == 3
    assert len(list(wb['리더십진단'].values)) == 4 and list(wb['최근 기록'].values)[1][3] == '박팀장 면담'
    draft = leaders.draft_mail(ids)
    assert draft['attach_value'] == [f"leaders:{ids[0]}.{ids[1]}"] and '2명' in draft['subject']
    files = outbox._load_attachments(draft['attach_value'])
    assert files[0][0].endswith('_2명.xlsx') and files[0][1][:2] == b'PK'
    with pytest.raises(ValueError):
        leaders.export_xlsx([])


def test_leader_excel_upload_status_columns_and_new_leaders(env):
    from services import excel_import
    leader_sync.sync_leaders()
    park = next(x for x in leaders.list_status() if x['성명'] == '박팀장')
    headers = ['사원번호', '성명', '과제구분', 'Platform', '25MBO', '비고']
    rows = [['L001', '박팀장', '국책', 'AI센터', 'S', '면담 예정'],
            ['', '새리더', '자체', '', '', ''],
            ['L999', '', '', '', '', '']]
    m = excel_import.suggest_mapping(headers, rows, use_ai=False, role='leader')
    targets = [x['target'] for x in m]
    assert targets == ['key:source_id', 'key:name', 'status:과제구분', 'ignore', 'eval:MBO:2025', 'field:note']
    assert '사번으로' in m[3]['reason']  # Platform 은 사번으로 People in SAIT 에서 읽는 칸
    assert not any(o['value'] == 'status:Platform' for o in excel_import.target_options(m, 'leader'))
    assert any(o['value'] == 'status:과제구분' for o in excel_import.target_options(m, 'leader'))
    assert not any(o['value'].startswith('status:') for o in excel_import.target_options(m, 'exec'))
    assert excel_import.suggest_mapping(headers, rows, use_ai=False)[2]['target'] == 'attr:과제구분'  # 임원 업로드는 그대로
    table = {'headers': headers, 'rows': rows}
    pre = excel_import.run(table, targets, create_missing=True, role='leader')
    assert pre['created'] == ['새리더'] and pre['unmatched'] == ['L999']
    assert pre['changes']['리더급 현황 · 과제구분'] == 2
    assert leaders.list_status() and {x['성명'] for x in leaders.list_status()} == {'박팀장', '정그룹', '서마스터'}  # 미리보기
    excel_import.run(table, targets, create_missing=True, apply=True, role='leader')
    rows_now = {x['성명']: x for x in leaders.list_status()}
    assert rows_now['박팀장']['과제구분'] == '국책' and rows_now['박팀장']['edited'] == ['과제구분']
    assert rows_now['새리더']['과제구분'] == '자체' and rows_now['새리더']['role'] == 'leader'
    assert profile.list_evaluations(park['id'])[0]['kind'] in ('MBO', '연봉등급')
    leader_sync.sync_leaders()  # 다시 불러와도 올린 값 유지
    assert {x['성명']: x for x in leaders.list_status()}['박팀장']['과제구분'] == '국책'
    wb = load_workbook(io.BytesIO(leaders.status_template()))
    assert [c.value for c in wb['리더급 현황'][1]] == leaders.STATUS_COLUMNS


def _upload(wb_or_bytes, name='현황.xlsx'):
    import base64
    if not isinstance(wb_or_bytes, bytes):
        buf = io.BytesIO()
        wb_or_bytes.save(buf)
        wb_or_bytes = buf.getvalue()
    return 'data:application/octet-stream;base64,' + base64.b64encode(wb_or_bytes).decode(), name


def test_upload_finds_header_row_and_sheet_and_explains_drm():
    from openpyxl import Workbook
    from services import excel_import
    wb = Workbook()
    wb.active.title = '안내'
    wb.active.append(['이 파일은 리더급 현황입니다'])
    ws = wb.create_sheet('리더급 현황')
    ws.append(['리더급 현황 (2026.11 기준)'])  # 제목 행
    ws.append([])
    ws.append(['', '작성: 피플팀'])
    ws.append(['사원번호', 'Platform', '그룹', '성명', '과제구분'])
    ws.append(['L001', 'AI센터', 'AI팀', '박팀장', '국책'])
    t = excel_import.parse_upload(*_upload(wb))
    assert t['sheet'] == '리더급 현황' and t['headers'] == ['사원번호', 'Platform', '그룹', '성명', '과제구분']
    assert t['rows'] == [['L001', 'AI센터', 'AI팀', '박팀장', '국책']]
    # 암호·DRM(OLE 머리나 zip 이 아닌 내용) — 알아볼 수 있는 안내
    for raw in (b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\0' * 600, b'SCDSA002' + b'x' * 100):
        with pytest.raises(excel_import.ImportError_, match='DRM'):
            excel_import.parse_upload(*_upload(raw))
    with pytest.raises(excel_import.ImportError_, match='xlsx·xls·csv'):
        excel_import.parse_upload(*_upload(b'abc', '현황.pdf'))


def test_platform_group_dept_come_from_own_record_by_sid(env):
    from sqlalchemy import create_engine, text
    with create_engine(f'sqlite:///{env}').begin() as c:  # 정그룹: 맡은 조직은 반도체연구소지만 소속은 AI팀
        c.execute(text("UPDATE researchers SET org_code='AI팀', department='AI전략실' WHERE researcher_id='L002'"))
    st = {x['name']: x['status'] for x in leader_sync.fetch_leaders()}['정그룹']
    assert (st['Platform'], st['그룹'], st['부서명']) == ('AI센터', 'AI팀', 'AI전략실')
    assert st['플랫폼장/팀장'] == 'O'  # 보직은 맡은 조직 기준 그대로
    leader_sync.sync_leaders()
    jung = {x['성명']: x for x in leaders.list_status()}['정그룹']
    with pytest.raises(ValueError, match='사번으로'):
        leaders.save_status(jung['id'], 'Platform', '다른플랫폼')
    # 예전에 고쳐 둔 값이 있어도 사번으로 읽은 값이 보인다
    from services.db import Person
    with db.SessionLocal() as s:
        s.get(Person, jung['id']).leader_manual = '{"Platform": "옛값", "과제구분": "국책"}'
        s.commit()
    jung = {x['성명']: x for x in leaders.list_status()}['정그룹']
    assert jung['Platform'] == 'AI센터' and jung['과제구분'] == '국책' and jung['edited'] == ['과제구분']
    # 사번으로 직접 추가하면 바로 채워진다 (불러오기 전에도)
    leaders.add_leader('한직접', '파트장', '', 'L004')
    han = {x['성명']: x for x in leaders.list_status()}['한직접']
    assert han['부서명'] == '경영지원' and han['CL'] == 'CL3'


def test_head_columns_follow_assignment_title(env):
    assert [leader_sync.head_layer(a, 1) for a in ('PM', 'PL', '파트장', '그룹장', '팀장', '본부장', '연구위원', 'APM팀')] == \
        [3, 2, 3, 2, 1, 1, 1, 1]
    assert leader_sync.head_layer('', 3) == 3
    team = [t for t in TEAM if t[9] != 'L001'] + [  # 박팀장이 과제 단계(3)가 아닌 2단계 조직의 PM 으로 등록된 경우
        ('비전과제', 'R&D', 'AI센터', 'AI팀', '비전과제', '1012', 'D-V', 'D-AI', '2', 'L001', '박팀장', 'PM', '', '2026', '09', '30'),
        ('AI팀', 'R&D', 'AI센터', 'AI팀', '', '1010', 'D-AI', 'D-C', '', 'L003', '최예전', 'PL', '', '2026', '09', '30')]
    _team(env, team)
    got = {x['name']: x['status'] for x in leader_sync.fetch_leaders()}
    assert (got['박팀장']['그룹장/PL'], got['박팀장']['PM/파트장']) == ('', 'O')
    assert (got['최예전']['그룹장/PL'], got['최예전']['플랫폼장/팀장']) == ('O', '')  # 단계 칸이 비어도 직책명으로


def test_numeric_sids_keep_leading_zeros(env, tmp_path):
    from services import excel_import
    assert [store.norm_sid(v) for v in ('12345', '12345.0', '00012345', 'E001', ' 7 ', '', '123456789')] == \
        ['00012345', '00012345', '00012345', 'E001', '00000007', '', '123456789']
    # 엑셀이 숫자로 읽어 0 이 지워진 사번 — DB 에 없어도 8자리로 새로 넣는다
    table = {'headers': ['사원번호', '성명', '과제구분'], 'rows': [['12345', '영빠짐', '국책']]}
    targets = [m['target'] for m in excel_import.suggest_mapping(table['headers'], table['rows'], use_ai=False,
                                                                   role='leader')]
    r = excel_import.run(table, targets, create_missing=True, apply=True, role='leader')
    assert r['created'] == ['영빠짐']
    row = {x['성명']: x for x in leaders.list_status()}['영빠짐']
    assert row['사원번호'] == '00012345'
    r = excel_import.run(table, targets, apply=True, role='leader')  # 다시 올려도 같은 사람으로
    assert r['matched'] == 1 and r['created'] == []
    leaders.add_leader('직접', source_id='777')
    assert '00000777' in {x['사원번호'] for x in leaders.list_status()}
    # 내보낸 엑셀의 사원번호 칸은 텍스트 서식
    wb = load_workbook(io.BytesIO(leaders.status_template()))
    assert {c.number_format for c in list(wb['리더급 현황'].columns)[0][1:]} == {'@'}
    # 예전에 짧게 들어간 사번은 시작할 때 8자리로 (겹치면 그대로)
    from services.db import Person
    with db.SessionLocal() as s:
        s.add(Person(name='짧은', source_id='4321', role='leader'))
        s.add(Person(name='겹침', source_id='777', role='leader'))  # 00000777 이 이미 있음
        s.commit()
    db.configure(str(db.engine.url))
    got = {p['name']: p['source_id'] for p in store.list_people(role='leader', include_inactive=True)}
    assert got['짧은'] == '00004321' and got['겹침'] == '777'


def test_delete_leaders_stays_deleted_after_sync(env):
    people_sync.sync_people()
    leader_sync.sync_leaders()
    rows = {x['성명']: x for x in leaders.list_status()}
    hid = store.add_history(None, '면담', '동향', author='나', person_ids=[rows['정그룹']['id']])['id']
    res = leaders.delete_leaders([rows['박팀장']['id'], rows['김영수']['id'], rows['정그룹']['id']], by='나')
    assert res['deleted'] == ['박팀장'] and res['execs'] == ['김영수']
    assert res['blocked'][0][0] == '정그룹' and '이 사람에게만' in res['blocked'][0][1]
    assert hid and '박팀장' not in {x['성명'] for x in leaders.list_status()}
    leader_sync.sync_leaders()  # 다시 불러와도 생기지 않는다
    assert '박팀장' not in {x['성명'] for x in leaders.list_status()}
    assert leaders.excluded_sids() == {'L001'}
    leaders.add_leader('박팀장', '팀장', '', 'L001')  # 같은 사번으로 직접 추가하면 다시
    assert leaders.excluded_sids() == set()
    leader_sync.sync_leaders()
    assert [x['성명'] for x in leaders.list_status()].count('박팀장') == 1


def test_plan_then_apply_only_chosen(env):
    from sqlalchemy import create_engine, text
    first = leader_sync.plan_leaders()
    assert {i['kind'] for i in first['items']} >= {'add', 'extras'} and leaders.list_status() == []  # 제안만
    leader_sync.apply_leaders([i['key'] for i in first['items'] if i['key'] != 'add:L002'])
    names = {x['성명'] for x in leaders.list_status()}
    assert '박팀장' in names and '정그룹' not in names
    with create_engine(f'sqlite:///{env}').begin() as c:
        c.execute(text("UPDATE researchers SET position='CL5' WHERE researcher_id='L001'"))
    _team(env, [t for t in TEAM if t[9] != 'M001'])  # 바뀌는 것 없음(서마스터는 R.Master 라 그대로)
    plan = leader_sync.plan_leaders()
    by_key = {i['key']: i for i in plan['items']}
    assert 'set:L001' in by_key and '직급 CL4 → CL5' in by_key['set:L001']['label']
    assert 'CL CL4 → CL5' in by_key['set:L001']['label']  # 현황 칸도
    assert 'add:L002' in by_key  # 지난번에 고르지 않은 새 리더는 다시 제안
    park = {x['성명']: x for x in leaders.list_status()}['박팀장']
    assert park['CL'] == 'CL4'  # 제안만으로는 그대로
    r = leader_sync.apply_leaders(['set:L001'])
    assert r['updated'] == ['박팀장'] and r['added'] == []
    assert {x['성명']: x for x in leaders.list_status()}['박팀장']['CL'] == 'CL5'
    assert not any(i['key'] == 'set:L001' for i in leader_sync.plan_leaders()['items'])  # 반영 뒤엔 제안 없음
