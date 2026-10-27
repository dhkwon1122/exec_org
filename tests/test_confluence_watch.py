import json

import pytest

from services import confluence, confluence_watch as cw, db, llm, outbox

WIKI = 'https://wiki.example.com'


class FakeResp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


class FakeWiki:
    """페이지마다 판별 본문을 갖고, CQL 검색은 공간·id 로만 거른다."""

    def __init__(self):
        self.pages = {}  # id → {'title','space','versions': [(when, by, body)]}
        self.calls = []

    def put(self, pid, title, space, when, by, body):
        p = self.pages.setdefault(pid, {'title': title, 'space': space, 'versions': []})
        p['versions'].append((when, by, body))

    def get(self, url, params=None, headers=None, **kw):
        params = params or {}
        self.calls.append((url, params))
        path = url.split('/rest/api/', 1)[1]
        if path.startswith('space/'):
            key = path.split('/')[1]
            return FakeResp(200, {'key': key, 'name': f'{key} 공간'}) if key != 'NOPE' else FakeResp(404, {})
        if path == 'content/search':
            cql = params['cql']
            out = []
            for pid, p in self.pages.items():
                if (f'space = "{p["space"]}"' in cql) or (f'id = {pid}' in cql) or ('ancestor' in cql and pid.startswith('2')):
                    when, by, _ = p['versions'][-1]
                    r = {'id': pid, 'title': p['title'], 'space': {'key': p['space']},
                         'version': {'number': len(p['versions']), 'when': when, 'by': {'displayName': by}}}
                    if 'body.storage' in params.get('expand', ''):
                        r['body'] = {'storage': {'value': p['versions'][-1][2]}}
                    out.append(r)
            return FakeResp(200, {'results': sorted(out, key=lambda r: r['version']['when'], reverse=True)})
        pid = path.split('/')[1]
        p = self.pages.get(pid)
        if p is None:
            return FakeResp(404, {})
        n = int(params['version']) if params.get('status') == 'historical' else len(p['versions'])
        return FakeResp(200, {'id': pid, 'title': p['title'], 'space': {'key': p['space']},
                              'version': {'number': n}, 'body': {'storage': {'value': p['versions'][n - 1][2]}}})


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATA_DIR', str(tmp_path))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    monkeypatch.setattr(llm, 'is_configured', lambda: False)
    for k, v in {'CONFLUENCE_URL': WIKI, 'CONFLUENCE_API_TOKEN': 'tok', 'CONFLUENCE_BROWSER_BASE_URL': WIKI}.items():
        monkeypatch.setenv(k, v)
    w = FakeWiki()
    monkeypatch.setattr(confluence.requests, 'get', w.get)
    return w


@pytest.mark.parametrize('target, kind, key', [
    ('EXEC', 'space', 'EXEC'),
    (f'{WIKI}/display/EXEC', 'space', 'EXEC'),
    (f'{WIKI}/display/EXEC/', 'space', 'EXEC'),
    (f'{WIKI}/spaces/EXEC/overview', 'space', 'EXEC'),
    (f'{WIKI}/spacedirectory/view.action?spaceKey=HR', 'space', 'HR'),
    (f'{WIKI}/pages/viewpage.action?pageId=200', 'page', '200'),
    (f'{WIKI}/display/EXEC/주간+보고', 'page', ''),
])
def test_parse_target(target, kind, key):
    t = cw.parse_target(target)
    assert t['kind'] == kind and (t.get('space_key') == key or t.get('page_id') == key or not key)


