import base64
import json

import pytest

from services import confluence, db, enrich, llm, mailbox, store
from tests.test_mailbox import FakePOP, _mail

WIKI = 'https://wiki.example.com'
STORAGE = ('<h1>2027 예산 지침</h1><p>총액 <strong>3억</strong> 이내</p>'
           '<table><tr><th>항목</th><th>금액</th></tr><tr><td>장비</td><td>2억</td></tr></table>'
           '<ac:link><ri:page ri:content-title="세부 기준"/></ac:link>'
           '<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[a < b]]></ac:plain-text-body>'
           '</ac:structured-macro>')


class FakeResp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.text = json.dumps(data)

    def json(self):
        return self._data


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    monkeypatch.setattr(db, 'DATA_DIR', str(tmp_path))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    monkeypatch.setattr(llm, 'is_configured', lambda: False)
    monkeypatch.setenv('CONFLUENCE_URL', 'https://api.wiki.example.com/rest/api')
    monkeypatch.setenv('CONFLUENCE_BROWSER_BASE_URL', WIKI)
    monkeypatch.setenv('CONFLUENCE_API_TOKEN', 'tok')
    monkeypatch.setenv('CONFLUENCE_DEP_TICKET', 'T-1')
    calls = []

    def fake_get(url, params=None, headers=None, **kw):
        calls.append((url, params, headers))
        if url.endswith('/content/403'):
            return FakeResp(403, {})
        if url.endswith('/rest/api/content'):  # 제목으로 찾기
            return FakeResp(200, {'results': [{'id': '77', 'title': params['title'], 'space': {'key': params['spaceKey']},
                                               'version': {'number': 2}, 'body': {'storage': {'value': '<p>찾음</p>'}}}]})
        pid = url.rsplit('/', 1)[-1]
        return FakeResp(200, {'id': pid, 'title': '2027 예산 지침', 'space': {'key': 'EXEC'}, 'version': {'number': 5},
                              'body': {'storage': {'value': STORAGE}}})
    monkeypatch.setattr(confluence.requests, 'get', fake_get)
    yield calls


@pytest.mark.parametrize('url, ref', [
    (f'{WIKI}/pages/viewpage.action?pageId=123456', ('id', '123456')),
    (f'{WIKI}/spaces/EXEC/pages/123456/2027+예산', ('id', '123456')),
    (f'{WIKI}/display/EXEC/2027+%EC%98%88%EC%82%B0', ('title', 'EXEC', '2027 예산')),
    ('123456', ('id', '123456')),
    (f'{WIKI}/x/' + base64.b64encode((123456).to_bytes(4, 'little')).decode().rstrip('='), ('id', '123456')),
    (f'{WIKI}/dashboard.action', ('', '')),
])
def test_page_ref(url, ref):
    assert confluence.page_ref(url) == ref


def test_url_prefixes(monkeypatch):
    assert confluence.is_confluence_url(f'{WIKI}/pages/1')
    assert confluence.is_confluence_url('https://api.wiki.example.com/rest/api/content/1')
    assert not confluence.is_confluence_url('https://wiki.example.com.evil.com/pages/1')
    assert confluence.find_urls(f'참고: {WIKI}/pages/1, 그리고 https://naver.com 과 ({WIKI}/pages/2).') == [
        f'{WIKI}/pages/1', f'{WIKI}/pages/2']
    monkeypatch.setenv('CONFLUENCE_URL_PREFIXES', 'https://confluence.corp.net, https://wiki2.corp.net')
    assert confluence.is_confluence_url('https://wiki2.corp.net/display/A/B')
    assert not confluence.is_confluence_url(f'{WIKI}/pages/1')


