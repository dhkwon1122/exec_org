"""업무 목록: 검색·필터, 새 업무 등록, 전체 최근 이력."""
import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, ctx, dcc, html, no_update

from datetime import date, timedelta

from components import todo_ui, ui
from components.history_form import decode_text_upload, history_form, register_form_callbacks, save_history
from services import auth, store

dash.register_page(__name__, path='/', name='업무', title='업무 기록 · 업무')


def _new_task_modal():
    return dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle('새 업무')),
        dbc.ModalBody([
            dbc.Label('제목'), dbc.Input(id='new-title', className='mb-2'),
            dbc.Label('설명'), dbc.Textarea(id='new-desc', rows=4, className='mb-2'),
            dbc.Row([
                dbc.Col([dbc.Label('상태'), dbc.Select(id='new-status', value='진행중',
                                                      options=[{'label': s, 'value': s} for s in store.STATUSES])]),
                dbc.Col([dbc.Label('담당자'), dbc.Input(id='new-owner')]),
            ], className='mb-2'),
            dbc.Row([
                dbc.Col([dbc.Label('시작일'), dbc.Input(id='new-start', type='date')]),
                dbc.Col([dbc.Label('마감일'), dbc.Input(id='new-due', type='date')]),
            ], className='mb-2'),
            dbc.Label('태그 (쉼표로 구분)'), dbc.Input(id='new-tags', className='mb-2'),
            dbc.Label('상위 업무 (선택)'), dcc.Dropdown(id='new-parent', placeholder='없음'),
            dbc.Alert(id='new-error', color='danger', is_open=False, className='mt-3 mb-0'),
        ]),
        dbc.ModalFooter([
            dbc.Button('취소', id='new-cancel', color='secondary', outline=True),
            dbc.Button('등록', id='new-save', color='primary'),
        ]),
    ], id='new-modal', is_open=False, size='lg')


def layout(**_):
    return html.Div([
        dcc.Store(id='home-refresh', data=0),
        dcc.Location(id='home-redirect', refresh='callback-nav'),
        _new_task_modal(),
        dbc.Row([
            dbc.Col([
                dbc.Row([
                    dbc.Col(dbc.Input(id='q', placeholder='검색: 제목·설명·태그·이력·참고자료', debounce=True,
                                      type='search'), md=5),
                    dbc.Col(dcc.Dropdown(id='f-status', multi=True, placeholder='상태 (전체)',
                                         options=store.STATUSES, value=[]), md=3),
                    dbc.Col(dcc.Dropdown(id='f-tag', placeholder='태그'), md=2),
                    dbc.Col(dbc.Button([html.I(className='bi bi-plus-lg me-1'), '새 업무'], id='new-open',
                                       color='primary', className='w-100'), md=2),
                ], className='g-2 mb-3'),
                html.Div(id='task-list'),
            ], lg=8),
            dbc.Col([
                dbc.Card([
                    dbc.CardHeader(html.Div([html.I(className='bi bi-list-check me-1'), '내 할 일 (7일 이내·지난 마감)',
                                             dcc.Link('전체', href='/todos', className='ms-auto small')],
                                            className='d-flex'), className='small'),
                    html.Div(id='home-todos'),
                ], className='mb-3'),
                dbc.Card([
                    dbc.CardHeader([html.I(className='bi bi-lightning-charge me-1'), '빠른 기록'], className='small'),
                    dbc.CardBody([
                        dcc.Dropdown(id='qr-task', placeholder='관련 업무 (임원 동향이면 비워도 됨)', className='mb-2'),
                        *history_form('qr', compact=True),
                        dbc.Alert(id='qr-ok', color='success', is_open=False, duration=5000,
                                  className='mt-2 mb-0 py-2 small'),
                    ]),
                ], className='mb-3'),
                html.H6('최근 이력', className='text-muted'),
                html.Div(id='recent-history'),
            ], lg=4),
        ]),
    ])


@callback(Output('task-list', 'children'), Output('recent-history', 'children'), Output('f-tag', 'options'),
          Input('q', 'value'), Input('f-status', 'value'), Input('f-tag', 'value'), Input('home-refresh', 'data'))
def render_list(q, statuses, tag, _):
    tasks = store.list_tasks(query=q or '', status=statuses or None, tag=tag or '')
    if tasks:
        items = []
        for t in tasks:
            meta = [m for m in [t['owner'] and f"담당 {t['owner']}",
                                t['due_date'] and f"마감 {t['due_date']}",
                                f"수정 {t['updated_at']}"] if m]
            items.append(dbc.ListGroupItem([
                html.Div([ui.status_badge(t['status']), ui.task_link(t)], className='mb-1'),
                html.Div(t['description'][:140] + ('…' if len(t['description']) > 140 else ''),
                         className='small text-muted mb-1') if t['description'] else None,
                html.Div([*ui.tag_badges(t['tags']), html.Span(' · '.join(meta), className='small text-muted')]),
            ]))
        task_view = dbc.ListGroup(items)
    else:
        task_view = dbc.Alert('조건에 맞는 업무가 없습니다. 오른쪽 위 "새 업무"로 등록하세요.', color='light')

    recent = store.recent_history(15)
    history_view = dbc.ListGroup([
        dbc.ListGroupItem([
            html.Div([html.I(className=f"bi {ui.ENTRY_ICON.get(h['entry_type'], 'bi-dot')} me-1"),
                      html.Span(h['entry_type'], className='fw-semibold me-2'),
                      dcc.Link(h['task_title'], href=f"/task/{h['task_id']}", className='small')]),
            html.Div(h['title'] or h['content'][:120], className='small', style={'whiteSpace': 'pre-wrap'}),
            html.Div(f"{h['occurred_at']} {h['author']}", className='small text-muted'),
        ], className='py-2') for h in recent
    ], flush=True) if recent else html.Div('아직 이력이 없습니다.', className='small text-muted')

    return task_view, history_view, store.all_tags()


