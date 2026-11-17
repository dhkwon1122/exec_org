"""추적 자료 — 주간보고·월간 실적처럼 주기적으로 받는 자료를 쌓아 두는 '추적' 업무 모아 보기 (services/tracking.py).
업무 화면에는 나오지 않고 여기(검색·자료)에서 본다."""
import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, dcc, html, no_update

from components import ui
from services import auth, tracking

dash.register_page(__name__, path='/tracking', name='추적 자료', title='업무 기록 · 추적 자료')


def layout(**_):
    return html.Div([
        dcc.Store(id='tk-changed', data=0),
        ui.find_tabs('/tracking'),
        ui.page_head('추적 자료', '주기적으로 받는 자료를 업무로 쌓아 두고 업데이트를 지켜봅니다'),
        dbc.Row([
            dbc.Col(dbc.Input(id='tk-q', type='search', debounce=True, placeholder='추적 자료 검색 (이름·내용)'), md=5),
            dbc.Col(dbc.InputGroup([
                dbc.Input(id='tk-new-title', placeholder='새 추적 자료 이름 (예: AI센터 주간보고)'),
                dbc.Button([html.I(className='bi bi-plus-lg me-1'), '만들기'], id='tk-new', color='primary'),
            ]), md=5),
            dbc.Col(dcc.Link([html.I(className='bi bi-envelope-paper me-1'), '메일에서 모으기'], href='/mail-series',
                             className='small', title='정기 메일 조건을 만들고 추적 업무에 연결'), md=2,
                    className='d-flex align-items-center'),
        ], className='g-2 mb-2'),
        dbc.Alert(id='tk-msg', is_open=False, duration=6000, className='small py-2'),
        html.Div(id='tk-list'),
    ])


def _row(r):
    t, st, latest = r['task'], r['status'], r['latest']
    if st['last']:
        since = '오늘' if st['days_since'] == 0 else f"{st['days_since']}일 전"
        line = f"업데이트 {st['count']}회 · 마지막 {st['last']} ({since})"
        if st['interval']:
            line += f" · 평소 약 {st['interval']}일마다 · 다음 예상 {st['expected']}"
    else:
        line = '아직 들어온 업데이트가 없습니다.'
    return dbc.ListGroupItem([
        html.Div([ui.task_link(t), dbc.Badge('평소보다 늦음', color='warning', className='ms-2') if st['late'] else None],
                 className='d-flex align-items-center'),
        html.Div(line, className='small text-muted'),
        html.Div([html.I(className='bi bi-journal-text me-1'), '최근: ',
                  dcc.Link(latest['title'], href=f"/history/{latest['id']}")], className='small text-truncate')
        if latest else None,
        html.Div([html.I(className='bi bi-envelope-paper me-1'), '메일 자동 연결: ',
                  *[dcc.Link(x['name'] + ' ', href=f"/mail-series?series={x['id']}") for x in r['rules']]]
                 if r['rules'] else
                 dcc.Link([html.I(className='bi bi-plus-lg me-1'), '메일 자동 연결 조건 걸기'],
                          href=f"/mail-series?task={t['id']}"), className='small mt-1'),
    ], className='py-2')


@callback(Output('tk-list', 'children'), Input('tk-q', 'value'), Input('tk-changed', 'data'))
def render(q, _):
    rows = tracking.overview(q or '')
    if not rows:
        return dbc.Alert('추적 자료가 없습니다. 위에서 이름으로 만들거나, 정기 메일 모음에서 "추적 업무 만들기"를 누르세요.',
                         color='light')
    late = sum(r['status']['late'] for r in rows)
    head = html.Div(f'{len(rows)}건' + (f' · 평소보다 늦은 자료 {late}건' if late else ''),
                    className='small text-muted mb-1')
    return [head, dbc.ListGroup([_row(r) for r in rows])]


@callback(Output('tk-changed', 'data'), Output('tk-msg', 'children'), Output('tk-msg', 'is_open'),
          Output('tk-msg', 'color'), Output('tk-new-title', 'value'),
          Input('tk-new', 'n_clicks'), State('tk-new-title', 'value'), State('tk-changed', 'data'),
          prevent_initial_call=True)
def create(n, title, changed):
    if not n:
        return no_update, no_update, no_update, no_update, no_update
    try:
        t = tracking.create(title, auth.current_name())
    except ValueError as e:
        return no_update, str(e), True, 'warning', no_update
    return (changed or 0) + 1, ['추적 자료를 만들었습니다 — ', ui.task_link(t), ' · ',
                                dcc.Link('메일 자동 연결 조건 걸기', href=f"/mail-series?task={t['id']}")], True, 'success', ''
