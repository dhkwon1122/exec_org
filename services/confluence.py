"""컨플루언스 페이지 읽기 — 참고자료 링크·업무 기록·메일에 컨플루언스 주소가 있으면 그 페이지 내용을 읽어 온다.

tea-meeting-agenda(confluence_agenda/web/docx_export.py)의 사내 Confluence 연결 방식을 그대로 따른다 — 같은 .env 를 쓸 수 있다.
  CONFLUENCE_URL             REST 기준 주소 (위키 주소 또는 REST 게이트웨이 주소, 끝의 /rest/api 는 떼고 씀)
  CONFLUENCE_API_TOKEN       PAT (Server/DC 는 Bearer) — 비우면 읽지 않는다
  CONFLUENCE_URL_PREFIXES    이 글자로 시작하는 주소를 컨플루언스 페이지로 본다 (쉼표로 여러 개)
                             비우면 CONFLUENCE_BROWSER_BASE_URL·CONFLUENCE_URL
  CONFLUENCE_USERNAME        Cloud 일 때만 (Basic 인증) — 사내 Server/DC 면 비울 것
  CONFLUENCE_AUTH_HEADER / CONFLUENCE_AUTH_SCHEME / CONFLUENCE_USER_AGENT
  CONFLUENCE_DEP_TICKET(_HEADER) / CONFLUENCE_DATA_CLASSIFICATION(_HEADER)   사내 게이트웨이 보안 헤더
  CONFLUENCE_CA_BUNDLE (또는 REQUESTS_CA_BUNDLE·SSL_CERT_FILE) / CONFLUENCE_NO_PROXY

페이지 주소 형태: …/pages/123456/…, ?pageId=123456, /display/SPACE/제목, /x/AbCd(짧은 링크), 페이지 id 숫자.
읽은 내용은 confluence_pages 에 남겨(CACHE_MINUTES 동안) 같은 페이지를 거듭 부르지 않는다.

호출 한도: 사내 API 는 분당 CONFLUENCE_RATE_PER_MINUTE(기본 15)회. 모든 호출이 _acquire() 를 거치고, 여러 gunicorn
워커·스레드가 같이 세도록 DATA_DIR/.confluence_rate.json 에 최근 1분 호출 시각을 파일 잠금으로 남긴다.
  - 화면에서 부르는 호출(interactive): 자리가 날 때까지 최대 INTERACTIVE_WAIT 초 기다리고, 그래도 없으면 RateLimited.
  - 백그라운드(with background(): — 워치 확인·링크 읽기·기록 AI 정리): 한도에서 CONFLUENCE_RATE_RESERVE(기본 3)회를
    화면용으로 남겨 두고 자리가 날 때까지 기다린다.
  - 429 를 받으면 Retry-After(없으면 60초) 동안 모두 멈추고, 백그라운드는 그 뒤 한 번 다시 부른다.
연결이 안 될 때: python -m services.confluence <페이지 주소>
"""
import base64
import contextvars
import fcntl
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote_plus, urlparse

import requests
from sqlalchemy import select

from services import db, mail_text
from services.db import ConfluencePage, Reference

log = logging.getLogger(__name__)

CACHE_MINUTES = 60
MAX_TEXT_CHARS = 200_000
AI_PAGES = 3  # AI 분석 때 함께 읽는 페이지 수
AI_CHARS_PER_PAGE = 6000

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='confluence')


def _env(name: str, default: str = '') -> str:
    return os.environ.get(f'CONFLUENCE_{name}', default).strip()


def _normalize_base_url(url: str) -> str:
    return re.sub(r'/rest/api/?$', '', (url or '').rstrip('/'), flags=re.I)


def is_configured() -> bool:
    return bool(_env('URL') and _env('API_TOKEN'))


def prefixes() -> list[str]:
    raw = [p.strip() for p in _env('URL_PREFIXES').split(',') if p.strip()]
    if not raw:
        raw = [_env('BROWSER_BASE_URL'), _normalize_base_url(_env('URL'))]
    return [p.rstrip('/').lower() for p in raw if p]