def test_read_page_with_headers_and_cache(env):
    p = confluence.read(f'{WIKI}/pages/viewpage.action?pageId=123456')
    assert p['title'] == '2027 예산 지침' and p['version'] == 5
    assert '항목 | 금액' in p['text'] and '장비 | 2억' in p['text'] and '세부 기준' in p['text'] and 'a < b' in p['text']
    url, _, headers = env[0]
    assert url == 'https://api.wiki.example.com/rest/api/content/123456'
    assert headers['Authorization'] == 'Bearer tok' and headers['X-Dep-Ticket'] == 'T-1'
    assert headers['User-Agent'].startswith('curl/')
    confluence.read(f'{WIKI}/pages/viewpage.action?pageId=123456')
    assert len(env) == 1  # 저장된 것
    confluence.read(f'{WIKI}/pages/viewpage.action?pageId=123456', force=True)
    assert len(env) == 2
    assert confluence.read(f'{WIKI}/display/EXEC/회의록')['page_id'] == '77'


def test_errors_are_reported(monkeypatch):
    with pytest.raises(confluence.ConfluenceError, match='403'):
        confluence.read(f'{WIKI}/pages/403')
    with pytest.raises(confluence.ConfluenceError, match='알아보지 못했습니다'):
        confluence.read(f'{WIKI}/dashboard.action')
    monkeypatch.setenv('CONFLUENCE_API_TOKEN', '')
    with pytest.raises(confluence.ConfluenceError, match='설정되지 않았습니다'):
        confluence.read(f'{WIKI}/pages/1', force=True)


def test_reference_link_reads_page(env):
    t = store.create_task('예산')
    ref = store.add_reference(t['id'], 'link', url=f'{WIKI}/pages/123456')
    assert ref['title'] == ref['url']
    confluence.refresh_reference(ref['id'])
    r = store.get_reference(ref['id'])
    assert r['title'] == '2027 예산 지침' and '장비 | 2억' in r['content'] and r['content_at']
    assert [x['id'] for x in store.list_references(query='장비')] == [ref['id']]  # 내용으로 검색
    bad = store.add_reference(t['id'], 'link', title='권한 없음', url=f'{WIKI}/pages/403')
    other = store.add_reference(t['id'], 'link', url='https://naver.com')
    assert confluence.read_new_references() == 1  # 컨플루언스 링크만
    r = store.get_reference(bad['id'])
    assert '403' in r['content_error'] and r['content_at'] and r['title'] == '권한 없음'
    assert store.get_reference(other['id'])['content_at'] == ''
    assert confluence.read_new_references() == 0


def test_history_ai_reads_linked_page(env, monkeypatch):
    seen = []
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    monkeypatch.setattr(llm, 'analyze_record', lambda text, *a, **k: seen.append(text) or
                        {'title': 't', 'summary': 's', 'keywords': [], 'todos': []})
    t = store.create_task('예산')
    h = store.add_history(t['id'], f'팀장 피드백 — 지침 참고 {WIKI}/pages/123456', '피드백', '나')
    enrich.enrich_entry(h['id'])
    assert "컨플루언스 페이지 '2027 예산 지침'" in seen[0] and '총액 3억 이내' in seen[0].replace('\n', ' ')


def test_mail_ai_reads_linked_page(env, monkeypatch, tmp_path):
    monkeypatch.setattr(mailbox, '_STATUS_FILE', str(tmp_path / 'st.json'))
    for k, v in {'MAIL_POP3_HOST': 'pop.example', 'MAIL_POP3_USER': 'me', 'MAIL_SINCE_DAYS': '0'}.items():
        monkeypatch.setenv(k, v)
    mailbox.fetch_new(connect=lambda cfg: FakePOP([_mail('지침 공유', 'a@x', text=f'아래 페이지 확인 부탁드립니다 {WIKI}/pages/9')]))
    seen = []
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    monkeypatch.setattr(llm, 'analyze_record', lambda text, *a, **k: seen.append(text) or
                        {'title': 't', 'summary': 's', 'keywords': [], 'todos': []})
    mid = mailbox.list_mails()[0]['id']
    mailbox.run_actions(mid, ['summary'], '나')
    assert '장비 | 2억' in seen[0]
    m = mailbox.get_mail(mid)
    assert m['suggestion']['confluence'] == ["컨플루언스 '2027 예산 지침'"] and '장비' not in m['body']


# ── 호출 한도 (분당 N회) ──────────────────────────────────────────────

class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(confluence.time, 'time', c.time)
    monkeypatch.setattr(confluence.time, 'sleep', c.sleep)
    return c


