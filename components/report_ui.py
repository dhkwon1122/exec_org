"""업무 화면의 '보고 단계' 카드 — 목표 단계, 단계별 보고한 날, 보고 남기기 (services/reporting.py)."""
from datetime import date

import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, html, no_update

from services import auth, reporting

_COLOR = {'reached': 'success', 'skipped': 'light', 'next': 'primary', 'todo': 'light', 'beyond': 'light'}


def _target_select(value: str):
    return dbc.Select(id='rp-target', value=value or '', size='sm', style={'width': '170px'},
                      options=[{'label': '보고 업무 아님', 'value': ''}]
                      + [{'label': f'목표: {s}', 'value': s} for s in reporting.STAGES])


def _form(stage: str, hidden: bool = False):
    """보고 남기기 칸 — 콜백이 늘 읽으므로 보고 업무가 아닐 때도 숨겨 둔다."""
    return html.Div([
        dbc.Row([
            dbc.Col(dbc.Select(id='rp-stage', size='sm', value=stage,
                               options=[{'label': s, 'value': s} for s in reporting.STAGES]), md=3),
            dbc.Col(dbc.Input(id='rp-date', type='date', size='sm', value=date.today().isoformat()), md=3),
            dbc.Col(dbc.Input(id='rp-note', size='sm', placeholder='받은 의견·지시 (선택)'), md=4),
            dbc.Col(dbc.Button('보고 기록', id='rp-add', size='sm', color='primary', className='w-100'), md=2),
        ], className='g-1'),
        dbc.Alert(id='rp-msg', is_open=False, color='warning', className='small py-1 px-2 mt-2 mb-0'),
    ], style={'display': 'none'} if hidden else None)


def card(task: dict):
    p = reporting.progress(task)
    if p is None:
        return html.Div([
            html.Div([html.I(className='bi bi-megaphone me-1 text-muted'),
                      html.Span('보고가 목표인 업무면 목표 단계를 정하세요', className='small text-muted me-2'),
                      _target_select('')], className='d-flex align-items-center flex-wrap'),
            _form(reporting.STAGES[0], hidden=True),
        ], className='mt-2')
    steps = []
    for st in p['stages']:
        if st['state'] == 'beyond':
            continue
        steps.append(html.Div([
            dbc.Badge([st['stage'], html.Span(f" {st['date'][5:]}", className='fw-normal') if st['date'] else None],
                      color=_COLOR[st['state']],
                      text_color='dark' if st['state'] in ('todo', 'skipped') else None,
                      className='border' + (' opacity-50' if st['state'] == 'skipped' else ''),
                      title={'reached': f"{st['date']} 보고", 'skipped': '건너뜀 (위 단계까지 보고함)',
                             'next': '다음 보고', 'todo': '아직'}[st['state']]),
            html.Span('×', id={'type': 'rp-undo', 'stage': st['stage']}, n_clicks=0, title='이 단계 보고 지우기',
                      className='small text-muted ms-1', style={'cursor': 'pointer'}) if st['date'] else None,
            html.I(className='bi bi-chevron-right text-muted mx-1 small') if st['stage'] != p['target'] else None,
        ], className='d-inline-flex align-items-center mb-1'))
    status = (dbc.Badge(f"{p['target']} 보고 완료", color='success', className='ms-2') if p['done'] else
              html.Span(f"다음: {p['next']}", className='small fw-semibold text-primary ms-2'))
    return dbc.Card(dbc.CardBody([
        html.Div([html.I(className='bi bi-megaphone me-1'), html.Span('보고 단계', className='fw-semibold'), status,
                  html.Div(_target_select(p['target']), className='ms-auto')],
                 className='d-flex align-items-center mb-2'),
        html.Div(steps, className='d-flex flex-wrap align-items-center mb-2'),
        _form(p['next'] or p['target']),
    ]), className='mt-2 border-primary-subtle')


def register(refresh_id: str, task_store_id: str):
    """콜백 — 목표 바꾸기·보고 남기기·지우기. 바뀌면 refresh_id(업무 화면 다시 그리기)를 올린다."""
    @callback(Output(refresh_id, 'data', allow_duplicate=True), Output('history-changed', 'data', allow_duplicate=True),
              Output('rp-msg', 'children'), Output('rp-msg', 'is_open'),
              Input('rp-target', 'value'), Input('rp-add', 'n_clicks'),
              Input({'type': 'rp-undo', 'stage': ALL}, 'n_clicks'),
              State(task_store_id, 'data'), State('rp-stage', 'value'), State('rp-date', 'value'),
              State('rp-note', 'value'), State(refresh_id, 'data'), State('history-changed', 'data'),
              prevent_initial_call=True)
    def act(target, _add, _undo, tid, stage, day, note, n, hn):
        trig = ctx.triggered_id
        if not tid or not ctx.triggered or trig is None:
            return no_update, no_update, no_update, no_update
        try:
            if trig == 'rp-target':
                from services import store
                t = store.get_task(tid)
                if t is None or (target or '') == t['report_target']:
                    return no_update, no_update, no_update, no_update  # 화면이 다시 그려질 때 같은 값
                reporting.set_target(tid, target or '', auth.current_name())
            elif trig == 'rp-add':
                if not _add:
                    return no_update, no_update, no_update, no_update
                reporting.mark(tid, stage, day or '', auth.current_name(), note or '')
            elif isinstance(trig, dict):
                if not ctx.triggered[0]['value']:
                    return no_update, no_update, no_update, no_update
                reporting.unmark(tid, trig['stage'])
        except ValueError as e:
            return no_update, no_update, str(e), True
        return (n or 0) + 1, (hn or 0) + 1, '', False
