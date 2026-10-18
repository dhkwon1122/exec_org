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
        dbc.Row([
            dbc.Col([
                dbc.Row([
                    dbc.Col(dbc.Input(id='pl-q', type='search', debounce=True,
                                      placeholder='이름·직위·소속·별칭·메모 검색'), md=4),
                    dbc.Col(dbc.Checkbox(id='pl-inactive', label='추적 중지 포함', value=False), md=2,
                            className='d-flex align-items-center'),
                    dbc.Col(dbc.Button([html.I(className='bi bi-arrow-repeat me-1'), 'People in SAIT에서 불러오기'],
                                       id='pl-sync', color='secondary', outline=True, className='w-100',
                                       disabled=not people_sync.is_available(),
                                       title='People in SAIT 인사 정보에서 직급이 임원(' +
                                             '/'.join(people_sync.exec_positions()) + ')인 사람의 이름·직위·소속을 '
                                             '비교해 추가·변경할 것을 제안합니다. 고른 것만 반영하고 별칭·메모·기록은 그대로 둡니다.'), md=4),
                    dbc.Col(dbc.Button([html.I(className='bi bi-person-plus me-1'), '직접 추가'], id='pn-open',
                                       color='primary', className='w-100'), md=2),
                ], className='g-2 mb-2'),
                dcc.Loading(dbc.Alert(id='pl-sync-msg', is_open=False, dismissable=True, className='py-2 small'),
                            type='dot'),
                html.Div(id='pl-list'),
            ], lg=7),
            dbc.Col([
                html.H6('최근 임원 동향', className='text-muted'),
                html.Div(id='pl-recent'),
            ], lg=5),
        ]),
    ])


@callback(Output('pl-list', 'children'), Output('pl-recent', 'children'),
          Input('pl-q', 'value'), Input('pl-inactive', 'value'), Input('pl-refresh', 'data'),
          Input('history-changed', 'data'))
def render(q, inactive, _, _h):
    people = store.list_people(q or '', include_inactive=bool(inactive))
    if people:
        rows = dbc.ListGroup([
            dbc.ListGroupItem([
                html.Div([
                    dcc.Link(p['name'], href=f"/people/{p['id']}", className='fw-semibold me-2'),
                    html.Span(' · '.join(x for x in [p['position'], p['org']] if x), className='small me-auto'),
                    dbc.Badge('추적 중지', color='secondary', className='me-2') if not p['active'] else None,
                    dbc.Badge('People in SAIT', color='info', className='me-2', title=f"사번 {p['source_id']}")
                    if p['source_id'] else None,
                    html.Span([f"기록 {p['count']}건", f" · 최근 {p['last_at'][:10]}" if p['last_at'] else ''],
                              className='small text-muted'),
                ], className='d-flex align-items-center flex-wrap'),
                html.Div('별칭: ' + ', '.join(p['aliases']), className='small text-muted') if p['aliases'] else None,
            ], className='py-2') for p in people
        ])
    else:
        rows = dbc.Alert('등록된 임원이 없습니다. "임원 추가"로 등록하세요.', color='light')
    recent = store.search_history('', entry_types=['동향'], limit=15)
    recent_view = dbc.ListGroup([ui.history_card(h, show_task=True, actions=False) for h in recent]) if recent \
        else html.Div('아직 동향 기록이 없습니다.', className='small text-muted')
    return rows, recent_view


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
    items, keys = plan['items'], set(people_sync.default_keys(plan))
    head = (f"People in SAIT 현재 임원 {plan['total']}명·과거 임원 {plan['past_total']}명 확인 — 제안 {len(items)}건, "
            f"변동 없음 {plan['unchanged']}명")
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
