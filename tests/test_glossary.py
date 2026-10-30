"""사내 용어집 — 같은 말로 찾기, AI 에게 뜻 알려 주기, 태그를 대표 용어로."""
import json

import pytest

from services import db, glossary, llm, store


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    for k in ('LLM_API_URL', 'LLM2_API_URL'):
        monkeypatch.delenv(k, raising=False)
    glossary.save_all([
        {'term': 'SAIT', 'synonyms': '종기원, 종합기술원', 'category': '조직', 'description': '삼성종합기술원'},
        {'term': '경영회의', 'synonyms': '임원회의, 주간회의', 'category': '회의'},
    ])
    yield


def test_save_validates_and_replaces():
    assert [t['term'] for t in glossary.list_terms()] == ['SAIT', '경영회의']
    assert glossary.list_terms('종합')[0]['synonyms'] == ['종기원', '종합기술원']
    with pytest.raises(ValueError, match='두 용어'):
        glossary.save_all([{'term': 'A', 'synonyms': 'x'}, {'term': 'B', 'synonyms': 'X'}])
    with pytest.raises(ValueError, match='대표 용어'):
        glossary.save_all([{'term': '', 'synonyms': 'x'}])
    r = glossary.save_all([{'term': 'SAIT', 'synonyms': 'SAIT, 종기원', 'category': '이상한 분류'}, {'term': ''}])
    assert r == {'saved': 1, 'deleted': 1}
    assert glossary.list_terms() == [{'id': glossary.list_terms()[0]['id'], 'term': 'SAIT', 'synonyms': ['종기원'],
                                      'category': '기타', 'description': ''}]


def test_excel_rows_merge_into_existing():
    rows = glossary.rows_from_table(['분류', '정식 명칭', '약어·다른 이름', '설명'],
                                    [['조직', 'SAIT', 'Samsung AIT', ''], ['과제·사업', '차세대 소자', '차소', '과제 약칭']])
    assert rows[0] == {'category': '조직', 'term': 'SAIT', 'synonyms': 'Samsung AIT', 'description': ''}
    assert glossary.merge_rows(rows) == {'added': 1, 'updated': 1}
    sait = next(t for t in glossary.list_terms() if t['term'] == 'SAIT')
    assert sait['synonyms'] == ['종기원', '종합기술원', 'Samsung AIT'] and sait['description'] == '삼성종합기술원'


def test_search_finds_records_written_with_other_names():
    t = store.create_task('조직 운영')
    a = store.add_history(t['id'], '종기원 임원회의 결과 공유')
    b = store.add_history(t['id'], 'SAIT 경영회의 안건 정리')
    store.add_history(t['id'], '예산 검토')
    assert glossary.expand('종기원') == ['종기원', 'SAIT', '종합기술원']
    assert glossary.expand('모르는말') == ['모르는말']
    assert {h['id'] for h in store.search_history('종합기술원')} == {a['id'], b['id']}
    assert {h['id'] for h in store.search_history('SAIT 주간회의')} == {a['id'], b['id']}  # 검색어마다 같은 말
    store.add_reference(t['id'], 'note', title='종기원 연락처', note='대표 번호')
    assert [r['title'] for r in store.list_references(query='SAIT')] == ['종기원 연락처']


def test_ai_gets_terms_from_the_text_and_tags_use_main_term(monkeypatch):
    systems = []

    def fake_chat_post(url, json=None, headers=None, timeout=None):
        systems.append(json['messages'][0]['content'])

        class R:
            status_code = 200
            text = ''

            def json(self):
                return {'choices': [{'message': {'content': __import__('json').dumps(
                    {'title': '회의', 'summary': '요약', 'keywords': ['종기원', 'SAIT', '임원회의', '예산'],
                     'todos': []}, ensure_ascii=False)}}]}
        return R()

    monkeypatch.setenv('LLM_API_URL', 'http://llm.local/v1/chat/completions')
    monkeypatch.setattr(llm.requests, 'post', fake_chat_post)
    r = llm.analyze_record('종기원 임원회의에서 예산 논의')
    assert r['keywords'] == ['SAIT', '경영회의', '예산']
    assert '[사내 용어집' in systems[0] and '- SAIT (= 종기원, 종합기술원): 삼성종합기술원' in systems[0]
    assert '- 경영회의 (= 임원회의, 주간회의)' in systems[0]
    llm.analyze_record('예산만 이야기함')
    assert '[사내 용어집' not in systems[1]  # 원문에 나온 용어만
    assert glossary.prompt_note('') == ''
