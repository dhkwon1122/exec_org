"""컨플루언스 워치 — 자주 보는 공간·페이지를 등록해 두면 바뀐 페이지를 모아 알려 주고 무엇이 바뀌었는지 요약한다.

흐름: 워치(공간 키 또는 페이지 주소 [+ 하위 페이지]) → CONFLUENCE_WATCH_MINUTES 마다(또는 '지금 확인')
CQL 로 최근 수정된 페이지를 찾음(content/search) → 페이지 판(version)마다 한 건 — 이 판 본문과 바로 앞 판 본문
(?status=historical&version=N-1)을 비교한 바뀐 줄 → AI 가 '무엇이 바뀌었나' 요약(LLM 없으면 바뀐 줄 수만).
안 본 업데이트 수는 사이드바 '컨플루언스' 옆에, 여러 건은 AI 로 묶어 요약하거나 메일로 보낸다.
연결 설정은 services/confluence.py 와 같다.
"""
import difflib
import fcntl
import json
import logging
import os
import re
import threading
import time
from datetime import datetime

from sqlalchemy import func, select

from services import confluence, db, llm
from services.confluence import ConfluenceError
from services.db import ConfluenceUpdate, ConfluenceWatch

log = logging.getLogger(__name__)

MAX_PAGES_PER_CHECK = 50
MAX_AI_PER_CHECK = 20
MAX_DIFF_CHARS = 5000
_started = False
lock_holder: list = []


def interval_minutes() -> int:
    try:
        return max(0, int(os.environ.get('CONFLUENCE_WATCH_MINUTES', '60')))
    except ValueError:
        return 60


def first_days() -> int:
    """새 워치를 처음 확인할 때 며칠 전 수정까지 가져올지."""
    try:
        return max(1, int(os.environ.get('CONFLUENCE_WATCH_FIRST_DAYS', '3')))
    except ValueError:
        return 3


def page_link(page_id: str) -> str:
    base = confluence._env('BROWSER_BASE_URL') or confluence._normalize_base_url(confluence._env('URL'))
    return f"{base.rstrip('/')}/pages/viewpage.action?pageId={page_id}" if base and page_id else ''


# ── 워치 ──────────────────────────────────────────────────────────────

_SPACE_KEY = re.compile(r'^[A-Za-z0-9_~\-]{1,100}$')


def parse_target(target: str) -> dict:
    """'EXEC'(공간 키) · 공간 주소(?spaceKey= · /display/KEY · /spaces/KEY) · 페이지 주소 → {'kind', 'space_key'|'page_id'|'title'}."""
    t = (target or '').strip()
    if not t:
        raise ValueError('공간 키나 페이지 주소를 넣으세요.')
    if _SPACE_KEY.match(t) and not t.isdigit():
        return {'kind': 'space', 'space_key': t}
    ref = confluence.page_ref(t)
    if ref[0] == 'id':
        return {'kind': 'page', 'page_id': ref[1]}
    if ref[0] == 'title':
        return {'kind': 'page', 'space_key': ref[1], 'title': ref[2]}
    m = re.search(r'[?&]spaceKey=([^&#]+)', t) or re.search(r'/display/([^/?#]+)/?(?:[?#]|$)', t) or \
        re.search(r'/spaces/([^/?#]+)(?:/overview|/pages/?)?/?(?:[?#]|$)', t)
    if m:
        return {'kind': 'space', 'space_key': m.group(1)}
    raise ValueError('공간 키(예: EXEC)나 컨플루언스 공간·페이지 주소를 넣으세요.')


def _watch_dict(w: ConfluenceWatch, unseen: int = 0) -> dict:
    return {'id': w.id, 'kind': w.kind, 'space_key': w.space_key or '', 'page_id': w.page_id or '',
            'children': bool(w.children), 'title': w.title or '', 'url': w.url or '', 'active': bool(w.active),
            'last_checked_at': w.last_checked_at.strftime('%Y-%m-%d %H:%M') if w.last_checked_at else '',
            'last_error': w.last_error or '', 'unseen': unseen,
            'label': (f"공간 {w.space_key}" if w.kind == 'space' else
                      f"페이지{' + 하위' if w.children else ''}") + (f" · {w.title}" if w.title else '')}