def is_confluence_url(url: str) -> bool:
    u = (url or '').strip().lower()
    return bool(u) and any(u == p or u.startswith(p + '/') or u.startswith(p + '?') for p in prefixes())


_URL = re.compile(r'https?://[^\s<>"\'\])}]+')


def browser_base() -> str:
    """사용자가 브라우저로 여는 위키 주소 — REST 게이트웨이 주소(CONFLUENCE_URL)와 다를 수 있다."""
    return (_env('BROWSER_BASE_URL') or _normalize_base_url(_env('URL'))).rstrip('/')


def page_url(page_id: str) -> str:
    """페이지를 브라우저로 여는 주소."""
    base = browser_base()
    return f'{base}/pages/viewpage.action?pageId={page_id}' if base and page_id else ''


def _is_api_url(url: str) -> bool:
    """REST 호출 주소인지 — /rest/api/ 이거나, 브라우저 주소와 다른 REST 게이트웨이 주소로 시작."""
    u = (url or '').lower()
    rest = _normalize_base_url(_env('URL')).lower()
    return '/rest/api/' in u or bool(rest and rest != browser_base().lower() and u.startswith(rest))


def display_url(url: str, page_id: str = '') -> str:
    """화면·메일에 보여 줄 주소 — API 주소면 그 페이지를 브라우저로 여는 주소로. 컨플루언스가 아니면 그대로."""
    if not url or not is_confluence_url(url) or not _is_api_url(url):
        return url
    if not page_id:
        m = re.search(r'/rest/api/content/(\d+)', url)
        page_id = m.group(1) if m else (page_ref(url)[1] if page_ref(url)[0] == 'id' else '')
    return page_url(page_id) or url


def find_urls(text: str) -> list[str]:
    """글 속 컨플루언스 주소 (순서대로, 중복 없이)."""
    out = []
    for m in _URL.finditer(text or ''):
        url = m.group(0).rstrip('.,;:')
        if is_confluence_url(url) and url not in out:
            out.append(url)
    return out


# ── 주소 → 페이지 ─────────────────────────────────────────────────────

def _tiny_to_id(code: str) -> str | None:
    """짧은 링크 /x/AbCd → 페이지 id (base64 로 적은 리틀엔디언 숫자)."""
    s = code.replace('-', '/').replace('_', '+')
    try:
        raw = base64.b64decode(s + '=' * (-len(s) % 4))
    except ValueError:
        return None
    n = int.from_bytes(raw, 'little')
    return str(n) if n else None


def page_ref(url: str) -> tuple:
    """('id', 페이지id) | ('title', 스페이스, 제목) | ('', '') — 못 알아보면."""
    url = (url or '').strip()
    if url.isdigit():
        return 'id', url
    for pat in (r'/pages/(\d+)(?:/|$|\?|#)', r'[?&]pageId=(\d+)', r'/rest/api/content/(\d+)'):
        m = re.search(pat, url)
        if m:
            return 'id', m.group(1)
    path = urlparse(url).path
    m = re.search(r'/display/([^/]+)/([^/?#]+)', path)
    if m:
        return 'title', unquote_plus(m.group(1)), unquote_plus(m.group(2))
    m = re.search(r'/x/([A-Za-z0-9_\-]+)', path)
    if m:
        pid = _tiny_to_id(m.group(1))
        if pid:
            return 'id', pid
    return '', ''


# ── REST 호출 (tea-meeting-agenda 와 같은 헤더·인증서·프록시 처리) ───────

def _auth_header(token: str) -> tuple[str, str]:
    username = _env('USERNAME')
    if username:  # Cloud — 이메일 + API 토큰
        return 'Authorization', 'Basic ' + base64.b64encode(f'{username}:{token}'.encode()).decode()
    scheme = os.environ.get('CONFLUENCE_AUTH_SCHEME', 'Bearer').strip()
    return _env('AUTH_HEADER') or 'Authorization', f'{scheme} {token}' if scheme else token


