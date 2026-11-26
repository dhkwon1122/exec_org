"""보고 진행 — 보고가 목표인 업무가 어디까지 보고됐는지, 다음 보고 단계별로 모아 본다 (services/reporting.py)."""
from datetime import date

import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, callback, dcc, html

from components import todo_ui, ui
from services import reporting

dash.register_page(__name__, path='/reporting', name='보고 진행', title='업무 기록 · 보고 진행')


def layout(**_):
    return html.Div([
        ui.page_head('보고 진행', '보고가 목표인 업무가 어디까지 보고됐는지'), ui.work_tabs('reporting'),
        dbc.Row([
            dbc.Col(dbc.Input(id='rpv-q', type='search', debounce=True, placeholder='업무 제목 검색'), md=6),
            dbc.Col(dbc.Checkbox(id='rpv-done', value=False, label='목표까지 보고한 업무도 보기'), md=6,
                    className='d-flex align-items-center'),
        ], className='g-2 mb-2'),
        html.Div(['단계: ', ' → '.join(reporting.STAGES), ' · 업무 화면의 "보고 단계"에서 목표를 정하고 보고를 남깁니다.'],
                 className='small text-muted mb-3'),
        html.Div(id='rpv-list'),
    ])


def _row(r):
    t, p = r['task'], r['progress']
    since = (date.today() - date.fromisoformat(p['current_date'])).days if p['current_date'] else None
    return dbc.ListGroupItem([
        html.Div([ui.task_link(t), html.Span(ui.status_badge(t['status']), className='ms-2'),
                  todo_ui.due_badge(t['due_date'], False) if t['due_date'] else None],
                 className='d-flex align-items-center flex-wrap gap-1'),
        html.Div([
            html.Span([dbc.Badge(st['stage'], color='success' if st['date'] else 'light',
                                 text_color=None if st['date'] else 'dark',
                                 className='border me-1' + (' opacity-50' if st['state'] == 'skipped' else ''),
                                 title=st['date'] or None)
                       for st in p['stages'] if st['state'] != 'beyond']),
        ], className='mt-1'),
        html.Div(' · '.join(x for x in [
            f"{p['current']} 보고 {p['current_date']}" + (f' ({since}일 전)' if since else ' (오늘)' if since == 0 else '')
            if p['current'] else '아직 보고 전',
            f"목표 {p['target']}", f"담당 {todo_ui.assignee_names(t)}" if t['assignees'] else ''] if x),
            className='small text-muted mt-1'),
    ], className='py-2')


@callback(Output('rpv-list', 'children'), Input('rpv-q', 'value'), Input('rpv-done', 'value'),
          Input('history-changed', 'data'))
def render(q, show_done, _):
    rows = reporting.overview(include_done=bool(show_done))
    words = (q or '').lower().split()
    rows = [r for r in rows if all(w in r['task']['title'].lower() for w in words)]
    if not rows:
        return dbc.Alert('보고 업무가 없습니다. 업무 화면의 "보고 단계"에서 목표 단계를 정하면 여기에 모입니다.', color='light')
    groups: dict[str, list] = {}
    for r in rows:
        key = '목표까지 보고 완료' if r['progress']['done'] else f"다음: {r['progress']['next']} 보고"
        groups.setdefault(key, []).append(r)
    return [html.Div([
        html.H6([name, html.Span(f' {len(items)}', className='text-muted fw-normal')], className='mt-3'),
        dbc.ListGroup([_row(r) for r in items]),
    ]) for name, items in groups.items()]
