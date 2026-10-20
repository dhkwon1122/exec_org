"""임원 목록 — 최근에 기록된 임원이 위로. 오른쪽은 최근 동향."""
import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, ctx, dcc, html, no_update

from components import ui
from services import people_sync, store

dash.register_page(__name__, path='/people', name='임원', title='업무 기록 · 임원')


def person_form(prefix: str):
    """임원 추가·수정 입력칸 (목록 화면의 추가 창, 임원 화면의 수정 창에서 같이 씀)."""
    return [
        dbc.Row([
            dbc.Col([dbc.Label('이름'), dbc.Input(id=f'{prefix}-name')], md=4),
            dbc.Col([dbc.Label('직위'), dbc.Input(id=f'{prefix}-position', placeholder='예: 부사장')], md=4),
            dbc.Col([dbc.Label('소속'), dbc.Input(id=f'{prefix}-org', placeholder='예: ○○연구센터')], md=4),
        ], className='g-2 mb-2'),
        dbc.Label('별칭 (쉼표 구분)'),
        dbc.Input(id=f'{prefix}-aliases', className='mb-1', placeholder='예: 김부사장, 김 부사장, 센터장님'),
        html.Div('원문에서 이 사람을 부르는 다른 이름들 — 이름이나 별칭이 나오면 그 기록이 자동으로 연결됩니다.',
                 className='small text-muted mb-2'),
        dbc.Label('메모 (경력·관심 분야 등)'), dbc.Textarea(id=f'{prefix}-note', rows=3),
        dbc.Alert(id=f'{prefix}-error', color='danger', is_open=False, className='mt-3 mb-0'),
    ]


def layout(**_):
    return html.Div([
        dcc.Store(id='pl-refresh', data=0),
        dcc.Store(id='ps-plan'),
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle('People in SAIT 변경 제안')),
            dbc.ModalBody(_plan_layout()),
            dbc.ModalFooter([
                html.Span('고른 것만 반영합니다. 고르지 않은 덮어쓰기는 지금 값을 유지하고 다음에 기본 해제로 제안합니다.',
                          className='small text-muted me-auto'),
                dbc.Button('취소', id='ps-cancel', color='secondary', outline=True),
                dbc.Button('선택 반영', id='ps-apply', color='primary'),
            ]),
        ], id='ps-modal', is_open=False, size='lg', scrollable=True),
        dcc.Location(id='pl-redirect', refresh='callback-nav'),
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle('임원 추가')),
            dbc.ModalBody(person_form('pn')),
            dbc.ModalFooter([dbc.Button('취소', id='pn-cancel', color='secondary', outline=True),
                             dbc.Button('추가', id='pn-save', color='primary')]),
        ], id='pn-modal', is_open=False, size='lg'),
        dcc.Store(id='pl-all', data=False),
        dbc.Row([
            dbc.Col(dbc.Input(id='pl-q', type='search', debounce=True,
                              placeholder='사번·이름·직위·소속·학력·별칭·메모 검색'), md=3),
            dbc.Col(dbc.Button(id='pl-all-btn', color='secondary', outline=True, className='w-100'), md=3),
            dbc.Col(dbc.Button([html.I(className='bi bi-arrow-repeat me-1'), 'People in SAIT에서 불러오기'],
                               id='pl-sync', color='secondary', outline=True, className='w-100',
                               disabled=not people_sync.is_available(),
                               title='People in SAIT 인사 정보에서 직급이 임원(' +
                                     '/'.join(people_sync.exec_positions()) + ')인 사람의 정보를 '
                                     '비교해 추가·변경할 것을 제안합니다. 고른 것만 반영하고 별칭·메모·기록은 그대로 둡니다.'), md=3),
            dbc.Col(dbc.ButtonGroup([
                dbc.Button([html.I(className='bi bi-file-earmark-spreadsheet me-1'), '엑셀 업로드'], href='/people/import',
                           color='secondary', outline=True, title='사번·평가(25MBO 등)·주요 이력 등을 엑셀로 한꺼번에'),
                dbc.Button([html.I(className='bi bi-person-plus me-1'), '직접 추가'], id='pn-open', color='primary'),
            ], className='w-100'), md=3),
        ], className='g-2 mb-2'),
        dcc.Loading(dbc.Alert(id='pl-sync-msg', is_open=False, dismissable=True, className='py-2 small'), type='dot'),
        html.Div(id='pl-count', className='small text-muted mb-1'),
        html.Div(id='pl-list', className='mb-4'),
        html.H6('최근 임원 동향', className='text-muted'),
        html.Div(id='pl-recent'),
    ])


