import base64
import io
import json

import openpyxl
import pytest

from services import db, excel_import, llm, profile, store
from services.db import PersonCareer


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    monkeypatch.setattr(llm, 'is_configured', lambda: False)
    yield


def _person(name='김영수', sid='E001', **kw):
    p = store.create_person(name, kw.pop('position', '부사장'))
    with db.SessionLocal() as s:
        row = s.get(db.Person, p['id'])
        row.source_id = sid
        s.commit()
    return p


def _career(pid, rows):
    with db.SessionLocal() as s:
        for i, (kind, d, title, org, pos, asg) in enumerate(rows):
            s.add(PersonCareer(person_id=pid, kind=kind, date=d, title=title, org=org, position=pos, assignment=asg,
                               source_key=f'k{i}'))
        s.commit()


CAREER = [
    ('직급·소속', '2022-01', '', '전략기획실', 'CL4', ''),
    ('발령', '2022-07-01', '전보', '전략기획실', 'CL4', ''),
    ('발령', '2022-07-15', '조직명 변경', '전략기획팀', 'CL4', ''),
    ('발령', '2023-03-01', '임원 선임', 'AI센터', '상무', '센터장'),
    ('발령', '2025-03-01', '승진', '전략기획실', '부사장', '실장'),
]


def test_highlights_rule_draft_and_save():
    p = _person()
    _career(p['id'], CAREER)
    draft, msg = profile.draft_highlights(p['id'])
    assert [d['date'] for d in draft] == ['2022.07', '2023.03', '2025.03']  # 핵심 발령만 (조직명 변경 제외)
    assert draft[1]['text'] == '임원 선임 · AI센터 · 상무 · 센터장' and '핵심 발령' in msg
    assert profile.list_highlights(p['id']) == []  # 초안은 저장 전까지 DB 에 없다
    draft[1]['text'] = '상무 선임, AI센터장'
    profile.save_highlights(p['id'], draft + [{'date': '', 'text': '해외 법인 근무 경험'},
                                             {'date': '2021.6', 'text': '그룹 영입'}, {'date': '', 'text': '  '}])
    hl = profile.list_highlights(p['id'])
    assert [(h['date'], h['text']) for h in hl] == [
        ('2021.06', '그룹 영입'), ('2022.07', '전보 · 전략기획실 · CL4'), ('2023.03', '상무 선임, AI센터장'),
        ('2025.03', '승진 · 전략기획실 · 부사장 · 실장'), ('', '해외 법인 근무 경험')]


def test_highlights_ai_draft(monkeypatch):
    p = _person()
    _career(p['id'], CAREER)
    seen = {}

    def fake_chat(system, user, temperature=0.2):
        seen['user'] = user
        return '<think>..</think>```json\n' + json.dumps({'highlights': [
            {'date': '2023-03', 'text': '상무 선임, AI센터장 보임'}, {'date': '2025.03', 'text': '부사장 승진, 전략기획실장'}]}) + '\n```'
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    monkeypatch.setattr(llm, 'chat', fake_chat)
    draft, msg = profile.draft_highlights(p['id'])
    assert [(d['date'], d['source']) for d in draft] == [('2023.03', 'ai'), ('2025.03', 'ai')]
    assert '임원 선임' in seen['user'] and 'AI 가' in msg

    monkeypatch.setattr(llm, 'chat', lambda *a, **k: (_ for _ in ()).throw(llm.LLMError('타임아웃')))
    draft, msg = profile.draft_highlights(p['id'])  # AI 실패 → 규칙 초안
    assert len(draft) == 3 and '실패' in msg


