"""이력 검색 — 원문·AI 요약·키워드·업무명을 한 번에 찾는다. 메일 첨부 문서(파일 이름·요약·태그·본문)도 함께.
임베딩 서버가 있으면(services/embed.py) '의미 검색'으로 글자는 달라도 뜻이 가까운 이력·문서·자료를 아래에 덧붙인다.

검색어를 공백으로 여러 개 넣으면 모두 포함된 이력만(AND), "따옴표"로 묶으면 한 덩어리로 찾는다.
키워드 칩(#키워드)을 누르면 /search?q=키워드 로 바로 검색된다.
"""
import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, dcc, html

from components import doc_ui, ui
from components.history_form import people_options
from services import doc_summary, embed, glossary, store

dash.register_page(__name__, path='/search', name='이력 검색', title='업무 기록 · 이력 검색')


def _sem_status():
    """의미 검색(임베딩)이 켜져 있는지 — 설정·서버 연결·색인 현황."""
    st = embed.summary()
    if not st['configured']:
        return dbc.Badge([html.I(className='bi bi-stars me-1'), '의미 검색 꺼짐'], color='light', text_color='secondary',
                         className='border', id='s-sem-status',
                         title='임베딩 서버가 설정되지 않았습니다 (.env 의 EMBED_BASE_URL) — 키워드로만 찾습니다.')
    if st['ok'] is False:
        return dbc.Badge([html.I(className='bi bi-exclamation-triangle me-1'), '의미 검색: 서버 연결 안 됨'],
                         color='warning', text_color='dark', id='s-sem-status',
                         title=f"{st['at']} {st['error']} — 키워드 검색은 그대로 됩니다.")
    done = st['total'] and st['indexed'] >= st['total']
    label = '의미 검색 켜짐' + ('' if done else f" · 색인 중 {st['indexed']:,}/{st['total']:,}")
    return dbc.Badge([html.I(className='bi bi-stars me-1'), label], color='success' if done else 'info',
                     id='s-sem-status',
                     title=f"모델 {st['model']} · 색인 {st['indexed']:,}/{st['total']:,}"
                           + (f" · 마지막 확인 {st['at']}" if st['at'] else ''))


