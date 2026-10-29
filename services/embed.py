"""의미 검색 — 이력·메일 첨부 문서·참고자료를 임베딩(벡터)으로 바꿔 두고, 검색어와 뜻이 가까운 것을 찾는다.
글자가 똑같지 않아도("인사 발령" → "보직 변경 통보") 찾을 수 있게 키워드 검색을 보완한다.

임베딩 서버는 Researcher-board(People in SAIT)의 BGE-M3 서버(bge-embed, services/bge_server.py)를 같이 쓴다.
  POST {EMBED_BASE_URL}/api/embed  {"model": "bge-m3", "input": ["…", …]} → {"data": [{"index": 0, "embedding": […]}, …]}

환경변수
  EMBED_BASE_URL   예: http://bge-embed:7138 — 비우면 의미 검색 꺼짐 (키워드 검색은 그대로)
  EMBED_MODEL      기본 bge-m3
  EMBED_API_KEY    bge-embed 서버의 EMBED_API_KEY 와 같은 값 (Bearer)
  EMBED_TIMEOUT    초, 기본 120
  EMBED_MIN_SCORE  이 유사도(코사인, 0~1)보다 낮으면 결과에서 뺀다. 기본 0.45

벡터는 embeddings 표에 (종류, id) 마다 하나씩, 만들 때 쓴 글의 sha256 과 함께 둔다. 글이 바뀌면(AI 요약이
붙거나 고치면) 다시 만든다. 백그라운드(start)가 빠진 것부터 조금씩 채우고, 10분마다 바뀐 것을 찾는다.
"""
import fcntl
import hashlib
import logging
import os
import threading
import time
from datetime import datetime

import numpy as np
import requests
from sqlalchemy import delete, func, or_, select

from services import db
from services.db import Embedding, HistoryEntry, MailAttachment, Reference, Task

log = logging.getLogger(__name__)

KINDS = ('history', 'mail', 'ref')
MAX_CHARS = 3000          # 한 항목에서 임베딩할 글자 (앞부분 — 제목·요약·키워드가 먼저 온다)
MAX_ITEMS = 32            # 한 번에 보내는 개수
MAX_TOTAL_CHARS = 60000   # 한 번에 보내는 글자 (bge-embed 서버 한도 65536 안쪽)
SCAN_SECONDS = 600        # 바뀐 글 찾기 간격
AUTO_TYPES = ('상태변경', '연결', '자료', '할일')  # store.AUTO_ENTRY_TYPES — 시스템이 남긴 기록은 색인하지 않는다


class EmbedError(RuntimeError):
    """임베딩 서버 미설정·연결 실패·응답 오류. 메시지는 화면에 그대로 보여 준다."""


def _cfg() -> tuple[str, str, str, float]:
    return (os.environ.get('EMBED_BASE_URL', '').strip().rstrip('/'),
            os.environ.get('EMBED_MODEL', '').strip() or 'bge-m3',
            os.environ.get('EMBED_API_KEY', '').strip(),
            float(os.environ.get('EMBED_TIMEOUT', '') or 120))


def is_configured() -> bool:
    return bool(_cfg()[0])


def model_name() -> str:
    return _cfg()[1]


def min_score() -> float:
    try:
        return float(os.environ.get('EMBED_MIN_SCORE', '') or 0.45)
    except ValueError:
        return 0.45


def _chunks(texts: list[str]) -> list[list[str]]:
    out, cur, size = [], [], 0
    for t in texts:
        if cur and (len(cur) >= MAX_ITEMS or size + len(t) > MAX_TOTAL_CHARS):
            out.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += len(t)
    if cur:
        out.append(cur)
    return out