def add_watch(target: str, children: bool = True, by: str = '') -> dict:
    """워치 추가 — 컨플루언스에서 공간·페이지를 확인해 이름을 붙인다."""
    t = parse_target(target)
    if t['kind'] == 'space':
        data = confluence._get(f"space/{t['space_key']}")
        key, pid, title = data.get('key') or t['space_key'], '', data.get('name') or ''
    else:
        if 'page_id' in t:
            data = confluence._get(f"content/{t['page_id']}", {'expand': 'space'})
        else:
            found = confluence._get('content', {'spaceKey': t['space_key'], 'title': t['title'], 'expand': 'space',
                                                'limit': 1}).get('results') or []
            if not found:
                raise ConfluenceError(f"'{t['title']}' 페이지를 찾을 수 없습니다.")
            data = found[0]
        key, pid, title = (data.get('space') or {}).get('key', ''), str(data.get('id') or ''), data.get('title') or ''
    with db.SessionLocal() as s:
        same = s.scalar(select(ConfluenceWatch).where(ConfluenceWatch.kind == t['kind'],
                                                      ConfluenceWatch.space_key == key if t['kind'] == 'space'
                                                      else ConfluenceWatch.page_id == pid))
        if same is not None:
            raise ValueError(f"이미 지켜보고 있습니다: {same.title or same.space_key}")
        w = ConfluenceWatch(kind=t['kind'], space_key=key, page_id=pid, children=bool(children) and t['kind'] == 'page',
                            title=title[:500], url=(target or '').strip()[:1000], created_by=by)
        s.add(w)
        s.commit()
        return _watch_dict(w)


def list_watches() -> list[dict]:
    with db.SessionLocal() as s:
        unseen = dict(s.execute(select(ConfluenceUpdate.watch_id, func.count()).where(ConfluenceUpdate.seen.is_(False))
                                .group_by(ConfluenceUpdate.watch_id)).all())
        return [_watch_dict(w, unseen.get(w.id, 0)) for w in s.scalars(select(ConfluenceWatch).order_by(ConfluenceWatch.id))]


def delete_watch(watch_id: int) -> None:
    with db.SessionLocal() as s:
        w = s.get(ConfluenceWatch, watch_id)
        if w:
            s.delete(w)
            s.commit()


def set_active(watch_id: int, active: bool) -> None:
    with db.SessionLocal() as s:
        w = s.get(ConfluenceWatch, watch_id)
        if w:
            w.active = bool(active)
            s.commit()


# ── 확인 ──────────────────────────────────────────────────────────────

def _cql(w: ConfluenceWatch, days: int) -> str:
    if w.kind == 'space':
        where = f'space = "{w.space_key}" and type in (page, blogpost)'
    elif w.children:
        where = f'(id = {w.page_id} or ancestor = {w.page_id})'
    else:
        where = f'id = {w.page_id}'
    return f'{where} and lastmodified >= now("-{days}d") order by lastmodified desc'


def _days_since(w: ConfluenceWatch) -> int:
    if not w.last_checked_at:
        return first_days()
    return min(30, max(1, (datetime.now() - w.last_checked_at).days + 1))  # 하루 겹치게 — 같은 판은 한 번만 남는다


def _body(page_id: str, version: int | None = None) -> str:
    params = {'expand': 'body.storage'}
    if version is not None:
        params.update({'status': 'historical', 'version': version})
    data = confluence._get(f'content/{page_id}', params)
    return confluence.storage_to_text(((data.get('body') or {}).get('storage') or {}).get('value') or '')


def _diff(old: str, new: str) -> str:
    out = [f'{line[0]} {line[1:]}' for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm='', n=0)
           if not line.startswith(('---', '+++', '@@')) and line[1:].strip()]
    text = '\n'.join(out)
    return text[:MAX_DIFF_CHARS] + ('\n…(이하 생략)' if len(text) > MAX_DIFF_CHARS else '')


SUMMARY_SYSTEM = """너는 임원실 직원이 지켜보는 컨플루언스 페이지의 변경을 알려 주는 비서다.
페이지가 어떻게 바뀌었는지 핵심만 한국어로 짧게(1~3문장) 쓴다. 숫자·일정·결정·담당 변경을 먼저.
글자 고침·서식처럼 사소한 변경이면 '사소한 수정'이라고만 쓴다. 바뀐 줄에 없는 내용은 지어내지 않는다.
반드시 JSON 만 출력: {"summary": "", "important": true|false}"""


def _summarize(u: ConfluenceUpdate, prev_text: str | None) -> None:
    if not llm.is_configured():
        u.summary_status = 'none'
        return
    if prev_text is None:
        what = f"새 페이지(또는 앞 판을 못 읽음) — 본문:\n<<<\n{(u.text or '')[:6000]}\n>>>"
    elif not u.diff:
        u.summary, u.summary_status = '본문 글은 그대로 — 서식·첨부만 바뀐 것 같습니다.', 'done'
        return
    else:
        what = f"바뀐 줄 (- 앞 판, + 이번 판):\n<<<\n{u.diff}\n>>>"
    try:
        data = llm._extract_json(llm.chat(SUMMARY_SYSTEM, f"페이지: {u.title} (공간 {u.space}, {u.version}판)\n"
                                                          f"수정한 사람: {u.modified_by} · 수정 메모: {u.message or '-'}\n\n{what}"))
        u.summary = ('[중요] ' if data.get('important') else '') + str(data.get('summary') or '').strip()[:1000]
        u.summary_status = 'done'
    except llm.LLMError as e:
        u.summary, u.summary_status = f'요약 실패: {str(e)[:200]}', 'failed'


