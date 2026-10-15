"""전체 참고자료 검색: 어느 업무에 어떤 자료가 있었는지 한 곳에서 찾는다."""
import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, callback, dcc, html

from components import ui
from services import store

dash.register_page(__name__, path='/references', name='참고자료', title='업무 기록 · 참고자료')


def layout(**_):
    return html.Div([
        dbc.Row([
            dbc.Col(dbc.Input(id='rq', type='search', debounce=True,
                              placeholder='검색: 자료 제목·메모·URL·파일명·업무명'), md=8),
            dbc.Col(dbc.Select(id='rkind', value='', options=[
                {'label': '전체 유형', 'value': ''}, {'label': '링크', 'value': 'link'},
                {'label': '파일', 'value': 'file'}, {'label': '메모', 'value': 'note'}]), md=4),
        ], className='g-2 mb-3'),
        html.Div(id='ref-results'),
    ])


@callback(Output('ref-results', 'children'), Input('rq', 'value'), Input('rkind', 'value'))
def search(q, kind):
    refs = store.list_references(query=q or '', kind=kind or '')
    if not refs:
        return dbc.Alert('참고자료가 없습니다.', color='light')
    return dbc.ListGroup([
        dbc.ListGroupItem([
            html.Div([
                html.I(className=f"bi {ui.REF_ICON[r['kind']]} me-2"),
                html.Span(r['title'], className='fw-semibold me-auto'),
                html.Span(r['created_at'], className='small text-muted'),
            ], className='d-flex align-items-center'),
            ui.ref_target(r),
            html.Div(r['note'][:200], className='small mt-1', style={'whiteSpace': 'pre-wrap'}) if r['note'] else None,
            html.Div(['업무: ', dcc.Link(f"#{r['task_id']} {r['task_title']}", href=f"/task/{r['task_id']}"),
                      html.Span([html.I(className='bi bi-clock-history ms-2 me-1'), '이력에 첨부'])
                      if r['history_id'] else None],
                     className='small text-muted mt-1'),
        ]) for r in refs
    ])
