"""임원 정보 엑셀 일괄 업로드 — 첫 행 제목을 보고 DB 칸을 맞춘 뒤(규칙 + 사내 AI) 미리보기 → 반영."""
import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, callback, ctx, dcc, html, no_update

from services import auth, excel_import, llm

dash.register_page(__name__, path='/people/import', name='임원 엑셀 업로드', title='업무 기록 · 임원 엑셀 업로드')

_SOURCE = {'rule': ('규칙', 'secondary'), 'ai': ('AI', 'info'), 'new': ('새 항목', 'warning')}


def layout(**_):
    return html.Div([
        dcc.Store(id='imp-table'),
        dcc.Store(id='imp-previewed', data=False),
        html.Div([dcc.Link([html.I(className='bi bi-arrow-left me-1'), '임원 목록'], href='/people',
                           className='small text-decoration-none')], className='mb-2'),
        html.H4([html.I(className='bi bi-file-earmark-spreadsheet me-2'), '임원 정보 엑셀 일괄 업로드']),
        dbc.Alert([
            html.Div('엑셀(xlsx) 또는 CSV 의 첫 행을 제목으로 읽어, 열마다 DB 의 어느 칸에 넣을지 맞춥니다.'),
            html.Ul([
                html.Li(['임원을 찾는 열: ', html.B('사번'), '(없으면 ', html.B('성명'), ') — 꼭 하나는 있어야 합니다.']),
                html.Li(['평가는 제목에 연도와 종류를 쓰면 됩니다 — 예: ', html.Code('25MBO'), ', ', html.Code('24Mbo'), ', ',
                         html.Code('2024 임원종합평가'), ', ', html.Code("'23 종합"), '. 처음 보는 평가 종류는 새로 만듭니다.']),
                html.Li(['주요 이력 열은 한 칸에 여러 줄(예: ', html.Code('2023.03 상무 승진, AI센터장'),
                         ') — 그 임원의 주요 이력을 이 내용으로 바꿉니다.']),
                html.Li('규칙으로 모르는 제목은 사내 AI 가 판단하고, 맞는 칸이 없으면 같은 이름의 "추가 항목"을 만듭니다. '
                        '아래 표에서 열마다 바꿀 수 있습니다.'),
                html.Li('빈 칸은 건드리지 않습니다. 반영 전에 미리보기로 몇 건이 바뀌는지 확인하세요.'),
            ], className='mb-0 small'),
        ], color='light', className='small'),
        dcc.Upload(id='imp-upload', children=html.Div([html.I(className='bi bi-upload me-2'),
                                                       '여기로 파일을 끌어 놓거나 눌러서 선택 (xlsx, csv)']),
                   className='border border-2 rounded text-center p-4 mb-2 text-muted',
                   style={'borderStyle': 'dashed', 'cursor': 'pointer'}, multiple=False),
        dbc.Checkbox(id='imp-ai', value=llm.is_configured(), disabled=not llm.is_configured(),
                     label='모르는 제목은 사내 AI 로 매칭' + ('' if llm.is_configured() else ' (LLM 미설정)'),
                     className='small mb-2'),
        dbc.Alert(id='imp-error', color='danger', is_open=False),
        dcc.Loading(html.Div(id='imp-mapping'), type='dot'),
        html.Div(id='imp-actions', style={'display': 'none'}, children=[
            dbc.Checkbox(id='imp-create', value=False, className='small my-2',
                         label='DB 에 없는 사번(또는 이름)은 새 임원으로 추가 (끄면 건너뛰고 목록만 보여 줌)'),
            html.Div([
                dbc.Button([html.I(className='bi bi-eye me-1'), '미리보기'], id='imp-preview', color='secondary',
                           outline=True, className='me-2'),
                dbc.Button([html.I(className='bi bi-check2 me-1'), '반영'], id='imp-apply', color='primary',
                           disabled=True),
            ]),
        ]),
        dcc.Loading(html.Div(id='imp-result', className='mt-3'), type='dot'),
    ])


