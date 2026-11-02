"""이력 검색 — 원문·AI 요약·키워드·업무명을 한 번에 찾는다. 메일 첨부 문서(파일 이름·요약·태그·본문)도 함께.
임베딩 서버가 있으면(services/embed.py) '의미 검색'으로 글자는 달라도 뜻이 가까운 이력·문서·자료를 아래에 덧붙인다.

검색어를 공백으로 여러 개 넣으면 모두 포함된 이력만(AND), "따옴표"로 묶으면 한 덩어리로 찾는다.
키워드 칩(#키워드)을 누르면 /search?q=키워드 로 바로 검색된다.
"""
import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, callback, dcc, html

from components import doc_ui, ui
from components.history_form import people_options
from services import doc_summary, embed, glossary, store

dash.register_page(__name__, path='/search', name='이력 검색', title='업무 기록 · 이력 검색')


def layout(q: str = '', person: str = '', **_):
    return html.Div([
        ui.page_head('이력 검색'),
        dbc.Row([
            dbc.Col([
                dbc.InputGroup([
                    dbc.InputGroupText(html.I(className='bi bi-search')),
                    dbc.Input(id='s-q', value=q or '', type='search', debounce=True, autoFocus=True,
                              placeholder='검색어 (여러 단어는 모두 포함, "따옴표"는 구절 검색)'),
                ], className='mb-2'),
                dbc.Row([
                    dbc.Col(dcc.Dropdown(id='s-types', multi=True, placeholder='유형 (전체)',
                                         options=store.ENTRY_TYPES + store.AUTO_ENTRY_TYPES), md=2),
                    dbc.Col(dcc.Dropdown(id='s-person', placeholder='임원', options=people_options(),
                                         value=int(person) if str(person).isdigit() else None), md=2),
                    dbc.Col(dcc.Dropdown(id='s-author', placeholder='작성자', options=store.history_authors()), md=2),
                    dbc.Col(dbc.InputGroup([dbc.InputGroupText('부터'), dbc.Input(id='s-from', type='date')],
                                           size='sm'), md=2),
                    dbc.Col(dbc.InputGroup([dbc.InputGroupText('까지'), dbc.Input(id='s-to', type='date')],
                                           size='sm'), md=2),
                    dbc.Col([dbc.Checkbox(id='s-auto', label='자동 기록 포함', value=False, className='me-3'),
                             html.Span(dbc.Checkbox(id='s-sem', label='의미 검색', value=True),
                                       title='글자가 같지 않아도 뜻이 가까운 이력·문서를 함께 찾기',
                                       style={} if embed.is_configured() else {'display': 'none'})],
                            md=2, className='d-flex align-items-center flex-wrap'),
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
                html.H6('문서 태그', className='text-muted mt-3'),
                html.Div([
                    dcc.Link(f'#{k} {n}', href=ui.search_href(k),
                             className='badge rounded-pill text-bg-light border text-decoration-none me-1 mb-1')
                    for k, n in doc_summary.known_tags(30)
                ] or html.Div('아직 요약한 첨부 문서가 없습니다.', className='small text-muted')),
            ], lg=3),
        ]),
    ])


@callback(Output('s-results', 'children'), Output('s-count', 'children'),
          Input('s-q', 'value'), Input('s-types', 'value'), Input('s-person', 'value'), Input('s-author', 'value'),
          Input('s-from', 'value'), Input('s-to', 'value'), Input('s-auto', 'value'), Input('s-sem', 'value'))
def search(q, types, person_id, author, date_from, date_to, include_auto, semantic=True):
    terms = store.search_terms(q or '')
    groups = [glossary.expand(t) for t in terms]  # 용어집의 같은 말
    extra = [w for g in groups for w in g[1:]]
    filters = dict(entry_types=types or None, author=author or '', person_id=person_id,
                   date_from=date_from or '', date_to=date_to or '', include_auto=bool(include_auto))
    rows = store.search_history(q or '', **filters)
    docs = doc_summary.search(q or '', limit=20) if terms else []
    similar, sim_docs, sim_refs, sem_note = [], [], [], None
    if terms and semantic and embed.is_configured():
        try:
            sem_q = f"{q} ({', '.join(extra)})" if extra else q  # 같은 말도 함께 넣어 뜻을 찾는다
            similar, sim_docs, sim_refs = _semantic(sem_q, filters, {h['id'] for h in rows}, docs)
        except embed.EmbedError as e:
            sem_note = dbc.Alert(f'의미 검색을 하지 못했습니다 — {e}', color='warning', className='small py-1 px-2')
    if not rows and not docs and not similar and not sim_docs and not sim_refs:
        return [sem_note, dbc.Alert('조건에 맞는 이력이 없습니다.', color='light')], ''
    label = f'{len(rows)}건' + (' (최대 200건까지 표시)' if len(rows) >= 200 else '')
    if docs:
        label += f' · 첨부 문서 {len(docs)}건'
    if extra:
        label += f" · 용어집의 같은 말로도 찾음: {', '.join(extra)}"
    terms = terms + extra  # 같은 말도 강조
    if similar or sim_docs or sim_refs:
        label += f' · 뜻이 비슷한 이력 {len(similar)}건' + (f', 문서·자료 {len(sim_docs) + len(sim_refs)}건'
                                                       if sim_docs or sim_refs else '')
    return [sem_note,
            _docs_card(docs) if docs else None,
            dbc.ListGroup([ui.history_card(h, terms=terms, show_task=True, actions=False) for h in rows]),
            _similar_section(similar, sim_docs, sim_refs)], label


def _semantic(q: str, filters: dict, shown: set[int], docs: list[dict]):
    """키워드로 이미 나온 것은 빼고, 뜻이 가까운 이력(같은 거르기 조건)·메일 첨부·참고자료."""
    hits = embed.search(q, limit=30)
    score = {(h['kind'], h['id']): h['score'] for h in hits}
    hist_ids = [h['id'] for h in hits if h['kind'] == 'history' and h['id'] not in shown]
    similar = store.search_history('', ids=hist_ids, **filters) if hist_ids else []
    similar = sorted(({**h, 'score': score[('history', h['id'])]} for h in similar), key=lambda h: -h['score'])
    keys = {d['doc_key'] for d in docs}
    sim_docs = [{**d, 'score': score[('mail', d['id'])]}
                for d in doc_summary.attachments([h['id'] for h in hits if h['kind'] == 'mail'])
                if d['doc_key'] not in keys][:10]
    sim_refs = []
    for h in hits:
        if h['kind'] == 'ref' and len(sim_refs) < 10:
            r = store.get_reference(h['id'])
            if r:
                task = store.get_task(r['task_id']) if r['task_id'] else None
                sim_refs.append({**r, 'score': h['score'], 'task': {'id': task['id'], 'title': task['title']}
                                 if task else None})
    return similar, sim_docs, sim_refs


def _score_badge(score: float):
    return dbc.Badge(f'비슷함 {round(score * 100)}%', color='info', className='me-2',
                     title='의미 검색 유사도 (검색어 글자가 그대로 들어 있지 않아도 뜻이 가까운 것)')


def _similar_section(similar: list[dict], sim_docs: list[dict], sim_refs: list[dict]):
    if not similar and not sim_docs and not sim_refs:
        return None
    parts = [html.H6([html.I(className='bi bi-stars me-1'), '뜻이 비슷한 기록',
                      html.Span(' — 검색어가 그대로 들어 있지는 않은 것', className='small text-muted fw-normal')],
                     className='mt-4 mb-2')]
    if similar:
        parts.append(dbc.ListGroup([html.Div([_score_badge(h['score']),
                                              ui.history_card(h, show_task=True, actions=False)], className='mb-1')
                                    for h in similar]))
    items = []
    for d in sim_docs:
        items.append(dbc.ListGroupItem([
            _score_badge(d['score']),
            html.A([html.I(className=f"bi {doc_ui._icon(d['filename'])} me-1"), d['filename']],
                   href=f"/mail-files/{d['id']}", className='text-break me-2'),
            dcc.Link(f"{d['mail_date']} · {d['mail_subject'][:50]}", href=f"/inbox?mail={d['mail_id']}",
                     className='text-muted'),
            html.Div(d['summary'][:200], className='text-muted mt-1') if d['summary'] else None,
        ], className='small'))
    for r in sim_refs:
        items.append(dbc.ListGroupItem([
            html.Div([_score_badge(r['score']), html.Span(r['title'], className='fw-semibold me-2'),
                      html.Span(['업무: ', ui.task_link(r['task'])], className='text-muted') if r['task'] else None]),
            ui.ref_target(r),
            html.Div(r['summary'] or r['content_summary'], className='text-muted mt-1')
            if r['summary'] or r['content_summary'] else None,
        ], className='small'))
    if items:
        parts += [html.Div('문서·자료', className='small fw-semibold text-muted mt-3 mb-1'), dbc.ListGroup(items)]
    return html.Div(parts)


def _docs_card(docs: list[dict]):
    """검색어가 들어 있는 첨부 문서 (같은 문서는 가장 최근 판만)."""
    return dbc.Card(dbc.CardBody([
        html.Div([html.I(className='bi bi-paperclip me-1'), f'첨부 문서 {len(docs)}건'], className='fw-semibold mb-2'),
        dbc.ListGroup([dbc.ListGroupItem([
            html.Div([
                html.A([html.I(className=f"bi {doc_ui._icon(d['filename'])} me-1"), d['filename']],
                       href=f"/mail-files/{d['id']}", className='text-break me-2'),
                dcc.Link(f"{d['mail_date']} · {d['mail_subject'][:50]}", href=f"/inbox?mail={d['mail_id']}",
                         className='text-muted'),
            ]),
            doc_ui.insight_view(d) if d['analyzed'] else None,
            html.Div(d['snippet'], className='text-muted mt-1') if d['snippet'] and not d['analyzed'] else None,
        ], className='small') for d in docs], flush=True),
    ]), className='mb-3')
