import json

import pytest
from sqlalchemy import create_engine, text

from services import db, enrich, llm, store


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    for k in ('LLM_API_URL', 'LLM2_API_URL', 'LLM_MODEL', 'LLM2_MODEL', 'LLM_API_KEY'):
        monkeypatch.delenv(k, raising=False)
    yield


class _Resp:
    def __init__(self, content, status=200):
        self.status_code = status
        self._body = {'choices': [{'message': {'content': content}}]}
        self.text = json.dumps(self._body, ensure_ascii=False)

    def json(self):
        return self._body


def _fake_llm(monkeypatch, content, calls=None):
    monkeypatch.setenv('LLM_API_URL', 'http://llm.local/v1/chat/completions')

    def post(url, json=None, headers=None, timeout=None):
        if calls is not None:
            calls.append(json)
        return _Resp(content)
    monkeypatch.setattr(llm.requests, 'post', post)


def test_analyze_record_parses_thinking_and_codefence(monkeypatch):
    calls = []
    _fake_llm(monkeypatch, '<think>음…</think>```json\n{"title": "장비 예산 피드백", "summary": "예산 20% 감액 요청.",'
                           ' "keywords": ["#예산", "장비", "예산", "감액", "비용 절감"]}\n```', calls)
    out = llm.analyze_record('팀장님: 장비 예산 좀 줄여봐 20%정도', '장비 도입', '피드백')
    assert out == {'title': '장비 예산 피드백', 'summary': '예산 20% 감액 요청.',
                   'keywords': ['예산', '장비', '감액', '비용 절감']}
    assert '장비 도입' in calls[0]['messages'][1]['content']


def test_llm2_env_fallback(monkeypatch):
    monkeypatch.setenv('LLM2_API_URL', 'http://x/v1/chat/completions')
    assert llm.is_configured()


def test_enrich_saves_raw_then_fills_ai_fields(monkeypatch):
    _fake_llm(monkeypatch, '{"title": "T", "summary": "S", "keywords": "가, 나"}')
    t = store.create_task('업무')
    h = store.add_history(t['id'], '  원문 그대로\n두 번째 줄 ', '피드백')
    enrich.enrich_entry(h['id'])
    got = store.get_history(h['id'])
    assert got['content'] == '원문 그대로\n두 번째 줄'
    assert (got['title'], got['summary'], got['keywords'], got['ai_status']) == ('T', 'S', ['가', '나'], 'done')


def test_enrich_failure_keeps_record(monkeypatch):
    _fake_llm(monkeypatch, '죄송합니다 JSON 없음')
    t = store.create_task('업무')
    h = store.add_history(t['id'], '중요한 원문', '피드백')
    enrich.enrich_entry(h['id'])
    got = store.get_history(h['id'])
    assert got['content'] == '중요한 원문'
    assert got['ai_status'] == 'failed' and 'JSON' in got['ai_error']


def test_submit_without_llm_is_noop():
    t = store.create_task('업무')
    h = store.add_history(t['id'], '원문')
    enrich.submit(h['id'])
    assert store.get_history(h['id'])['ai_status'] == ''


def test_search_history_and_terms():
    a = store.create_task('장비 도입')
    b = store.create_task('채용')
    store.add_history(a['id'], '공급사 미팅 결과 A사 유력', '회의', keywords=['공급사', '벤더 선정'])
    store.add_history(a['id'], '예산 감액 피드백', '피드백', summary='팀장 피드백: 예산 20% 감액')
    store.add_history(b['id'], '면접 일정 조율', '진행', author='kim')
    # 키워드로만 매칭되는 동의어
    assert [h['content'] for h in store.search_history('벤더')] == ['공급사 미팅 결과 A사 유력']
    # 여러 단어 AND (업무명 + 요약)
    assert len(store.search_history('장비 팀장')) == 1
    assert store.search_history('장비 면접') == []
    # 따옴표 구절
    assert store.search_terms('"벤더 선정" 예산') == ['벤더 선정', '예산']
    assert len(store.search_history('"벤더 선정"')) == 1
    # 자동 이력(업무 생성 등)은 기본 제외
    assert all(h['entry_type'] != '상태변경' for h in store.search_history(''))
    assert any(h['entry_type'] == '상태변경' for h in store.search_history('', include_auto=True))
    assert len(store.search_history('', author='kim')) == 1
    assert len(store.search_history('', entry_types=['피드백'])) == 1
    # 업무 목록 검색도 AI 필드를 본다
    assert [t['id'] for t in store.list_tasks('벤더')] == [a['id']]


def test_search_date_range():
    t = store.create_task('a')
    store.add_history(t['id'], '옛날', occurred_at='2024-03-10T09:00')
    store.add_history(t['id'], '최근', occurred_at='2026-01-05T18:30')
    assert [h['content'] for h in store.search_history('', date_from='2026-01-01')] == ['최근']
    assert [h['content'] for h in store.search_history('', date_to='2024-03-10')] == ['옛날']


def test_keyword_stats_and_update():
    t = store.create_task('a')
    h1 = store.add_history(t['id'], 'x', keywords='예산, 장비')
    store.add_history(t['id'], 'y', keywords=['예산'])
    assert store.keyword_stats()[0] == ('예산', 2)
    store.update_history(h1['id'], keywords='장비, 구매', title='제목')
    assert store.get_history(h1['id'])['keywords'] == ['장비', '구매']
    with pytest.raises(ValueError):
        store.update_history(h1['id'], content='  ')


def test_migration_adds_new_columns(tmp_path):
    path = tmp_path / 'old.db'
    eng = create_engine(f'sqlite:///{path}')
    with eng.begin() as c:  # 이전 버전 스키마(AI 컬럼 없음)
        c.execute(text('CREATE TABLE tasks (id INTEGER PRIMARY KEY, title VARCHAR(300) NOT NULL, description TEXT,'
                       ' status VARCHAR(20) NOT NULL, owner VARCHAR(100), tags VARCHAR(500), parent_id INTEGER,'
                       ' start_date VARCHAR(10), due_date VARCHAR(10), created_at DATETIME, updated_at DATETIME)'))
        c.execute(text('CREATE TABLE history (id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL,'
                       ' entry_type VARCHAR(20) NOT NULL, content TEXT NOT NULL, author VARCHAR(100),'
                       ' occurred_at DATETIME, created_at DATETIME)'))
        c.execute(text("INSERT INTO tasks (id, title, status) VALUES (1, '옛 업무', '진행중')"))
        c.execute(text("INSERT INTO history (task_id, entry_type, content, occurred_at)"
                       " VALUES (1, '메모', '옛 기록', '2025-01-01 10:00:00')"))
    db.configure(f'sqlite:///{path}')
    h = store.list_history(1)[0]
    assert h['content'] == '옛 기록' and h['keywords'] == [] and h['ai_status'] == ''
    assert len(store.search_history('옛')) == 1
