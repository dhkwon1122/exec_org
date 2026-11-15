"""연결로 넓히는 검색 (services/graph.py)."""
import pytest

from services import db, graph, store


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    yield


def test_expand_follows_links_to_tasks_people_keywords_and_other_records():
    budget = store.create_task('2027 예산 편성')
    sub = store.create_task('인건비 산정', parent_id=budget['id'])
    other = store.create_task('채용 계획')
    kim = store.create_person('김영수', position='부사장')
    h1 = store.add_history(budget['id'], '인건비 증액 논의', '회의', keywords='인건비, 증액', person_ids=[kim['id']])
    h2 = store.add_history(budget['id'], '인건비 재검토 요청', '요청', keywords='인건비, 재검토')
    # 검색어(인건비)는 없지만 연결된 기록들
    r1 = store.add_history(budget['id'], '본부별 배분안 공유', '메모', keywords='배분')
    r2 = store.add_history(other['id'], '김 부사장 면담 — 채용 일정', '동향', person_ids=[kim['id']])
    r3 = store.add_history(sub['id'], '산정 기준 확정', '메모', keywords='증액')
    store.add_history(other['id'], '관계없는 기록', '메모', keywords='기타')
    ref = store.add_reference(budget['id'], 'link', title='예산 지침', url='https://x', history_id=h1['id'])
    seeds = graph.seed_weights([h1['id'], h2['id']])
    g = graph.expand(seeds, terms=['인건비'])
    assert g['tasks'][0]['id'] == budget['id'] and g['tasks'][0]['hits'] == 2
    assert sub['id'] in {t['id'] for t in g['tasks']}  # 하위 업무로 퍼짐
    assert [p['name'] for p in g['people']] == ['김영수']
    assert '인건비' not in {k['keyword'] for k in g['keywords']}  # 검색어 자체는 빼고
    assert [r['id'] for r in g['refs']] == [ref['id']]
    related = {r['id']: r for r in g['related']}
    assert {r1['id'], r2['id'], r3['id']} <= set(related) and h1['id'] not in related
    assert '같은 업무: 2027 예산 편성' in related[r1['id']]['why']
    assert '같은 사람: 김영수' in related[r2['id']]['why']
    assert related[r1['id']]['score'] > related[r2['id']]['score']  # 두 씨앗이 걸친 업무 쪽이 위
    assert graph.expand({}) == {'tasks': [], 'people': [], 'keywords': [], 'refs': [], 'related': []}


def test_expand_respects_filters():
    t = store.create_task('업무')
    h = store.add_history(t['id'], '씨앗', '회의')
    store.add_history(t['id'], '옛 기록', '메모', occurred_at='2020-01-01')
    new = store.add_history(t['id'], '새 기록', '메모', occurred_at='2026-10-01')
    g = graph.expand(graph.seed_weights([h['id']]), filters={'date_from': '2026-01-01'})
    assert [r['id'] for r in g['related']] == [new['id']]
