"""임원 한 사람의 기록 모음 — 동향 기록하기, 연결된 모든 이력(동향·업무 기록) 타임라인, 자주 나온 키워드."""
import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, ctx, dcc, html, no_update

from components import ui
from components.history_form import decode_text_upload, history_form, register_form_callbacks, save_history
from pages.people import person_form
from services import auth, store

dash.register_page(__name__, path_template='/people/<person_id>', name='임원 상세', title='업무 기록 · 임원')


def layout(person_id=None, **_):
    try:
        pid = int(person_id)
    except (TypeError, ValueError):
        pid = None
    if pid is None or store.get_person(pid) is None:
        return dbc.Alert(['임원을 찾을 수 없습니다. ', dcc.Link('임원 목록으로', href='/people')], color='warning')

    form = history_form('pp', people_value=[pid], default_type='동향')
    return html.Div([
        dcc.Store(id='pp-id', data=pid),
        dcc.Store(id='pp-refresh', data=0),
        dcc.Store(id='pp-pending', data=[]),
        dcc.Interval(id='pp-poll', interval=3000, disabled=True),
        dcc.Location(id='pp-redirect', refresh='callback-nav'),
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle('임원 정보 수정')),
            dbc.ModalBody(person_form('pe')),
            dbc.ModalFooter([
                dbc.Button('삭제', id='pe-delete', color='danger', outline=True, className='me-auto'),
                dbc.Button('취소', id='pe-cancel', color='secondary', outline=True),
                dbc.Button('저장', id='pe-save', color='primary'),
            ]),
        ], id='pe-modal', is_open=False, size='lg'),
        html.Div(id='pp-header', className='mb-3'),
        dbc.Row([
            dbc.Col([
                dbc.Card(dbc.CardBody([
                    html.Div([html.I(className='bi bi-binoculars me-1'), '이 임원에 대해 기록'],
                             className='fw-semibold mb-2'),
                    dcc.Dropdown(id='pp-task', placeholder='관련 업무 (동향이면 비워 두세요)', className='mb-2'),
                    *form,
                ]), className='mb-3'),
                html.Div(id='pp-count', className='small text-muted mb-2'),
                html.Div(id='pp-history'),
            ], lg=8),
            dbc.Col(html.Div(id='pp-side'), lg=4),
        ]),
    ])


register_form_callbacks('pp')


@callback(Output('pp-header', 'children'), Output('pp-history', 'children'), Output('pp-count', 'children'),
          Output('pp-side', 'children'), Output('pp-task', 'options'), Output('pp-pending', 'data'),
          Output('pp-poll', 'disabled'),
          Input('pp-refresh', 'data'), Input('history-changed', 'data'), Input('todo-changed', 'data'),
          State('pp-id', 'data'))
