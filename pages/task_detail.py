"""업무 상세: 개요, 이력 타임라인, 참고자료, 연결 업무, 하위 업무."""
import base64

import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, no_update

from components import ui
from services import store

dash.register_page(__name__, path_template='/task/<task_id>', name='업무 상세', title='업무 기록 · 상세')


def _opts(values):
    return [{'label': v, 'value': v} for v in values]


def _edit_modal():
    return dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle('업무 수정')),
        dbc.ModalBody([
            dbc.Label('제목'), dbc.Input(id='ed-title', className='mb-2'),
            dbc.Label('설명'), dbc.Textarea(id='ed-desc', rows=5, className='mb-2'),
            dbc.Row([
                dbc.Col([dbc.Label('담당자'), dbc.Input(id='ed-owner')]),
                dbc.Col([dbc.Label('태그 (쉼표 구분)'), dbc.Input(id='ed-tags')]),
            ], className='mb-2'),
            dbc.Row([
                dbc.Col([dbc.Label('시작일'), dbc.Input(id='ed-start', type='date')]),
                dbc.Col([dbc.Label('마감일'), dbc.Input(id='ed-due', type='date')]),
            ], className='mb-2'),
            dbc.Label('상위 업무'), dcc.Dropdown(id='ed-parent', placeholder='없음'),
            dbc.Alert(id='ed-error', color='danger', is_open=False, className='mt-3 mb-0'),
        ]),
        dbc.ModalFooter([
            dbc.Button('삭제', id='ed-delete', color='danger', outline=True, className='me-auto'),
            dbc.Button('취소', id='ed-cancel', color='secondary', outline=True),
            dbc.Button('저장', id='ed-save', color='primary'),
        ]),
    ], id='ed-modal', is_open=False, size='lg')


def _history_form():
    return dbc.Card(dbc.CardBody([
        dbc.Row([
            dbc.Col(dbc.Select(id='h-type', value='메모', options=_opts(store.ENTRY_TYPES)), xs=6, md=3),
            dbc.Col(dbc.Input(id='h-when', type='datetime-local'), xs=6, md=4),
        ], className='g-2 mb-2'),
        dbc.Textarea(id='h-content', rows=3, placeholder='무슨 일이 있었나요? (회의 내용, 결정 사항, 진행 상황, 이슈 등)',
                     className='mb-2'),
        html.Div([
            html.Span('일시를 비워두면 지금 시각으로 기록됩니다.', className='small text-muted me-auto'),
            dbc.Button('이력 추가', id='h-add', color='primary', size='sm'),
        ], className='d-flex align-items-center'),
        dbc.Alert(id='h-error', color='danger', is_open=False, className='mt-2 mb-0 py-2'),
    ]), className='mb-3')


def _reference_form():
    return dbc.Card(dbc.CardBody([
        dbc.RadioItems(id='r-kind', value='link', inline=True, className='mb-2',
                       options=[{'label': '링크', 'value': 'link'}, {'label': '파일', 'value': 'file'},
                                {'label': '메모', 'value': 'note'}]),
        dbc.Input(id='r-title', placeholder='제목 (비우면 자동)', className='mb-2'),
        dbc.Input(id='r-url', placeholder='https://…', className='mb-2'),
        dcc.Upload(id='r-file', children=html.Div(['파일을 끌어다 놓거나 ', html.A('선택')]),
                   className='mb-2 border rounded text-center small py-3', style={'borderStyle': 'dashed'},
                   max_size=50 * 1024 * 1024),
        html.Div(id='r-file-name', className='small text-muted mb-2'),
        dbc.Textarea(id='r-note', rows=3, placeholder='설명 / 메모', className='mb-2'),
        html.Div(dbc.Button('자료 추가', id='r-add', color='primary', size='sm'), className='text-end'),
        dbc.Alert(id='r-error', color='danger', is_open=False, className='mt-2 mb-0 py-2'),
    ]), className='mb-3')


def _link_form():
    return dbc.Card(dbc.CardBody([
        dbc.Row([
            dbc.Col(dbc.Select(id='l-rel', value='관련', options=_opts(store.RELATIONS)), xs=4, md=3),
            dbc.Col(dcc.Dropdown(id='l-target', placeholder='연결할 업무 선택'), xs=8, md=9),
        ], className='g-2 mb-2'),
        dbc.Input(id='l-note', placeholder='연결 사유 (선택)', className='mb-2'),
        html.Div([
            html.Span('"선행"은 이 업무가 선택한 업무보다 먼저라는 뜻입니다.', className='small text-muted me-auto'),
            dbc.Button('연결', id='l-add', color='primary', size='sm'),
        ], className='d-flex align-items-center'),
        dbc.Alert(id='l-error', color='danger', is_open=False, className='mt-2 mb-0 py-2'),
    ]), className='mb-3')


