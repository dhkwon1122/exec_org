"""업무 목록: 검색·필터, 새 업무 등록, 전체 최근 이력.
보기는 목록·간트·내 업무(내가 담당인 업무를 마감일별로 — 예전 /todos 화면, /?view=mine)."""
import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, no_update

from datetime import date, timedelta

from components import gantt, task_summary_ui, todo_ui, ui
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
                dbc.Col([dbc.Label('담당자'), todo_ui.assignee_picker('new')]),
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


def _today_label() -> str:
    d = date.today()
    return f"{d.month}월 {d.day}일 {'월화수목금토일'[d.weekday()]}요일"


def layout(view: str = '', **_):
    return html.Div([
        dcc.Store(id='home-refresh', data=0),
        dcc.Location(id='home-redirect', refresh='callback-nav'),
        _new_task_modal(),
        ui.page_head('업무', _today_label()),
        dbc.Row([
            dbc.Col([
                dbc.Row([
                    dbc.Col(dbc.Input(id='q', placeholder='검색: 제목·설명·태그·이력·참고자료', debounce=True,
                                      type='search'), md=5),
                    dbc.Col(dcc.Dropdown(id='f-status', multi=True, placeholder='상태 (전체)',
                                         options=store.STATUSES, value=[]), md=3),
                    dbc.Col(dcc.Dropdown(id='f-tag', placeholder='태그'), md=2, id='f-tag-col'),
                    dbc.Col(dbc.Button([html.I(className='bi bi-plus-lg me-1'), '새 업무'], id='new-open',
                                       color='primary', className='w-100'), md=2),
                ], className='g-2 mb-2'),
                html.Div([
                    dbc.RadioItems(id='home-view', value='mine' if view == 'mine' else 'list', inline=True,
                                   className='d-flex me-3',
                                   inputClassName='btn-check', labelClassName='btn btn-sm btn-outline-primary me-1',
                                   labelCheckedClassName='active', persistence=True, persistence_type='local',
                                   options=[{'label': html.Span([html.I(className='bi bi-list-ul me-1'), '목록']),
                                             'value': 'list'},
                                            {'label': html.Span([html.I(className='bi bi-bar-chart-steps me-1'), '간트']),
                                             'value': 'gantt'},
                                            {'label': html.Span([html.I(className='bi bi-person-check me-1'), '내 업무']),
                                             'value': 'mine'}]),
                    html.Div([
                        dbc.RadioItems(id='gantt-scale', value='month', inline=True, className='d-flex me-1',
                                       inputClassName='btn-check',
                                       labelClassName='btn btn-sm btn-outline-secondary me-1',
                                       labelCheckedClassName='active', persistence=True, persistence_type='local',
                                       options=[{'label': v[0], 'value': k} for k, v in gantt.SCALES.items()]),
                        dbc.ButtonGroup([
                            dbc.Button(html.I(className='bi bi-chevron-left'), id='gantt-prev', size='sm',
                                       color='secondary', outline=True, title='앞으로'),
                            dbc.Button('오늘', id='gantt-today', size='sm', color='secondary', outline=True),
                            dbc.Button(html.I(className='bi bi-chevron-right'), id='gantt-next', size='sm',
                                       color='secondary', outline=True, title='뒤로'),
                        ], id='gantt-nav', className='me-3'),
                        dcc.Store(id='gantt-offset', data=0),
                    ], id='gantt-controls', className='d-flex align-items-center', style={'display': 'none'}),
                    html.Div([
                        html.Span('하위 업무 펼치기', className='small text-muted me-2'),
                        dbc.RadioItems(id='home-depth', value=1, inline=True, className='d-flex',
                                       inputClassName='btn-check',
                                       labelClassName='btn btn-sm btn-outline-secondary me-1',
                                       labelCheckedClassName='active', persistence=True, persistence_type='local',
                                       options=[{'label': '최상위만', 'value': 0}, {'label': '1단계', 'value': 1},
                                                {'label': '2단계', 'value': 2}, {'label': '3단계', 'value': 3},
                                                {'label': '전체', 'value': 99}]),
                    ], id='home-depth-box', className='d-flex align-items-center'),
                    dbc.Switch(id='home-show-done', label='완료 업무 표시', value=True, persistence=True,
                               persistence_type='local', className='small ms-auto mb-0 me-3'),
                    dbc.Checkbox(id='tp-show-done', label='최근 7일 완료 보기', value=False,
                                 className='small ms-auto me-3', style={'display': 'none'}),
                    html.Span('체크 = 완료', className='small text-muted'),
                ], className='d-flex align-items-center flex-wrap mb-2'),
                html.Div(id='task-list'),
                html.Div([  # 내 업무 보기 — 내가 담당인 업무·하위 업무를 마감일별로
                    dbc.Card(dbc.CardBody(todo_ui.add_form('tp')), className='mb-2'),
                    html.Div(id='tp-groups'),
                ], id='home-mine', style={'display': 'none'}),
                dbc.Offcanvas(id='gantt-panel', placement='end', is_open=False, scrollable=True,
                              style={'width': 'min(560px, 100vw)'}),  # 간트에서 업무를 누르면 AI 요약
            ], lg=8, id='home-main-col'),
            dbc.Col(id='home-side-col', lg=4, children=[
                dbc.Card(id='home-todos-card', children=[
                    dbc.CardHeader(html.Div([html.I(className='bi bi-list-check me-1'), '내 업무 (7일 이내·지난 마감)',
                                             dcc.Link('전체', href='/?view=mine', className='ms-auto small')],
                                            className='d-flex'), className='small'),
                    html.Div(id='home-todos'),
                ], className='mb-3'),
                dbc.Card([
                    dbc.CardHeader([html.I(className='bi bi-lightning-charge me-1'), '빠른 기록'], className='small'),
                    dbc.CardBody([
                        dcc.Dropdown(id='qr-task', multi=True, className='mb-2',
                                     placeholder='관련 업무 — 여러 업무에 걸치면 모두 (임원 동향이면 비워도 됨)'),
                        *history_form('qr', compact=True),
                        dbc.Alert(id='qr-ok', color='success', is_open=False, duration=5000,
                                  className='mt-2 mb-0 py-2 small'),
                    ]),
                ], className='mb-3'),
                html.H6('최근 이력', className='text-muted'),
                html.Div(id='recent-history'),
            ]),
        ]),
    ])