def test_interactive_calls_wait_briefly_then_rate_limited(env, clock, monkeypatch):
    monkeypatch.setenv('CONFLUENCE_RATE_PER_MINUTE', '5')
    for i in range(5):
        confluence._get(f'content/{i}')
    assert confluence.calls_last_minute() == 5
    with pytest.raises(confluence.RateLimited, match='분당 5회'):
        confluence._get('content/9')  # 자리가 나려면 60초 — 화면은 15초까지만 기다린다
    assert len(env) == 5 and clock.t == 1_000_000.0
    clock.t += 50  # 50초 뒤엔 10초만 기다리면 된다
    confluence._get('content/9')
    assert len(env) == 6 and 1_000_060 <= clock.t < 1_000_061


def test_background_leaves_room_for_screen(env, clock, monkeypatch):
    monkeypatch.setenv('CONFLUENCE_RATE_PER_MINUTE', '5')
    monkeypatch.setenv('CONFLUENCE_RATE_RESERVE', '2')
    with confluence.background():
        for i in range(3):
            confluence._get(f'content/{i}')
        start = clock.t
        confluence._get('content/3')  # 백그라운드 몫(3회)을 다 써서 1분 기다림
        assert clock.t - start >= 59
    clock.t = start + 1  # (같은 1분 안의) 화면 호출은 남겨 둔 몫으로 바로
    confluence._get('content/4')
    assert len(env) == 5


def test_429_blocks_everyone_and_background_retries(env, clock, monkeypatch):
    calls = []

    class R429:
        status_code, text, headers = 429, '', {'Retry-After': '30'}

    real = confluence.requests.get

    def flaky(url, params=None, **kw):
        calls.append(url)
        return R429() if len(calls) == 1 else real(url, params, **kw)

    monkeypatch.setattr(confluence.requests, 'get', flaky)
    with pytest.raises(confluence.RateLimited, match='429'):
        confluence._get('content/1')  # 화면 호출은 다시 하지 않고 알림
    with pytest.raises(confluence.RateLimited):
        confluence._get('content/2')  # 30초 동안 모두 멈춤 (15초 넘게 기다려야 함)
    calls.clear()
    with confluence.background():
        t0 = clock.t
        assert confluence._get('content/3')['id'] == '3'  # 429 → 30초 기다렸다 다시
        assert clock.t - t0 >= 30 and len(calls) == 2


def test_rate_limited_reference_is_left_for_background(env, clock, monkeypatch):
    monkeypatch.setenv('CONFLUENCE_RATE_PER_MINUTE', '1')
    t = store.create_task('예산')
    a = store.add_reference(t['id'], 'link', url=f'{WIKI}/pages/1')
    b = store.add_reference(t['id'], 'link', url=f'{WIKI}/pages/2')
    confluence.refresh_reference(a['id'])
    with pytest.raises(confluence.RateLimited):
        confluence.refresh_reference(b['id'])
    r = store.get_reference(b['id'])
    assert r['content_at'] == '' and '호출 한도' in r['content_error']  # 아직 안 읽음 → 백그라운드가 이어 읽음
    assert confluence.read_new_references() == 0  # 화면 몫으로는 여전히 한도 — 다음 기회에
    with confluence.background():
        monkeypatch.setenv('CONFLUENCE_RATE_RESERVE', '0')
        assert confluence.read_new_references() == 1
    assert store.get_reference(b['id'])['content']


# ── 참고자료 요약 · 보여 줄 주소 ─────────────────────────────────────

def test_display_url_maps_api_address_to_page(monkeypatch):
    # CONFLUENCE_URL 은 REST 게이트웨이(api.wiki…), 사용자는 wiki… 로 연다
    assert confluence.page_url('5') == f'{WIKI}/pages/viewpage.action?pageId=5'
    assert confluence.display_url('https://api.wiki.example.com/rest/api/content/123?expand=body') == \
        f'{WIKI}/pages/viewpage.action?pageId=123'
    assert confluence.display_url('https://api.wiki.example.com/pages/viewpage.action?pageId=7') == \
        f'{WIKI}/pages/viewpage.action?pageId=7'
    keep = f'{WIKI}/display/EXEC/주간보고'
    assert confluence.display_url(keep) == keep  # 이미 페이지 주소
    assert confluence.display_url('https://naver.com/rest/api/x') == 'https://naver.com/rest/api/x'