def embed(texts: list[str]) -> list[np.ndarray]:
    """글 목록 → 같은 순서의 단위 벡터(float32). 실패하면 EmbedError."""
    base, model, key, timeout = _cfg()
    if not base:
        raise EmbedError('임베딩 서버가 설정되지 않았습니다 (.env 의 EMBED_BASE_URL).')
    headers = {'Content-Type': 'application/json'}
    if key:
        try:
            key.encode('latin-1')
        except UnicodeEncodeError as e:
            raise EmbedError('EMBED_API_KEY 에 영문·숫자·기호가 아닌 글자가 있습니다.') from e
        headers['Authorization'] = f'Bearer {key}'
    texts = [(t or ' ')[:MAX_CHARS] for t in texts]
    out: list[np.ndarray] = []
    for chunk in _chunks(texts):
        try:
            resp = requests.post(f'{base}/api/embed', json={'model': model, 'input': chunk}, headers=headers,
                                 timeout=timeout)
        except requests.exceptions.ConnectionError as e:
            raise EmbedError(f'임베딩 서버에 연결할 수 없습니다 ({base}).') from e
        except requests.exceptions.Timeout as e:
            raise EmbedError('임베딩 서버 응답 시간 초과.') from e
        if resp.status_code != 200:
            raise EmbedError(f'임베딩 HTTP {resp.status_code}: {resp.text[:200]}')
        try:
            data = sorted(resp.json()['data'], key=lambda d: d.get('index', 0))
            vecs = [np.asarray(d['embedding'], dtype=np.float32) for d in data]
        except (KeyError, TypeError, ValueError) as e:
            raise EmbedError(f'임베딩 응답 형식 오류: {resp.text[:200]}') from e
        if len(vecs) != len(chunk):
            raise EmbedError(f'임베딩 개수가 맞지 않습니다 ({len(vecs)}/{len(chunk)}).')
        out += [v / (np.linalg.norm(v) or 1.0) for v in vecs]
    return out


# ── 무엇을 임베딩하나 ───────────────────────────────────────────────

def _join(*parts) -> str:
    return '\n'.join(p.strip() for p in parts if p and p.strip())[:MAX_CHARS]


def _history_text(h: HistoryEntry, task_title: str) -> str:
    return _join(h.title, h.summary, f"키워드: {h.keywords}" if h.keywords else '',
                 f"업무: {task_title}" if task_title else '', h.content)


def _mail_text(a: MailAttachment) -> str:
    return _join(a.filename, a.doc_type, a.summary, f"태그: {a.tags}" if a.tags else '', a.text)


def _ref_text(r: Reference, task_title: str) -> str:
    return _join(r.title, r.file_name if r.file_name != r.title else '', r.note, r.doc_type, r.summary,
                 f"태그: {r.tags}" if r.tags else '', r.content_summary,
                 f"업무: {task_title}" if task_title else '', r.text or r.content)


def _sources(s, kind: str, ids: list[int] | None = None):
    """(id, 글) — 색인할 항목. ids 를 주면 그 항목만."""
    if kind == 'history':
        q = (select(HistoryEntry, Task.title).outerjoin(Task, Task.id == HistoryEntry.task_id)
             .where(*_hist_cond()))
        if ids is not None:
            q = q.where(HistoryEntry.id.in_(ids))
        return [(h.id, _history_text(h, t or '')) for h, t in s.execute(q)]
    if kind == 'mail':  # 글자를 뽑았거나 요약한 문서만 (사진 등은 이름뿐이라 뺀다)
        q = select(MailAttachment).where(_mail_cond())
        if ids is not None:
            q = q.where(MailAttachment.id.in_(ids))
        return [(a.id, _mail_text(a)) for a in s.scalars(q)]
    q = select(Reference, Task.title).outerjoin(Task, Task.id == Reference.task_id)
    if ids is not None:
        q = q.where(Reference.id.in_(ids))
    return [(r.id, _ref_text(r, t or '')) for r, t in s.execute(q)]


def _hist_cond():
    return (HistoryEntry.entry_type.notin_(AUTO_TYPES),
            or_(HistoryEntry.ai_status.is_(None), HistoryEntry.ai_status != 'pending'))  # AI 정리가 끝난 뒤에


