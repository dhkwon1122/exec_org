"""전체 참고자료 검색: 어느 업무에 어떤 자료가 있었는지 한 곳에서 찾는다.
파일은 메일 첨부와 올린 파일을 같은 문서끼리 묶어 판이 바뀌어 온 순서대로(services/documents.py), 그 밖에 링크·메모."""
import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, callback, dcc, html

from components import doc_ui, ui
from services import documents, store

dash.register_page(__name__, path='/references', name='참고자료', title='업무 기록 · 참고자료')


def layout(**_):
    return html.Div([
        ui.page_head('참고자료', '업무와 이력에 붙은 링크·파일·메모'),
        dbc.Row([
            dbc.Col(dbc.Input(id='rq', type='search', debounce=True,
                              placeholder='검색: 자료 제목·메모·URL·파일명·업무명·컨플루언스 내용'), md=8),
            dbc.Col(dbc.Select(id='rkind', value='', options=[
                {'label': '전체 유형', 'value': ''}, {'label': '링크', 'value': 'link'},
                {'label': '문서(파일)', 'value': 'file'}, {'label': '메모', 'value': 'note'},
                {'label': '메일로 받은 문서', 'value': 'mail'}]), md=4),
        ], className='g-2 mb-3'),
        html.Div(id='ref-results'),
    ])


@callback(Output('ref-results', 'children'), Input('rq', 'value'), Input('rkind', 'value'), Input('doc-changed', 'data'))
def search(q, kind, _changed=None):
    """문서(메일 첨부·올린 파일을 같은 문서끼리 판으로)와 링크·메모."""
    docs = documents.search(q or '') if kind in ('', 'file', 'mail', None) else []
    if kind == 'mail':  # 메일로 받은 판이 있는 문서만
        docs = [d for d in docs if any(v['kind'] == 'mail' for v in d['versions'])]
    refs = [r for r in store.list_references(query=q or '', kind=kind if kind in ('link', 'note') else '')
            if r['kind'] != 'file'] if kind in ('', 'link', 'note', None) else []
    if not refs and not docs:
        return dbc.Alert('참고자료가 없습니다.', color='light')
    sections = []
    if docs:
        sections += [html.H6([html.I(className='bi bi-files me-1'), f'문서 {len(docs)}'], className='mb-2'),
                     doc_ui.document_cards(docs)]
    if refs:
        sections += [html.H6([html.I(className='bi bi-link-45deg me-1'), f'링크·메모 {len(refs)}'],
                             className='mt-3 mb-2' if docs else 'mb-2'), _refs_view(refs)]
    return html.Div(sections)


def _refs_view(refs: list[dict]):
    return dbc.ListGroup([
        dbc.ListGroupItem([
            html.Div([
                html.I(className=f"bi {ui.REF_ICON[r['kind']]} me-2"),
                html.Span(r['title'], className='fw-semibold me-auto'),
                html.Span(r['created_at'], className='small text-muted'),
            ], className='d-flex align-items-center'),
            ui.ref_target(r),
            ui.confluence_box(r),
            html.Div(r['note'][:200], className='small mt-1', style={'whiteSpace': 'pre-wrap'}) if r['note'] else None,
            html.Div([*(['업무: ', dcc.Link(f"#{r['task_id']} {r['task_title']}", href=f"/task/{r['task_id']}")]
                        if r['task_id'] else ['임원 동향 기록']),
                      html.Span([html.I(className='bi bi-clock-history ms-2 me-1'), '이력에 첨부'])
                      if r['history_id'] else None],
                     className='small text-muted mt-1'),
        ]) for r in refs
    ])
