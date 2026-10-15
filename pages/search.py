"""이력 검색 — 원문·AI 요약·키워드·업무명을 한 번에 찾는다.

검색어를 공백으로 여러 개 넣으면 모두 포함된 이력만(AND), "따옴표"로 묶으면 한 덩어리로 찾는다.
키워드 칩(#키워드)을 누르면 /search?q=키워드 로 바로 검색된다.
"""
import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, callback, dcc, html

from components import ui
from services import store

dash.register_page(__name__, path='/search', name='이력 검색', title='업무 기록 · 이력 검색')


def layout(q: str = '', **_):
    return html.Div([
        dbc.Row([
            dbc.Col([
                dbc.InputGroup([
                    dbc.InputGroupText(html.I(className='bi bi-search')),
                    dbc.Input(id='s-q', value=q or '', type='search', debounce=True, autoFocus=True,
                              placeholder='검색어 (여러 단어는 모두 포함, "따옴표"는 구절 검색)'),
                ], className='mb-2'),
                dbc.Row([
                    dbc.Col(dcc.Dropdown(id='s-types', multi=True, placeholder='유형 (전체)',
                                         options=store.ENTRY_TYPES + store.AUTO_ENTRY_TYPES), md=4),
                    dbc.Col(dcc.Dropdown(id='s-author', placeholder='작성자', options=store.history_authors()), md=2),
                    dbc.Col(dbc.InputGroup([dbc.InputGroupText('부터'), dbc.Input(id='s-from', type='date')],
                                           size='sm'), md=2),
                    dbc.Col(dbc.InputGroup([dbc.InputGroupText('까지'), dbc.Input(id='s-to', type='date')],
                                           size='sm'), md=2),
                    dbc.Col(dbc.Checkbox(id='s-auto', label='자동 기록 포함', value=False), md=2,
                            className='d-flex align-items-center'),
                ], className='g-2 mb-3'),
                html.Div(id='s-count', className='small text-muted mb-2'),
                html.Div(id='s-results'),
            ], lg=9),
            dbc.Col([
                html.H6('자주 나온 키워드', className='text-muted'),
                html.Div([
                    dcc.Link(f'#{k} {n}', href=ui.search_href(k),
                             className='badge rounded-pill text-bg-light border text-decoration-none me-1 mb-1')
                    for k, n in store.keyword_stats(40)
                ] or html.Div('아직 키워드가 없습니다.', className='small text-muted')),
            ], lg=3),
        ]),
    ])


@callback(Output('s-results', 'children'), Output('s-count', 'children'),
          Input('s-q', 'value'), Input('s-types', 'value'), Input('s-author', 'value'),
          Input('s-from', 'value'), Input('s-to', 'value'), Input('s-auto', 'value'))
def search(q, types, author, date_from, date_to, include_auto):
    terms = store.search_terms(q or '')
    rows = store.search_history(q or '', entry_types=types or None, author=author or '',
                                date_from=date_from or '', date_to=date_to or '', include_auto=bool(include_auto))
    if not rows:
        return dbc.Alert('조건에 맞는 이력이 없습니다.', color='light'), ''
    label = f'{len(rows)}건' + (' (최대 200건까지 표시)' if len(rows) >= 200 else '')
    return dbc.ListGroup([ui.history_card(h, terms=terms, show_task=True, actions=False) for h in rows]), label