def _num(v, unit=''):
    return '-' if v is None else f'{v}{unit}'


def _status_cell(p):
    if p['hr_state'] == 'former':
        return dbc.Badge('퇴직·전출', color='secondary')
    status = p['employment_status'] or ('재직' if p['hr_state'] == 'current' else '-')
    return dbc.Badge(status, color='success' if status == '재직' else 'warning', text_color=None
                     if status == '재직' else 'dark') if status != '-' else '-'


_HEAD = ['사번', 'CL', '재직상태', '성명', '연차', '나이', '최종학력', '입사일', '근속', '소속', '기록']


def _people_table(people):
    rows = []
    for p in people:
        name = [dcc.Link(p['name'], href=f"/people/{p['id']}", className='fw-semibold')]
        if not p['active']:
            name.append(dbc.Badge('추적 중지', color='light', text_color='secondary', className='ms-1 border'))
        rows.append(html.Tr([
            html.Td(p['source_id'] or '-', className='text-muted'),
            html.Td(p['position'] or '-'),
            html.Td(_status_cell(p)),
            html.Td(name, title='별칭: ' + ', '.join(p['aliases']) if p['aliases'] else None),
            html.Td(_num(p['grade_years'], '년차'), title=f"승격일 {p['promotion_date']}" if p['promotion_date'] else None),
            html.Td(_num(p['age'], '세'), title=f"{p['birth_year']}년생" if p['birth_year'] else None),
            html.Td(p['education'] or '-'),
            html.Td(p['hire_date'] or '-'),
            html.Td(_num(p['tenure'], '년')),
            html.Td(p['org'] or '-', className='small'),
            html.Td([f"{p['count']}건", html.Div(p['last_at'][:10], className='text-muted')
                     if p['last_at'] else None], className='small text-nowrap'),
        ], className='text-muted' if not p['employed'] else None))
    return dbc.Table([html.Thead(html.Tr([html.Th(h, className='text-nowrap') for h in _HEAD])), html.Tbody(rows)],
                     hover=True, size='sm', responsive=True, className='align-middle mb-0 small')


@callback(Output('pl-all', 'data'), Input('pl-all-btn', 'n_clicks'), State('pl-all', 'data'),
          prevent_initial_call=True)
def toggle_all(n, show_all):
    return not show_all if n else no_update


@callback(Output('pl-list', 'children'), Output('pl-count', 'children'), Output('pl-all-btn', 'children'),
          Output('pl-recent', 'children'),
          Input('pl-q', 'value'), Input('pl-all', 'data'), Input('pl-refresh', 'data'),
          Input('history-changed', 'data'))
def render(q, show_all, _, _h):
    people = store.list_people(q or '', include_inactive=True, employed_only=not show_all, order='rank')
    if people:
        rows = _people_table(people)
    elif q:
        rows = dbc.Alert('검색 결과가 없습니다.', color='light')
    else:
        rows = dbc.Alert('등록된 임원이 없습니다. "People in SAIT에서 불러오기" 또는 "직접 추가"로 등록하세요.',
                         color='light')
    n_emp = sum(p['employed'] for p in people)
    count = (f'전체 {len(people)}명 (재직 {n_emp} · 퇴직·전출 등 {len(people) - n_emp})' if show_all
             else f'재직 중인 임원 {len(people)}명')
    btn = ([html.I(className='bi bi-people me-1'), '재직 임원만 보기'] if show_all
           else [html.I(className='bi bi-clock-history me-1'), '전체 조회 (퇴직·전출 포함)'])
    recent = store.search_history('', entry_types=['동향'], limit=15)
    recent_view = dbc.ListGroup([ui.history_card(h, show_task=True, actions=False) for h in recent]) if recent \
        else html.Div('아직 동향 기록이 없습니다.', className='small text-muted')
    return rows, count, btn, recent_view