def _headers() -> dict:
    headers = {}
    if _env('DEP_TICKET'):  # 사내 게이트웨이 보안 정책 헤더 — 값이 있을 때만
        headers[_env('DEP_TICKET_HEADER') or 'X-Dep-Ticket'] = _env('DEP_TICKET')
    if _env('DATA_CLASSIFICATION'):
        headers[_env('DATA_CLASSIFICATION_HEADER') or 'X-Data-Classification'] = _env('DATA_CLASSIFICATION')
    name, value = _auth_header(_env('API_TOKEN'))
    headers[name] = value
    headers['Accept'] = 'application/json'  # 게이트웨이가 오류를 XML 로 주지 않게
    headers['User-Agent'] = _env('USER_AGENT') or 'curl/8.0.0'  # python-requests UA 는 WAF 가 끊는 일이 있었다
    return headers


def _ssl_verify():
    for key in ('CONFLUENCE_CA_BUNDLE', 'REQUESTS_CA_BUNDLE', 'SSL_CERT_FILE'):
        path = os.environ.get(key, '').strip()
        if path and Path(path).is_file():
            return path
        if path:
            log.warning('%s=%s 파일이 없어 기본 인증서로 시도합니다', key, path)
    return True


def _proxies():
    return {'http': None, 'https': None} if _env('NO_PROXY').lower() in ('1', 'true', 'yes', 'on') else None


class ConfluenceError(RuntimeError):
    """설정 없음·연결 실패·권한 없음 — 메시지는 화면에 그대로 보여 준다."""


class RateLimited(ConfluenceError):
    """호출 한도 — 잠시 뒤 다시 하면 된다 (백그라운드가 이어서 읽는다)."""


# ── 호출 한도 ─────────────────────────────────────────────────────────

INTERACTIVE_WAIT = 15  # 초 — 화면이 이보다 오래 기다리지 않게
BACKGROUND_WAIT = 15 * 60
_background = contextvars.ContextVar('confluence_background', default=False)


def rate_per_minute() -> int:
    try:
        return max(1, int(_env('RATE_PER_MINUTE', '15')))
    except ValueError:
        return 15


def _reserve() -> int:
    try:
        return max(0, min(rate_per_minute() - 1, int(_env('RATE_RESERVE', '3'))))
    except ValueError:
        return 3


@contextmanager
def background():
    """이 안의 호출은 백그라운드 몫 — 화면용 몫을 남기고, 자리가 날 때까지 기다린다."""
    token = _background.set(True)
    try:
        yield
    finally:
        _background.reset(token)


def _rate_file() -> str:
    return os.path.join(db.DATA_DIR, '.confluence_rate.json')


def _with_rate_state(fn):
    """잠근 상태 파일 {'calls': [시각…], 'blocked_until': 시각} 을 fn(state, now) 로 고치고 그 결과를 돌려준다."""
    os.makedirs(db.DATA_DIR, exist_ok=True)
    with open(_rate_file(), 'a+', encoding='utf-8') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            try:
                state = json.loads(f.read() or '{}')
            except ValueError:
                state = {}
            now = time.time()
            state['calls'] = [t for t in state.get('calls', []) if now - t < 60]
            result = fn(state, now)
            f.seek(0)
            f.truncate()
            f.write(json.dumps(state))
            f.flush()
            return result
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _acquire() -> None:
    """호출 한 번 자리를 잡는다 — 없으면 기다리고, 기다릴 수 있는 시간을 넘으면 RateLimited."""
    bg = _background.get()
    limit = rate_per_minute() - (_reserve() if bg else 0)
    deadline = time.time() + (BACKGROUND_WAIT if bg else INTERACTIVE_WAIT)

    def take(state, now):
        calls = state['calls']
        blocked = state.get('blocked_until', 0)
        if now >= blocked and len(calls) < limit:
            calls.append(now)
            return 0.0
        free_at = sorted(calls)[len(calls) - limit] + 60 if len(calls) >= limit else now
        return max(blocked, free_at) - now

    while True:
        wait = _with_rate_state(take)
        if wait <= 0:
            return
        if time.time() + wait > deadline:
            raise RateLimited(f'컨플루언스 호출 한도(분당 {rate_per_minute()}회) — 약 {int(wait) + 1}초 뒤 다시 하세요. '
                              '읽지 못한 링크는 백그라운드에서 이어서 읽습니다.')
        time.sleep(min(wait, 5) + 0.05)