def render(_, _h, _t, pid):
    p = store.get_person(pid)
    if p is None:
        return dbc.Alert('삭제된 임원입니다.', color='warning'), None, '', None, [], [], True
    header = dbc.Card(dbc.CardBody([
        html.Div([
            html.H4([html.I(className='bi bi-person-badge me-2'), p['name'],
                     html.Span(f" {p['position']}", className='text-muted')], className='mb-0 me-auto'),
            dbc.Badge('추적 중지', color='secondary', className='me-2') if not p['active'] else None,
            dbc.Button('추적 재개' if not p['active'] else '추적 중지', id='pp-toggle-active', color='secondary',
                       outline=True, size='sm', className='me-2',
                       title='추적 중지하면 목록과 자동 연결에서 빠집니다 (기록은 그대로)'),
            dbc.Button([html.I(className='bi bi-pencil me-1'), '수정'], id='pe-open', color='secondary',
                       outline=True, size='sm'),
        ], className='d-flex align-items-center flex-wrap gap-1 mb-1'),
        html.Div(p['org'], className='text-muted') if p['org'] else None,
        _hr_line(p),
        html.Div([dbc.Badge('People in SAIT', color='info', className='me-1'),
                  "불러오기 때 People in SAIT 값과 비교해 바뀐 점을 제안하고 경력 이력을 쌓음 "
                  f"(최근 {p['synced_at'][:10]})"], className='small text-muted')
        if p['source_id'] else None,
        html.Div('별칭: ' + ', '.join(p['aliases']), className='small text-muted') if p['aliases'] else None,
        ui.multiline(p['note']) if p['note'] else None,
    ]))

    hist = store.search_history('', person_id=pid, limit=500)
    timeline = dbc.ListGroup([
        ui.history_card(h, show_task=True, added_todos=store.todo_titles_for_history(h['id']) if h['ai_todos'] else None)
        for h in hist]) if hist else html.Div('이 임원과 연결된 기록이 없습니다.', className='text-muted small')
    n_trend = sum(h['entry_type'] == '동향' for h in hist)
    count = f'기록 {len(hist)}건 (동향 {n_trend}건)' if hist else ''

    tasks = {}
    for h in hist:
        if h['task_id']:
            tasks.setdefault(h['task_id'], h['task_title'])
    kw = store.keyword_stats(25, person_id=pid)
    side = [
        dbc.Card([
            dbc.CardHeader('자주 나온 키워드', className='small'),
            dbc.CardBody(html.Div([
                dcc.Link(f'#{k} {n}' if n > 1 else f'#{k}', href=ui.search_href(k),
                         className='badge rounded-pill text-bg-light border text-decoration-none me-1 mb-1')
                for k, n in kw]) if kw else html.Div('없음', className='small text-muted')),
        ], className='mb-3'),
        _career_card(store.list_career(pid)),
        dbc.Card([
            dbc.CardHeader(f'관련 업무 ({len(tasks)})', className='small'),
            dbc.ListGroup([dbc.ListGroupItem(dcc.Link(f'#{tid} {title}', href=f'/task/{tid}'), className='small')
                           for tid, title in tasks.items()], flush=True)
            if tasks else dbc.CardBody('없음', className='small text-muted'),
        ]),
    ]
    task_opts = [{'label': f"#{t['id']} {t['title']}", 'value': t['id']} for t in store.list_tasks()]
    pending = sorted(h['id'] for h in hist if h['ai_status'] == 'pending')
    return header, timeline, count, side, task_opts, pending, not pending


def _hr_line(p: dict):
    """인사 정보 한 줄 — 사번·재직상태·연차·나이·최종학력·입사일·근속 (People in SAIT 에서 불러온 값)."""
    parts = [
        ('사번', p['source_id']),
        ('재직상태', '퇴직·전출' if p['hr_state'] == 'former' else p['employment_status']),
        ('연차', f"{p['grade_years']}년차" if p['grade_years'] is not None else ''),
        ('나이', f"{p['age']}세 ({p['birth_year']}년생)" if p['age'] is not None else ''),
        ('최종학력', p['education']),
        ('입사일', p['hire_date']),
        ('근속', f"{p['tenure']}년" if p['tenure'] is not None else ''),
    ]
    parts = [(k, v) for k, v in parts if v]
    if not parts:
        return None
    return html.Div([html.Span([html.Span(k, className='text-muted me-1'), v], className='me-3 text-nowrap')
                     for k, v in parts], className='small d-flex flex-wrap my-1')


def _career_card(career: list[dict]):
    """People in SAIT 에서 쌓은 경력 이력 — 직급·소속이 바뀐 달과 인사발령. 없으면 카드를 안 보인다."""
    if not career:
        return None
    rows = []
    for c in career:
        if c['kind'] == '발령':
            body = [html.Span(c['title'] or '발령', className='fw-semibold me-1'),
                    ' · '.join(x for x in (c['org'], c['position'], c['assignment']) if x)]
        else:
            body = ' · '.join(x for x in (c['position'], c['org']) if x)
        rows.append(dbc.ListGroupItem([
            html.Div([html.Span(c['date'], className='text-muted me-2'),
                      dbc.Badge(c['kind'], color='light', text_color='dark', className='border')],
                     className='small'),
            html.Div(body, className='small'),
        ], className='py-1'))
    return dbc.Card([
        dbc.CardHeader(f'경력 이력 ({len(career)}) · People in SAIT', className='small'),
        html.Div(dbc.ListGroup(rows, flush=True), style={'maxHeight': '360px', 'overflowY': 'auto'}),
    ], className='mb-3')


@callback(Output('pp-refresh', 'data', allow_duplicate=True), Input('pp-poll', 'n_intervals'),
          State('pp-id', 'data'), State('pp-pending', 'data'), State('pp-refresh', 'data'),
          prevent_initial_call=True)
