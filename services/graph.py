"""연결로 넓히는 검색 — 이 앱의 데이터를 지식 그래프로 보고, 검색으로 찾은 기록에서 연결을 따라간다.

노드: 기록(history) · 업무(tasks) · 사람(people — 임원·리더) · 키워드(기록의 키워드) · 자료(references_)
연결(이미 DB 에 있는 것만 — AI 호출 없음):
  기록 ─ 업무        history.task_id, history_tasks(여러 업무에 걸친 기록)
  기록 ─ 사람        history_people
  기록 ─ 키워드      history.keywords
  기록 ─ 자료        references_.history_id, history_references
  업무 ─ 업무        상위·하위(parent_id), 연결 업무(task_links)

expand(seeds) 순서
  1) 씨앗 = 검색으로 찾은 기록 (앞쪽일수록 무게가 크다 — 키워드 검색 순위·의미 검색 점수)
  2) 한 걸음: 씨앗에 걸린 업무·사람·키워드·자료마다 걸린 씨앗들의 무게를 더해 점수 (많은 결과가 가리킬수록 위)
     업무는 상위·하위·연결 업무에도 점수의 절반을 나눠 준다
  3) 두 걸음: 위쪽 업무·사람·키워드에 걸린 다른 기록(씨앗이 아닌 것) — 걸친 노드 점수의 합으로 순위, 이유를 붙인다
     ('같은 업무: …', '같은 사람: …', '키워드: …')
"""
from collections import defaultdict

from sqlalchemy import or_, select

from services import db, store
from services.db import (HistoryEntry, HistoryPerson, HistoryReference, HistoryTask, Person, Reference, Task,
                         TaskLink)


def seed_weights(keyword_ids: list[int], semantic: list[tuple[int, float]] | None = None) -> dict[int, float]:
    """씨앗 무게 — 키워드 검색 i 번째 1/(1+i/10), 의미 검색은 점수×0.8. 같은 기록이면 큰 쪽."""
    w: dict[int, float] = {}
    for i, hid in enumerate(keyword_ids):
        w[hid] = max(w.get(hid, 0), 1 / (1 + i / 10))
    for hid, score in semantic or []:
        w[hid] = max(w.get(hid, 0), max(float(score), 0) * 0.8)
    return w


def _neighbors(s, hids: list[int]) -> dict[int, dict[str, set]]:
    """기록 → {'task': {id}, 'person': {id}, 'keyword': {글자}, 'ref': {id}}."""
    out: dict[int, dict[str, set]] = {h: {'task': set(), 'person': set(), 'keyword': set(), 'ref': set()} for h in hids}
    if not hids:
        return out
    for hid, tid, kws in s.execute(select(HistoryEntry.id, HistoryEntry.task_id, HistoryEntry.keywords)
                                   .where(HistoryEntry.id.in_(hids))):
        if tid:
            out[hid]['task'].add(tid)
        out[hid]['keyword'].update(k.lower() for k in store._tags(kws or ''))
    for hid, tid in s.execute(select(HistoryTask.history_id, HistoryTask.task_id).where(HistoryTask.history_id.in_(hids))):
        out[hid]['task'].add(tid)
    for hid, pid in s.execute(select(HistoryPerson.history_id, HistoryPerson.person_id)
                              .where(HistoryPerson.history_id.in_(hids))):
        out[hid]['person'].add(pid)
    for hid, rid in s.execute(select(Reference.history_id, Reference.id).where(Reference.history_id.in_(hids))):
        out[hid]['ref'].add(rid)
    for hid, rid in s.execute(select(HistoryReference.history_id, HistoryReference.reference_id)
                              .where(HistoryReference.history_id.in_(hids))):
        out[hid]['ref'].add(rid)
    return out