def _mail_cond():
    return or_(func.coalesce(MailAttachment.text, '') != '', func.coalesce(MailAttachment.summary, '') != '')


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _missing(s, kind: str, limit: int) -> list[int]:
    """아직 벡터가 없거나 다른 모델로 만든 항목 id — 최근 것부터."""
    have = select(Embedding.item_id).where(Embedding.kind == kind, Embedding.model == model_name())
    model = {'history': HistoryEntry, 'mail': MailAttachment, 'ref': Reference}[kind]
    q = select(model.id).where(model.id.notin_(have))
    if kind == 'history':
        q = q.where(*_hist_cond())
    elif kind == 'mail':
        q = q.where(_mail_cond())
    return list(s.scalars(q.order_by(model.id.desc()).limit(limit)))


_stale: dict[str, set[int]] = {k: set() for k in KINDS}
_last_scan = 0.0


def scan(now: float | None = None) -> int:
    """글이 바뀐 항목을 다시 만들 목록에 올리고, 지워진 항목의 벡터는 지운다. 올린 수."""
    global _last_scan
    _last_scan = time.time() if now is None else now
    n = 0
    with db.SessionLocal() as s:
        for kind in KINDS:
            saved = dict(s.execute(select(Embedding.item_id, Embedding.text_hash)
                                   .where(Embedding.kind == kind)).all())
            current = dict(_sources(s, kind))
            gone = [i for i in saved if i not in current]
            if gone:
                s.execute(delete(Embedding).where(Embedding.kind == kind, Embedding.item_id.in_(gone)))
            changed = {i for i, t in current.items() if i in saved and saved[i] != _hash(t)}
            n += len(changed - _stale[kind])
            _stale[kind] |= changed
        s.commit()
    return n


def index_pending(limit: int = MAX_ITEMS) -> int:
    """벡터가 없거나 글이 바뀐 항목을 limit 개까지 만든다. 만든 수 (EmbedError 는 그대로 올린다)."""
    with db.SessionLocal() as s:
        todo: list[tuple[str, int, str]] = []
        for kind in KINDS:
            room = limit - len(todo)
            if room <= 0:
                break
            ids = list(_stale[kind])[:room]
            ids += [i for i in _missing(s, kind, room - len(ids)) if i not in ids] if room > len(ids) else []
            todo += [(kind, i, t) for i, t in _sources(s, kind, ids)] if ids else []
            _stale[kind] -= set(ids)  # 사라졌거나 색인 대상이 아니게 된 것도 목록에서 뺀다
        if not todo:
            return 0
        vecs = embed([t for _, _, t in todo])
        model, now = model_name(), datetime.now().replace(microsecond=0)
        for (kind, i, t), v in zip(todo, vecs):
            row = s.scalar(select(Embedding).where(Embedding.kind == kind, Embedding.item_id == i))
            if row is None:
                row = Embedding(kind=kind, item_id=i)
                s.add(row)
            row.model, row.text_hash, row.dim, row.vector, row.updated_at = (
                model, _hash(t), int(v.shape[0]), v.astype(np.float32).tobytes(), now)
        s.commit()
    _matrix.clear()
    return len(todo)


def status() -> dict:
    """{'indexed': 벡터가 있는 수, 'total': 색인할 수} — 화면 표시용."""
    with db.SessionLocal() as s:
        indexed = s.scalar(select(func.count()).select_from(Embedding).where(Embedding.model == model_name())) or 0
        total = (s.scalar(select(func.count()).select_from(HistoryEntry).where(*_hist_cond())) or 0) + \
                (s.scalar(select(func.count()).select_from(MailAttachment).where(_mail_cond())) or 0) + \
                (s.scalar(select(func.count()).select_from(Reference)) or 0)
    return {'indexed': indexed, 'total': total}


# ── 찾기 ──────────────────────────────────────────────────────────

