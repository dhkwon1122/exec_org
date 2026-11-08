"""의미 검색 — 글자가 달라도 뜻이 가까운 이력·문서·자료를 찾는다 (임베딩 서버는 가짜)."""
import json

import numpy as np
import pytest

from services import db, embed, store
from services.db import Embedding

# 가짜 임베딩: 낱말 → 뜻 축. 같은 축 낱말이 있으면 가깝다.
CONCEPTS = {'인사': 0, '발령': 0, '보직': 0, '전보': 0, '임명': 0,
            '출장': 1, '방문': 1, '해외': 1,
            '예산': 2, '집행': 2, '비용': 2}


def _vec(text: str) -> list[float]:
    v = [0.0] * 4
    for word, axis in CONCEPTS.items():
        if word in text:
            v[axis] += 1.0
    v[3] = 0.3  # 아무 뜻 없는 글도 0 벡터가 아니게
    return v


class FakeResp:
    def __init__(self, body, status=200):
        self.status_code, self._body, self.text = status, body, json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    monkeypatch.setattr(db, 'DATA_DIR', str(tmp_path))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    for k in ('LLM_API_URL', 'LLM2_API_URL', 'EMBED_MIN_SCORE', 'EMBED_MODEL'):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv('EMBED_BASE_URL', 'http://embed.local')
    monkeypatch.setenv('EMBED_API_KEY', 'k')
    monkeypatch.setattr(embed, '_stale', {k: set() for k in embed.KINDS})
    monkeypatch.setattr(embed, '_matrix', {})
    monkeypatch.setattr(embed, '_queries', {})
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({'url': url, 'input': json['input'], 'auth': headers.get('Authorization')})
        data = [{'index': i, 'embedding': _vec(t)} for i, t in enumerate(json['input'])]
        return FakeResp({'data': list(reversed(data))})  # 순서가 섞여 와도 index 로 맞춘다

    monkeypatch.setattr(embed.requests, 'post', fake_post)
    yield calls


def _index_all():
    n = 0
    while True:
        k = embed.index_pending()
        if not k:
            return n
        n += k


def test_finds_meaning_not_just_letters(fresh_db):
    t = store.create_task('조직 운영')
    a = store.add_history(t['id'], '김 상무 보직 변경 통보를 받음', title='보직 변경')
    b = store.add_history(t['id'], '다음 주 해외 방문 일정 조율')
    store.add_history(t['id'], '상태를 바꿈', entry_type='상태변경')  # 자동 기록은 색인하지 않음
    assert _index_all() == 2
    assert fresh_db[0]['url'] == 'http://embed.local/api/embed' and fresh_db[0]['auth'] == 'Bearer k'
    assert embed.status() == {'indexed': 2, 'total': 2}

    hits = embed.search('인사 발령')
    assert [(h['kind'], h['id']) for h in hits] == [('history', a['id'])]
    assert hits[0]['score'] > 0.9
    assert store.search_history('인사 발령') == []  # 키워드로는 못 찾는다
    assert [h['id'] for h in embed.search('출장')] == [b['id']]
    assert embed.search('점심 메뉴') == []  # 뜻이 먼 것은 기준 아래
    n = len(fresh_db)
    embed.search('인사 발령')
    assert len(fresh_db) == n  # 같은 검색어는 다시 묻지 않는다


def test_changed_text_is_reembedded_and_deleted_items_dropped(fresh_db):
    t = store.create_task('업무')
    h = store.add_history(t['id'], '회의 메모')
    _index_all()
    assert embed.search('예산') == []
    store.update_history(h['id'], content='내년 예산 집행 계획 회의')
    assert embed.scan() == 1 and _index_all() == 1
    assert [x['id'] for x in embed.search('비용')] == [h['id']]
    store.delete_history(h['id'])
    embed.scan()
    with db.SessionLocal() as s:
        assert s.query(Embedding).count() == 0
    assert embed.search('비용') == []


def test_documents_and_references_and_model_change(fresh_db, monkeypatch):
    from services import doc_versions  # noqa: F401 — 첨부 표 준비
    t = store.create_task('운영')
    r = store.add_reference(t['id'], 'note', title='비용 정리 메모', note='항목별 집행 현황')
    store.add_reference(t['id'], 'link', title='사내 포털', url='https://portal.example')
    assert _index_all() == 2
    hits = embed.search('예산')
    assert hits[0] == {'kind': 'ref', 'id': r['id'], 'score': hits[0]['score']}
    assert [h['kind'] for h in embed.search('예산', kinds=('history',))] == []

    monkeypatch.setenv('EMBED_MODEL', 'other-model')  # 모델을 바꾸면 모두 다시
    assert embed.status()['indexed'] == 0
    assert embed.search('예산') == []
    assert _index_all() == 2 and embed.search('예산')[0]['id'] == r['id']


def test_server_errors_and_off_switch(fresh_db, monkeypatch):
    store.add_history(store.create_task('x')['id'], '인사 발령')
    monkeypatch.setattr(embed.requests, 'post', lambda *a, **k: FakeResp({'detail': 'unauthorized'}, 401))
    with pytest.raises(embed.EmbedError, match='HTTP 401'):
        embed.index_pending()
    monkeypatch.delenv('EMBED_BASE_URL')
    assert embed.is_configured() is False and embed.search('인사') == [] and embed.start() is False