def _mapping_table(table: dict, mapping: list[dict]):
    opts = excel_import.target_options(mapping)
    rows = []
    for m in mapping:
        label, color = _SOURCE.get(m['source'], ('', 'light'))
        rows.append(html.Tr([
            html.Td(html.B(m['header'])),
            html.Td(', '.join(m['samples']) or '-', className='small text-muted', style={'maxWidth': '260px'}),
            html.Td(dcc.Dropdown(id={'type': 'imp-target', 'index': m['index']}, options=opts, value=m['target'],
                                 clearable=False, style={'minWidth': '260px'})),
            html.Td([dbc.Badge(label, color=color, className='me-1'), html.Span(m['reason'], className='small')]),
        ]))
    return html.Div([
        html.Div(f"{len(table['rows'])}행 · {len(table['headers'])}열을 읽었습니다. 열마다 넣을 칸을 확인하세요.",
                 className='small text-muted mb-1'),
        dbc.Table([html.Thead(html.Tr([html.Th('엑셀 제목'), html.Th('예시 값'), html.Th('넣을 칸'), html.Th('판단')])),
                   html.Tbody(rows)], size='sm', hover=True, responsive=True, className='align-middle small'),
    ])


@callback(Output('imp-table', 'data'), Output('imp-mapping', 'children'), Output('imp-actions', 'style'),
          Output('imp-error', 'children'), Output('imp-error', 'is_open'), Output('imp-result', 'children'),
          Output('imp-apply', 'disabled'),
          Input('imp-upload', 'contents'), State('imp-upload', 'filename'), State('imp-ai', 'value'),
          prevent_initial_call=True)
def on_upload(contents, filename, use_ai):
    if not contents:
        return (no_update,) * 7
    try:
        table = excel_import.parse_upload(contents, filename)
    except excel_import.ImportError_ as e:
        return None, None, {'display': 'none'}, str(e), True, None, True
    mapping = excel_import.suggest_mapping(table['headers'], table['rows'], use_ai=bool(use_ai))
    return (table, [html.Div([html.I(className='bi bi-file-earmark me-1'), filename], className='fw-semibold mb-1'),
                    _mapping_table(table, mapping)], {}, '', False, None, True)


def _summary(r: dict, applied: bool):
    changes = [html.Li(f'{k}: {n}건') for k, n in r['changes'].items() if n]
    body = [html.Div(f"{'반영 완료' if applied else '미리보기'} — {r['rows']}행 중 임원 {r['matched']}명 확인"
                     + (f", 새 임원 {len(r['created'])}명" if r['created'] else ''), className='fw-semibold'),
            html.Ul(changes, className='mb-1') if changes else html.Div('바뀌는 값이 없습니다.', className='small')]
    if r['created']:
        body.append(html.Div('새로 추가' + ('' if applied else '할') + ' 임원: ' + ', '.join(r['created']), className='small'))
    if r['unmatched']:
        body.append(html.Div(f"DB 에 없어 건너뜀 {len(r['unmatched'])}건: " + ', '.join(r['unmatched'][:30])
                             + (' …' if len(r['unmatched']) > 30 else ''), className='small text-danger'))
    if r['ambiguous']:
        body.append(html.Div('같은 이름이 여럿이라 건너뜀(사번 열을 쓰세요): ' + ', '.join(r['ambiguous']),
                             className='small text-danger'))
    if applied:
        body.append(dcc.Link('임원 목록으로', href='/people', className='small'))
    return dbc.Alert(body, color='success' if applied else 'secondary')


@callback(Output('imp-result', 'children', allow_duplicate=True), Output('imp-apply', 'disabled', allow_duplicate=True),
          Input('imp-preview', 'n_clicks'), Input('imp-apply', 'n_clicks'),
          State('imp-table', 'data'), State({'type': 'imp-target', 'index': ALL}, 'value'), State('imp-create', 'value'),
          prevent_initial_call=True)
def preview_or_apply(_p, _a, table, targets, create):
    if not table or ctx.triggered_id not in ('imp-preview', 'imp-apply') or not ctx.triggered[0]['value']:
        return no_update, no_update
    apply = ctx.triggered_id == 'imp-apply'
    try:
        r = excel_import.run(table, list(targets), create_missing=bool(create), apply=apply, by=auth.current_name())
    except excel_import.ImportError_ as e:
        return dbc.Alert(str(e), color='danger'), True
    return _summary(r, apply), apply  # 반영 뒤에는 다시 미리보기해야 반영 가능


@callback(Output('imp-apply', 'disabled', allow_duplicate=True),
          Input({'type': 'imp-target', 'index': ALL}, 'value'), Input('imp-create', 'value'),
          prevent_initial_call=True)
def mapping_changed(*_):
    return True  # 매칭을 바꾸면 미리보기를 다시 해야 반영할 수 있다
