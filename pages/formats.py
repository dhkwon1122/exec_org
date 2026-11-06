"""AI 요약 형식 — 기록 유형·문서 종류마다 AI 가 채울 칸을 정한다 (services/summary_formats.py)."""
import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, no_update

from components import ui
from services import summary_formats

dash.register_page(__name__, path='/formats', name='요약 형식', title='업무 기록 · 요약 형식')


def layout(**_):
    return html.Div([
        ui.page_head('요약 형식', 'AI 가 정리할 때 채울 칸 — 기록 유형·문서 종류마다'),
        dbc.Alert([
            html.Div('형식을 정해 두면 AI 정리 때 요약 글과 함께 그 칸을 채워, 기록 카드와 문서 요약에 칸별로 보여 줍니다.'),
            html.Ul([
                html.Li('기록 — 기록 유형(회의·피드백·이슈…)마다. 기록을 저장하거나 "다시 생성"할 때.'),
                html.Li('문서 — 문서 종류(회의록·보고서·계획서…)마다. AI 가 종류를 고른 뒤 그 형식대로. 첨부·올린 파일 요약 때.'),
                html.Li('한 줄에 한 칸: "칸 이름: 무엇을 쓰는지". 설명은 AI 가 참고합니다. 형식이 없는 종류는 요약 글만.'),
                html.Li('형식을 바꿔도 이미 정리한 것은 그대로입니다 — 바뀐 형식으로 보려면 그 기록·문서를 다시 정리하세요.'),
            ], className='mb-0'),
        ], color='light', className='small'),
        dbc.Row([
            dbc.Col(html.Div(id='fmt-list'), lg=6),
            dbc.Col(dbc.Card(dbc.CardBody([
                html.H6('형식 편집', className='mb-2'),
                dbc.RadioItems(id='fmt-target', value='history', inline=True, className='mb-2 small',
                               options=[{'label': v, 'value': k} for k, v in summary_formats.TARGETS.items()]),
                dcc.Dropdown(id='fmt-kind', placeholder='종류 고르기', clearable=False, className='mb-2'),
                dbc.Textarea(id='fmt-text', rows=7, className='small mb-2',
                             placeholder='안건: 논의한 주제\n결정 사항: 정해진 것\n할 일: 누가 언제까지 무엇을'),
                html.Div([
                    dbc.Button('저장', id='fmt-save', color='primary', size='sm', className='me-2'),
                    dbc.Button([html.I(className='bi bi-trash me-1'), '이 형식 지우기'], id='fmt-del', color='link',
                               size='sm', className='text-danger p-0 ms-auto'),
                ], className='d-flex align-items-center'),
                dbc.Alert(id='fmt-msg', is_open=False, className='small py-2 mt-2 mb-0'),
            ])), lg=6),
        ], className='g-3'),
        dcc.Store(id='fmt-changed', data=0),
    ])


def _list_view():
    formats = summary_formats.list_formats()
    parts = []
    for target, label in summary_formats.TARGETS.items():
        rows = [f for f in formats if f['target'] == target]
        parts.append(html.H6(label, className='mt-1 mb-2'))
        if not rows:
            parts.append(html.Div('아직 형식이 없습니다.', className='small text-muted mb-3'))
            continue
        parts.append(dbc.ListGroup([dbc.ListGroupItem([
            html.Div(f['kind'], className='fw-semibold'),
            html.Div(' · '.join(x['name'] for x in f['sections']), className='small text-muted'),
        ], id={'type': 'fmt-item', 'target': target, 'kind': f['kind'].encode('utf-8').hex()},  # 한글이 id 에 그대로면
            action=True, n_clicks=0, className='py-2')                                            # 눌린 단추를 못 찾는다
            for f in rows], className='mb-3'))
    return parts


@callback(Output('fmt-list', 'children'), Input('fmt-changed', 'data'))
def render_list(_):
    return _list_view()


@callback(Output('fmt-target', 'value'), Output('fmt-kind', 'value'),
          Input({'type': 'fmt-item', 'target': ALL, 'kind': ALL}, 'n_clicks'), prevent_initial_call=True)
def pick(_):
    trig = ctx.triggered_id
    if not trig or not (ctx.triggered and ctx.triggered[0]['value']):
        return no_update, no_update
    return trig['target'], bytes.fromhex(trig['kind']).decode('utf-8')


@callback(Output('fmt-kind', 'options'), Input('fmt-target', 'value'), Input('fmt-changed', 'data'))
def kind_options(target, _):
    have = {f['kind'] for f in summary_formats.list_formats(target)}
    return [{'label': k + (' — 형식 있음' if k in have else ''), 'value': k} for k in summary_formats.kinds(target)]


@callback(Output('fmt-text', 'value'), Input('fmt-target', 'value'), Input('fmt-kind', 'value'))
def load(target, kind):
    if not kind or kind not in summary_formats.kinds(target):
        return ''
    return summary_formats.to_text(summary_formats.get(target, kind))


@callback(Output('fmt-changed', 'data'), Output('fmt-msg', 'children'), Output('fmt-msg', 'is_open'),
          Output('fmt-msg', 'color'), Output('fmt-text', 'value', allow_duplicate=True),
          Input('fmt-save', 'n_clicks'), Input('fmt-del', 'n_clicks'),
          State('fmt-target', 'value'), State('fmt-kind', 'value'), State('fmt-text', 'value'),
          State('fmt-changed', 'data'), prevent_initial_call=True)
def save(_s, _d, target, kind, text, n):
    if not kind:
        return no_update, '종류를 고르세요.', True, 'warning', no_update
    if ctx.triggered_id == 'fmt-del':
        summary_formats.delete(target, kind)
        return (n or 0) + 1, f"'{kind}' 형식을 지웠습니다 — 앞으로는 요약 글만.", True, 'secondary', ''
    try:
        sections = summary_formats.save(target, kind, text)
    except ValueError as e:
        return no_update, str(e), True, 'danger', no_update
    return ((n or 0) + 1, f"'{kind}' 형식을 저장했습니다 — 칸 {len(sections)}개: " + ', '.join(x['name'] for x in sections),
            True, 'success', summary_formats.to_text(sections))
