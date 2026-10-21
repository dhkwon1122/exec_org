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
                           ' "keywords": ["#예산", "장비", "예산", "감액", "비용 절감"],'
                           ' "todos": [{"title": "예산안 수정", "due": "2026-10-24", "assignee": "권대현"},'
                           ' {"title": "B사 견적 요청", "due": "다음주"}, "회의록 공유", {"due": "x"}]}\n```', calls)
    out = llm.analyze_record('팀장님: 장비 예산 좀 줄여봐 20%정도', '장비 도입', '피드백')
    assert out == {'title': '장비 예산 피드백', 'summary': '예산 20% 감액 요청.',
                   'keywords': ['예산', '장비', '감액', '비용 절감'],
                   'todos': [{'title': '예산안 수정', 'due': '2026-10-24', 'assignee': '권대현'},
                             {'title': 'B사 견적 요청', 'due': '', 'assignee': ''},
                             {'title': '회의록 공유', 'due': '', 'assignee': ''}]}
    prompt = calls[0]['messages'][1]['content']
    assert '장비 도입' in prompt and '기록 기준일:' in prompt


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
    assert h['ai_token'] == '' and h['ai_reference_date'] == ''
    token = store.begin_history_ai(h['id'])
    store.set_history_ai(h['id'], 'done', title='새 요약', token=token, reference_date='2025-01-01')
    assert store.get_history(h['id'])['ai_reference_date'] == '2025-01-01'


@pytest.mark.parametrize('fail', [False, True])
def test_ai_response_cannot_overwrite_user_edit(monkeypatch, fail):
    t = store.create_task('업무')
    h = store.add_history(t['id'], '이전 원문')

    def during_request(*args, **kwargs):
        store.update_history(h['id'], content='수정한 원문', title='직접 쓴 제목', summary='직접 쓴 요약')
        if fail:
            raise llm.LLMError('늦게 도착한 실패')
        return {'title': '예전 AI 제목', 'summary': '예전 AI 요약', 'keywords': [], 'todos': []}

    monkeypatch.setattr(llm, 'analyze_record', during_request)
    enrich.enrich_entry(h['id'])
    got = store.get_history(h['id'])
    assert (got['content'], got['title'], got['summary']) == ('수정한 원문', '직접 쓴 제목', '직접 쓴 요약')
    assert got['ai_status'] == '' and got['ai_error'] == ''


def test_only_latest_ai_request_can_save(monkeypatch):
    t = store.create_task('업무')
    h = store.add_history(t['id'], '원문')
    old = store.begin_history_ai(h['id'])
    current = store.begin_history_ai(h['id'])
    store.set_history_ai(h['id'], 'done', title='최신 제목', token=current)
    store.set_history_ai(h['id'], 'done', title='예전 제목', token=old)
    store.set_history_ai(h['id'], 'failed', error='예전 오류', token=old)
    got = store.get_history(h['id'])
    assert got['title'] == '최신 제목' and got['ai_status'] == 'done' and got['ai_error'] == ''
    monkeypatch.setattr(llm, 'analyze_record', lambda *a, **kw: pytest.fail('취소된 작업은 LLM 을 호출하면 안 됨'))
    enrich.enrich_entry(h['id'], old)


def test_queued_request_is_invalidated_by_user_edit(monkeypatch):
    t = store.create_task('업무')
    h = store.add_history(t['id'], '원문')
    jobs = []
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    monkeypatch.setattr(enrich._executor, 'submit', lambda *args: jobs.append(args))
    enrich.submit(h['id'])
    store.update_history(h['id'], title='수동 제목')
    monkeypatch.setattr(llm, 'analyze_record', lambda *a, **kw: pytest.fail('편집 전에 대기 중이던 작업'))
    jobs[0][0](*jobs[0][1:])
    assert store.get_history(h['id'])['title'] == '수동 제목'


def test_failed_submission_keeps_saved_record_and_allows_retry(monkeypatch):
    t = store.create_task('업무')
    h = store.add_history(t['id'], '중요한 기록')
    monkeypatch.setattr(llm, 'is_configured', lambda: True)

    def reject(*_):
        raise RuntimeError('executor shutdown')

    monkeypatch.setattr(enrich._executor, 'submit', reject)
    enrich.submit(h['id'])
    got = store.get_history(h['id'])
    assert got['content'] == '중요한 기록' and got['ai_status'] == 'failed'
    assert 'executor shutdown' in got['ai_error']


def test_past_record_and_regeneration_use_event_date(monkeypatch):
    calls = []
    _fake_llm(monkeypatch, '{"title":"T", "todos":[{"title":"보고서 제출", "due":"2020-01-10"}]}', calls)
    t = store.create_task('업무')
    h = store.add_history(t['id'], '금요일까지 보고서 제출', occurred_at='2020-01-06T10:00')
    enrich.enrich_entry(h['id'])
    enrich.enrich_entry(h['id'])
    for call in calls:
        prompt = call['messages'][1]['content']
        assert '기록 기준일: 2020-01-06 (월요일)' in prompt
        assert '기록 기준일을 기준으로 실제 날짜로 바꾸고' in prompt
    got = store.get_history(h['id'])
    assert got['ai_reference_date'] == '2020-01-06' and got['ai_todos'][0]['due'] == '2020-01-10'
    store.update_history(h['id'], occurred_at='2020-02-03T10:00')
    assert store.get_history(h['id'])['ai_todos'] == []


def test_unspecified_reference_date_defaults_to_today(monkeypatch):
    from datetime import date
    calls = []
    _fake_llm(monkeypatch, '{"title":"T"}', calls)
    llm.analyze_record('오늘 기록')
    assert f'기록 기준일: {date.today().isoformat()}' in calls[0]['messages'][1]['content']