def _block(seconds: float) -> None:
    def put(state, now):
        state['blocked_until'] = max(state.get('blocked_until', 0), now + seconds)
    _with_rate_state(put)


def calls_last_minute() -> int:
    return _with_rate_state(lambda state, now: len(state['calls']))


def _get(path: str, params: dict | None = None) -> dict:
    if not is_configured():
        raise ConfluenceError('컨플루언스가 설정되지 않았습니다 (.env 의 CONFLUENCE_URL·CONFLUENCE_API_TOKEN).')
    for attempt in range(2):
        _acquire()
        resp = _request(path, params)
        if resp.status_code != 429:
            break
        try:
            retry = float(resp.headers.get('Retry-After') or 60)
        except (TypeError, ValueError):
            retry = 60.0
        _block(retry)  # 다른 워커·스레드도 같이 멈춘다
        if attempt or not _background.get():
            raise RateLimited(f'컨플루언스 호출 한도 초과(429) — 약 {int(retry)}초 뒤 다시 하세요.')
    return _parse(resp)


def _request(path: str, params: dict | None):
    url = f"{_normalize_base_url(_env('URL'))}/rest/api/{path}"
    try:
        resp = requests.get(url, params=params, headers=_headers(), timeout=30, verify=_ssl_verify(),
                            proxies=_proxies())
    except requests.exceptions.SSLError as e:
        hint = (' — https 주소가 실제로는 http 만 쓰는 것 같습니다. CONFLUENCE_URL 의 스킴·포트를 확인하세요.'
                if 'WRONG_VERSION_NUMBER' in str(e) else ' — 사내 루트 인증서를 CONFLUENCE_CA_BUNDLE 로 지정하세요.')
        raise ConfluenceError(f'컨플루언스 연결 실패(SSL){hint}') from e
    except (requests.RequestException, OSError) as e:
        raise ConfluenceError(f'컨플루언스 연결 실패: {type(e).__name__} — 주소·프록시(CONFLUENCE_NO_PROXY)를 확인하세요.') from e
    return resp


def _parse(resp) -> dict:
    if resp.status_code == 401:
        raise ConfluenceError('컨플루언스 인증 실패(401) — CONFLUENCE_API_TOKEN 을 확인하세요.')
    if resp.status_code == 403:
        raise ConfluenceError('컨플루언스 접근 거부(403) — 페이지 권한 또는 게이트웨이 헤더(CONFLUENCE_DEP_TICKET)를 확인하세요.')
    if resp.status_code == 404:
        raise ConfluenceError('컨플루언스 페이지를 찾을 수 없습니다(404).')
    if resp.status_code >= 400:
        raise ConfluenceError(f'컨플루언스 API 오류 (HTTP {resp.status_code}): {resp.text[:200]}')
    try:
        return resp.json()
    except ValueError as e:
        raise ConfluenceError(f'컨플루언스 응답이 JSON 이 아닙니다: {resp.text[:200]}') from e


def storage_to_text(storage: str) -> str:
    """페이지 본문(storage XHTML) → 글. 표는 ' | ' 줄로, 코드 블록·다른 페이지 링크 제목은 글로 남긴다."""
    s = storage or ''
    s = re.sub(r'<!\[CDATA\[(.*?)\]\]>', lambda m: m.group(1).replace('&', '&amp;').replace('<', '&lt;'), s, flags=re.S)
    s = re.sub(r'<ri:page\b[^>]*ri:content-title="([^"]*)"[^>]*/?>', r' \1 ', s)
    s = re.sub(r'<ri:attachment\b[^>]*ri:filename="([^"]*)"[^>]*/?>', r' [첨부: \1] ', s)
    return mail_text.html_to_text(s)


