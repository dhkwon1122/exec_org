"""삭제한 업무·기록 복원과 명시적 영구 삭제."""
import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, no_update
from components import ui
from services import records

dash.register_page(__name__, path='/trash', name='휴지통', title='업무 기록 · 휴지통')


def layout(**_):
    return html.Div([ui.page_head('휴지통'),
        html.P('업무와 기록, 첨부파일은 복원할 때까지 보존됩니다. 업무 복원 시 함께 삭제된 기록도 복원됩니다. '
               '여러 업무에 연결된 기록은 다른 업무에서 계속 볼 수 있습니다.', className='text-muted small'),
        dbc.Row([dbc.Col(dbc.Input(id='trash-q', type='search', debounce=True, placeholder='삭제한 항목 검색'), md=8),
                 dbc.Col(dbc.Select(id='trash-kind', value='', options=[{'label': '전체', 'value': ''},
                     {'label': '업무', 'value': 'task'}, {'label': '기록', 'value': 'history'}]), md=4)], className='g-2 mb-3'),
        dbc.Alert(id='trash-message', is_open=False, color='info'), html.Div(id='trash-results'),
        dcc.Store(id='trash-target'),
        dcc.ConfirmDialog(id='trash-confirm', message='영구 삭제하면 기록과 첨부파일을 복구할 수 없습니다. 계속할까요?'),
    ])


@callback(Output('trash-results', 'children'), Input('trash-q', 'value'), Input('trash-kind', 'value'),
          Input('history-changed', 'data'))
def render(q, kind, _):
    rows = records.list_trash(q or '', kind or '')
    if not rows:
        return dbc.Alert('휴지통이 비어 있습니다.' if not q else '검색 결과가 없습니다.', color='light')
    return [html.P(f'{len(rows)}건 · 최근 100건 표시', className='small text-muted'), dbc.ListGroup([
        dbc.ListGroupItem([
            html.Div([dbc.Badge('업무' if row['kind'] == 'task' else '기록', color='secondary', className='me-2'),
                      html.Strong(row['title']),
                      html.Div(f"{row['at']} · {row['by'] or '담당자'}" +
                          (f" · 기록 {row['count']}건 포함" if row['count'] else ''), className='small text-muted mt-1')],
                     className='flex-grow-1 text-break'),
            dbc.Button('복원', id={'type': 'trash-restore', 'kind': row['kind'], 'id': row['id']},
                       color='primary', outline=True),
            dbc.Button('영구 삭제', id={'type': 'trash-purge', 'kind': row['kind'], 'id': row['id']},
                       color='danger', outline=True),
        ], className='d-flex align-items-center flex-wrap gap-2 py-3') for row in rows])]


@callback(Output('history-changed', 'data', allow_duplicate=True), Output('trash-message', 'children'),
          Output('trash-message', 'is_open'), Output('trash-confirm', 'displayed'), Output('trash-target', 'data'),
          Input({'type': 'trash-restore', 'kind': ALL, 'id': ALL}, 'n_clicks'),
          Input({'type': 'trash-purge', 'kind': ALL, 'id': ALL}, 'n_clicks'), Input('trash-confirm', 'submit_n_clicks'),
          State('trash-target', 'data'), State('history-changed', 'data'), prevent_initial_call=True)
def act(_r, _p, _c, target, changed):
    if not ctx.triggered or not ctx.triggered[0]['value']:
        return (no_update,) * 5
    trig = ctx.triggered_id
    if isinstance(trig, dict) and trig['type'] == 'trash-purge':
        return no_update, no_update, False, True, trig
    try:
        if trig == 'trash-confirm' and target:
            records.purge(target['kind'], target['id'])
            message = '영구 삭제했습니다.'
        elif isinstance(trig, dict) and trig['type'] == 'trash-restore':
            records.restore(trig['kind'], trig['id'])
            message = '복원했습니다.'
        else:
            return (no_update,) * 5
    except ValueError as error:
        return no_update, str(error), True, False, None
    return (changed or 0) + 1, message, True, False, None