@callback(Output('home-todos', 'children'), Input('todo-changed', 'data'), Input('home-refresh', 'data'))
def render_my_todos(_, _r):
    me = (auth.current_user() or {}).get('user_id', '')
    soon = (date.today() + timedelta(days=7)).isoformat()
    todos = [t for t in store.list_todos(assignee_id=me, done=False) if t['due_date'] and t['due_date'] <= soon]
    if not todos:
        return html.Div('급한 할 일이 없습니다.', className='small text-muted p-3')
    return dbc.ListGroup([todo_ui.todo_row(t) for t in todos[:8]], flush=True)


@callback(Output('qr-task', 'options'), Input('home-refresh', 'data'))
def quick_task_options(_):
    return [{'label': f"#{t['id']} {t['title']}", 'value': t['id']}
            for t in store.list_tasks(status=['예정', '진행중', '보류'])]


@callback(Output('qr-content', 'value', allow_duplicate=True), Input('qr-file', 'contents'),
          State('qr-content', 'value'), prevent_initial_call=True)
def quick_load_file(contents, current):
    if not contents:
        return no_update
    text = decode_text_upload(contents)
    return f'{current.rstrip()}\n\n{text}' if (current or '').strip() else text


register_form_callbacks('qr')


@callback(Output('home-refresh', 'data'), Output('qr-content', 'value'), Output('qr-when', 'value'),
          Output('qr-links', 'value'), Output('qr-attach', 'contents'), Output('qr-attach', 'filename'),
          Output('qr-attach-names', 'children', allow_duplicate=True),
          Output('qr-error', 'children'), Output('qr-error', 'is_open'),
          Output('qr-ok', 'children'), Output('qr-ok', 'is_open'), Output('qr-people', 'value'),
          Input('qr-add', 'n_clicks'), State('qr-task', 'value'), State('qr-type', 'value'),
          State('qr-content', 'value'), State('qr-when', 'value'), State('qr-ai', 'value'),
          State('qr-links', 'value'), State('qr-attach', 'contents'), State('qr-attach', 'filename'),
          State('qr-people', 'value'), State('home-refresh', 'data'), prevent_initial_call=True)
def quick_record(_, task_id, etype, content, when, use_ai, links, att, att_names, people, n):
    nu = no_update
    try:
        h = save_history(int(task_id) if task_id else None, etype, content, when, use_ai, links, att, att_names,
                         auth.current_name(), people)
    except ValueError as e:
        msg = str(e)
        if not task_id and not people:
            msg = '업무를 고르거나, 임원 동향이면 관련 임원을 고르세요 (원문에 등록된 임원 이름이 있으면 자동 연결).'
        return (nu,) * 7 + (msg, True, nu, False, nu)
    n_refs = len(h['refs'])
    where = [f"#{task_id} 업무"] if task_id else []
    where += [p['name'] for p in h['people']]
    msg = [f"{', '.join(where)}에 기록했습니다" + (f' (관련 자료 {n_refs}건)' if n_refs else '') + '. ']
    if task_id:
        msg.append(dcc.Link('업무 열기', href=f'/task/{task_id}'))
    elif h['people']:
        msg.append(dcc.Link('임원 기록 보기', href=f"/people/{h['people'][0]['id']}"))
    if use_ai:
        msg.insert(1, 'AI 요약·키워드는 잠시 후 채워집니다. ')
    return (n or 0) + 1, '', None, '', None, None, '', '', False, msg, True, []


@callback(Output('new-modal', 'is_open'), Output('new-parent', 'options'), Output('new-error', 'children'),
          Output('new-error', 'is_open'), Output('home-redirect', 'pathname'),
          Input('new-open', 'n_clicks'), Input('new-cancel', 'n_clicks'), Input('new-save', 'n_clicks'),
          State('new-title', 'value'), State('new-desc', 'value'), State('new-status', 'value'),
          State('new-owner', 'value'), State('new-start', 'value'), State('new-due', 'value'),
          State('new-tags', 'value'), State('new-parent', 'value'), 
          prevent_initial_call=True)
def new_task(_o, _c, _s, title, desc, status, owner, start, due, tags, parent):
    author = auth.current_name()
    if ctx.triggered_id not in ('new-open', 'new-cancel', 'new-save'):
        return (no_update,) * 5
    if ctx.triggered_id == 'new-open':
        opts = [{'label': f"#{t['id']} {t['title']}", 'value': t['id']} for t in store.list_tasks()]
        return True, opts, '', False, no_update
    if ctx.triggered_id == 'new-cancel':
        return False, no_update, '', False, no_update
    try:
        t = store.create_task(title, desc or '', status or '진행중', owner or author or '', tags or '',
                              parent, start or '', due or '')
    except ValueError as e:
        return True, no_update, str(e), True, no_update
    return False, no_update, '', False, f"/task/{t['id']}"