def layout(task_id=None, **_):
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        tid = None
    if tid is None or store.get_task(tid) is None:
        return dbc.Alert(['업무를 찾을 수 없습니다. ', dcc.Link('목록으로', href='/')], color='warning')

    return html.Div([
        dcc.Store(id='td-id', data=tid),
        dcc.Store(id='td-refresh', data=0),
        dcc.Location(id='td-redirect', refresh='callback-nav'),
        _edit_modal(),
        html.Div(id='td-header', className='mb-3'),
        dbc.Row([
            dbc.Col([
                dbc.Tabs([
                    dbc.Tab([_history_form(), html.Div(id='td-history')], label='이력', tab_id='history'),
                    dbc.Tab([_reference_form(), html.Div(id='td-refs')], label='참고자료', tab_id='refs'),
                    dbc.Tab([_link_form(), html.Div(id='td-links')], label='연결 업무', tab_id='links'),
                ], id='td-tabs', active_tab='history', className='mb-3'),
            ], lg=8),
            dbc.Col(html.Div(id='td-side'), lg=4),
        ]),
    ])


# ── 렌더링 ────────────────────────────────────────────────────────────

@callback(Output('td-header', 'children'), Output('td-side', 'children'), Output('td-history', 'children'),
          Output('td-refs', 'children'), Output('td-links', 'children'), Output('l-target', 'options'),
          Input('td-refresh', 'data'), State('td-id', 'data'))
def render(_, tid):
    t = store.get_task(tid)
    if t is None:
        return dbc.Alert('삭제된 업무입니다.', color='warning'), None, None, None, None, []
    parent = store.get_task(t['parent_id']) if t['parent_id'] else None

    header = dbc.Card(dbc.CardBody([
        html.Div([
            html.H4([html.Span(f"#{t['id']} ", className='text-muted'), t['title']], className='mb-0 me-auto'),
            dbc.Select(id='td-status', value=t['status'], options=_opts(store.STATUSES),
                       style={'width': '110px'}, className='me-2'),
            dbc.Button([html.I(className='bi bi-pencil me-1'), '수정'], id='ed-open', color='secondary',
                       outline=True, size='sm'),
        ], className='d-flex align-items-center gap-2 mb-2'),
        html.Div([ui.status_badge(t['status']), *ui.tag_badges(t['tags'])], className='mb-2'),
        ui.multiline(t['description']) if t['description'] else html.Div('설명 없음', className='text-muted small'),
    ]))

    def row(label, value):
        return html.Tr([html.Th(label, className='text-muted fw-normal small', style={'width': '35%'}),
                        html.Td(value or '-', className='small')])

    children = store.child_tasks(tid)
    side = [
        dbc.Card(dbc.CardBody(html.Table([html.Tbody([
            row('담당자', t['owner']), row('시작일', t['start_date']), row('마감일', t['due_date']),
            row('상위 업무', ui.task_link(parent) if parent else None),
            row('등록', t['created_at']), row('최종 수정', t['updated_at']),
        ])], className='table table-sm mb-0')), className='mb-3'),
        dbc.Card([
            dbc.CardHeader(f'하위 업무 ({len(children)})', className='small'),
            dbc.ListGroup([dbc.ListGroupItem([ui.status_badge(c['status']), ui.task_link(c)]) for c in children],
                          flush=True) if children else dbc.CardBody('없음', className='small text-muted'),
        ]),
    ]

    hist = store.list_history(tid)
    history = dbc.ListGroup([
        dbc.ListGroupItem([
            html.Div([
                html.I(className=f"bi {ui.ENTRY_ICON.get(h['entry_type'], 'bi-dot')} me-2"),
                dbc.Badge(h['entry_type'], color='dark' if h['entry_type'] in store.ENTRY_TYPES else 'light',
                          text_color=None if h['entry_type'] in store.ENTRY_TYPES else 'dark', className='me-2'),
                html.Span(h['occurred_at'], className='small text-muted me-2'),
                html.Span(h['author'], className='small me-auto'),
                dbc.Button(html.I(className='bi bi-x'), id={'type': 'h-del', 'id': h['id']}, color='link',
                           size='sm', className='text-muted p-0', title='삭제'),
            ], className='d-flex align-items-center mb-1'),
            ui.multiline(h['content']),
        ]) for h in hist
    ]) if hist else html.Div('이력이 없습니다.', className='text-muted small')

    refs = store.list_references(task_id=tid)
    ref_view = dbc.ListGroup([
        dbc.ListGroupItem([
            html.Div([
                html.I(className=f"bi {ui.REF_ICON[r['kind']]} me-2"),
                html.Span(r['title'], className='fw-semibold me-auto'),
                html.Span(r['created_at'], className='small text-muted me-2'),
                dbc.Button(html.I(className='bi bi-x'), id={'type': 'r-del', 'id': r['id']}, color='link',
                           size='sm', className='text-muted p-0', title='삭제'),
            ], className='d-flex align-items-center'),
            ui.ref_target(r),
            html.Div(r['note'], className='small mt-1', style={'whiteSpace': 'pre-wrap'}) if r['note'] else None,
        ]) for r in refs
    ]) if refs else html.Div('참고자료가 없습니다.', className='text-muted small')

    links = store.linked_tasks(tid)
    link_view = dbc.ListGroup([
        dbc.ListGroupItem([
            html.Div([
                dbc.Badge(l['relation'], color='info', className='me-2'),
                ui.status_badge(l['status']), ui.task_link(l),
                html.Span(className='me-auto'),
                dbc.Button(html.I(className='bi bi-x'), id={'type': 'l-del', 'id': l['link_id']}, color='link',
                           size='sm', className='text-muted p-0', title='연결 해제'),
            ], className='d-flex align-items-center'),
            html.Div(l['note'], className='small text-muted mt-1') if l['note'] else None,
        ]) for l in links
    ]) if links else html.Div('연결된 업무가 없습니다.', className='text-muted small')

    linked_ids = {l['id'] for l in links} | {tid}
    targets = [{'label': f"#{o['id']} {o['title']}", 'value': o['id']}
               for o in store.list_tasks() if o['id'] not in linked_ids]
    return header, side, history, ref_view, link_view, targets


