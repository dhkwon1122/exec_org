"""전체 참고자료 검색: 어느 업무에 어떤 자료가 있었는지 한 곳에서 찾는다.
파일은 메일 첨부와 올린 파일을 같은 문서끼리 묶어 판이 바뀌어 온 순서대로(services/documents.py), 그 밖에 링크·메모.
임베딩 서버가 있으면(services/embed.py) 검색어와 글자는 달라도 뜻이 가까운 문서·링크·메모를 아래에 덧붙인다."""
import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, ctx, dcc, html

from components import doc_ui, ui
from services import documents, embed, store

dash.register_page(__name__, path='/references', name='참고자료', title='업무 기록 · 참고자료')


def layout(**_):
    return html.Div([
        ui.find_tabs('/references'),
        ui.page_head('참고자료', '업무와 이력에 붙은 링크·파일·메모'),
        dbc.Row([
            dbc.Col(dbc.Input(id='rq', type='search', debounce=True,
                              placeholder='검색: 자료 제목·메모·URL·파일명·업무명·컨플루언스 내용'), md=8),
            dbc.Col(dbc.Select(id='rkind', value='', options=[
                {'label': '전체 유형', 'value': ''}, {'label': '링크', 'value': 'link'},
                {'label': '문서(파일)', 'value': 'file'}, {'label': '메모', 'value': 'note'},
                {'label': '메일로 받은 문서', 'value': 'mail'},
                {'label': '이력에서 뺀 것', 'value': 'excluded'}]), md=4),
        ], className='g-2 mb-3'),
        dcc.Store(id='ref-limit', data=PAGE),
        html.Div(id='ref-results'),
        dbc.Button(id='ref-more', color='secondary', outline=True, size='sm', className='mb-2',
                   style={'display': 'none'}),
        html.Div(id='ref-links'),
        html.Div(id='ref-similar'),
    ])


PAGE = 30  # 문서 카드를 한 번에 몇 개씩


@callback(Output('ref-results', 'children'), Output('ref-links', 'children'), Output('ref-limit', 'data'),
          Output('ref-more', 'children'), Output('ref-more', 'style'), Output('ref-similar', 'children'),
          Input('rq', 'value'), Input('rkind', 'value'), Input('doc-changed', 'data'), Input('ref-more', 'n_clicks'),
          State('ref-limit', 'data'))
def search(q, kind, _changed=None, _more=None, limit=PAGE):
    """문서(메일 첨부·올린 파일을 같은 문서끼리 판으로 — 미리 만들어 둔 목록에서)와 링크·메모."""
    limit = (limit or PAGE) + PAGE if ctx.triggered_id == 'ref-more' else PAGE
    hide = {'display': 'none'}
    if kind == 'excluded':  # 판 이력에서 뺀 문서·판 — 되돌리기
        return doc_ui.excluded_view(documents.excluded()), None, limit, '', hide, None
    docs, total = documents.cached(q or '', mail_only=kind == 'mail', limit=limit) \
        if kind in ('', 'file', 'mail', None) else ([], 0)
    refs = [r for r in store.list_references(query=q or '', kind=kind if kind in ('link', 'note') else '')
            if r['kind'] != 'file'] if kind in ('', 'link', 'note', None) else []
    similar, found = _similar(q, kind, docs, refs) if store.search_terms(q or '') else (None, False)
    if not refs and not docs:
        return None if found else dbc.Alert('참고자료가 없습니다.', color='light'), None, limit, '', hide, similar
    doc_view = html.Div([html.H6([html.I(className='bi bi-files me-1'), f'문서 {total}'], className='mb-2'),
                         doc_ui.document_cards(docs)]) if docs else None
    link_view = html.Div([html.H6([html.I(className='bi bi-link-45deg me-1'), f'링크·메모 {len(refs)}'],
                                  className='mt-3 mb-2' if docs else 'mb-2'), _refs_view(refs)]) if refs else None
    more = total > len(docs)
    return (doc_view, link_view, limit, f'문서 더 보기 ({len(docs)} / {total})' if more else '', {} if more else hide,
            similar)


def _similar(q: str, kind: str, docs: list[dict], refs: list[dict]):
    """(뜻이 비슷한 자료 칸, 찾은 것이 있는지) — 키워드로 이미 나온 문서·자료는 빼고. 임베딩 서버가 없으면 (None, False)."""
    if not embed.is_configured():
        return None, False
    try:
        sim_docs, sim_refs = documents.similar(q, kind or '', skip_keys={d['key'] for d in docs},
                                               skip_refs={r['id'] for r in refs})
    except embed.EmbedError as e:
        return dbc.Alert(f'의미 검색을 하지 못했습니다 — {e}', color='warning', className='small py-1 px-2 mt-3'), False
    if not sim_docs and not sim_refs:
        return None, False
    return html.Div([
        html.H6([html.I(className='bi bi-stars me-1'), '뜻이 비슷한 자료',
                 html.Span(' — 검색어가 그대로 들어 있지는 않은 것', className='small text-muted fw-normal')],
                className='mt-4 mb-2'),
        *[html.Div([ui.score_badge(d['score']), doc_ui.document_cards([d])]) for d in sim_docs],
        _refs_view(sim_refs) if sim_refs else None,
    ]), True


def _refs_view(refs: list[dict]):
    return dbc.ListGroup([
        dbc.ListGroupItem([
            html.Div([
                ui.score_badge(r['score']) if 'score' in r else None,
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
