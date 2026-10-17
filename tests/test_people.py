import base64
import json
import sqlite3

import pytest

from components.history_form import save_history
from services import db, llm, store


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    yield


def test_person_crud_and_list_order():
    kim = store.create_person('김영수', '부사장', '전략기획실', aliases='김부사장, 김 부사장')
    lee = store.create_person('이민정', '상무', aliases=['이상무'])
    assert kim['aliases'] == ['김부사장', '김 부사장']
    store.add_history(None, '이상무 오늘 조직개편 언급', '동향', person_ids=[lee['id']])
    # 최근 기록된 사람이 위, 기록 없는 사람은 뒤
    assert [p['name'] for p in store.list_people()] == ['이민정', '김영수']
    assert store.list_people()[0]['count'] == 1
    assert [p['name'] for p in store.list_people('전략')] == ['김영수']
    store.update_person(kim['id'], active=False)
    assert [p['name'] for p in store.list_people()] == ['이민정']
    assert len(store.list_people(include_inactive=True)) == 2
    with pytest.raises(ValueError):
        store.create_person('  ')


def test_detect_people_by_name_and_alias():
    kim = store.create_person('김영수', '부사장', aliases='김부사장, CTO, 김')
    lee = store.create_person('이민정', '상무')
    assert store.detect_people('어제 김부사장이 예산 언급') == [kim['id']]
    assert store.detect_people('이민정 상무 면담') == [lee['id']]
    assert store.detect_people('김치찌개 먹음') == []  # 한 글자 별칭('김')은 쓰지 않는다
    store.update_person(kim['id'], active=False)
    assert store.detect_people('김부사장') == []  # 추적 중지한 임원은 자동 연결 안 함


def test_trend_without_task_requires_person():
    with pytest.raises(ValueError):
        store.add_history(None, '누구 얘기인지 모름', '동향')
    p = store.create_person('김영수', '부사장')
    h = store.add_history(None, '해외 출장 예정', '동향', person_ids=[p['id']])
    assert h['task_id'] is None and h['task_title'] == ''
    assert [x['name'] for x in h['people']] == ['김영수']
    with pytest.raises(ValueError):  # 업무 없는 동향에서 임원을 모두 빼면 어디에도 안 보이게 된다
        store.update_history(h['id'], person_ids=[])
    with pytest.raises(ValueError):
        store.add_history(None, 'x', '동향', person_ids=[999])


def test_save_history_auto_links_people_and_attaches_without_task():
    kim = store.create_person('김영수', '부사장', aliases='김부사장')
    lee = store.create_person('이민정', '상무')
    data = 'data:text/plain;base64,' + base64.b64encode('메모'.encode()).decode()
    h = save_history(None, '동향', '김부사장이 AI 조직 신설에 관심. 자료 https://conf.example.com/p/9', None, False,
                     '', [data], ['면담메모.txt'], '권대현', person_ids=[lee['id']])
    got = store.get_history(h['id'])
    assert sorted(p['id'] for p in got['people']) == sorted([kim['id'], lee['id']])
    assert sorted(r['kind'] for r in got['refs']) == ['file', 'link']
    f = next(r for r in got['refs'] if r['kind'] == 'file')
    assert f['task_id'] is None and f['file_path'].startswith(f"h{h['id']}")
    assert store.upload_abspath(f['file_path'])
    refs = store.list_references(query='면담메모')
    assert refs[0]['task_title'] == ''


def test_task_feedback_mentioning_executive_shows_on_both():
    kim = store.create_person('김영수', '부사장', aliases='김부사장')
    t = store.create_task('조직 개편안')
    h = save_history(t['id'], '피드백', '김부사장 피드백: 2안으로', None, False, '', None, None, '권대현')
    assert [x['id'] for x in store.list_history(t['id']) if x['entry_type'] == '피드백'] == [h['id']]
    assert [x['id'] for x in store.search_history('', person_id=kim['id'])] == [h['id']]


def test_search_by_person_name_and_keywords():
    kim = store.create_person('김영수', '부사장', '전략기획실', aliases='김부사장')
    t = store.create_task('a')
    h1 = store.add_history(None, '해외 출장 예정', '동향', person_ids=[kim['id']], keywords='출장')
    store.add_history(t['id'], '무관한 기록')
    # 원문에 이름이 없어도 연결된 임원의 이름·별칭·직위·소속으로 찾힌다
    assert [x['id'] for x in store.search_history('김영수')] == [h1['id']]
    assert [x['id'] for x in store.search_history('전략기획실 출장')] == [h1['id']]
    assert store.search_history('', entry_types=['동향'])[0]['people'][0]['name'] == '김영수'
    assert store.keyword_stats(person_id=kim['id']) == [('출장', 1)]


