"""보낸 메일 — 이 시스템에서 보낸 메일 기록 (업무 공유·원장님 일정·임원 부재 현황·회신). 실패한 것도 남는다."""
import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, callback, ctx, dcc, html, no_update

from components import ui
from services import mailer, outbox

dash.register_page(__name__, path='/outbox', name='보낸 메일', title='업무 기록 · 보낸 메일')

KIND = {'task': '업무 공유', 'schedule': '원장님 일정', 'absence': '임원 부재', 'mail': '회신'}


def layout(**_):
    return html.Div([
        dcc.Store(id='ob-sel'),
        ui.page_head('보낸 메일', '업무 공유·원장님 일정·임원 부재 현황·회신'),
        html.Div(dcc.Link([html.I(className='bi bi-arrow-left me-1'), '받은 메일'], href='/inbox',
                          className='small'), className='mb-2'),
        html.Div(None if mailer.is_configured() else dbc.Alert(
            '메일 API 가 설정되지 않았습니다 — .env 의 MAIL_API_URL·MAIL_TOKEN·MAIL_SYSTEM_ID·MAIL_FROM.',
            color='warning', className='small py-2')),
        dbc.Row([
            dbc.Col(html.Div(id='ob-list'), lg=5),
            dbc.Col(html.Div(id='ob-detail'), lg=7),
        ]),
    ])


def _related_link(m: dict):
    kind, rid = m['related_kind'], m['related_id']
    if kind == 'task' and rid.isdigit():
        return dcc.Link(f'#{rid} 업무', href=f'/task/{rid}')
    if kind == 'mail' and rid.isdigit():
        return dcc.Link('받은 메일', href=f'/inbox?mail={rid}')
    if kind in ('schedule', 'absence'):
        return dcc.Link(rid.replace('~', ' ~ '), href=f"/calendar?tab={'director' if kind == 'schedule' else 'absence'}")
    if kind == 'confluence':
        return dcc.Link('컨플루언스 변경', href='/confluence')
    return None


@callback(Output('ob-list', 'children'), Input('ob-sel', 'data'))
def render_list(sel):
    rows = outbox.list_sent()
    if not rows:
        return html.Div('보낸 메일이 없습니다. 업무 화면·원장님 일정·임원 부재·받은 메일에서 보낼 수 있습니다.',
                        className='small text-muted')
    return dbc.ListGroup([dbc.ListGroupItem([
        html.Div([
            dbc.Badge('보냄' if m['status'] == 'sent' else '실패', color='success' if m['status'] == 'sent' else 'danger',
                      className='me-1'),
            dbc.Badge(KIND.get(m['related_kind'], '메일'), color='light', text_color='dark', className='me-1'),
            html.Span(m['created_at'], className='small text-muted ms-auto'),
        ], className='d-flex align-items-center mb-1'),
        html.Div(m['subject'], className='fw-semibold text-truncate'),
        html.Div(m['to'], className='small text-muted text-truncate'),
    ], id={'type': 'ob-item', 'id': m['id']}, n_clicks=0, action=True, active=m['id'] == sel, className='py-2')
        for m in rows])


@callback(Output('ob-sel', 'data'), Input({'type': 'ob-item', 'id': ALL}, 'n_clicks'), prevent_initial_call=True)
def pick(_):
    if not isinstance(ctx.triggered_id, dict) or not ctx.triggered[0]['value']:
        return no_update
    return ctx.triggered_id['id']


@callback(Output('ob-detail', 'children'), Input('ob-sel', 'data'))
def render_detail(sel):
    m = outbox.get_sent(sel) if sel else None
    if m is None:
        return html.Div('왼쪽에서 메일을 고르세요.', className='small text-muted')
    rows = [('받는 사람', m['to']), ('참조', m['cc']), ('첨부', m['attachments']),
            ('보낸 사람', f"{m['sent_by']} · {m['created_at']}"), ('관련', _related_link(m))]
    return dbc.Card(dbc.CardBody([
        html.H5(m['subject'], className='mb-2'),
        dbc.Alert(f"보내지 못함: {m['error']}", color='danger', className='small py-2') if m['status'] != 'sent' else None,
        html.Table(html.Tbody([html.Tr([html.Th(k, className='text-muted fw-normal small pe-3'),
                                        html.Td(v, className='small')]) for k, v in rows if v]),
                   className='mb-2'),
        html.Iframe(srcDoc=f'<html><body style="font-family:sans-serif;margin:8px">{m["body_html"]}</body></html>',
                    style={'width': '100%', 'height': '55vh', 'border': '1px solid #e8e9ed', 'borderRadius': '8px',
                           'background': '#fff'}),
    ]))