# ── 상태 변경 ─────────────────────────────────────────────────────────

@callback(Output('td-refresh', 'data', allow_duplicate=True), Input('td-status', 'value'),
          State('td-id', 'data'), State('author-store', 'data'), State('td-refresh', 'data'),
          prevent_initial_call=True)
def change_status(status, tid, author, n):
    t = store.get_task(tid)
    if not t or not status or status == t['status']:
        return no_update
    store.update_task(tid, author=author or '', status=status)
    return (n or 0) + 1


# ── 업무 수정/삭제 ─────────────────────────────────────────────────────

@callback(Output('ed-modal', 'is_open'), Output('ed-title', 'value'), Output('ed-desc', 'value'),
          Output('ed-owner', 'value'), Output('ed-tags', 'value'), Output('ed-start', 'value'),
          Output('ed-due', 'value'), Output('ed-parent', 'value'), Output('ed-parent', 'options'),
          Output('ed-error', 'children'), Output('ed-error', 'is_open'),
          Output('td-refresh', 'data', allow_duplicate=True), Output('td-redirect', 'pathname'),
          Input('ed-open', 'n_clicks'), Input('ed-cancel', 'n_clicks'), Input('ed-save', 'n_clicks'),
          Input('ed-delete', 'n_clicks'),
          State('td-id', 'data'), State('ed-title', 'value'), State('ed-desc', 'value'),
          State('ed-owner', 'value'), State('ed-tags', 'value'), State('ed-start', 'value'),
          State('ed-due', 'value'), State('ed-parent', 'value'), State('author-store', 'data'),
          State('td-refresh', 'data'), prevent_initial_call=True)
def edit_task(n_open, _c, _s, _d, tid, title, desc, owner, tags, start, due, parent, author, n):
    nu = no_update
    trig = ctx.triggered_id
    if trig == 'ed-open':
        if not n_open:  # 헤더가 다시 그려지며 버튼이 새로 생길 때의 빈 트리거
            return (nu,) * 13
        t = store.get_task(tid)
        opts = [{'label': f"#{o['id']} {o['title']}", 'value': o['id']} for o in store.list_tasks() if o['id'] != tid]
        return (True, t['title'], t['description'], t['owner'], ', '.join(t['tags']), t['start_date'] or None,
                t['due_date'] or None, t['parent_id'], opts, '', False, nu, nu)
    if trig == 'ed-cancel':
        return (False,) + (nu,) * 8 + ('', False, nu, nu)
    if trig == 'ed-delete':
        store.delete_task(tid)
        return (False,) + (nu,) * 8 + ('', False, nu, '/')
    try:
        store.update_task(tid, author=author or '', title=title, description=desc or '', owner=owner or '',
                          tags=tags or '', start_date=start or '', due_date=due or '', parent_id=parent)
    except ValueError as e:
        return (True,) + (nu,) * 8 + (str(e), True, nu, nu)
    return (False,) + (nu,) * 8 + ('', False, (n or 0) + 1, nu)