def test_reference_gets_summary_and_page_url(env, monkeypatch):
    asked = []
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    monkeypatch.setattr(llm, 'chat', lambda system, user, temperature=0.2: asked.append(user) or
                        '2027 예산 지침 — 총액 3억 이내, 장비 2억.')
    t = store.create_task('예산')
    ref = store.add_reference(t['id'], 'link', url='https://api.wiki.example.com/rest/api/content/123456')
    confluence.refresh_reference(ref['id'])
    r = store.get_reference(ref['id'])
    assert r['content_summary'] == '2027 예산 지침 — 총액 3억 이내, 장비 2억.'
    assert r['url'] == f'{WIKI}/pages/viewpage.action?pageId=123456' and r['title'] == '2027 예산 지침'
    assert '장비 | 2억' in asked[0]
    confluence.refresh_reference(ref['id'])  # 내용이 그대로면 다시 요약하지 않는다
    assert len(asked) == 1
    assert [x['id'] for x in store.list_references(query='3억 이내')] == [ref['id']]  # 요약으로도 검색
    box = str(ui_box(r))
    assert '2027 예산 지침 — 총액 3억' in box and '원문 보기' in box


def ui_box(r):
    from components import ui
    return ui.confluence_box(r, {'type': 'r-conf', 'id': r['id']})


def test_summarize_missing_for_refs_read_before(env, monkeypatch):
    t = store.create_task('예산')
    ref = store.add_reference(t['id'], 'link', url=f'{WIKI}/pages/1')
    confluence.refresh_reference(ref['id'])  # LLM 없을 때 읽음 → 요약 없음
    assert store.get_reference(ref['id'])['content_summary'] == ''
    n_calls = len(env)
    monkeypatch.setattr(llm, 'is_configured', lambda: True)
    monkeypatch.setattr(llm, 'chat', lambda *a, **k: '요약됨')
    assert confluence.summarize_missing() == 1
    assert store.get_reference(ref['id'])['content_summary'] == '요약됨'
    assert len(env) == n_calls  # 컨플루언스는 부르지 않음
    assert confluence.summarize_missing() == 0


def test_failed_reference_retried_with_backoff(env, monkeypatch):
    from datetime import datetime, timedelta
    from services.db import Reference
    t = store.create_task('예산')
    ref = store.add_reference(t['id'], 'link', url=f'{WIKI}/pages/403')
    with pytest.raises(confluence.ConfluenceError):
        confluence.refresh_reference(ref['id'])
    r = store.get_reference(ref['id'])
    assert r['content_tries'] == 1 and '403' in r['content_error']
    assert confluence.retry_failed_references() == 0  # 30분이 안 지남

    def age(minutes):
        with db.SessionLocal() as s:
            x = s.get(Reference, ref['id'])
            x.content_at = datetime.now() - timedelta(minutes=minutes)
            s.commit()
    age(31)
    assert confluence.retry_failed_references() == 1
    assert store.get_reference(ref['id'])['content_tries'] == 2
    age(31)
    assert confluence.retry_failed_references() == 0  # 두 번째 실패 뒤엔 1시간
    # 권한이 생겨 읽히면 실패 횟수가 지워진다
    with db.SessionLocal() as s:
        x = s.get(Reference, ref['id'])
        x.url = f'{WIKI}/pages/77'
        s.commit()
    age(61)
    assert confluence.retry_failed_references() == 1
    r = store.get_reference(ref['id'])
    assert r['content_tries'] == 0 and r['content_error'] == '' and r['content']
    # 여섯 번 실패하면 자동 재시도는 멈춘다
    with db.SessionLocal() as s:
        x = s.get(Reference, ref['id'])
        x.url, x.content_error, x.content_tries = f'{WIKI}/pages/403', 'x', confluence.RETRY_MAX
        x.content_at = datetime.now() - timedelta(days=2)
        s.commit()
    assert confluence.retry_failed_references() == 0