def check_watch(watch_id: int) -> int:
    """워치 하나 확인 — 새로 찾은 업데이트 수. 실패는 워치에 남기고 ConfluenceError."""
    with db.SessionLocal() as s:
        w = s.get(ConfluenceWatch, watch_id)
        if w is None:
            return 0
        try:
            results = confluence._get('content/search', {'cql': _cql(w, _days_since(w)), 'limit': MAX_PAGES_PER_CHECK,
                                                         'expand': 'version,space'}).get('results') or []
        except ConfluenceError as e:
            w.last_error, w.last_checked_at = str(e)[:500], datetime.now().replace(microsecond=0)
            s.commit()
            raise
        new, ai_left = 0, MAX_AI_PER_CHECK
        for r in reversed(results):  # 오래된 수정부터 — 같은 페이지의 앞 판이 먼저 들어가 다음 판과 비교된다
            pid, ver = str(r.get('id') or ''), int((r.get('version') or {}).get('number') or 1)
            if not pid or s.scalar(select(ConfluenceUpdate.id).where(ConfluenceUpdate.watch_id == w.id,
                                                                     ConfluenceUpdate.page_id == pid,
                                                                     ConfluenceUpdate.version == ver)):
                continue
            v = r.get('version') or {}
            u = ConfluenceUpdate(watch_id=w.id, page_id=pid, title=(r.get('title') or '')[:500],
                                 space=(r.get('space') or {}).get('key', ''), version=ver,
                                 modified_at=str(v.get('when') or '')[:30],
                                 modified_by=str((v.get('by') or {}).get('displayName') or '')[:200],
                                 message=str(v.get('message') or '')[:500])
            prev_text = None
            try:
                u.text = _body(pid)[:confluence.MAX_TEXT_CHARS]
                if ver > 1:
                    before = s.scalar(select(ConfluenceUpdate.text).where(ConfluenceUpdate.page_id == pid,
                                                                          ConfluenceUpdate.version == ver - 1))
                    prev_text = before if before is not None else _body(pid, ver - 1)
                    u.diff = _diff(prev_text, u.text)
            except ConfluenceError as e:
                u.summary, u.summary_status = f'본문을 읽지 못함: {e}', 'failed'
            if not u.summary_status and ai_left > 0:  # 한 번에 너무 많으면 남은 것은 다음 확인 때 요약
                _summarize(u, prev_text)
                ai_left -= 1
            s.add(u)
            s.flush()
            new += 1
        for u in s.scalars(select(ConfluenceUpdate).where(ConfluenceUpdate.watch_id == w.id,
                                                          ConfluenceUpdate.summary_status == '')
                           .order_by(ConfluenceUpdate.id).limit(max(0, ai_left))):  # 지난번에 못 한 요약
            before = s.scalar(select(ConfluenceUpdate.text).where(ConfluenceUpdate.page_id == u.page_id,
                                                                  ConfluenceUpdate.version == u.version - 1))
            _summarize(u, None if u.version == 1 else before if before is not None else ('' if u.diff else None))
        w.last_error, w.last_checked_at = '', datetime.now().replace(microsecond=0)
        s.commit()
        return new


def check_all() -> dict:
    """켜 둔 워치를 모두 확인 — {'new': 새 업데이트 수, 'errors': [(워치 이름, 이유)]}."""
    new, errors = 0, []
    for w in list_watches():
        if not w['active']:
            continue
        try:
            new += check_watch(w['id'])
        except ConfluenceError as e:
            errors.append((w['title'] or w['space_key'], str(e)))
    return {'new': new, 'errors': errors}


# ── 업데이트 ──────────────────────────────────────────────────────────

def _update_dict(u: ConfluenceUpdate, watch_title: str = '') -> dict:
    return {'id': u.id, 'watch_id': u.watch_id, 'watch_title': watch_title, 'page_id': u.page_id, 'title': u.title or '',
            'space': u.space or '', 'version': u.version or 1, 'modified_at': (u.modified_at or '')[:16].replace('T', ' '),
            'modified_by': u.modified_by or '', 'message': u.message or '', 'diff': u.diff or '',
            'summary': u.summary or '', 'summary_status': u.summary_status or '', 'seen': bool(u.seen),
            'link': page_link(u.page_id),
            'added': sum(1 for line in (u.diff or '').splitlines() if line.startswith('+ ')),
            'removed': sum(1 for line in (u.diff or '').splitlines() if line.startswith('- '))}