def expand(seeds: dict[int, float], terms: list[str] | None = None, filters: dict | None = None,
           top: int = 6, related_limit: int = 10) -> dict:
    """씨앗 기록에서 연결을 따라간다 →
    {'tasks': [{id,title,status,score,hits}], 'people': [{id,name,position,role,score,hits}],
     'keywords': [{keyword,score,hits}], 'refs': [{id,title,kind,task_id,score}],
     'related': [기록 dict + {'score','why': [이유]}]}  (hits = 걸린 씨앗 수)"""
    terms_l = {t.lower() for t in terms or []}
    out = {'tasks': [], 'people': [], 'keywords': [], 'refs': [], 'related': []}
    if not seeds:
        return out
    with db.SessionLocal() as s:
        nb = _neighbors(s, list(seeds))
        score: dict[str, dict] = {k: defaultdict(float) for k in ('task', 'person', 'keyword', 'ref')}
        hits: dict[str, dict] = {k: defaultdict(int) for k in ('task', 'person', 'keyword', 'ref')}
        for hid, w in seeds.items():
            for kind, nodes in nb.get(hid, {}).items():
                for n in nodes:
                    if kind == 'keyword' and n in terms_l:
                        continue  # 검색어 그 자체는 넓히는 데 쓰지 않는다
                    score[kind][n] += w
                    hits[kind][n] += 1
        # 업무 ↔ 업무 (상위·하위·연결) — 점수의 절반을 이웃 업무에
        task_ids = list(score['task'])
        if task_ids:
            spread: dict[int, float] = defaultdict(float)
            for tid, pid in s.execute(select(Task.id, Task.parent_id).where(
                    or_(Task.id.in_(task_ids), Task.parent_id.in_(task_ids)))):
                if pid and tid in score['task'] and pid not in score['task']:
                    spread[pid] += score['task'][tid] / 2
                if pid in score['task'] and tid not in score['task']:
                    spread[tid] += score['task'][pid] / 2
            for a, b in s.execute(select(TaskLink.src_id, TaskLink.dst_id).where(
                    or_(TaskLink.src_id.in_(task_ids), TaskLink.dst_id.in_(task_ids)))):
                for x, y in ((a, b), (b, a)):
                    if x in score['task'] and y not in score['task']:
                        spread[y] += score['task'][x] / 2
            for tid, v in spread.items():
                score['task'][tid] += v

        def best(kind, n):
            return sorted(score[kind].items(), key=lambda kv: (-kv[1], str(kv[0])))[:n]
        top_tasks, top_people, top_kws = best('task', top), best('person', top), best('keyword', top + 2)
        titles = dict(s.execute(select(Task.id, Task.title).where(Task.id.in_([t for t, _ in top_tasks] or [0]))).all())
        status = dict(s.execute(select(Task.id, Task.status).where(Task.id.in_([t for t, _ in top_tasks] or [0]))).all())
        out['tasks'] = [{'id': t, 'title': titles.get(t, ''), 'status': status.get(t, ''), 'score': round(v, 2),
                         'hits': hits['task'].get(t, 0)} for t, v in top_tasks if t in titles]
        ppl = {p.id: p for p in s.scalars(select(Person).where(Person.id.in_([p for p, _ in top_people] or [0])))}
        out['people'] = [{'id': p, 'name': ppl[p].name, 'position': ppl[p].position or '', 'role': ppl[p].role or '',
                          'assignment': ppl[p].assignment or '', 'score': round(v, 2), 'hits': hits['person'][p]}
                         for p, v in top_people if p in ppl]
        out['keywords'] = [{'keyword': k, 'score': round(v, 2), 'hits': hits['keyword'][k]} for k, v in top_kws
                           if hits['keyword'][k] >= 2 or len(seeds) <= 3]  # 한 기록에만 나온 키워드는 빼고
        refs = {r.id: r for r in s.scalars(select(Reference).where(Reference.id.in_([r for r, _ in best('ref', top)] or [0])))}
        out['refs'] = [{'id': r, 'title': refs[r].title or refs[r].file_name or refs[r].url, 'kind': refs[r].kind,
                        'task_id': refs[r].task_id, 'score': round(v, 2)} for r, v in best('ref', top) if r in refs]
        # 두 걸음 — 위쪽 업무·사람·키워드에 걸린 다른 기록
        cand: dict[int, float] = defaultdict(float)
        why: dict[int, list] = defaultdict(list)
        tnames = {t['id']: t['title'] for t in out['tasks']}
        for tid, v in top_tasks:
            if tid not in tnames:
                continue
            q = select(HistoryEntry.id).where(or_(HistoryEntry.task_id == tid, HistoryEntry.id.in_(
                select(HistoryTask.history_id).where(HistoryTask.task_id == tid)))).order_by(
                HistoryEntry.occurred_at.desc()).limit(60)
            for (hid,) in s.execute(q):
                if hid not in seeds:
                    cand[hid] += v
                    why[hid].append(f'같은 업무: {tnames[tid]}')
        pnames = {p['id']: p['name'] for p in out['people']}
        for pid, v in top_people:
            if pid not in pnames:
                continue
            q = select(HistoryPerson.history_id).join(HistoryEntry, HistoryEntry.id == HistoryPerson.history_id).where(
                HistoryPerson.person_id == pid).order_by(HistoryEntry.occurred_at.desc()).limit(60)
            for (hid,) in s.execute(q):
                if hid not in seeds:
                    cand[hid] += v
                    why[hid].append(f'같은 사람: {pnames[pid]}')
        for k in out['keywords'][:5]:
            q = select(HistoryEntry.id, HistoryEntry.keywords).where(HistoryEntry.keywords.like(f"%{k['keyword']}%")) \
                .order_by(HistoryEntry.occurred_at.desc()).limit(60)
            for hid, kws in s.execute(q):
                if hid not in seeds and k['keyword'] in {x.lower() for x in store._tags(kws or '')}:
                    cand[hid] += k['score'] * 0.7
                    why[hid].append(f"키워드: {k['keyword']}")
    if cand:
        ranked = [h for h, _ in sorted(cand.items(), key=lambda kv: -kv[1])[:related_limit * 3]]
        f = dict(filters or {})
        rows = {h['id']: h for h in store.search_history('', ids=ranked, limit=len(ranked), **f)}
        out['related'] = [{**rows[h], 'score': round(cand[h], 2), 'why': list(dict.fromkeys(why[h]))}
                          for h in ranked if h in rows][:related_limit]
    return out