def test_evaluations_save_and_table():
    p = _person()
    profile.save_evaluations(p['id'], [
        {'year': 2025, 'kind': 'MBO', 'grade': 'A'}, {'year': '24', 'kind': '임원종합평가', 'grade': 'S'},
        {'year': 2024, 'kind': 'MBO', 'grade': 'B+', 'note': '목표 상향'}, {'year': 2023, 'kind': '역량평가', 'grade': '우수'},
        {'year': '', 'kind': 'MBO', 'grade': 'X'}, {'year': 2022, 'kind': 'MBO', 'grade': ''}])
    kinds, rows = profile.evaluation_table(p['id'])
    assert kinds == ['임원종합평가', 'MBO', '역량평가']
    assert rows == [{'year': 2025, 'MBO': 'A'}, {'year': 2024, '임원종합평가': 'S', 'MBO': 'B+'},
                    {'year': 2023, '역량평가': '우수'}]
    assert profile.eval_kinds() == ['임원종합평가', 'MBO', '역량평가']
    profile.save_evaluations(p['id'], [{'year': 2025, 'kind': 'MBO', 'grade': 'S'}])  # 표에서 지운 줄은 삭제
    assert [(e['year'], e['grade']) for e in profile.list_evaluations(p['id'])] == [(2025, 'S')]


def _xlsx(rows) -> str:
    wb = openpyxl.Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return 'data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,' + \
        base64.b64encode(buf.getvalue()).decode()


def test_excel_mapping_rules_and_ai(monkeypatch):
    table = excel_import.parse_upload(_xlsx([
        ['No', '사번', '성명', '25mbo', '24Mbo', '2024 임원종합평가', '취미', '평가 코멘트', None],
        [1, 'E001', '김영수', 'A', 'B', 'S', '등산', '리더십 우수', None],
        [None, None, None, None, None, None, None, None, None],
    ]), 'evals.xlsx')
    assert table['headers'] == ['No', '사번', '성명', '25mbo', '24Mbo', '2024 임원종합평가', '취미', '평가 코멘트']
    assert table['rows'] == [['1', 'E001', '김영수', 'A', 'B', 'S', '등산', '리더십 우수']]
    m = excel_import.suggest_mapping(table['headers'], table['rows'])
    assert [x['target'] for x in m] == ['ignore', 'key:source_id', 'key:name', 'eval:MBO:2025', 'eval:MBO:2024',
                                        'eval:임원종합평가:2024', 'attr:취미', 'attr:평가 코멘트']
    assert [x['source'] for x in m][-2:] == ['new', 'new']

    asked = {}

    def fake_chat(system, user, temperature=0.2):
        asked['user'] = user
        return json.dumps({'columns': [{'index': 6, 'target': 'field:bogus', 'reason': '?'},
                                       {'index': 7, 'target': 'attr:평가 코멘트', 'reason': '자유 서술'}]})
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    monkeypatch.setattr(llm, 'chat', fake_chat)
    m = excel_import.suggest_mapping(table['headers'], table['rows'])
    assert '"취미"' in asked['user'] and '"25mbo"' not in asked['user']  # 규칙으로 정한 열은 AI 에 안 묻는다
    assert (m[6]['target'], m[6]['source']) == ('attr:취미', 'new')  # 잘못된 AI 답은 버림
    assert (m[7]['target'], m[7]['source']) == ('attr:평가 코멘트', 'ai')
    labels = {o['value']: o['label'] for o in excel_import.target_options(m)}
    assert labels['eval:MBO:2025'] == '평가 · 2025 MBO' and 'ignore' in labels