def _task_row(t: dict, level: int = 0, n_children: int = 0, n_done: int = 0, n_hidden: int = 0,
              parent_title: str = ''):
    """업무 한 줄 — 완료 체크, 상태 바꾸기, 제목(업무 화면으로), 하위 진행률·담당·마감·태그.
    하위 업무는 단계만큼 들여 쓴다."""
    done = t['status'] == '완료'
    meta = [m for m in [n_children and f"하위 {n_done}/{n_children}", t['assignees'] and f"담당 {todo_ui.assignee_names(t)}",
                        f"수정 {t['updated_at'][:10]}" if level == 0 else ''] if m]
    return dbc.ListGroupItem(html.Div([
        dbc.Checkbox(id={'type': 'home-done', 'id': t['id']}, value=done, className='me-2 mt-1'),
        html.Div([
            html.Div([
                html.I(className='bi bi-arrow-return-right text-muted me-1') if level else None,
                dcc.Link([html.Span(f"#{t['id']}", className='task-no'), t['title']], href=f"/task/{t['id']}",
                         className='task-title' + (' done' if done else '')),
                html.Span(ui.source_icons(t), className='ms-1'),
                html.Span(f' · 상위 {parent_title}', className='small text-muted') if parent_title else None,
            ]),
            html.Div(t['description'][:140] + ('…' if len(t['description']) > 140 else ''),
                     className='small text-muted') if t['description'] and level == 0 else None,
            html.Div([
                todo_ui.due_badge(t['due_date'], done) if t['due_date'] else None,
                *ui.tag_badges(t['tags']),
                html.Span(' · '.join(meta), className='small text-muted me-2'),
                dcc.Link(f'하위 {n_hidden}개 더 ▸', href=f"/task/{t['id']}", className='small')
                if n_hidden else None,
            ], className='small'),
        ], className='flex-grow-1'),
        dbc.Select(id={'type': 'home-status', 'id': t['id']}, value=t['status'], size='sm',
                   options=[{'label': x, 'value': x} for x in store.STATUSES], style={'width': '92px'},
                   className='ms-2 status-select'),
    ], className='d-flex align-items-start'), className='py-2',
        style={'paddingLeft': f'{0.75 + level * 1.6}rem',
               'background': 'var(--bs-tertiary-bg)' if level else None})


@callback(Output('task-list', 'children'), Output('recent-history', 'children'), Output('f-tag', 'options'),
          Input('q', 'value'), Input('f-status', 'value'), Input('f-tag', 'value'), Input('home-refresh', 'data'),
          Input('home-depth', 'value'), Input('todo-changed', 'data'), Input('home-show-done', 'value'),
          Input('home-view', 'value'), Input('gantt-scale', 'value'), Input('gantt-offset', 'data'))