@callback(Output('pn-modal', 'is_open'), Output('pn-error', 'children'), Output('pn-error', 'is_open'),
          Output('pl-redirect', 'pathname'), Output('pn-name', 'value'), Output('pn-position', 'value'),
          Output('pn-org', 'value'), Output('pn-aliases', 'value'), Output('pn-note', 'value'),
          Input('pn-open', 'n_clicks'), Input('pn-cancel', 'n_clicks'), Input('pn-save', 'n_clicks'),
          State('pn-name', 'value'), State('pn-position', 'value'), State('pn-org', 'value'),
          State('pn-aliases', 'value'), State('pn-note', 'value'), prevent_initial_call=True)
def add_person(_o, _c, _s, name, position, org, aliases, note):
    nu = no_update
    if ctx.triggered_id not in ('pn-open', 'pn-cancel', 'pn-save'):
        return (nu,) * 9
    if ctx.triggered_id == 'pn-open':
        return True, '', False, nu, '', '', '', '', ''
    if ctx.triggered_id == 'pn-cancel':
        return False, '', False, nu, nu, nu, nu, nu, nu
    try:
        p = store.create_person(name, position or '', org or '', aliases or '', note or '')
    except ValueError as e:
        return True, str(e), True, nu, nu, nu, nu, nu, nu
    return False, '', False, f"/people/{p['id']}", nu, nu, nu, nu, nu


_PLAN_KINDS = [('add', '새 임원 추가'), ('past', '과거 임원 추가'), ('link', '직접 등록한 임원에 사번 연결'),
               ('set', '정보 변경 — 덮어쓸지')]
_PLAN_HINTS = {'add': "'성+직위' 별칭(예: 김부사장)도 함께 넣습니다",
               'past': '지금은 임원이 아닌 사람 — 추적 중지 상태로 넣어 목록·자동 연결에서는 빠지고 기록·경력은 이어 봅니다',
               'link': '연결해야 아래 정보 변경도 반영됩니다',
               'set': '왼쪽이 지금 값, 오른쪽이 People in SAIT 값'}


def _plan_layout():
    """제안 창 내용 — 체크 목록은 콜백 State 로 읽으므로 처음부터 자리를 만들어 둔다."""
    return [
        html.Div(id='ps-head', className='mb-3'),
        *[html.Div([
            html.Div([html.Span(id=f'ps-title-{kind}', className='fw-semibold me-2'),
                      html.Span(_PLAN_HINTS[kind], className='small text-muted')], className='mb-1'),
            dbc.Checklist(id=f'ps-{kind}', options=[], value=[], className='ms-2'),
        ], id=f'ps-group-{kind}', className='mb-3') for kind, _ in _PLAN_KINDS],
        dbc.Alert(id='ps-missing', color='warning', is_open=False, className='small py-2 mb-0'),
    ]