def _fetch(url: str) -> dict:
    ref = page_ref(url)
    expand = 'body.storage,space,version'
    if ref[0] == 'id':
        data = _get(f'content/{ref[1]}', {'expand': expand})
    elif ref[0] == 'title':
        found = _get('content', {'spaceKey': ref[1], 'title': ref[2], 'expand': expand, 'limit': 1}).get('results') or []
        if not found:
            raise ConfluenceError(f"'{ref[2]}' 페이지를 찾을 수 없습니다.")
        data = found[0]
    else:
        raise ConfluenceError('주소에서 페이지를 알아보지 못했습니다 — /pages/숫자 · ?pageId=숫자 · /display/스페이스/제목 형태여야 합니다.')
    text = storage_to_text(((data.get('body') or {}).get('storage') or {}).get('value') or '')
    return {'page_id': str(data.get('id') or ''), 'title': data.get('title') or '', 'space': (data.get('space') or {}).get('key', ''),
            'version': (data.get('version') or {}).get('number') or 0,
            'text': text[:MAX_TEXT_CHARS] + ('\n…(이하 생략)' if len(text) > MAX_TEXT_CHARS else '')}


def _page_dict(p: ConfluencePage) -> dict:
    return {'url': p.url, 'page_id': p.page_id or '', 'title': p.title or '', 'space': p.space or '',
            'version': p.version or 0, 'text': p.text or '', 'error': p.error or '',
            'fetched_at': p.fetched_at.strftime('%Y-%m-%d %H:%M') if p.fetched_at else ''}


def read(url: str, force: bool = False) -> dict:
    """페이지 내용 {'url','page_id','title','space','version','text','error','fetched_at'}.
    CACHE_MINUTES 안에 읽은 적 있으면 저장된 것. 실패하면 ConfluenceError (실패도 남겨 화면에 보여 준다)."""
    url = (url or '').strip()
    with db.SessionLocal() as s:
        p = s.scalar(select(ConfluencePage).where(ConfluencePage.url == url))
        fresh = p is not None and p.fetched_at and not p.error and \
            datetime.now() - p.fetched_at < timedelta(minutes=CACHE_MINUTES)
        if fresh and not force:
            return _page_dict(p)
        try:
            data, error, failure = _fetch(url), '', None
        except ConfluenceError as e:
            data, error, failure = {}, str(e), e
        if p is None:
            p = ConfluencePage(url=url[:1000])
            s.add(p)
        if data:
            for k, v in data.items():
                setattr(p, k, v)
        p.error = error[:500]
        p.fetched_at = datetime.now().replace(microsecond=0)
        s.commit()
        if failure is not None:
            raise failure  # 호출 한도(RateLimited)인지 그대로 알 수 있게
        return _page_dict(p)


def text_for_ai(text: str) -> tuple[str, list[str]]:
    """글 속 컨플루언스 페이지를 읽어 AI 에게 함께 줄 덧붙임 글 → (덧붙임, 안내 [읽은 페이지·실패 이유])."""
    if not is_configured():
        return '', []
    parts, notes = [], []
    for url in find_urls(text)[:AI_PAGES]:
        try:
            p = read(url)
        except ConfluenceError as e:
            notes.append(f'{url} — {e}')
            continue
        body = p['text'][:AI_CHARS_PER_PAGE] + ('\n…(이하 생략)' if len(p['text']) > AI_CHARS_PER_PAGE else '')
        parts.append(f"[참고: 본문에 링크된 컨플루언스 페이지 '{p['title']}']\n{body}")
        notes.append(f"컨플루언스 '{p['title']}'")
    return ('\n\n' + '\n\n'.join(parts) if parts else ''), notes