def render_list(q, statuses, tag, _, depth, _t, show_done=True, view='list', scale='month', offset=0):
    if view == 'mine':  # 내 업무 보기는 render_mine 이 그린다 — 최근 이력·태그 목록만
        return None, _recent_view(), store.all_tags()
    hide_done = not show_done and '완료' not in (statuses or [])  # 상태에서 '완료'를 고르면 그대로 보인다
    if q or tag:  # 검색·태그는 하위 업무까지 찾아 평평하게
        tasks = store.list_tasks(query=q or '', status=statuses or None, tag=tag or '')
        if hide_done:
            tasks = [t for t in tasks if t['status'] != '완료']
        titles = {t['id']: t['title'] for t in store.list_tasks()}
        progress = store.child_progress([t['id'] for t in tasks])
        rows = [_task_row(t, 0, progress.get(t['id'], (0, 0))[1], progress.get(t['id'], (0, 0))[0],
                          parent_title=titles.get(t['parent_id'], '')) for t in tasks]
        tree = [{'task': t, 'level': 0, 'n_children': progress.get(t['id'], (0, 0))[1],
                 'n_done': progress.get(t['id'], (0, 0))[0]} for t in tasks]
    else:  # 최상위 업무 아래로 고른 단계만큼 하위 업무를 펼친다
        tree = store.task_tree(statuses or None, depth if depth is not None else 1, hide_done=hide_done)
        rows = [_task_row(r['task'], r['level'], r['n_children'], r['n_done'], r['n_hidden']) for r in tree]
    if view == 'gantt' and tree:  # 같은 트리를 기간 막대로
        task_view = gantt.chart(tree, scale=scale or 'month', offset=offset or 0,
                                marks=store.history_marks([r['task']['id'] for r in tree]))
    else:
        task_view = dbc.ListGroup(rows) if rows else \
            dbc.Alert('조건에 맞는 업무가 없습니다. 오른쪽 위 "새 업무"로 등록하세요.', color='light')

    return task_view, _recent_view(), store.all_tags()


def _recent_view():
    recent = store.recent_history(15)
    return dbc.ListGroup([
        dbc.ListGroupItem([
            html.Div([html.I(className=f"bi {ui.ENTRY_ICON.get(h['entry_type'], 'bi-dot')} me-1"),
                      html.Span(h['entry_type'], className='fw-semibold me-2'),
                      dcc.Link(h['task_title'], href=f"/task/{h['task_id']}", className='small')]),
            html.Div(h['title'] or h['content'][:120], className='small', style={'whiteSpace': 'pre-wrap'}),
            html.Div(f"{h['occurred_at']} {h['author']}", className='small text-muted'),
        ], className='py-2') for h in recent
    ], flush=True) if recent else html.Div('아직 이력이 없습니다.', className='small text-muted')


todo_ui.register_add_callback('tp')

_MINE_GROUPS = [  # (이름, 글자 색, 고르는 조건(업무, 오늘, 7일 뒤))
    ('지난 마감', 'text-danger', lambda t, d, w: not t['done'] and t['overdue']),
    ('오늘', 'text-warning-emphasis', lambda t, d, w: not t['done'] and t['due_date'] == d),
    ('7일 이내', '', lambda t, d, w: not t['done'] and d < t['due_date'] <= w),
    ('이후', '', lambda t, d, w: not t['done'] and t['due_date'] > w),
    ('마감 없음', 'text-muted', lambda t, d, w: not t['done'] and not t['due_date']),
    ('최근 완료', 'text-success', lambda t, d, w: t['done']),
]


@callback(Output('tp-groups', 'children'), Output('tp-assignee', 'options'),
          Output('tp-task', 'options'), Output('tp-assignee', 'value'),
          Input('home-view', 'value'), Input('tp-show-done', 'value'), Input('todo-changed', 'data'),
          Input('home-refresh', 'data'), Input('q', 'value'), Input('f-status', 'value'))