def _plan_view(plan: dict) -> list:
    """제안을 화면 값으로: 머리말, 종류별 (보이기, 제목, 옵션, 기본 선택), 빠진 임원 경고."""
    items, keys = people_sync.proposals(plan), set(people_sync.default_keys(plan))
    n_auto = len(plan['items']) - len(items)
    head = (f"People in SAIT 현재 임원 {plan['total']}명·과거 임원 {plan['past_total']}명 확인 — 제안 {len(items)}건, "
            f"변동 없음 {plan['unchanged']}명")
    if n_auto:
        head += f" · 비어 있는 인사 정보 {n_auto}건 채움"
    if plan['career_new']:
        head += f" · 경력 이력 새로 {plan['career_new']}건(반영하면 함께 쌓임)"
    elif not items:
        head += ' · 반영할 변경이 없습니다.'
    out = [head]
    for kind, title in _PLAN_KINDS:
        opts = [{'label': i['label'], 'value': i['key']} for i in items if i['kind'] == kind]
        out += [None if opts else {'display': 'none'}, f'{title} ({len(opts)})', opts,
                [o['value'] for o in opts if o['value'] in keys]]
    missing = plan['missing']
    out += [['People in SAIT 최신 인원에 없는 임원(퇴임·이동?): ', ', '.join(missing),
             ' — 더 추적하지 않으려면 임원 화면에서 "추적 중지"'] if missing else '', bool(missing)]
    return out


_PLAN_OUTPUTS = [Output('ps-head', 'children')] + [
    o for kind, _ in _PLAN_KINDS for o in (Output(f'ps-group-{kind}', 'style'), Output(f'ps-title-{kind}', 'children'),
                                           Output(f'ps-{kind}', 'options'), Output(f'ps-{kind}', 'value'))
] + [Output('ps-missing', 'children'), Output('ps-missing', 'is_open')]


@callback(Output('ps-modal', 'is_open'), Output('ps-plan', 'data'), Output('ps-apply', 'disabled'), *_PLAN_OUTPUTS,
          Output('pl-sync-msg', 'children'), Output('pl-sync-msg', 'is_open'), Output('pl-sync-msg', 'color'),
          Output('pl-refresh', 'data', allow_duplicate=True),
          Input('pl-sync', 'n_clicks'), Input('ps-cancel', 'n_clicks'), Input('ps-apply', 'n_clicks'),
          State('ps-plan', 'data'), *[State(f'ps-{kind}', 'value') for kind, _ in _PLAN_KINDS],
          State('pl-refresh', 'data'), prevent_initial_call=True)
def sync_from_people_in_sait(n_sync, _c, n_apply, plan, *chosen_and_n):
    nu = no_update
    keep = (nu,) * len(_PLAN_OUTPUTS)
    trig = ctx.triggered_id
    if trig is None or not ctx.triggered[0]['value']:
        return (nu,) * 3 + keep + (nu,) * 4
    if trig == 'ps-cancel':
        return (False, None, nu) + keep + (nu,) * 4
    if trig == 'pl-sync':
        try:
            plan = people_sync.plan_sync()
        except people_sync.SyncError as e:
            return (nu,) * 3 + keep + (str(e), True, 'danger', nu)
        return (True, plan, not (plan['items'] or plan['career_new']), *_plan_view(plan), nu, False, nu, nu)
    if trig != 'ps-apply' or not plan:
        return (nu,) * 3 + keep + (nu,) * 4
    *chosen, n = chosen_and_n
    try:
        r = people_sync.apply_sync(plan, [k for v in chosen for k in (v or [])])
    except people_sync.SyncError as e:
        return (nu,) * 3 + keep + (str(e), True, 'danger', nu)
    lines = [html.Div(f"반영 완료 — 추가 {len(r['added'])}명, 과거 임원 {len(r['past_added'])}명, 연결 {len(r['linked'])}명, "
                      f"정보 갱신 {len(r['updated'])}명, 유지 {len(r['kept'])}건, 경력 이력 {r['career_added']}건")]
    for label, key in (('추가', 'added'), ('과거 임원', 'past_added'), ('연결', 'linked'), ('갱신', 'updated'),
                       ('유지', 'kept'),
                       ('건너뜀(그 사이 바뀜)', 'skipped')):
        if r[key]:
            lines.append(html.Div(f'{label}: ' + ', '.join(r[key])))
    return (False, None, nu) + keep + (lines, True, 'success', (n or 0) + 1)
