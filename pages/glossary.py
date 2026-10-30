"""사내 용어집 — 같은 것을 여러 이름으로 부르는 말(조직·과제·회의·약어)을 대표 용어로 묶는다 (services/glossary.py).
표에서 바로 고치고 '저장', 엑셀·CSV 로 한꺼번에 더하기."""
import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, ctx, dash_table, dcc, html, no_update

from components import ui
from services import excel_import, glossary

dash.register_page(__name__, path='/glossary', name='용어집', title='업무 기록 · 용어집')

COLUMNS = [{'name': '대표 용어', 'id': 'term'}, {'name': '같은 말 (쉼표)', 'id': 'synonyms'},
           {'name': '분류', 'id': 'category', 'presentation': 'dropdown'}, {'name': '설명', 'id': 'description'}]


def _rows(terms: list[dict]) -> list[dict]:
    return [{**t, 'synonyms': ', '.join(t['synonyms'])} for t in terms]


def layout(**_):
    cell = {'textAlign': 'left', 'whiteSpace': 'normal', 'height': 'auto', 'fontSize': '0.85rem', 'padding': '6px'}
    return html.Div([
        ui.page_head('용어집', '같은 것을 여러 이름으로 부르는 사내 용어 — 사람은 임원 별칭에'),
        dbc.Alert([
            html.Div('여기 적은 같은 말은 세 곳에 쓰입니다.'),
            html.Ul([
                html.Li('검색: 같은 말 하나로 찾으면 나머지 말이 들어간 기록·문서·자료도 함께 찾습니다 (예: 종기원 → SAIT, 종합기술원).'),
                html.Li('AI 정리: 원문에 나온 용어의 같은 말과 설명을 AI 에게 알려 줍니다 (요약·분류·일정·부재 추출 모두).'),
                html.Li('태그: AI 가 같은 말로 단 키워드·문서 태그를 대표 용어 하나로 맞춥니다.'),
            ], className='mb-0'),
        ], color='light', className='small'),
        dbc.Row([
            dbc.Col(dbc.Input(id='gl-q', type='search', debounce=True, placeholder='용어·같은 말·설명으로 찾기'), md=5),
            dbc.Col([
                dbc.Button([html.I(className='bi bi-plus-lg me-1'), '줄 추가'], id='gl-add', color='secondary',
                           outline=True, size='sm', className='me-2'),
                dbc.Button([html.I(className='bi bi-check2 me-1'), '저장'], id='gl-save', color='primary', size='sm',
                           className='me-2'),
                dcc.Upload(dbc.Button([html.I(className='bi bi-upload me-1'), '엑셀·CSV 더하기'], color='secondary',
                                      outline=True, size='sm'), id='gl-upload', multiple=False,
                           className='d-inline-block'),
            ], md=7, className='d-flex align-items-center justify-content-md-end mt-2 mt-md-0'),
        ], className='g-2 mb-2'),
        dbc.Alert(id='gl-msg', is_open=False, dismissable=True, className='small py-2'),
        dash_table.DataTable(
            id='gl-table', columns=COLUMNS, data=_rows(glossary.list_terms()), editable=True, row_deletable=True,
            dropdown={'category': {'options': [{'label': c, 'value': c} for c in glossary.CATEGORIES]}},
            style_table={'overflowX': 'auto'}, style_cell=cell,
            style_cell_conditional=[{'if': {'column_id': 'term'}, 'width': '16%', 'fontWeight': 600},
                                    {'if': {'column_id': 'synonyms'}, 'width': '28%'},
                                    {'if': {'column_id': 'category'}, 'width': '11%'}],
            css=[{'selector': '.Select-menu-outer', 'rule': 'display: block !important'}]),
        html.Div('칸을 눌러 바로 고치고, 지울 줄은 ×, 다 고쳤으면 저장. 엑셀은 첫 행에 대표 용어 · 같은 말 · 분류 · 설명 '
                 '제목을 두면 됩니다(같은 대표 용어는 같은 말을 합칩니다). 검색 중에 저장하면 보이는 줄만 저장되니 '
                 '검색을 비우고 저장하세요.', className='small text-muted mt-2'),
    ])


@callback(Output('gl-table', 'data'), Output('gl-msg', 'children'), Output('gl-msg', 'is_open'),
          Output('gl-msg', 'color'),
          Input('gl-q', 'value'), Input('gl-add', 'n_clicks'), Input('gl-save', 'n_clicks'),
          Input('gl-upload', 'contents'), State('gl-upload', 'filename'), State('gl-table', 'data'),
          prevent_initial_call=True)
def act(q, _add, _save, contents, filename, rows):
    trig = ctx.triggered_id
    if trig == 'gl-add':
        return [{'term': '', 'synonyms': '', 'category': '', 'description': ''}] + (rows or []), no_update, False, no_update
    try:
        if trig == 'gl-save':
            if q:
                return no_update, '검색을 비운 뒤 저장하세요 (지금은 일부 줄만 보입니다).', True, 'warning'
            r = glossary.save_all(rows or [])
            msg = f"용어 {r['saved']}개를 저장했습니다" + (f", {r['deleted']}개를 지웠습니다" if r['deleted'] else '') + '.'
            return _rows(glossary.list_terms()), msg, True, 'success'
        if trig == 'gl-upload' and contents:
            table = excel_import.parse_upload(contents, filename)
            r = glossary.merge_rows(glossary.rows_from_table(table['headers'], table['rows']))
            return (_rows(glossary.list_terms(q or '')), f"{filename}: 새 용어 {r['added']}개, 합친 용어 {r['updated']}개.",
                    True, 'success')
    except (ValueError, excel_import.ImportError_) as e:
        return no_update, str(e), True, 'danger'
    return _rows(glossary.list_terms(q or '')), no_update, False, no_update