def render_mine(view, show_done, _, _r, q, statuses):
    """내 업무 보기 — 내가 담당인 업무·하위 업무를 마감일별로. 검색어(제목·상위 업무)와 상태 고르기를 따른다."""
    if view != 'mine':
        return no_update, no_update, no_update, no_update
    me = (auth.current_user() or {}).get('user_id', '')
    today = date.today()
    d, w = today.isoformat(), (today + timedelta(days=7)).isoformat()
    todos = store.list_todos(assignee_id=me, assignee_name=auth.current_name(),
                             done=None if show_done else False,
                             done_since=(today - timedelta(days=7)).isoformat() if show_done else '')
    if (q or '').strip():
        words = q.lower().split()
        todos = [t for t in todos if all(x in f"{t['title']} {t['task_title']}".lower() for x in words)]
    if statuses:
        todos = [t for t in todos if t['status'] in statuses or (t['done'] and show_done)]
    sections = []
    for label, cls, pred in _MINE_GROUPS:
        items = [t for t in todos if pred(t, d, w)]
        if items:
            sections.append(html.Div([
                html.H6([label, html.Span(f' {len(items)}', className='text-muted fw-normal')],
                        className=f'mt-3 {cls}'),
                todo_ui.todo_list(items),
            ]))
    if not sections:
        sections = [html.Div('내가 담당인 업무가 없습니다.', className='text-muted small mt-2')]
    task_opts = [{'label': f"#{t['id']} {t['title']}", 'value': t['id']}
                 for t in store.list_tasks(status=['예정', '진행중', '보류'])]
    # 새 업무의 담당자 기본값은 나
    return sections, todo_ui.assignee_options(), task_opts, [f'u:{me}'] if me else []


@callback(Output('home-todos', 'children'), Input('todo-changed', 'data'), Input('home-refresh', 'data'))
def render_my_todos(_, _r):
    me = (auth.current_user() or {}).get('user_id', '')
    soon = (date.today() + timedelta(days=7)).isoformat()
    todos = [t for t in store.list_todos(assignee_id=me, done=False, assignee_name=auth.current_name())
             if t['due_date'] and t['due_date'] <= soon]
    if not todos:
        return html.Div('급한 업무가 없습니다.', className='small text-muted p-3')
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
def quick_record(_, task_ids, etype, content, when, use_ai, links, att, att_names, people, n):
    nu = no_update
    task_ids = [int(x) for x in ([task_ids] if isinstance(task_ids, (int, str)) else task_ids or [])]
    try:
        h = save_history(None, etype, content, when, use_ai, links, att, att_names, auth.current_name(), people,
                         also_task_ids=task_ids)
    except ValueError as e:
        msg = str(e)
        if not task_ids and not people:
            msg = '업무를 고르거나, 임원 동향이면 관련 임원을 고르세요 (원문에 등록된 임원 이름이 있으면 자동 연결).'
        return (nu,) * 7 + (msg, True, nu, False, nu)
    n_refs = len(h['refs'])
    task_id = h['task_id']
    where = [f"#{t} 업무" for t in task_ids]
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
          Output('new-assignees', 'options'), Output('new-assignees', 'value'),
          Input('new-open', 'n_clicks'), Input('new-cancel', 'n_clicks'), Input('new-save', 'n_clicks'),
          State('new-title', 'value'), State('new-desc', 'value'), State('new-status', 'value'),
          State('new-assignees', 'value'), State('new-assignee-extra', 'value'), State('new-start', 'value'),
          State('new-due', 'value'), State('new-tags', 'value'), State('new-parent', 'value'),
          prevent_initial_call=True)
def new_task(_o, _c, _s, title, desc, status, assignees, extra, start, due, tags, parent):
    nu = no_update
    if ctx.triggered_id not in ('new-open', 'new-cancel', 'new-save'):
        return (nu,) * 7
    if ctx.triggered_id == 'new-open':
        opts = [{'label': f"#{t['id']} {t['title']}", 'value': t['id']} for t in store.list_tasks()]
        me = (auth.current_user() or {}).get('user_id', '')
        return True, opts, '', False, nu, todo_ui.assignee_options(), [f'u:{me}'] if me else []  # 기본 담당자는 나
    if ctx.triggered_id == 'new-cancel':
        return False, nu, '', False, nu, nu, nu
    try:
        t = store.create_task(title, desc or '', status or '진행중', tags=tags or '', parent_id=parent,
                              start_date=start or '', due_date=due or '',
                              assignees=todo_ui.assignees_from(assignees, extra))
    except ValueError as e:
        return True, nu, str(e), True, nu, nu, nu
    return False, nu, '', False, f"/task/{t['id']}", nu, nu


@callback(Output('home-refresh', 'data', allow_duplicate=True),
          Input({'type': 'home-done', 'id': ALL}, 'value'), Input({'type': 'home-status', 'id': ALL}, 'value'),
          State('home-refresh', 'data'), prevent_initial_call=True)