def test_excel_preview_and_apply():
    kim = _person('김영수', '00012345')
    lee = _person('이민정', 'E002', position='상무')
    table = {'headers': ['사번', '성명', '25MBO', '24 임원종합평가', '입사일', '출생연도', '주요이력', '취미'],
             'rows': [['12345', '김영수', 'A', 'S', '1995-03-01', '1968-05-02', '2023.03 상무 선임\n2025.03 부사장 승진', '등산'],
                      ['E002', '', 'B', '', '', '', '', ''],
                      ['E777', '신규임원', 'A', '', '', '', '', ''],
                      ['', '', 'A', '', '', '', '', '']]}
    targets = [x['target'] for x in excel_import.suggest_mapping(table['headers'], table['rows'], use_ai=False)]
    r = excel_import.run(table, targets)  # 미리보기 — DB 그대로
    assert r['matched'] == 2 and r['unmatched'] == ['E777', '(빈 행)']
    assert r['changes']['평가 · 2025 MBO'] == 2 and r['changes']['주요 이력 (한 줄에 하나, 기존 것을 바꿈)'] == 1
    assert profile.list_evaluations(kim['id']) == []

    r = excel_import.run(table, targets, apply=True)
    assert profile.evaluation_table(kim['id'])[1] == [{'year': 2025, 'MBO': 'A'}, {'year': 2024, '임원종합평가': 'S'}]
    assert profile.evaluation_table(lee['id'])[1] == [{'year': 2025, 'MBO': 'B'}]
    k = store.get_person(kim['id'])
    assert (k['hire_date'], k['birth_year']) == ('1995-03-01', '1968')  # 앞자리 0 빠진 사번도 찾음
    assert [h['text'] for h in profile.list_highlights(kim['id'])] == ['상무 선임', '부사장 승진']
    assert profile.list_attributes(kim['id']) == [{'name': '취미', 'value': '등산'}]
    assert '취미' in profile.attribute_names()

    again = excel_import.run(table, targets)  # 같은 파일을 다시 미리보기 → 평가·항목은 바뀌는 게 없다
    assert again['changes']['평가 · 2025 MBO'] == 0 and again['changes']['추가 항목 · 취미'] == 0
    assert again['changes']['주요 이력 (한 줄에 하나, 기존 것을 바꿈)'] == 0

    r = excel_import.run(table, targets, create_missing=True, apply=True)
    assert r['created'] == ['신규임원']
    new = next(p for p in store.list_people() if p['name'] == '신규임원')
    assert new['source_id'] == 'E777' and profile.list_evaluations(new['id'])[0]['grade'] == 'A'


def test_excel_requires_key_and_handles_same_names():
    _person('김영수', 'E001')
    _person('김영수', 'E009')
    table = {'headers': ['성명', '25MBO'], 'rows': [['김영수', 'A']]}
    r = excel_import.run(table, ['key:name', 'eval:MBO:2025'], apply=True)
    assert r['ambiguous'] == ['김영수'] and r['matched'] == 0
    with pytest.raises(excel_import.ImportError_, match='사번 또는 성명'):
        excel_import.run(table, ['ignore', 'eval:MBO:2025'])
    with pytest.raises(excel_import.ImportError_):
        excel_import.parse_upload('data:text/plain;base64,' + base64.b64encode(b'x').decode(), 'a.txt')


def test_csv_upload_cp949():
    data = '사번,25MBO\nE001,A\n'.encode('cp949')
    t = excel_import.parse_upload('data:text/csv;base64,' + base64.b64encode(data).decode(), 'a.csv')
    assert t == {'headers': ['사번', '25MBO'], 'rows': [['E001', 'A']]}


def test_import_page_path_wins_over_person_template():
    import app  # noqa: F401 — 페이지 등록
    from dash._pages import _path_to_page
    page, _ = _path_to_page('people/import')
    assert page['module'] == 'pages.people_import'
    page, vars_ = _path_to_page('people/3')
    assert page['module'] == 'pages.person' and vars_ == {'person_id': '3'}


def test_excluded_columns_are_not_uploaded():
    kim = _person('김영수', 'E001')
    table = {'headers': ['사번', '25MBO', '24 임원종합평가', '취미', '입사일'],
             'rows': [['E001', 'A', 'S', '등산', '1995-03-01']]}
    targets = [x['target'] for x in excel_import.suggest_mapping(table['headers'], table['rows'], use_ai=False)]
    final = excel_import.exclude_columns(targets, [True, True, False, False, True])
    assert final == ['key:source_id', 'eval:MBO:2025', 'ignore', 'ignore', 'field:hire_date']
    r = excel_import.run(table, final, apply=True)
    assert '추가 항목 · 취미' not in r['changes'] and '평가 · 2024 임원종합평가' not in r['changes']
    assert profile.evaluation_table(kim['id'])[1] == [{'year': 2025, 'MBO': 'A'}]  # 뺀 평가 열은 안 들어감
    assert profile.list_attributes(kim['id']) == [] and '취미' not in profile.attribute_names()  # 새 항목도 안 생김
    assert store.get_person(kim['id'])['hire_date'] == '1995-03-01'
    assert excel_import.exclude_columns(['a', 'b'], None) == ['a', 'b']