def test_delete_person_protects_trend_only_records():
    kim = store.create_person('김영수', '부사장')
    lee = store.create_person('이민정', '상무')
    t = store.create_task('a')
    store.add_history(t['id'], '업무 기록', person_ids=[kim['id']])
    store.add_history(None, '두 사람 동향', '동향', person_ids=[kim['id'], lee['id']])
    store.delete_person(kim['id'])  # 다른 임원·업무로 여전히 보이므로 삭제 가능
    assert store.get_person(kim['id']) is None
    h = store.add_history(None, '이상무만의 동향', '동향', person_ids=[lee['id']])
    with pytest.raises(ValueError):
        store.delete_person(lee['id'])
    store.delete_history(h['id'])
    store.update_history(store.search_history('두 사람')[0]['id'], person_ids=[lee['id']])
    with pytest.raises(ValueError):
        store.delete_person(lee['id'])


def test_llm_prompt_includes_people_and_trend_rule(monkeypatch):
    calls = []
    monkeypatch.setenv('LLM_API_URL', 'http://llm.local/v1/chat/completions')

    class R:
        status_code = 200
        text = ''

        def json(self):
            return {'choices': [{'message': {'content': json.dumps({'title': 't', 'summary': 's', 'keywords': []})}}]}

    monkeypatch.setattr(llm.requests, 'post', lambda url, json=None, **kw: calls.append(json) or R())
    llm.analyze_record('원문', '', '동향', ['김영수 부사장'])
    prompt = calls[0]['messages'][1]['content']
    assert '관련 임원: 김영수 부사장' in prompt and "'동향'이면" in prompt


def test_migrates_old_db_where_task_was_required(tmp_path):
    path = tmp_path / 'old.db'
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE tasks (id INTEGER PRIMARY KEY, title VARCHAR(300) NOT NULL, description TEXT, status VARCHAR(20)
      NOT NULL, owner VARCHAR(100), tags VARCHAR(500), parent_id INTEGER, start_date VARCHAR(10), due_date VARCHAR(10),
      created_at DATETIME, updated_at DATETIME);
    CREATE TABLE history (id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      entry_type VARCHAR(20) NOT NULL, content TEXT NOT NULL, author VARCHAR(100), occurred_at DATETIME,
      created_at DATETIME);
    CREATE INDEX ix_history_task_id ON history (task_id);
    CREATE TABLE references_ (id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      kind VARCHAR(10) NOT NULL, title VARCHAR(300) NOT NULL, url TEXT, file_name VARCHAR(300), file_path VARCHAR(500),
      note TEXT, created_at DATETIME);
    INSERT INTO tasks (id, title, status) VALUES (7, '옛 업무', '진행중');
    INSERT INTO history (id, task_id, entry_type, content, occurred_at) VALUES (41, 7, '메모', '옛 기록', '2025-01-01 10:00:00');
    INSERT INTO references_ (task_id, kind, title, url) VALUES (7, 'link', '옛 링크', 'https://old.example');
    """)
    c.commit()
    c.close()
    db.configure(f'sqlite:///{path}')
    c = sqlite3.connect(path)
    assert not [r for r in c.execute('PRAGMA table_info(history)') if r[1] == 'task_id' and r[3]]
    assert not [r for r in c.execute('PRAGMA table_info(references_)') if r[1] == 'task_id' and r[3]]
    assert list(c.execute('PRAGMA foreign_key_check')) == []
    h = store.list_history(7)[0]
    assert (h['id'], h['content']) == (41, '옛 기록')  # id·내용 그대로
    p = store.create_person('김영수', '부사장')
    assert store.add_history(None, '새 동향', '동향', person_ids=[p['id']])['task_id'] is None
    store.delete_task(7)  # 다시 만든 테이블에서도 업무 삭제 시 이력이 같이 지워진다
    assert store.search_history('옛 기록') == []


def test_editing_keeps_seconds_when_minute_unchanged():
    p = store.create_person('김영수', '부사장')
    h = store.add_history(None, 'x', '동향', person_ids=[p['id']], occurred_at='2026-10-17T09:30:45')
    store.update_history(h['id'], occurred_at='2026-10-17T09:30', title='제목만 수정')
    assert store.get_history(h['id'])['occurred_at'] == '2026-10-17 09:30'
    from services.db import HistoryEntry
    with db.SessionLocal() as s:
        assert s.get(HistoryEntry, h['id']).occurred_at.second == 45
    store.update_history(h['id'], occurred_at='2026-10-16T08:00')
    assert store.get_history(h['id'])['occurred_at'] == '2026-10-16 08:00'