# ── 참고자료 링크 ─────────────────────────────────────────────────────

SUMMARY_SYSTEM = """너는 임원실 직원이 참고자료로 붙인 컨플루언스 페이지를 요약하는 비서다.
무엇에 관한 문서인지와 핵심 내용(결론·결정·수치·일정·담당)을 한국어 3~5문장으로 쓴다. 문서에 없는 내용은 쓰지 않는다.
요약 글만 출력한다."""


def summarize_text(title: str, text: str) -> str:
    """페이지 내용 → AI 요약 (LLM 이 없으면 '')."""
    from services import llm
    if not llm.is_configured() or not (text or '').strip():
        return ''
    body = text[:llm.MAX_INPUT_CHARS]
    try:
        return llm.chat(SUMMARY_SYSTEM, f"페이지 제목: {title}\n\n본문:\n<<<\n{body}\n>>>").strip()[:3000]
    except llm.LLMError as e:
        return f'요약 실패: {str(e)[:200]}'


def refresh_reference(ref_id: int) -> dict:
    """컨플루언스 링크 참고자료의 내용을 (다시) 읽어 붙인다. 제목이 주소 그대로면 페이지 제목으로,
    주소가 API 주소면 브라우저로 여는 주소로 바꾸고, 내용이 바뀌었거나 요약이 없으면 AI 요약을 붙인다."""
    with db.SessionLocal() as s:
        r = s.get(Reference, ref_id)
        if r is None or r.kind != 'link' or not is_confluence_url(r.url):
            raise ConfluenceError('컨플루언스 링크가 아닙니다.')
        url = r.url
    limited = False
    try:
        p, error = read(url, force=True), ''
    except RateLimited as e:
        p, error, limited = None, str(e), True
    except ConfluenceError as e:
        p, error = None, str(e)
    with db.SessionLocal() as s:
        r = s.get(Reference, ref_id)
        if r is None:
            raise ConfluenceError('참고자료를 찾을 수 없습니다.')
        # 한도에 걸린 것은 '아직 안 읽음'으로 둬서 백그라운드(read_new_references)가 이어 읽는다
        r.content_at = None if limited else datetime.now().replace(microsecond=0)
        r.content_error = error[:500]
        if not limited:
            r.content_tries = (r.content_tries or 0) + 1 if error else 0
        if p:
            changed = p['text'] != (r.content or '') or not r.content_summary
            r.content = p['text']
            if p['title'] and (not r.title or r.title == r.url):
                r.title = p['title'][:300]
            r.url = display_url(r.url, p['page_id'])  # 사용자에게는 페이지 주소로
        s.commit()
    if p and changed:  # AI 요약은 DB 를 잡지 않은 채로 (몇 초 걸린다)
        summary = summarize_text(p['title'], p['text'])
        if summary:
            with db.SessionLocal() as s:
                r = s.get(Reference, ref_id)
                if r is not None:
                    r.content_summary = summary
                    s.commit()
    if error:
        raise (RateLimited if limited else ConfluenceError)(error)
    return p


def read_new_references(limit: int = 10) -> int:
    """아직 읽지 않은 컨플루언스 링크 참고자료를 읽는다 — 읽은 수. (기록·자료를 저장한 뒤, 쉬는 시간에)"""
    if not is_configured():
        return 0
    with db.SessionLocal() as s:
        rows = s.execute(select(Reference.id, Reference.url).where(Reference.kind == 'link', Reference.content_at.is_(None))
                         .order_by(Reference.id.desc()).limit(200)).all()
    n = 0
    for ref_id, url in rows:
        if n >= limit:
            break
        if is_confluence_url(url):
            try:
                refresh_reference(ref_id)
            except RateLimited:
                break  # 한도 — 다음 기회에
            except ConfluenceError:
                pass  # 실패도 content_at·content_error 로 남아 화면에서 다시 읽을 수 있다
            n += 1
    return n