def layout(q: str = '', person: str = '', **_):
    return html.Div([
        ui.find_tabs('/search'),
        ui.page_head('이력 검색'),
        dbc.Row([
            dbc.Col([
                dbc.InputGroup([
                    dbc.InputGroupText(html.I(className='bi bi-search')),
                    dbc.Input(id='s-q', value=q or '', type='search', debounce=True, autoFocus=True,
                              placeholder='검색어 (여러 단어는 모두 포함, "따옴표"는 구절 검색)'),
                ], className='mb-2'),
                html.Details([html.Summary('상세 검색 조건', className='composer-toggle'), dbc.Row([
                    dbc.Col(dcc.Dropdown(id='s-types', multi=True, placeholder='유형 (전체)',
                                         options=store.ENTRY_TYPES + store.AUTO_ENTRY_TYPES), md=2),
                    dbc.Col(dcc.Dropdown(id='s-person', placeholder='임원·리더', options=people_options(),
                                         value=int(person) if str(person).isdigit() else None), md=2),
                    dbc.Col(dcc.Dropdown(id='s-author', placeholder='작성자', options=store.history_authors()), md=2),
                    dbc.Col(dbc.InputGroup([dbc.InputGroupText('부터'), dbc.Input(id='s-from', type='date')],
                                           size='sm'), md=2),
                    dbc.Col(dbc.InputGroup([dbc.InputGroupText('까지'), dbc.Input(id='s-to', type='date')],
                                           size='sm'), md=2),
                    dbc.Col([dbc.Checkbox(id='s-auto', label='자동 기록 포함', value=False, className='me-3'),
                             html.Span(dbc.Checkbox(id='s-sem', label='의미 검색', value=True),
                                       title='글자가 같지 않아도 뜻이 가까운 이력·문서를 함께 찾기',
                                       style={} if embed.is_configured() else {'display': 'none'}),
                             _sem_status()],
                            md=2, className='d-flex align-items-center flex-wrap'),
                ], className='g-2 mt-1 mb-3')], open=bool(person), className='mb-3'),
                dbc.Button('검색 조건 초기화', id='s-reset', color='link', size='sm', className='mb-2 p-0'),
                html.Div(id='s-count', className='small text-muted mb-2'),
                html.Div(id='s-results'),
                ui.pager('s-page'),
                html.Div(id='s-similar'),
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


_FILTERS = [Input('s-q', 'value'), Input('s-types', 'value'), Input('s-person', 'value'), Input('s-author', 'value'),
            Input('s-from', 'value'), Input('s-to', 'value'), Input('s-auto', 'value'), Input('s-sem', 'value')]


@callback(Output('s-page', 'active_page'), *_FILTERS, prevent_initial_call=True)
def first_page(*_):
    """조건이 바뀌면 첫 쪽부터."""
    return 1


ui.scroll_to_top_on_page('s-page', 's-count')


@callback(Output('s-results', 'children'), Output('s-count', 'children'), Output('s-page', 'max_value'),
          Output('s-page', 'style'), Output('s-similar', 'children'),
          *[State(i.component_id, i.component_property) for i in _FILTERS], Input('s-page', 'active_page'), Input('history-changed', 'data'))
def search(q, types, person_id, author, date_from, date_to, include_auto, semantic=True, page=1, _changed=None):
    """조건이 바뀌면 first_page 가 쪽을 1로 돌려 여기로 온다 (첫 화면은 쪽의 처음 값으로). 한 쪽에 30건."""
    terms = store.search_terms(q or '')
    groups = [glossary.expand(t) for t in terms]  # 용어집의 같은 말
    extra = [w for g in groups for w in g[1:]]
    filters = dict(entry_types=types or None, author=author or '', person_id=person_id,
                   date_from=date_from or '', date_to=date_to or '', include_auto=bool(include_auto))
    rows = store.search_history(q or '', **filters)
    docs = doc_summary.search(q or '', limit=20) if terms else []
    similar, sim_docs, sim_refs, sem_note = [], [], [], None
    used_sem = bool(terms and semantic and embed.is_configured())
    if used_sem:
        try:
            sem_q = f"{q} ({', '.join(extra)})" if extra else q  # 같은 말도 함께 넣어 뜻을 찾는다
            similar, sim_docs, sim_refs = _semantic(sem_q, filters, {h['id'] for h in rows}, docs)
        except embed.EmbedError as e:
            sem_note = dbc.Alert(f'의미 검색을 하지 못했습니다 — {e}', color='warning', className='small py-1 px-2')
    if not rows and not docs and not similar and not sim_docs and not sim_refs:
        return [sem_note, dbc.Alert('조건에 맞는 이력이 없습니다.', color='light')], '', *ui.pager_state(0), None
    shown, page = ui.page_of(rows, page)
    label = f'{len(rows)}건' + (' (최대 200건까지)' if len(rows) >= 200 else '') + ui.page_note(len(rows), page)
    if docs:
        label += f' · 첨부 문서 {len(docs)}건'
    if extra:
        label += f" · 용어집의 같은 말로도 찾음: {', '.join(extra)}"
    terms = terms + extra  # 같은 말도 강조
    if used_sem and sem_note is None and not (similar or sim_docs or sim_refs):
        label += ' · 의미 검색: 더 찾은 기록 없음'
    if similar or sim_docs or sim_refs:
        label += f' · 뜻이 비슷한 이력 {len(similar)}건' + (f', 문서·자료 {len(sim_docs) + len(sim_refs)}건'
                                                       if sim_docs or sim_refs else '')
    cards = dbc.ListGroup([ui.history_card(h, terms=terms, show_task=True, actions=False) for h in shown])
    return ([sem_note, _docs_card(docs) if docs else None, cards], label, *ui.pager_state(len(rows)),
            _similar_section(similar, sim_docs, sim_refs))


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


def _similar_section(similar: list[dict], sim_docs: list[dict], sim_refs: list[dict]):
    if not similar and not sim_docs and not sim_refs:
        return None
    parts = [html.H6([html.I(className='bi bi-stars me-1'), '뜻이 비슷한 기록',
                      html.Span(' — 검색어가 그대로 들어 있지는 않은 것', className='small text-muted fw-normal')],
                     className='mt-4 mb-2')]
    if similar:
        parts.append(dbc.ListGroup([html.Div([ui.score_badge(h['score']),
                                              ui.history_card(h, show_task=True, actions=False)], className='mb-1')
                                    for h in similar]))
    items = []
    for d in sim_docs:
        items.append(dbc.ListGroupItem([
            ui.score_badge(d['score']),
            html.A([html.I(className=f"bi {doc_ui._icon(d['filename'])} me-1"), d['filename']],
                   href=f"/mail-files/{d['id']}", className='text-break me-2'),
            dcc.Link(f"{d['mail_date']} · {d['mail_subject'][:50]}", href=f"/inbox?mail={d['mail_id']}",
                     className='text-muted'),
            html.Div(d['summary'][:200], className='text-muted mt-1') if d['summary'] else None,
        ], className='small'))
    for r in sim_refs:
        items.append(dbc.ListGroupItem([
            html.Div([ui.score_badge(r['score']), html.Span(r['title'], className='fw-semibold me-2'),
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

@callback(Output('s-q', 'value'), Output('s-types', 'value'), Output('s-person', 'value'),
          Output('s-author', 'value'), Output('s-from', 'value'), Output('s-to', 'value'), Output('s-auto', 'value'),
          Input('s-reset', 'n_clicks'), prevent_initial_call=True)
def reset(_):
    return '', [], None, None, None, None, False