def list_updates(watch_id: int | None = None, unseen_only: bool = False, limit: int = 200) -> list[dict]:
    with db.SessionLocal() as s:
        stmt = select(ConfluenceUpdate, ConfluenceWatch.title).join(ConfluenceWatch,
                                                                     ConfluenceWatch.id == ConfluenceUpdate.watch_id)
        if watch_id:
            stmt = stmt.where(ConfluenceUpdate.watch_id == watch_id)
        if unseen_only:
            stmt = stmt.where(ConfluenceUpdate.seen.is_(False))
        rows = s.execute(stmt.order_by(ConfluenceUpdate.modified_at.desc(), ConfluenceUpdate.id.desc()).limit(limit))
        return [_update_dict(u, t or '') for u, t in rows]


def unseen_count() -> int:
    with db.SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(ConfluenceUpdate).where(ConfluenceUpdate.seen.is_(False))) or 0


def mark_seen(ids: list[int] | None = None) -> int:
    """고른 업데이트(없으면 안 본 것 전부)를 확인함으로."""
    with db.SessionLocal() as s:
        stmt = select(ConfluenceUpdate).where(ConfluenceUpdate.seen.is_(False))
        if ids is not None:
            stmt = stmt.where(ConfluenceUpdate.id.in_([int(i) for i in ids]))
        rows = list(s.scalars(stmt))
        for u in rows:
            u.seen = True
        s.commit()
        return len(rows)


DIGEST_SYSTEM = """너는 임원실 직원에게 컨플루언스 변경 소식을 정리해 주는 비서다. 여러 페이지의 변경 요약을 읽고
꼭 알아야 할 것부터 짧은 브리핑으로 쓴다: 1) 주요 변경 3~5개(무엇이 어떻게 — 페이지 이름 포함) 2) 그 밖의 변경 한 줄.
요약에 없는 내용은 지어내지 않는다. 일반 글(마크다운 목록 가능)로 쓴다."""


def digest(update_ids: list[int]) -> str:
    """여러 업데이트를 묶어 AI 브리핑. LLM 이 없으면 목록 글."""
    with db.SessionLocal() as s:
        rows = list(s.scalars(select(ConfluenceUpdate).where(ConfluenceUpdate.id.in_([int(i) for i in update_ids]))
                              .order_by(ConfluenceUpdate.modified_at)))
    lines = [f"- {u.title} ({u.space}, {u.version}판, {u.modified_by}, {(u.modified_at or '')[:10]}): "
             f"{u.summary or (f'바뀐 줄 {len((u.diff or '').splitlines())}줄' if u.diff else '새 페이지')}" for u in rows]
    if not lines:
        raise ValueError('요약할 업데이트가 없습니다.')
    if not llm.is_configured():
        return '\n'.join(lines)
    return llm.chat(DIGEST_SYSTEM, '변경 목록:\n' + '\n'.join(lines)[:llm.MAX_INPUT_CHARS]).strip()


# ── 주기 확인 ─────────────────────────────────────────────────────────

_STATUS_FILE = 'confluence_watch_status.json'


def _status_path() -> str:
    return os.path.join(db.DATA_DIR, '.' + _STATUS_FILE)


def last_run() -> dict:
    try:
        with open(_status_path(), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def run_once() -> dict:
    at = datetime.now().strftime('%Y-%m-%d %H:%M')
    try:
        r = check_all()
        st = {'at': at, 'ok': not r['errors'], 'new': r['new'], 'errors': [f'{a}: {b}' for a, b in r['errors']][:5]}
    except Exception as e:  # noqa: BLE001 — 주기 실행이 죽지 않게
        log.exception('컨플루언스 워치 확인 실패')
        st = {'at': at, 'ok': False, 'new': 0, 'errors': [f'{type(e).__name__}: {str(e)[:200]}']}
    try:
        os.makedirs(db.DATA_DIR, exist_ok=True)
        with open(_status_path(), 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False)
    except OSError:
        pass
    return st


def start() -> bool:
    """CONFLUENCE_WATCH_MINUTES 마다 확인 (컨플루언스가 설정돼 있을 때, 여러 워커 중 하나만)."""
    global _started
    if _started or not interval_minutes() or not confluence.is_configured():
        return False
    try:
        os.makedirs(db.DATA_DIR, exist_ok=True)
        lock = open(os.path.join(db.DATA_DIR, '.confluence_watch.lock'), 'w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    _started = True

    def loop():
        time.sleep(60)
        while True:
            if list_watches():
                run_once()
            time.sleep(interval_minutes() * 60)

    threading.Thread(target=loop, name='confluence-watch', daemon=True).start()
    lock_holder.append(lock)
    return True