RETRY_MAX = 6  # 이만큼 연달아 실패하면 쉬는 시간 재시도를 멈춘다 ('다시 읽기'는 언제든)
RETRY_BASE_MINUTES = 30  # 재시도 간격: 30분 → 1시간 → 2시간 … (실패할수록 두 배, 최대 하루)


def retry_wait(tries: int) -> timedelta:
    return timedelta(minutes=min(24 * 60, RETRY_BASE_MINUTES * 2 ** max(0, (tries or 1) - 1)))


def retry_failed_references(limit: int = 2) -> int:
    """읽지 못한 컨플루언스 참고자료를 간격을 늘려 가며 하나씩 다시 읽는다 — 시도한 수 (쉬는 시간에)."""
    if not is_configured():
        return 0
    now = datetime.now()
    with db.SessionLocal() as s:
        rows = s.execute(select(Reference.id, Reference.url, Reference.content_at, Reference.content_tries)
                         .where(Reference.kind == 'link', Reference.content_error != '',
                                Reference.content_error.is_not(None), Reference.content_at.is_not(None))
                         .order_by(Reference.content_at)).all()  # 오래 기다린 것부터
    n = 0
    for ref_id, url, at, tries in rows:
        if n >= limit:
            break
        if not is_confluence_url(url) or (tries or 0) >= RETRY_MAX or now - at < retry_wait(tries):
            continue
        try:
            refresh_reference(ref_id)
        except RateLimited:
            break
        except ConfluenceError:
            pass  # 실패 횟수가 늘어 다음 시도는 더 뒤에
        n += 1
    return n


def summarize_missing(limit: int = 3) -> int:
    """읽어 둔 내용은 있는데 AI 요약이 없는 컨플루언스 참고자료(요약 기능 전에 읽은 것 등)를 요약 — 컨플루언스는 부르지 않는다."""
    from services import llm
    if not llm.is_configured():
        return 0
    with db.SessionLocal() as s:
        rows = s.execute(select(Reference.id, Reference.title, Reference.content).where(
            Reference.kind == 'link', Reference.content != '', Reference.content.is_not(None),
            (Reference.content_summary == '') | Reference.content_summary.is_(None))
            .order_by(Reference.id.desc()).limit(limit)).all()
    for ref_id, title, text in rows:
        summary = summarize_text(title, text) or '요약 없음'
        with db.SessionLocal() as s:
            r = s.get(Reference, ref_id)
            if r is not None:
                r.content_summary = summary
                s.commit()
    return len(rows)


def _read_new_references_bg() -> int:
    with background():
        return read_new_references()


def read_new_references_later() -> None:
    """저장 화면이 기다리지 않게 백그라운드에서."""
    if is_configured():
        _executor.submit(_read_new_references_bg)


def main(argv: list[str]) -> int:
    """연결 확인 — python -m services.confluence <페이지 주소>"""
    from dotenv import load_dotenv
    load_dotenv()
    print('설정:', '됨' if is_configured() else '안 됨 (CONFLUENCE_URL·CONFLUENCE_API_TOKEN)')
    print('REST 기준 주소:', _normalize_base_url(_env('URL')) or '-')
    print('컨플루언스로 볼 주소:', ', '.join(prefixes()) or '-')
    print('SSL 검증:', _ssl_verify(), '| 프록시 무시:', bool(_proxies()))
    print('보낼 헤더:', ', '.join(sorted(_headers())))
    print(f'호출 한도: 분당 {rate_per_minute()}회 (백그라운드는 {_reserve()}회를 화면용으로 남김) · 최근 1분 호출 {calls_last_minute()}회')
    if not argv:
        return 0
    url = argv[0]
    print('주소 해석:', page_ref(url), '| 컨플루언스 주소로 인식:', is_confluence_url(url))
    try:
        p = _fetch(url)
    except ConfluenceError as e:
        print('실패:', e)
        return 1
    print(f"성공: '{p['title']}' (스페이스 {p['space']}, 버전 {p['version']}, {len(p['text'])}자)")
    print(p['text'][:500])
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
