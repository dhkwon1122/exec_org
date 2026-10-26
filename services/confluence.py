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
연결이 안 될 때: python -m services.confluence <페이지 주소>
"""
import base64
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
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
    for pat in (r'/pages/(\d+)(?:/|$|\?|#)', r'[?&]pageId=(\d+)'):
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


def _get(path: str, params: dict | None = None) -> dict:
    if not is_configured():
        raise ConfluenceError('컨플루언스가 설정되지 않았습니다 (.env 의 CONFLUENCE_URL·CONFLUENCE_API_TOKEN).')
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
            data, error = _fetch(url), ''
        except ConfluenceError as e:
            data, error = {}, str(e)
        if p is None:
            p = ConfluencePage(url=url[:1000])
            s.add(p)
        if data:
            for k, v in data.items():
                setattr(p, k, v)
        p.error = error[:500]
        p.fetched_at = datetime.now().replace(microsecond=0)
        s.commit()
        if error:
            raise ConfluenceError(error)
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

def refresh_reference(ref_id: int) -> dict:
    """컨플루언스 링크 참고자료의 내용을 (다시) 읽어 붙인다. 제목이 주소 그대로면 페이지 제목으로."""
    with db.SessionLocal() as s:
        r = s.get(Reference, ref_id)
        if r is None or r.kind != 'link' or not is_confluence_url(r.url):
            raise ConfluenceError('컨플루언스 링크가 아닙니다.')
        url = r.url
    try:
        p, error = read(url, force=True), ''
    except ConfluenceError as e:
        p, error = None, str(e)
    with db.SessionLocal() as s:
        r = s.get(Reference, ref_id)
        if r is None:
            raise ConfluenceError('참고자료를 찾을 수 없습니다.')
        r.content_at = datetime.now().replace(microsecond=0)
        r.content_error = error[:500]
        if p:
            r.content = p['text']
            if p['title'] and (not r.title or r.title == r.url):
                r.title = p['title'][:300]
        s.commit()
    if error:
        raise ConfluenceError(error)
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
            except ConfluenceError:
                pass  # 실패도 content_at·content_error 로 남아 화면에서 다시 읽을 수 있다
            n += 1
    return n


def read_new_references_later() -> None:
    """저장 화면이 기다리지 않게 백그라운드에서."""
    if is_configured():
        _executor.submit(read_new_references)


def main(argv: list[str]) -> int:
    """연결 확인 — python -m services.confluence <페이지 주소>"""
    from dotenv import load_dotenv
    load_dotenv()
    print('설정:', '됨' if is_configured() else '안 됨 (CONFLUENCE_URL·CONFLUENCE_API_TOKEN)')
    print('REST 기준 주소:', _normalize_base_url(_env('URL')) or '-')
    print('컨플루언스로 볼 주소:', ', '.join(prefixes()) or '-')
    print('SSL 검증:', _ssl_verify(), '| 프록시 무시:', bool(_proxies()))
    print('보낼 헤더:', ', '.join(sorted(_headers())))
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
