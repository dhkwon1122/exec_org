"""후속조치 모아보기 — 등록하지 않은 제안과 확인이 필요한 업무."""
from datetime import date, timedelta

import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, no_update

from components import ui
from components.history_form import task_options
from services import auth, followups

dash.register_page(__name__, path='/followups', name='후속조치', title='업무 기록 · 후속조치')


def layout(**_):
    return html.Div([
        ui.page_head('후속조치', '기록에서 찾은 요청을 업무로 이어가세요'),
        dbc.Row([
            dbc.Col(dbc.Input(id='fu-q', type='search', placeholder='제안·업무 제목 검색', debounce=True), md=7),
            dbc.Col(dbc.Select(id='fu-view', value='pending', options=[
                {'label': '미등록 제안', 'value': 'pending'}, {'label': '보류한 제안', 'value': 'deferred'},
                {'label': '처리 제외한 제안', 'value': 'excluded'}, {'label': '확인이 필요한 업무', 'value': 'tasks'}]), md=5),
        ], className='g-2 mb-3'),
        html.P('보류·처리 제외는 내 목록에만 적용됩니다. 보류한 제안은 기한이 지나면 다시 표시됩니다.', className='small text-muted'),
        dbc.Alert(id='fu-message', is_open=False, color='info'), html.Div(id='fu-results'), ui.pager('fu-page'),
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle('후속조치 확인')),
            dbc.ModalBody([
                dcc.Store(id='fu-target'),
                html.Div([
                    dbc.Label('업무 제목'), dbc.Input(id='fu-title', className='mb-2'),
                    dbc.Label('상위 업무'), dcc.Dropdown(id='fu-parent', className='mb-2'),
                    dbc.Label('마감'), dbc.Input(id='fu-due', type='date', className='mb-2'),
                    dbc.Label('담당자 (쉼표로 구분)'), dbc.Input(id='fu-assignee'),
                ], id='fu-create-fields'),
                html.Div([dbc.Label('다시 확인할 날짜'), dbc.Input(id='fu-until', type='date')], id='fu-defer-fields'),
                dbc.Alert(id='fu-error', color='danger', is_open=False, className='mt-3'),
            ]),
            dbc.ModalFooter([dbc.Button('취소', id='fu-cancel', color='secondary', outline=True),
                             dbc.Button('반영', id='fu-save', color='primary')]),
        ], id='fu-modal', is_open=False, scrollable=True),
    ])


@callback(Output('fu-page', 'active_page'), Input('fu-q', 'value'), Input('fu-view', 'value'), prevent_initial_call=True)
def reset_page(*_):
    return 1


@callback(Output('fu-results', 'children'), Output('fu-page', 'max_value'), Output('fu-page', 'style'),
          Input('fu-q', 'value'), Input('fu-view', 'value'), Input('fu-page', 'active_page'),
          Input('history-changed', 'data'), Input('todo-changed', 'data'))
def render(q, view, page, _h, _t):
    user_id = (auth.current_user() or {}).get('user_id', '')
    rows = followups.attention_tasks() if view == 'tasks' else followups.suggestions(user_id, view)
    terms = (q or '').lower().split()
    rows = [r for r in rows if all(t in (r['title'] + ' ' + r.get('source_title', '')).lower() for t in terms)]
    shown, _ = ui.page_of(rows, page)
    cards = []
    for r in shown:
        if view == 'tasks':
            cards.append(dbc.ListGroupItem([
                dcc.Link(r['title'], href=f"/task/{r['id']}", className='record-title'),
                html.Div([dbc.Badge(reason, color='warning', className='me-1') for reason in r['reasons']]),
                html.Div(f"담당 {r['assignee']} · 마감 {r['due'] or '-'} · 최근 변경 {r['at']}", className='small text-muted mt-2'),
            ], className='py-3'))
            continue
        actions = [('등록', 'create'), ('보류', 'defer'), ('처리 제외', 'exclude')] if view == 'pending' else [('다시 확인', 'reset')]
        cards.append(dbc.ListGroupItem([
            html.Strong(r['title']),
            html.Div(f"마감 {r['due'] or '미지정'} · 담당 {r['assignee'] or '미지정'}" +
                     (f" · 보류 {r['until']}까지" if view == 'deferred' else ''), className='small text-muted mt-1'),
            dcc.Link(f"원문: {r['source_title']}", href=f"/history/{r['id']}", className='small'),
            html.Div([dbc.Button(label, id={'type': 'fu-act', 'action': action, 'hid': r['id'], 'key': r['key']},
                color='primary' if action == 'create' else 'secondary', outline=action != 'create',
                size='sm') for label, action in actions], className='d-flex gap-2 mt-2'),
        ], className='py-3'))
    return [html.P(f'{len(rows)}건', className='small text-muted'),
            dbc.ListGroup(cards) if cards else dbc.Alert('확인할 항목이 없습니다.', color='light')], *ui.pager_state(len(rows))


