"""전체 참고자료 검색: 어느 업무에 어떤 자료가 있었는지 한 곳에서 찾는다.
직접 등록한 링크·파일·메모와 기록에 붙은 자료, 받은 메일의 첨부 문서(같은 문서는 최신 판 하나로)를 함께."""
import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, callback, dcc, html

from components import doc_ui, ui
from services import doc_versions, store

dash.register_page(__name__, path='/references', name='참고자료', title='업무 기록 · 참고자료')


def layout(**_):
    return html.Div([
        ui.page_head('참고자료', '업무와 이력에 붙은 링크·파일·메모'),
        dbc.Row([
            dbc.Col(dbc.Input(id='rq', type='search', debounce=True,
                              placeholder='검색: 자료 제목·메모·URL·파일명·업무명·컨플루언스 내용'), md=8),
            dbc.Col(dbc.Select(id='rkind', value='', options=[
                {'label': '전체 유형', 'value': ''}, {'label': '링크', 'value': 'link'},
                {'label': '파일', 'value': 'file'}, {'label': '메모', 'value': 'note'},
                {'label': '메일 첨부', 'value': 'mail'}]), md=4),
        ], className='g-2 mb-3'),
        html.Div(id='ref-results'),
    ])


def _mail_docs(docs: list[dict]):
    return dbc.ListGroup([
        dbc.ListGroupItem([
            html.Div([
                html.A([html.I(className=f"bi {doc_ui._icon(d['file']['filename'])} me-2"), d['file']['filename']],
                       href=f"/mail-files/{d['file']['id']}", className='fw-semibold me-2 text-break'),
                dbc.Badge('메일 첨부', color='light', text_color='dark', className='border me-1'),
                dbc.Badge(f"{d['n_versions']}판 중 최신", color='success', className='me-1') if d['n_versions'] > 1 else None,
                html.Span(className='me-auto'),
                html.Span(d['mail_date'], className='small text-muted'),
            ], className='d-flex align-items-center flex-wrap'),
            doc_ui.insight_view(d['file']['insight']) if d['file']['insight']['analyzed'] else None,
            html.Div(['메일: ', dcc.Link(d['mail_subject'][:60], href=f"/inbox?mail={d['mail_id']}"),
                      *([' · 업무: '] + [html.Span([ui.task_link(t), ' ']) for t in d['tasks']] if d['tasks'] else [])],
                     className='small text-muted mt-1'),
        ], className='small') for d in docs
    ])


@callback(Output('ref-results', 'children'), Input('rq', 'value'), Input('rkind', 'value'))
def search(q, kind):
    refs = store.list_references(query=q or '', kind=kind) if kind != 'mail' else []
    docs = doc_versions.latest_documents(q or '') if kind in ('', 'mail', None) else []
    if not refs and not docs:
        return dbc.Alert('참고자료가 없습니다.', color='light')
    sections = []
    if docs:
        sections += [html.H6([html.I(className='bi bi-paperclip me-1'), f'메일 첨부 문서 {len(docs)}'],
                             className='mb-2'), _mail_docs(docs)]
    if refs:
        sections += [html.H6([html.I(className='bi bi-folder2 me-1'), f'등록한 자료·기록 첨부 {len(refs)}'],
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
