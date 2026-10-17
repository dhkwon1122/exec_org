"""임원 목록 — 최근에 기록된 임원이 위로. 오른쪽은 최근 동향."""
import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, ctx, dcc, html, no_update

from components import ui
from services import store

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
                                      placeholder='이름·직위·소속·별칭·메모 검색'), md=6),
                    dbc.Col(dbc.Checkbox(id='pl-inactive', label='추적 중지한 임원 포함', value=False), md=3,
                            className='d-flex align-items-center'),
                    dbc.Col(dbc.Button([html.I(className='bi bi-person-plus me-1'), '임원 추가'], id='pn-open',
                                       color='primary', className='w-100'), md=3),
                ], className='g-2 mb-3'),
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
