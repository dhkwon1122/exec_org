"""보고가 목표인 업무 — 목표 단계, 단계별 보고, 어디까지 보고됐는지."""
import pytest

from services import db, reporting, store


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))


def test_target_marks_and_progress():
    t = store.create_task('2027 사업계획 보고')
    assert reporting.progress(t) is None and reporting.label(t) == ''
    t = reporting.set_target(t['id'], '대표이사', '담당자')
    p = reporting.progress(t)
    assert p['current'] == '' and p['next'] == '그룹장' and not p['done']
    assert reporting.label(t) == '보고 전 · 다음 그룹장 (목표 대표이사)'
    reporting.mark(t['id'], '그룹장', '2026-11-20', '담당자')
    t = reporting.mark(t['id'], '원장', '2026-11-24', '담당자', note='예산 다시 검토')  # 팀장은 건너뜀
    p = reporting.progress(t)
    states = {s['stage']: s['state'] for s in p['stages']}
    assert states['그룹장'] == 'reached' and states['팀장'] == 'skipped' and states['원장'] == 'reached'
    assert states['부문(파트장)'] == 'next' and states['대표이사'] == 'todo' and states['실'] == 'beyond'
    assert p['current'] == '원장' and p['current_date'] == '2026-11-24'
    recs = [h for h in store.list_history(t['id']) if h['entry_type'] == '보고']
    assert {h['title'] for h in recs} == {'그룹장 보고', '원장 보고'}
    assert any('예산 다시 검토' in h['content'] and h['occurred_at'].startswith('2026-11-24') for h in recs)
    t = reporting.mark(t['id'], '대표이사', '2026-11-26')
    assert reporting.progress(t)['done'] and reporting.label(t) == '대표이사 보고 완료'
    t = reporting.unmark(t['id'], '대표이사')
    assert reporting.progress(t)['next'] == '부문(파트장)'
    with pytest.raises(ValueError):
        reporting.mark(t['id'], '없는 단계')
    with pytest.raises(ValueError):
        reporting.mark(t['id'], '팀장', '2026-13-01')
    with pytest.raises(ValueError):
        reporting.set_target(t['id'], '아무개')
    t = reporting.set_target(t['id'], '')
    assert reporting.progress(t) is None and t['report_log']  # 보고 날짜는 남긴다


def test_mark_without_target_sets_it_and_overview_order():
    a = store.create_task('A')
    reporting.mark(a['id'], '팀장', '2026-11-01')  # 목표 없이 보고 → 그 단계가 목표 (완료)
    assert store.get_task(a['id'])['report_target'] == '팀장'
    b = reporting.set_target(store.create_task('B')['id'], '원장')
    c = reporting.set_target(store.create_task('C')['id'], '원장')
    reporting.mark(c['id'], '그룹장', '2026-11-10')
    reporting.mark(c['id'], '팀장', '2026-11-12')
    store.create_task('보고 아님')
    rows = reporting.overview()
    assert [r['task']['title'] for r in rows] == ['B', 'C']  # 다음 단계가 낮은 것 먼저, 완료(A)는 빠짐
    assert [r['task']['title'] for r in reporting.overview(include_done=True)][-1] == 'A'
    store.update_task(b['id'], status='완료')
    assert [r['task']['title'] for r in reporting.overview()] == ['C']


def test_reporting_page_and_nav():
    import app
    from components import ui
    from pages import reporting as page
    t = reporting.set_target(store.create_task('보고 업무')['id'], '원장')
    reporting.mark(t['id'], '그룹장', '2026-11-20')
    out = page.render('', False, 0)
    assert '다음: 팀장 보고' in str(out) and '보고 업무' in str(out)
    assert app.section_active('/reporting')[0]
    assert '보고 진행' in str(ui.work_tabs('reporting'))