@callback(Output('fu-modal', 'is_open'), Output('fu-target', 'data'), Output('fu-title', 'value'),
          Output('fu-parent', 'options'), Output('fu-parent', 'value'), Output('fu-due', 'value'),
          Output('fu-assignee', 'value'), Output('fu-until', 'value'),
          Output('fu-create-fields', 'style'), Output('fu-defer-fields', 'style'),
          Output('fu-error', 'children'), Output('fu-error', 'is_open'),
          Output('fu-message', 'children'), Output('fu-message', 'is_open'),
          Output('history-changed', 'data', allow_duplicate=True), Output('todo-changed', 'data', allow_duplicate=True),
          Input({'type': 'fu-act', 'action': ALL, 'hid': ALL, 'key': ALL}, 'n_clicks'),
          Input('fu-cancel', 'n_clicks'), Input('fu-save', 'n_clicks'),
          State('fu-target', 'data'), State('fu-title', 'value'), State('fu-parent', 'value'),
          State('fu-due', 'value'), State('fu-assignee', 'value'), State('fu-until', 'value'),
          State('history-changed', 'data'), State('todo-changed', 'data'), prevent_initial_call=True)
def act(_a, _c, _s, target, title, parent, due, assignee, until, hn, tn):
    nu = no_update
    if not ctx.triggered or not ctx.triggered[0]['value']:
        return (nu,) * 16
    trig = ctx.triggered_id
    user = auth.current_user() or {}
    if trig == 'fu-cancel':
        return (False,) + (nu,) * 15
    if isinstance(trig, dict):
        target = trig
        if trig['action'] in ('create', 'defer'):
            from services import store
            h = store.get_history(trig['hid'])
            todo = next((t for t in (h or {}).get('ai_todos', []) if followups.suggestion_key(t) == trig['key']), None)
            if not todo:
                return (nu,) * 10 + ('제안이 변경됐습니다. 새로고침해 주세요.', True, nu, nu, nu, nu)
            return (True, trig, todo['title'], task_options(), (h or {}).get('task_id'), todo.get('due') or None,
                todo.get('assignee', ''), (date.today() + timedelta(days=7)).isoformat(),
                {} if trig['action'] == 'create' else {'display': 'none'},
                {} if trig['action'] == 'defer' else {'display': 'none'}, '', False, nu, False, nu, nu)
    if not target:
        return (nu,) * 16
    try:
        action = target['action']
        if action == 'create':
            result = followups.convert(target['hid'], target['key'], title, due or '', assignee or '', parent,
                                       user.get('name', ''))
            message = ['업무로 등록했습니다. ' if result['created'] else '이미 등록된 업무입니다. ',
                       dcc.Link('업무 열기', href=f"/task/{result['id']}")]
        else:
            status = {'defer': 'deferred', 'exclude': 'excluded', 'reset': 'pending'}[action]
            followups.decide(user.get('user_id', ''), target['hid'], target['key'], status, until or '')
            message = '보류했습니다.' if action == 'defer' else '처리 제외했습니다.' if action == 'exclude' else '다시 확인 목록으로 옮겼습니다.'
    except ValueError as error:
        return (nu,) * 10 + (str(error), True, str(error), True, nu, nu)
    return (False,) + (nu,) * 9 + ('', False, message, True, (hn or 0) + 1, (tn or 0) + 1)