# ── 이력 ─────────────────────────────────────────────────────────────

@callback(Output('td-refresh', 'data', allow_duplicate=True), Output('h-content', 'value'),
          Output('h-when', 'value'), Output('h-error', 'children'), Output('h-error', 'is_open'),
          Input('h-add', 'n_clicks'), State('td-id', 'data'), State('h-type', 'value'),
          State('h-content', 'value'), State('h-when', 'value'), State('author-store', 'data'),
          State('td-refresh', 'data'), prevent_initial_call=True)
def add_history(_, tid, etype, content, when, author, n):
    try:
        store.add_history(tid, content, etype, author or '', when or None)
    except ValueError as e:
        return no_update, no_update, no_update, str(e), True
    return (n or 0) + 1, '', None, '', False


@callback(Output('td-refresh', 'data', allow_duplicate=True), Input({'type': 'h-del', 'id': ALL}, 'n_clicks'),
          Input({'type': 'r-del', 'id': ALL}, 'n_clicks'), Input({'type': 'l-del', 'id': ALL}, 'n_clicks'),
          State('td-refresh', 'data'), prevent_initial_call=True)
def delete_item(_h, _r, _l, n):
    trig = ctx.triggered_id
    if not trig or not ctx.triggered[0]['value']:  # 목록이 새로 그려질 때(n_clicks=None) 무시
        return no_update
    {'h-del': store.delete_history, 'r-del': store.delete_reference,
     'l-del': store.unlink_tasks}[trig['type']](trig['id'])
    return (n or 0) + 1


# ── 참고자료 ──────────────────────────────────────────────────────────

@callback(Output('r-url', 'style'), Output('r-file', 'style'), Output('r-file-name', 'children'),
          Input('r-kind', 'value'), Input('r-file', 'filename'))
def toggle_ref_inputs(kind, fname):
    hide = {'display': 'none'}
    return (None if kind == 'link' else hide,
            {'borderStyle': 'dashed'} if kind == 'file' else hide,
            f'선택된 파일: {fname}' if kind == 'file' and fname else '')


@callback(Output('td-refresh', 'data', allow_duplicate=True), Output('r-title', 'value'), Output('r-url', 'value'),
          Output('r-note', 'value'), Output('r-file', 'contents'), Output('r-file', 'filename'),
          Output('r-error', 'children'), Output('r-error', 'is_open'),
          Input('r-add', 'n_clicks'), State('td-id', 'data'), State('r-kind', 'value'), State('r-title', 'value'),
          State('r-url', 'value'), State('r-note', 'value'), State('r-file', 'contents'),
          State('r-file', 'filename'), State('author-store', 'data'), State('td-refresh', 'data'),
          prevent_initial_call=True)
def add_reference(_, tid, kind, title, url, note, contents, fname, author, n):
    data = None
    if kind == 'file' and contents:
        data = base64.b64decode(contents.split(',', 1)[1])
    try:
        store.add_reference(tid, kind, title or '', url or '', note or '', fname or '', data, author or '')
    except ValueError as e:
        return (no_update,) * 6 + (str(e), True)
    return (n or 0) + 1, '', '', '', None, None, '', False


# ── 업무 연결 ─────────────────────────────────────────────────────────

@callback(Output('td-refresh', 'data', allow_duplicate=True), Output('l-target', 'value'), Output('l-note', 'value'),
          Output('l-error', 'children'), Output('l-error', 'is_open'),
          Input('l-add', 'n_clicks'), State('td-id', 'data'), State('l-target', 'value'), State('l-rel', 'value'),
          State('l-note', 'value'), State('author-store', 'data'), State('td-refresh', 'data'),
          prevent_initial_call=True)
def add_link(_, tid, target, rel, note, author, n):
    if not target:
        return no_update, no_update, no_update, '연결할 업무를 선택하세요.', True
    try:
        store.link_tasks(tid, int(target), rel, note or '', author or '')
    except ValueError as e:
        return no_update, no_update, no_update, str(e), True
    return (n or 0) + 1, None, '', '', False