def test_requests_are_split_by_count_and_size():
    texts = ['가' * 2000] * 40
    chunks = embed._chunks(texts)
    assert all(len(c) <= embed.MAX_ITEMS and sum(map(len, c)) <= embed.MAX_TOTAL_CHARS for c in chunks)
    assert sum(len(c) for c in chunks) == 40


def test_search_page_adds_similar_records(fresh_db, monkeypatch):
    monkeypatch.setattr(embed, '_started', True)  # 앱을 불러와도 색인 스레드는 띄우지 않게
    import app  # noqa: F401 — 페이지 등록
    from pages import search as page
    t = store.create_task('조직 운영')
    store.add_history(t['id'], '보직 변경 통보', title='보직 변경')
    store.add_history(t['id'], '인사 발령 회의 메모')
    _index_all()
    out, label, _more, _style, similar = page.search('인사 발령', None, None, None, None, None, False, True)
    assert label == '1건 · 뜻이 비슷한 이력 1건'
    text = json.dumps([out, similar], default=lambda o: o.to_plurality_json() if hasattr(o, 'to_plurality_json') else str(o),
                      ensure_ascii=False)
    assert '보직 변경' in text and '비슷함' in text
    out, label, *_ = page.search('인사 발령', None, None, None, None, None, False, False)
    assert label == '1건'


def test_summary_shows_whether_semantic_search_is_on(fresh_db, monkeypatch, tmp_path):
    store.add_history(store.create_task('x')['id'], '인사 발령')
    st = embed.summary()
    assert st['configured'] and st['indexed'] == 0 and st['total'] == 1 and st['ok'] is None
    _index_all()
    embed._save_state(True)
    st = embed.summary()
    assert st['indexed'] == st['total'] == 1 and st['ok'] is True and st['model'] == 'bge-m3'
    embed._save_state(False, '임베딩 서버에 연결할 수 없습니다')
    assert embed.summary()['ok'] is False and '연결' in embed.summary()['error']
    monkeypatch.delenv('EMBED_BASE_URL')
    assert embed.summary() == {'configured': False}


def _page_text(out) -> str:
    return json.dumps(out, default=lambda o: o.to_plurality_json() if hasattr(o, 'to_plurality_json') else str(o),
                      ensure_ascii=False)


def test_references_similar_documents_and_notes(fresh_db):
    from services import documents
    t = store.create_task('조직 운영')
    store.add_reference(t['id'], 'file', file_name='해외 방문 일정.docx', file_bytes=b'a')
    note = store.add_reference(t['id'], 'note', title='비용 정리 메모', note='항목별 현황')
    kw = store.add_reference(t['id'], 'note', title='출장 준비물', note='여권')
    _index_all()
    documents.refresh_cache(full=True)

    docs, refs = documents.similar('출장')  # 글자는 달라도 뜻이 가까운 문서와, 키워드로도 걸리는 메모
    assert [d['base'] for d in docs] == ['해외 방문 일정'] and docs[0]['score'] > 0.9
    assert [r['id'] for r in refs] == [kw['id']]
    docs, refs = documents.similar('출장', skip_keys={docs[0]['key']}, skip_refs={kw['id']})
    assert docs == [] and refs == []  # 키워드로 이미 보여 준 것은 빼기
    docs, refs = documents.similar('예산')
    assert docs == [] and [(r['id'], r['task_title']) for r in refs] == [(note['id'], '조직 운영')]
    assert documents.similar('예산', kind='file') == ([], [])  # 유형 고르기를 따른다
    assert documents.similar('출장', kind='link') == ([], [])
    assert documents.similar('출장', kind='mail') == ([], [])  # 메일로 받은 문서만 — 올린 파일은 빠짐


def test_references_page_adds_similar_section(fresh_db, monkeypatch):
    monkeypatch.setattr(embed, '_started', True)
    import app  # noqa: F401 — 페이지 등록
    from types import SimpleNamespace
    from pages import references as page
    monkeypatch.setattr(page, 'ctx', SimpleNamespace(triggered_id='rq'))  # 검색창에서 부른 것처럼
    t = store.create_task('조직 운영')
    store.add_reference(t['id'], 'file', file_name='해외 방문 일정.docx', file_bytes=b'a')
    store.add_reference(t['id'], 'note', title='비용 정리 메모', note='항목별 현황')
    _index_all()
    out = page.search('예산', '')
    assert out[0] is None and '뜻이 비슷한 자료' in _page_text(out[5]) and '비용 정리 메모' in _page_text(out[5])
    out = page.search('출장', '')
    assert '해외 방문 일정' in _page_text(out[5]) and '비슷함' in _page_text(out[5])
    assert page.search('', '')[5] is None  # 검색어가 없으면 의미 검색 안 함
    out = page.search('점심', '')
    assert out[5] is None and '참고자료가 없습니다' in _page_text(out[0])

    monkeypatch.setattr(embed.requests, 'post', lambda *a, **k: FakeResp({'detail': 'down'}, 500))
    monkeypatch.setattr(embed, '_queries', {})
    out = page.search('인사', '')
    assert '의미 검색을 하지 못했습니다' in _page_text(out[5]) and '참고자료가 없습니다' in _page_text(out[0])
    monkeypatch.delenv('EMBED_BASE_URL')
    assert page.search('예산', '')[5] is None