def poll_ai(_, pid, pending, n):
    if sorted(store.pending_ai_ids(person_id=pid)) == sorted(pending or []):
        return no_update
    return (n or 0) + 1


@callback(Output('pp-content', 'value', allow_duplicate=True), Input('pp-file', 'contents'),
          State('pp-content', 'value'), prevent_initial_call=True)
def load_text_file(contents, current):
    if not contents:
        return no_update
    text = decode_text_upload(contents)
    return f'{current.rstrip()}\n\n{text}' if (current or '').strip() else text


@callback(Output('pp-refresh', 'data', allow_duplicate=True), Output('pp-content', 'value'),
          Output('pp-when', 'value'), Output('pp-links', 'value'), Output('pp-attach', 'contents'),
          Output('pp-attach', 'filename'), Output('pp-attach-names', 'children', allow_duplicate=True),
          Output('pp-error', 'children'), Output('pp-error', 'is_open'), Output('pp-people', 'value'),
          Input('pp-add', 'n_clicks'), State('pp-id', 'data'), State('pp-task', 'value'), State('pp-type', 'value'),
          State('pp-content', 'value'), State('pp-when', 'value'), State('pp-ai', 'value'),
          State('pp-links', 'value'), State('pp-attach', 'contents'), State('pp-attach', 'filename'),
          State('pp-people', 'value'), State('pp-refresh', 'data'), prevent_initial_call=True)
def add_record(_, pid, task_id, etype, content, when, use_ai, links, att, att_names, people, n):
    people = sorted(set(people or []) | {pid})  # 이 화면에서 쓴 기록은 항상 이 임원에게 연결
    try:
        save_history(task_id or None, etype, content, when, use_ai, links, att, att_names, auth.current_name(),
                     people)
    except ValueError as e:
        return (no_update,) * 7 + (str(e), True, no_update)
    return (n or 0) + 1, '', None, '', None, None, '', '', False, [pid]


@callback(Output('pe-modal', 'is_open'), Output('pe-name', 'value'), Output('pe-position', 'value'),
          Output('pe-org', 'value'), Output('pe-aliases', 'value'), Output('pe-note', 'value'),
          Output('pe-error', 'children'), Output('pe-error', 'is_open'),
          Output('pp-refresh', 'data', allow_duplicate=True), Output('pp-redirect', 'pathname'),
          Input('pe-open', 'n_clicks'), Input('pe-cancel', 'n_clicks'), Input('pe-save', 'n_clicks'),
          Input('pe-delete', 'n_clicks'), Input('pp-toggle-active', 'n_clicks'),
          State('pp-id', 'data'), State('pe-name', 'value'), State('pe-position', 'value'), State('pe-org', 'value'),
          State('pe-aliases', 'value'), State('pe-note', 'value'), State('pp-refresh', 'data'),
          prevent_initial_call=True)
def edit_person(n_open, _c, _s, _d, n_toggle, pid, name, position, org, aliases, note, n):
    nu = no_update
    trig = ctx.triggered_id
    if trig is None or (trig in ('pe-open', 'pp-toggle-active') and not ctx.triggered[0]['value']):
        return (nu,) * 10  # 머리말이 다시 그려지며 버튼이 새로 생길 때의 빈 트리거
    p = store.get_person(pid)
    if trig == 'pe-open':
        return True, p['name'], p['position'], p['org'], ', '.join(p['aliases']), p['note'], '', False, nu, nu
    if trig == 'pe-cancel':
        return (False,) + (nu,) * 5 + ('', False, nu, nu)
    if trig == 'pp-toggle-active':
        store.update_person(pid, active=not p['active'])
        return (nu,) * 8 + ((n or 0) + 1, nu)
    if trig not in ('pe-save', 'pe-delete'):
        return (nu,) * 10
    try:
        if trig == 'pe-delete':
            store.delete_person(pid)
            return (False,) + (nu,) * 5 + ('', False, nu, '/people')
        store.update_person(pid, name=name, position=position or '', org=org or '', aliases=aliases or '',
                            note=note or '')
    except ValueError as e:
        return (True,) + (nu,) * 5 + (str(e), True, nu, nu)
    return (False,) + (nu,) * 5 + ('', False, (n or 0) + 1, nu)
