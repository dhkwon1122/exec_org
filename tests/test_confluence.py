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