_matrix: dict[str, tuple] = {}   # kind → (판별 열쇠, id 배열, 행렬)
_queries: dict[str, np.ndarray] = {}


def _load(s, kind: str):
    model = model_name()
    key = s.execute(select(func.count(), func.max(Embedding.updated_at), func.max(Embedding.id))
                    .where(Embedding.kind == kind, Embedding.model == model)).one()
    key = tuple(key)
    cached = _matrix.get(kind)
    if cached and cached[0] == key:
        return cached[1], cached[2]
    rows = s.execute(select(Embedding.item_id, Embedding.vector).where(
        Embedding.kind == kind, Embedding.model == model)).all()
    vecs = [np.frombuffer(v, dtype=np.float32) for _, v in rows]
    dims = {v.shape[0] for v in vecs}
    if len(dims) > 1:  # 섞여 있으면 가장 많은 차원만
        dim = max(dims, key=lambda d: sum(1 for v in vecs if v.shape[0] == d))
        rows, vecs = zip(*[(r, v) for r, v in zip(rows, vecs) if v.shape[0] == dim]) if vecs else ([], [])
    ids = np.asarray([r[0] for r in rows], dtype=np.int64)
    mat = np.vstack(vecs) if len(vecs) else np.zeros((0, 1), dtype=np.float32)
    _matrix[kind] = (key, ids, mat)
    return ids, mat


def _query_vector(query: str) -> np.ndarray:
    q = query.strip()
    if q not in _queries:
        if len(_queries) > 200:
            _queries.clear()
        _queries[q] = embed([q])[0]
    return _queries[q]


def search(query: str, kinds=KINDS, limit: int = 30, threshold: float | None = None) -> list[dict]:
    """검색어와 뜻이 가까운 항목 — [{'kind', 'id', 'score'}] 높은 순. 설정이 없으면 []. 실패하면 EmbedError."""
    if not is_configured() or not (query or '').strip():
        return []
    qv = _query_vector(query)
    cut = min_score() if threshold is None else threshold
    out = []
    with db.SessionLocal() as s:
        for kind in kinds:
            ids, mat = _load(s, kind)
            if not len(ids) or mat.shape[1] != qv.shape[0]:
                continue
            scores = mat @ qv
            top = np.argsort(-scores)[:limit]
            out += [{'kind': kind, 'id': int(ids[i]), 'score': float(scores[i])} for i in top if scores[i] >= cut]
    return sorted(out, key=lambda d: -d['score'])[:limit * len(kinds)]


# ── 백그라운드 ──────────────────────────────────────────────────────

_started = False
_lock_holder: list = []


def step(now: float | None = None) -> bool:
    """한 번 돈다 — 무언가 만들었으면 True."""
    now = time.time() if now is None else now
    if now - _last_scan >= SCAN_SECONDS:
        scan(now)
    return index_pending() > 0


def start() -> bool:
    """색인 백그라운드를 시작한다(여러 워커 중 파일 잠금을 잡은 하나만). 시작했으면 True."""
    global _started
    if _started or not is_configured():
        return False
    try:
        os.makedirs(db.DATA_DIR, exist_ok=True)
        lock = open(os.path.join(db.DATA_DIR, '.embed_worker.lock'), 'w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    _started = True

    def loop():
        time.sleep(20)  # 앱이 다 뜬 뒤
        wait = 60
        while True:
            try:
                did = step()
                wait = 60
            except EmbedError as e:  # 서버가 꺼져 있으면 점점 덜 자주
                log.warning('의미 검색 색인 실패: %s', e)
                did, wait = False, min(wait * 2, 1800)
            except Exception:  # noqa: BLE001 — 백그라운드가 죽지 않게
                log.exception('의미 검색 색인 오류')
                did = False
            time.sleep(1 if did else wait)

    threading.Thread(target=loop, name='embed-worker', daemon=True).start()
    _lock_holder.append(lock)
    return True