def change_status(_d, _s, n):
    """목록에서 완료 체크·상태 바꾸기. 목록이 다시 그려질 때도 불리지만 DB 와 같은 값이면 아무것도 안 한다."""
    trig = ctx.triggered_id
    if not isinstance(trig, dict) or not ctx.triggered:
        return no_update
    t = store.get_task(trig['id'])
    value = ctx.triggered[0]['value']
    if t is None or value is None:
        return no_update
    if trig['type'] == 'home-done':
        changed = store.set_todo_done(t['id'], bool(value), by=auth.current_name())
    else:
        changed = value != t['status']
        if changed:
            store.update_task(t['id'], author=auth.current_name(), status=value)
    return (n or 0) + 1 if changed else no_update


@callback(Output('home-main-col', 'lg'), Output('home-side-col', 'style'), Output('gantt-controls', 'style'),
          Output('task-list', 'style'), Output('home-mine', 'style'), Output('home-depth-box', 'style'),
          Output('home-show-done', 'style'), Output('tp-show-done', 'style'), Output('f-tag-col', 'style'),
          Output('home-todos-card', 'style'),
          Input('home-view', 'value'))
def switch_view(view):
    """간트는 넓게 — 오른쪽 '내 업무·빠른 기록·최근 이력'을 접고, 주·월·분기·전체와 앞뒤 이동을 보인다.
    내 업무 보기는 하위 업무 펼치기·태그 대신 '최근 7일 완료 보기', 오른쪽 '내 업무' 카드는 겹치므로 접는다."""
    show, hide = {}, {'display': 'none'}
    mine, gantt_ = view == 'mine', view == 'gantt'
    return (12 if gantt_ else 8, hide if gantt_ else show, {'display': 'flex'} if gantt_ else hide,
            hide if mine else show, show if mine else hide, hide if mine else {'display': 'flex'},
            hide if mine else show, show if mine else hide, hide if mine else show, hide if mine else show)


@callback(Output('gantt-offset', 'data'), Output('gantt-nav', 'style'),
          Input('gantt-prev', 'n_clicks'), Input('gantt-next', 'n_clicks'), Input('gantt-today', 'n_clicks'),
          Input('gantt-scale', 'value'), State('gantt-offset', 'data'))
def move_gantt(_p, _n, _t, scale, offset):
    """한 화면씩 앞뒤로 (보기를 바꾸거나 '오늘'이면 처음으로). '전체'는 옮길 것이 없다."""
    nav = {'display': 'none'} if scale == 'all' else {}
    trig = ctx.triggered_id
    if trig == 'gantt-prev':
        return (offset or 0) - 1, nav
    if trig == 'gantt-next':
        return (offset or 0) + 1, nav
    return 0, nav


@callback(Output('gantt-panel', 'is_open'), Output('gantt-panel', 'title'), Output('gantt-panel', 'children'),
          Input({'type': 'gantt-task', 'id': ALL}, 'n_clicks'), prevent_initial_call=True)
def open_task_panel(_):
    """간트에서 업무 이름을 누르면 — 그 업무의 AI 요약(하위 업무·이력·참고자료)과 하위 업무, 업무 화면으로 가는 길."""
    trig = ctx.triggered_id
    if not trig or not (ctx.triggered and ctx.triggered[0]['value']):
        return no_update, no_update, no_update
    t = store.get_task(trig['id'])
    if t is None:
        return no_update, no_update, no_update
    kids = store.child_tasks(t['id'])
    body = [
        html.Div([ui.status_badge(t['status']),
                  html.Span(' · '.join(x for x in [t['start_date'] and f"시작 {t['start_date']}",
                                                   t['due_date'] and f"마감 {t['due_date']}"] if x),
                            className='small text-muted ms-1'),
                  dcc.Link([html.I(className='bi bi-box-arrow-up-right me-1'), '업무 열기'], href=f"/task/{t['id']}",
                           className='small ms-auto')], className='d-flex align-items-center mb-3'),
        task_summary_ui.card(t['id']),
        html.Div([html.Div(f'하위 업무 {len(kids)}', className='small fw-semibold mb-1'),
                  dbc.ListGroup([dbc.ListGroupItem([ui.status_badge(k['status']), ui.task_link(k)], className='small py-1')
                                 for k in kids], flush=True)]) if kids else None,
    ]
    return True, f"#{t['id']} {t['title']}", body
