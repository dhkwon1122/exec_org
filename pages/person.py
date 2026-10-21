"""임원 한 사람의 기록 모음 — 동향 기록하기, 연결된 모든 이력(동향·업무 기록) 타임라인, 자주 나온 키워드."""
from datetime import date

import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, ctx, dash_table, dcc, html, no_update

from components import absence_ui, ui
from components.history_form import decode_text_upload, history_form, register_form_callbacks, save_history
from pages.people import person_form
from services import absences, auth, llm, profile, store

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
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle('주요 이력 정리')),
            dbc.ModalBody([
                html.Div([
                    dbc.Button([html.I(className='bi bi-magic me-1'),
                                'AI로 발령이력 정리' if llm.is_configured() else '발령이력으로 초안 만들기'],
                               id='hl-draft', color='secondary', outline=True, size='sm', className='me-2'),
                    dbc.Button([html.I(className='bi bi-plus me-1'), '줄 추가'], id='hl-add-row', color='secondary',
                               outline=True, size='sm'),
                ], className='mb-2'),
                dcc.Loading(dbc.Alert(id='hl-msg', is_open=False, color='info', className='small py-2'), type='dot'),
                dash_table.DataTable(
                    id='hl-table', editable=True, row_deletable=True, data=[],
                    columns=[{'name': '날짜 (YYYY.MM)', 'id': 'date'}, {'name': '주요 이력', 'id': 'text'}],
                    style_table={'maxHeight': '50vh', 'overflowY': 'auto'},
                    style_cell={'textAlign': 'left', 'fontSize': '0.9rem', 'whiteSpace': 'normal', 'height': 'auto',
                                'fontFamily': 'inherit'},
                    style_cell_conditional=[{'if': {'column_id': 'date'}, 'width': '130px'}]),
                html.Div('초안은 경력 이력(People in SAIT 인사발령·직급/소속 변천)에서 만듭니다. 칸을 눌러 고치고, 줄 끝 × 로 지운 뒤 '
                         '저장하면 DB 에 남습니다. 저장할 때 날짜순으로 정렬됩니다.', className='small text-muted mt-2'),
            ]),
            dbc.ModalFooter([dbc.Button('취소', id='hl-cancel', color='secondary', outline=True),
                             dbc.Button('저장', id='hl-save', color='primary')]),
        ], id='hl-modal', is_open=False, size='lg'),
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle('평가 이력')),
            dbc.ModalBody([
                dbc.Button([html.I(className='bi bi-plus me-1'), '줄 추가'], id='ev-add-row', color='secondary',
                           outline=True, size='sm', className='mb-2'),
                dash_table.DataTable(
                    id='ev-table', editable=True, row_deletable=True, data=[],
                    columns=[{'name': '연도', 'id': 'year', 'type': 'numeric'},
                             {'name': '평가 종류', 'id': 'kind', 'presentation': 'dropdown'},
                             {'name': '등급·결과', 'id': 'grade'}, {'name': '메모', 'id': 'note'}],
                    dropdown={'kind': {'options': []}},
                    style_table={'maxHeight': '50vh', 'overflowY': 'auto'},
                    style_cell={'textAlign': 'left', 'fontSize': '0.9rem', 'fontFamily': 'inherit'}),
                html.Div('해마다 임원종합평가·MBO 를 한 줄씩. 여러 임원을 한꺼번에 넣을 때는 임원 목록의 "엑셀 업로드"를 쓰세요.',
                         className='small text-muted mt-2'),
                dbc.Alert(id='ev-error', color='danger', is_open=False, className='small py-2 mt-2 mb-0'),
            ]),
            dbc.ModalFooter([dbc.Button('취소', id='ev-cancel', color='secondary', outline=True),
                             dbc.Button('저장', id='ev-save', color='primary')]),
        ], id='ev-modal', is_open=False, size='lg'),
        html.Details([html.Summary('임원 프로필 · 평가 · 경력', className='composer-toggle'), html.Div(id='pp-profile')], className='mb-3'),
        dbc.Row([
            dbc.Col([
                html.Details([html.Summary('이 임원에 대해 기록 추가', className='composer-toggle'), dbc.Card(dbc.CardBody([
                    html.Div([html.I(className='bi bi-binoculars me-1'), '이 임원에 대해 기록'],
                             className='fw-semibold mb-2'),
                    dcc.Dropdown(id='pp-task', multi=True, className='mb-2',
                                 placeholder='관련 업무 — 여러 업무에 걸치면 모두 (동향이면 비워 두세요)'),
                    *form,
                ]), className='mt-2')], className='record-composer mb-3'),
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
    timeline = ui.history_stream(hist, show_task=True) if hist else html.Div('이 임원과 연결된 기록이 없습니다.', className='text-muted small')
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
        _absence_card(pid),
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


def _absence_card(pid: int):
    """부재 — 지금·앞으로의 부재 먼저, 그다음 최근 지난 부재 (캘린더의 '임원 부재' 탭에서 등록)."""
    rows = absences.for_person(pid, limit=8)
    today = date.today().isoformat()
    items = [dbc.ListGroupItem([
        html.Span(r['kind'], className='badge me-2',
                  style={'background': absence_ui.KIND_COLOR.get(r['kind'], '#6b7280'), 'color': '#fff'}),
        html.Span(absence_ui._period(r)),
        dbc.Badge('부재 중', color='danger', className='ms-2') if r['start_date'] <= today <= r['end_date'] else None,
        html.Div(' · '.join(x for x in [r['place'], f"대결 {r['deputy']}" if r['deputy'] else ''] if x),
                 className='text-muted') if r['place'] or r['deputy'] else None,
    ], className='small', style={'opacity': .6} if r['end_date'] < today else None) for r in rows]
    return dbc.Card([
        dbc.CardHeader(html.Div([html.Span('부재 일정'), dcc.Link('캘린더에서 등록', href='/calendar?tab=absence',
                                                              className='ms-auto small')], className='d-flex'),
                       className='small'),
        dbc.ListGroup(items, flush=True) if items else dbc.CardBody('등록된 부재가 없습니다.', className='small text-muted'),
    ], className='mb-3')


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
def add_record(_, pid, task_ids, etype, content, when, use_ai, links, att, att_names, people, n):
    people = sorted(set(people or []) | {pid})  # 이 화면에서 쓴 기록은 항상 이 임원에게 연결
    try:
        save_history(None, etype, content, when, use_ai, links, att, att_names, auth.current_name(),
                     people, also_task_ids=task_ids or [])
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


def _profile_cards(pid: int):
    """주요 이력 · 평가 이력 · 추가 항목 카드."""
    hls = profile.list_highlights(pid)
    hl_body = html.Div([
        html.Div([html.Span(h['date'], className='text-muted me-2 text-nowrap', style={'minWidth': '72px'}),
                  html.Span(h['text'])], className='d-flex small py-1 border-bottom') for h in hls
    ]) if hls else html.Div('아직 정리한 주요 이력이 없습니다 — "정리"를 눌러 발령이력으로 초안을 만드세요.',
                            className='small text-muted')
    kinds, years = profile.evaluation_table(pid)
    ev_body = dbc.Table([
        html.Thead(html.Tr([html.Th('연도')] + [html.Th(k) for k in kinds])),
        html.Tbody([html.Tr([html.Td(r['year'])] + [html.Td(r.get(k, '-')) for k in kinds]) for r in years]),
    ], size='sm', className='mb-0 small text-center') if years else html.Div('평가 이력이 없습니다.', className='small text-muted')
    attrs = profile.list_attributes(pid)
    head = lambda title, btn_id, label: dbc.CardHeader(html.Div([  # noqa: E731
        html.Span(title, className='me-auto'),
        dbc.Button(label, id=btn_id, color='link', size='sm', className='p-0 text-decoration-none')],
        className='d-flex align-items-center'), className='small')
    cols = [
        dbc.Col(dbc.Card([head(f'주요 이력 ({len(hls)})', 'hl-open', [html.I(className='bi bi-pencil me-1'), '정리']),
                          dbc.CardBody(hl_body, style={'maxHeight': '320px', 'overflowY': 'auto'})]), lg=7),
        dbc.Col([dbc.Card([head('평가 이력', 'ev-open', [html.I(className='bi bi-pencil me-1'), '편집']),
                           dbc.CardBody(ev_body)]),
                 dbc.Card([dbc.CardHeader('추가 항목 (엑셀 업로드)', className='small'),
                           dbc.CardBody(html.Div([html.Div([html.Span(a['name'], className='text-muted me-2'), a['value']],
                                                           className='small') for a in attrs]))], className='mt-3')
                 if attrs else None], lg=5),
    ]
    return dbc.Row(cols, className='g-3')


@callback(Output('pp-profile', 'children'), Input('pp-refresh', 'data'), State('pp-id', 'data'))
def render_profile(_, pid):
    return _profile_cards(pid) if store.get_person(pid) else None


@callback(Output('hl-modal', 'is_open'), Output('hl-table', 'data'), Output('hl-msg', 'children'),
          Output('hl-msg', 'is_open'), Output('pp-refresh', 'data', allow_duplicate=True),
          Input('hl-open', 'n_clicks'), Input('hl-cancel', 'n_clicks'), Input('hl-save', 'n_clicks'),
          Input('hl-draft', 'n_clicks'), Input('hl-add-row', 'n_clicks'),
          State('hl-table', 'data'), State('pp-id', 'data'), State('pp-refresh', 'data'), prevent_initial_call=True)
def edit_highlights(_o, _c, _s, _d, _a, rows, pid, n):
    nu = no_update
    trig = ctx.triggered_id
    if trig is None or not ctx.triggered[0]['value']:
        return (nu,) * 5  # 카드가 다시 그려지며 버튼이 새로 생길 때의 빈 트리거
    if trig == 'hl-open':
        return True, profile.list_highlights(pid), '', False, nu
    if trig == 'hl-cancel':
        return False, nu, '', False, nu
    if trig == 'hl-add-row':
        return nu, (rows or []) + [{'date': '', 'text': '', 'source': 'manual'}], nu, nu, nu
    if trig == 'hl-draft':
        draft, msg = profile.draft_highlights(pid)
        if not draft:
            return nu, nu, msg, True, nu
        if any((r.get('text') or '').strip() for r in rows or []):
            msg += ' (기존 내용은 바꿨습니다 — 저장하지 않고 취소하면 그대로 남습니다)'
        return nu, draft, msg, True, nu
    if trig == 'hl-save':
        profile.save_highlights(pid, rows or [])
        return False, nu, '', False, (n or 0) + 1
    return (nu,) * 5


@callback(Output('ev-modal', 'is_open'), Output('ev-table', 'data'), Output('ev-table', 'dropdown'),
          Output('ev-error', 'children'), Output('ev-error', 'is_open'),
          Output('pp-refresh', 'data', allow_duplicate=True),
          Input('ev-open', 'n_clicks'), Input('ev-cancel', 'n_clicks'), Input('ev-save', 'n_clicks'),
          Input('ev-add-row', 'n_clicks'),
          State('ev-table', 'data'), State('pp-id', 'data'), State('pp-refresh', 'data'), prevent_initial_call=True)
def edit_evaluations(_o, _c, _s, _a, rows, pid, n):
    nu = no_update
    trig = ctx.triggered_id
    if trig is None or not ctx.triggered[0]['value']:
        return (nu,) * 6
    if trig == 'ev-open':
        kinds = profile.eval_kinds()
        evals = profile.list_evaluations(pid)
        if not evals:  # 빈 표 대신 작년 기본 두 줄을 깔아 둔다
            from datetime import date
            evals = [{'year': date.today().year - 1, 'kind': k, 'grade': '', 'note': ''} for k in kinds[:2]]
        return True, evals, {'kind': {'options': [{'label': k, 'value': k} for k in kinds], 'clearable': False}}, \
            '', False, nu
    if trig == 'ev-cancel':
        return False, nu, nu, '', False, nu
    if trig == 'ev-add-row':
        last = (rows or [{}])[-1]
        return nu, (rows or []) + [{'year': last.get('year') or '', 'kind': '', 'grade': '', 'note': ''}], nu, nu, nu, nu
    if trig == 'ev-save':
        bad = [r for r in rows or [] if (r.get('grade') or r.get('note')) and
               (not profile.normalize_year(r.get('year')) or not (r.get('kind') or '').strip())]
        if bad:
            return nu, nu, nu, '연도·평가 종류가 비어 있는 줄이 있습니다.', True, nu
        profile.save_evaluations(pid, rows or [])
        return False, nu, nu, '', False, (n or 0) + 1
    return (nu,) * 6
