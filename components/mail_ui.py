"""메일 보내기 창 — 앱 레이아웃에 한 번만 들어간다(app.py). 화면은 전역 Store 'mail-draft' 에 초안
(services/outbox.py 의 draft_*)을 넣으면 창이 열린다. 보내면 보낸 메일 기록(/outbox)에 남는다."""
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, ctx, dcc, html, no_update

from services import auth, mailer, outbox


def compose_modal():
    return dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle([html.I(className='bi bi-send me-2'), '메일 보내기'])),
        dbc.ModalBody([
            dcc.Store(id='mc-meta'),
            html.Div(id='mc-config', className='mb-2'),
            dbc.Label('받는 사람', className='small mb-1'),
            dcc.Dropdown(id='mc-to', multi=True, placeholder='사용자·전에 보낸 주소·받은 메일의 보낸 사람에서 고르기'),
            dbc.Input(id='mc-to-extra', size='sm', className='mt-1',
                      placeholder='직접 입력 — 쉼표로 여러 명 (비우면 나에게)'),
            dbc.Label('참조', className='small mb-1 mt-2'),
            dbc.Input(id='mc-cc', size='sm', placeholder='쉼표로 여러 명 (선택)'),
            dbc.Label('제목', className='small mb-1 mt-2'),
            dbc.Input(id='mc-subject'),
            dbc.Label('본문', className='small mb-1 mt-2'),
            dbc.Textarea(id='mc-text', rows=10),
            html.Div(id='mc-table-box', children=[
                dbc.Checkbox(id='mc-table-on', value=True, label='아래 표를 본문에 함께 넣기', className='small mt-2'),
                html.Iframe(id='mc-table', style={'width': '100%', 'height': '220px', 'border': '1px solid #e8e9ed',
                                                  'borderRadius': '8px', 'background': '#fff'}),
            ], style={'display': 'none'}),
            html.Div(id='mc-attach-box', children=[
                dbc.Label('첨부', className='small mb-1 mt-2'),
                dbc.Checklist(id='mc-attach', options=[], value=[], className='small'),
            ], style={'display': 'none'}),
            dbc.Alert(id='mc-result', is_open=False, className='small py-2 mt-3 mb-0'),
        ]),
        dbc.ModalFooter([
            dcc.Link([html.I(className='bi bi-clock-history me-1'), '보낸 메일'], href='/outbox',
                     className='small me-auto'),
            dbc.Button('닫기', id='mc-close', color='secondary', outline=True),
            dbc.Button([html.I(className='bi bi-send me-1'), '보내기'], id='mc-send', color='primary'),
        ]),
    ], id='mc-modal', is_open=False, size='lg', scrollable=True)


def _config_note():
    if not mailer.is_configured():
        return dbc.Alert('메일 API 가 설정되지 않아 보낼 수 없습니다 — .env 의 MAIL_API_URL·MAIL_TOKEN·MAIL_SYSTEM_ID·'
                         'MAIL_FROM (People in SAIT 와 같은 값). 써 둔 내용은 보내기를 누르면 실패로 기록됩니다.',
                         color='warning', className='small py-2 mb-0')
    return html.Div(f'보내는 사람: {mailer.sender()}', className='small text-muted')


@callback(Output('mc-modal', 'is_open'), Output('mc-to', 'options'), Output('mc-to', 'value'),
          Output('mc-to-extra', 'value'), Output('mc-cc', 'value'), Output('mc-subject', 'value'),
          Output('mc-text', 'value'), Output('mc-table', 'srcDoc'), Output('mc-table-box', 'style'),
          Output('mc-attach', 'options'), Output('mc-attach', 'value'), Output('mc-attach-box', 'style'),
          Output('mc-meta', 'data'), Output('mc-config', 'children'), Output('mc-result', 'is_open'),
          Input('mail-draft', 'data'), Input('mc-close', 'n_clicks'), prevent_initial_call=True)
def open_compose(draft, _close):
    nu = no_update
    if ctx.triggered_id == 'mc-close':
        return (False,) + (nu,) * 13 + (False,)
    if not draft:
        return (nu,) * 15
    book = outbox.address_book()
    known = {o['value'] for o in book}
    to, _ = outbox.parse_addrs(draft.get('to') or '')
    book += [{'label': a, 'value': a} for a in to if a not in known]
    table = draft.get('table_html') or ''
    atts = draft.get('attachments') or []
    hide = {'display': 'none'}
    return (True, book, to, '', '', draft.get('subject', ''), draft.get('text', ''),
            f'<html><body style="font-family:sans-serif;margin:8px">{table}</body></html>' if table else '',
            {} if table else hide, atts, [], {} if atts else hide,
            {'table_html': table, 'related_kind': draft.get('related_kind', ''),
             'related_id': draft.get('related_id', '')}, _config_note(), False)


@callback(Output('mc-result', 'children'), Output('mc-result', 'is_open', allow_duplicate=True),
          Output('mc-result', 'color'),
          Input('mc-send', 'n_clicks'),
          State('mc-to', 'value'), State('mc-to-extra', 'value'), State('mc-cc', 'value'),
          State('mc-subject', 'value'), State('mc-text', 'value'), State('mc-table-on', 'value'),
          State('mc-attach', 'value'), State('mc-meta', 'data'), prevent_initial_call=True)
def send(n, to, extra, cc, subject, text, table_on, attach, meta):
    if not n:
        return no_update, no_update, no_update
    meta = meta or {}
    try:
        r = outbox.send(list(to or []) + [extra or ''], subject, text or '',
                        table_html=meta.get('table_html', '') if table_on else '', cc=cc or '',
                        attachments=attach or [], related_kind=meta.get('related_kind', ''),
                        related_id=meta.get('related_id', ''), by=auth.current_name(),
                        default_to=outbox.self_address(auth.current_user()))
    except ValueError as e:
        return str(e), True, 'warning'
    except mailer.MailError as e:
        return [f'보내지 못했습니다: {e}', html.Br(), '보낸 메일 기록에 실패로 남겼습니다.'], True, 'danger'
    return f"보냈습니다 — {r['to']}" + (f" (참조 {r['cc']})" if r['cc'] else ''), True, 'success'
