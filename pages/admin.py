"""접근 권한 관리 (관리자 전용) — 사용자 DB 의 계정 중 이 시스템을 쓸 수 있는 사람을 정한다."""
import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, no_update

from services import auth, users

dash.register_page(__name__, path='/admin', name='접근 권한', title='업무 기록 · 접근 권한')


def layout(**_):
    if not auth.is_admin():
        return dbc.Alert('관리자만 볼 수 있는 화면입니다.', color='warning')
    source = ('People in SAIT 사용자 DB(app_users)의 계정' if users.is_shared()
              else '이 앱 로컬 사용자 DB의 계정 (scripts/create_user.py 로 생성)')
    return html.Div([
        dcc.Store(id='ad-refresh', data=0),
        html.H4('접근 권한', className='mb-1'),
        html.P([f'{source} 중에서, 여기에 등록된 사람만 로그인할 수 있습니다. ',
                '권한을 회수하면 이미 로그인한 사람도 다음 클릭부터 막힙니다.'], className='text-muted small'),
        dbc.Card(dbc.CardBody(dbc.Row([
            dbc.Col(dcc.Dropdown(id='ad-user', placeholder='아이디·이름으로 사용자 검색'), md=6),
            dbc.Col(dbc.Select(id='ad-role', value='member',
                               options=[{'label': v, 'value': k} for k, v in auth.ROLES.items()]), md=3),
            dbc.Col(dbc.Button('권한 부여', id='ad-grant', color='primary', className='w-100'), md=3),
        ], className='g-2')), className='mb-2'),
        dbc.Alert(id='ad-msg', is_open=False, className='py-2'),
        html.Div(id='ad-list'),
    ])


@callback(Output('ad-list', 'children'), Output('ad-user', 'options'), Input('ad-refresh', 'data'))
def render(_):
    if not auth.is_admin():
        return no_update, no_update
    members = auth.list_members()
    me = auth.current_user()['user_id']
    try:
        all_users = users.list_users()
    except users.UserDBError as e:
        return dbc.Alert(str(e), color='danger'), []
    granted = {m['user_id'] for m in members}
    opts = [{'label': f"{u['name']} ({u['user_id']})", 'value': u['user_id']}
            for u in all_users if u['user_id'] not in granted]
    rows = [html.Tr([
        html.Td([m['name'], html.Div(m['user_id'], className='small text-muted')]),
        html.Td(dbc.Badge(auth.ROLES[m['role']], color='danger' if m['role'] == 'admin' else 'secondary')),
        html.Td([m['granted_by'], html.Div(m['created_at'], className='small text-muted')], className='small'),
        html.Td(html.Span('사용자 DB에 없음', className='small text-danger') if not m['in_user_db'] else ''),
        html.Td('환경변수 지정' if m['bootstrap'] else ('본인' if m['user_id'] == me else
                dbc.Button('회수', id={'type': 'ad-revoke', 'id': m['user_id']}, color='danger', outline=True,
                           size='sm')), className='small text-muted text-end'),
    ]) for m in members]
    table = dbc.Table([html.Thead(html.Tr([html.Th('사용자'), html.Th('권한'), html.Th('부여'), html.Th(''),
                                           html.Th('')])), html.Tbody(rows)], hover=True, size='sm')
    return table, opts


@callback(Output('ad-refresh', 'data'), Output('ad-msg', 'children'), Output('ad-msg', 'is_open'),
          Output('ad-msg', 'color'),
          Input('ad-grant', 'n_clicks'), Input({'type': 'ad-revoke', 'id': ALL}, 'n_clicks'),
          State('ad-user', 'value'), State('ad-role', 'value'), State('ad-refresh', 'data'),
          prevent_initial_call=True)
def change(_g, _r, user_id, role, n):
    if not auth.is_admin():  # 화면을 우회한 호출도 서버에서 막는다
        return no_update, '관리자만 할 수 있습니다.', True, 'danger'
    trig = ctx.triggered_id
    me = auth.current_user()['user_id']
    try:
        if trig == 'ad-grant':
            if not user_id:
                return no_update, '사용자를 선택하세요.', True, 'warning'
            auth.grant(user_id, role, granted_by=me)
            msg = f'{user_id} 에게 {auth.ROLES[role]} 권한을 부여했습니다.'
        elif isinstance(trig, dict):
            if not ctx.triggered[0]['value']:
                return no_update, no_update, no_update, no_update
            auth.revoke(trig['id'], by=me)
            msg = f"{trig['id']} 의 접근 권한을 회수했습니다."
        else:
            return no_update, no_update, no_update, no_update
    except (ValueError, users.UserDBError) as e:
        return no_update, str(e), True, 'danger'
    return (n or 0) + 1, msg, True, 'success'