def test_watch_space_collects_updates_with_diff_and_summary(wiki, monkeypatch):
    wiki.put('100', '주간 보고', 'EXEC', '2026-10-26T09:00:00.000+09:00', '김팀장', '<p>안건 3개</p><p>담당: 이과장</p>')
    w = cw.add_watch('EXEC', by='나')
    assert w['title'] == 'EXEC 공간' and w['kind'] == 'space'
    with pytest.raises(ValueError, match='이미 지켜보고'):
        cw.add_watch(f'{WIKI}/display/EXEC')
    assert cw.check_watch(w['id']) == 1
    u = cw.list_updates()[0]
    assert (u['title'], u['version'], u['modified_by'], u['summary_status']) == ('주간 보고', 1, '김팀장', 'none')
    assert u['link'] == f'{WIKI}/pages/viewpage.action?pageId=100'
    assert cw.check_watch(w['id']) == 0  # 같은 판은 한 번만

    # 수정 → 앞 판(이미 모은 판)과 비교, AI 요약
    wiki.put('100', '주간 보고', 'EXEC', '2026-10-27T10:00:00.000+09:00', '박대리', '<p>안건 4개</p><p>담당: 이과장</p>')
    asked = []
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    monkeypatch.setattr(llm, 'chat', lambda s, u: asked.append(u) or json.dumps(
        {'summary': '안건이 3개에서 4개로 늘었다.', 'important': True}, ensure_ascii=False))
    assert cw.check_watch(w['id']) == 1
    u = cw.list_updates()[0]
    assert u['version'] == 2 and u['diff'] == '- 안건 3개\n+ 안건 4개' and (u['added'], u['removed']) == (1, 1)
    assert u['summary'] == '[중요] 안건이 3개에서 4개로 늘었다.'
    assert '바뀐 줄' in asked[0] and '박대리' in asked[0]
    assert not any(p.get('status') == 'historical' for _, p in wiki.calls)  # 앞 판은 모아 둔 것으로

    assert cw.unseen_count() == 2 and cw.list_watches()[0]['unseen'] == 2
    cw.mark_seen([u['id']])
    assert cw.unseen_count() == 1 and len(cw.list_updates(unseen_only=True)) == 1
    assert cw.mark_seen() == 1 and cw.unseen_count() == 0


def test_first_seen_page_reads_previous_version(wiki):
    for i in range(3):
        wiki.put('200', '평가 지침', 'HR', f'2026-10-2{i}T09:00:00', '최부장', f'<p>마감 11/{i + 1}</p>')
    w = cw.add_watch(f'{WIKI}/pages/viewpage.action?pageId=200', children=True)
    assert w['kind'] == 'page' and w['children'] and w['title'] == '평가 지침'
    cw.check_watch(w['id'])
    u = cw.list_updates()[0]
    assert u['version'] == 3 and u['diff'] == '- 마감 11/2\n+ 마감 11/3'
    assert any(p.get('status') == 'historical' and p.get('version') == 2 for _, p in wiki.calls)


def test_errors_kept_on_watch_and_check_all(wiki):
    with pytest.raises(confluence.ConfluenceError):
        cw.add_watch('NOPE')
    w = cw.add_watch('EXEC')
    wiki.pages.clear()
    monkey = wiki.get

    def broken(url, params=None, **kw):
        if 'search' in url:
            return FakeResp(403, {})
        return monkey(url, params, **kw)
    confluence.requests.get = broken
    r = cw.check_all()
    assert r['new'] == 0 and '403' in r['errors'][0][1]
    assert '403' in cw.list_watches()[0]['last_error']
    cw.set_active(w['id'], False)
    assert cw.check_all() == {'new': 0, 'errors': []}


def test_digest_and_mail_draft(wiki):
    wiki.put('100', '주간 보고', 'EXEC', '2026-10-26T09:00:00', '김팀장', '<p>안건</p>')
    w = cw.add_watch('EXEC')
    cw.check_watch(w['id'])
    ups = cw.list_updates()
    assert '주간 보고' in cw.digest([u['id'] for u in ups])  # LLM 없으면 목록
    d = outbox.draft_confluence(ups, '요약 글')
    assert d['subject'].startswith('[컨플루언스 변경]') and '주간 보고' in d['table_html'] and d['text'].startswith('요약 글')
    assert d['related_kind'] == 'confluence'
    with pytest.raises(ValueError):
        cw.digest([])


def test_check_uses_few_calls_and_runs_in_background(wiki, monkeypatch):
    """호출 한도(분당 15회) — 검색 한 번에 본문까지, 앞 판은 모아 둔 게 없을 때만 따로."""
    for pid in ('100', '101', '102'):
        wiki.put(pid, f'페이지 {pid}', 'EXEC', '2026-10-26T09:00:00', '김팀장', f'<p>{pid} 첫 판</p>')
    w = cw.add_watch('EXEC')
    wiki.calls.clear()
    assert cw.run_once(w['id'])['new'] == 3
    assert [u for u, _ in wiki.calls if '/content/' in u and 'search' not in u] == []  # 본문은 검색 결과로
    assert len(wiki.calls) == 1
    for pid in ('100', '101'):
        wiki.put(pid, f'페이지 {pid}', 'EXEC', '2026-10-27T09:00:00', '박대리', f'<p>{pid} 둘째 판</p>')
    wiki.calls.clear()
    assert cw.run_once()['new'] == 2
    assert len(wiki.calls) == 1  # 앞 판은 모아 둔 것 — 검색 한 번이면 끝
    assert not cw.is_running() and cw.last_run()['new'] == 2
