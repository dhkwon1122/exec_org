"""메일 첨부 문서의 판(버전) 보기 — 받은 메일 상세와 업무 상세가 같이 쓴다 (services/doc_versions.py).
파일마다 AI 요약·태그(services/doc_summary.py)를 보여 주고, 'AI 요약' 버튼으로 그 자리에서 요약한다."""
import dash_bootstrap_components as dbc
from dash import MATCH, Input, Output, State, callback, dcc, html, no_update

from services import doc_summary, llm

_ICON = {'pdf': 'bi-file-earmark-pdf', 'xlsx': 'bi-file-earmark-excel', 'xls': 'bi-file-earmark-excel',
         'csv': 'bi-file-earmark-spreadsheet', 'pptx': 'bi-file-earmark-slides', 'ppt': 'bi-file-earmark-slides',
         'docx': 'bi-file-earmark-word', 'doc': 'bi-file-earmark-word', 'hwp': 'bi-file-earmark-text',
         'hwpx': 'bi-file-earmark-text', 'zip': 'bi-file-earmark-zip', 'png': 'bi-file-earmark-image',
         'jpg': 'bi-file-earmark-image', 'jpeg': 'bi-file-earmark-image'}


def _size(n: int) -> str:
    return f'{n / 1024 / 1024:.1f}MB' if n >= 1024 * 1024 else f'{max(1, n // 1024)}KB'


def _icon(filename: str) -> str:
    return _ICON.get(filename.rsplit('.', 1)[-1].lower() if '.' in filename else '', 'bi-file-earmark')


def _file_link(f: dict):
    return html.A([html.I(className=f"bi {_icon(f['filename'])} me-1"), f['filename']],
                  href=f"/mail-files/{f['id']}", className='text-break')


def insight_view(ins: dict):
    """요약 블록 — 문서 종류·태그·요약·앞 판과 달라진 점·주요 날짜. 요약 전이면 못 읽은 이유만."""
    from components.ui import search_href
    if not ins['analyzed']:
        return html.Div(f"내용을 읽지 못했습니다 — {ins['note']}", className='text-muted') if ins['note'] else None
    return html.Div([
        html.Div([
            dbc.Badge(ins['doc_type'], color='info', className='me-1') if ins['doc_type'] else None,
            *[dcc.Link(f'#{t}', href=search_href(t),
                       className='badge rounded-pill text-bg-light border text-decoration-none me-1')
              for t in ins['tags']],
        ], className='mb-1') if ins['doc_type'] or ins['tags'] else None,
        html.Div(ins['summary'], style={'whiteSpace': 'pre-wrap'}),
        html.Div([html.I(className='bi bi-arrow-left-right me-1'), html.B('앞 판과 달라진 점 '), ins['changes']],
                 className='mt-1 doc-changes') if ins['changes'] else None,
        html.Ul([html.Li([html.Span(d['date'], className='fw-semibold me-1'), d['what']]) for d in ins['key_dates']],
                className='list-unstyled mt-1 mb-0') if ins['key_dates'] else None,
        html.Div(f"AI 요약 {ins['analyzed_at']}", className='text-muted mt-1', style={'fontSize': '0.75rem'}),
    ], className='doc-insight')


def _ai_button(ins: dict):
    if not ins['readable'] or not llm.is_configured():
        return None
    return dbc.Button([html.I(className='bi bi-arrow-repeat' if ins['analyzed'] else 'bi bi-magic'),
                       '' if ins['analyzed'] else ' AI 요약'],
                      id={'type': 'doc-ai', 'id': ins['id']}, color='link', size='sm',
                      className='p-0 ms-2 text-decoration-none' + (' text-muted' if ins['analyzed'] else ''),
                      title='다시 요약' if ins['analyzed'] else '문서를 읽어 요약·태그 달기')


def _version_row(v: dict, current_mail: int | None):
    here = current_mail is not None and v['mail_id'] == current_mail
    return html.Li([
        dbc.Badge(f"{v['version']}판", color='primary' if v['latest'] else 'light',
                  text_color=None if v['latest'] else 'dark', className='me-2'),
        _file_link(v),
        html.Span(f" · {_size(v['size'])}", className='text-muted'),
        html.Span(' · 앞 판과 내용 같음', className='text-muted') if v['dup'] else None,
        html.Div([
            html.Span(v['mail_date'] or v['received'][:10], className='me-1'),
            html.Span('이 메일', className='fw-semibold') if here else
            dcc.Link(v['mail_subject'][:60], href=f"/inbox?mail={v['mail_id']}"),
        ], className='text-muted'),
    ], className='mb-1')


def doc_groups(groups: list[dict], current_mail: int | None = None, empty: str = ''):
    """문서마다: 이 메일(들)의 파일, 판 번호, 최신 판이 아니면 최신 판 안내, 전체 판 이력(펼치기)."""
    if not groups:
        return html.Div(empty, className='small text-muted') if empty else None
    items = []
    for g in groups:
        latest = g['latest']
        n = latest['n_versions']
        head = []
        for f in g['files']:
            badge = (dbc.Badge('최신', color='success', className='ms-2') if f['latest'] else
                     dbc.Badge(f"예전 판 — 최신은 {n}판", color='warning', className='ms-2'))
            head.append(html.Div([
                _file_link(f),
                html.Span(f" · {_size(f['size'])}", className='text-muted'),
                dbc.Badge(f"{f['version']}판 / {n}", color='light', text_color='dark', className='ms-2')
                if n > 1 else None,
                badge if n > 1 else None,
                html.Span(' (앞 판과 내용 같음)', className='text-muted') if f['dup'] else None,
                _ai_button(f['insight']),
            ]))
            head.append(dcc.Loading(html.Div(insight_view(f['insight']), id={'type': 'doc-ai-box', 'id': f['id']},
                                             className='ms-3 mb-1'), type='dot', delay_show=300))
        if n > 1 and not any(f['latest'] for f in g['files']):
            head.append(html.Div(['최신 판: ', _file_link(latest),
                                  html.Span(f" ({latest['mail_date']} 메일)", className='text-muted')]))
        via = g.get('via_tasks') or []
        if via:  # 하위 업무에 연결된 메일에서 온 문서
            from components.ui import task_link
            head.append(html.Div([dbc.Badge('하위 업무', color='info', className='me-1'),
                                  *[html.Span([task_link(t), ' ']) for t in via]], className='mt-1'))
        items.append(dbc.ListGroupItem([
            *head,
            html.Details([
                html.Summary(f"'{g['base']}' 판 이력 {len(g['versions'])}건", className='text-muted'),
                html.Ul([_version_row(v, current_mail) for v in reversed(g['versions'])],
                        className='list-unstyled mt-1 mb-0 ps-2'),
            ]) if len(g['versions']) > 1 else None,
        ], className='small'))
    return dbc.ListGroup(items)


@callback(Output({'type': 'doc-ai-box', 'id': MATCH}, 'children'), Input({'type': 'doc-ai', 'id': MATCH}, 'n_clicks'),
          State({'type': 'doc-ai', 'id': MATCH}, 'id'), prevent_initial_call=True)
def analyze_doc(n, ident):
    """파일 하나 요약 — 이미 요약했으면 다시(앞 판 비교 포함)."""
    if not n:
        return no_update
    done = (doc_summary.get_insight(ident['id']) or {}).get('analyzed', False)
    try:
        return insight_view(doc_summary.analyze(ident['id'], force=done))
    except (ValueError, llm.LLMError) as e:
        return dbc.Alert(str(e), color='warning', className='small py-1 px-2 mb-0')


# ── 문서별 판 이력 (services/documents.py) ─────────────────────────────

SHOW_VERSIONS = 4  # 최근 판 몇 개를 펼쳐 보이고 나머지는 접을지


def _version_line(v: dict, delete: bool):
    """판 하나 — 판 번호·날짜·파일·출처, 그 판에서 달라진 점(없으면 요약 한 줄)."""
    from components.ui import task_link
    ins = v['insight'] if v['insight'] and v['insight']['analyzed'] else None
    if v['kind'] == 'mail':
        source = [html.I(className='bi bi-envelope me-1'),
                  dcc.Link(v['mail_subject'][:50], href=f"/inbox?mail={v['mail_id']}")]
    else:
        source = [html.I(className='bi bi-clock-history me-1' if v['history_title'] else 'bi bi-upload me-1'),
                  f"기록: {v['history_title']}" if v['history_title'] else '직접 올림']
        if v.get('via_task'):
            source += [' · ', task_link(v['via_task'])]
    return html.Div([
        html.Div([
            dbc.Badge(f"{v['version']}판", color='primary' if v['latest'] else 'light',
                      text_color=None if v['latest'] else 'dark', className='me-2 border'),
            html.Span(v['date'], className='me-2 text-muted'),
            html.A([html.I(className=f"bi {_icon(v['filename'])} me-1"), v['filename']], href=v['href'],
                   className='text-break me-2'),
            html.Span(_size(v['size']), className='text-muted me-2') if v['size'] else None,
            html.Span('(앞 판과 같은 파일)', className='text-muted me-2') if v['dup'] else None,
            html.Span(source, className='text-muted me-auto'),
            _version_ai_button(v),
            dbc.Button(html.I(className='bi bi-x'), id={'type': 'r-del', 'id': v['ref_id']}, color='link', size='sm',
                       className='text-muted p-0 ms-2', title='이 파일 지우기') if delete and v['kind'] == 'ref' else None,
        ], className='d-flex align-items-center flex-wrap'),
        dcc.Loading(html.Div(_version_what(v['insight']), id={'type': 'doc-v-box', 'id': v['id']}),
                    type='dot', delay_show=300),
    ], className='doc-version')


def _version_what(ins: dict | None):
    """판 한 줄 아래 — 앞 판과 달라진 점, 없으면 요약 앞부분."""
    if not ins or not ins['analyzed']:
        return None
    if ins['changes']:
        return html.Div([html.I(className='bi bi-arrow-left-right me-1'), ins['changes']], className='doc-changes mt-1')
    if ins['summary']:
        return html.Div(ins['summary'][:200] + ('…' if len(ins['summary']) > 200 else ''), className='text-muted mt-1')
    return None


def _version_ai_button(v: dict):
    """판마다 AI 요약 — 메일로 받은 판이든 올린 판이든, 바로 앞 판과 비교한다."""
    ins = v['insight']
    if v['dup'] or not ins or not ins['readable'] or not llm.is_configured():
        return None
    done = ins['analyzed']
    return dbc.Button([html.I(className='bi bi-arrow-repeat' if done else 'bi bi-magic'), '' if done else ' AI 요약'],
                      id={'type': 'doc-v-ai', 'id': v['id']}, color='link', size='sm',
                      className='p-0 text-decoration-none' + (' text-muted' if done else ''),
                      title='다시 요약 (앞 판과 비교)' if done else '문서를 읽어 요약·태그 달고 앞 판과 비교')


@callback(Output({'type': 'doc-v-box', 'id': MATCH}, 'children'), Input({'type': 'doc-v-ai', 'id': MATCH}, 'n_clicks'),
          State({'type': 'doc-v-ai', 'id': MATCH}, 'id'), prevent_initial_call=True)
def analyze_version(n, ident):
    """판 하나 요약 — id 'm12' 는 메일 첨부, 'r5' 는 올린 파일. 이미 요약했으면 다시(앞 판 비교 포함)."""
    if not n:
        return no_update
    kind, num = ident['id'][0], int(ident['id'][1:])
    run, get = ((doc_summary.analyze, doc_summary.get_insight) if kind == 'm'
                else (doc_summary.analyze_ref, doc_summary.get_ref_insight))
    done = (get(num) or {}).get('analyzed', False)
    try:
        ins = run(num, force=done)
    except (ValueError, llm.LLMError) as e:
        return dbc.Alert(str(e), color='warning', className='small py-1 px-2 mb-0 mt-1')
    return insight_view(ins)


def document_cards(docs: list[dict], delete: bool = False, empty: str = ''):
    """문서마다 카드 — 이름·판 수·최근 날짜, 최신 판의 종류·태그·요약, 아래로 판 이력(최근 판부터)."""
    from components.ui import search_href
    if not docs:
        return html.Div(empty, className='small text-muted') if empty else None
    cards = []
    for d in docs:
        ins = d['insight']
        rows = [_version_line(v, delete) for v in reversed(d['versions'])]
        cards.append(dbc.Card(dbc.CardBody([
            html.Div([
                html.I(className=f"bi {_icon(d['latest']['filename'])} me-2"),
                html.Span(d['base'], className='fw-semibold me-2'),
                html.Span(f".{d['ext']}", className='text-muted me-2') if d['ext'] else None,
                dbc.Badge(f"판 {d['n_versions']}개", color='info', className='me-2') if d['n_versions'] > 1 else None,
                html.Span(f"최근 {d['updated']}", className='small text-muted ms-auto'),
            ], className='d-flex align-items-center flex-wrap'),
            html.Div([
                dbc.Badge(ins['doc_type'], color='light', text_color='dark', className='me-1 border') if ins['doc_type'] else None,
                *[dcc.Link(f'#{t}', href=search_href(t), className='badge rounded-pill text-bg-light border text-decoration-none me-1')
                  for t in ins['tags']],
                html.Div(ins['summary'], className='small mt-1', style={'whiteSpace': 'pre-wrap'}) if ins['summary'] else None,
            ], className='mt-1') if ins else None,
            html.Div(rows[:SHOW_VERSIONS], className='doc-timeline small mt-2'),
            html.Details([html.Summary(f'이전 판 {len(rows) - SHOW_VERSIONS}개', className='small text-muted'),
                          html.Div(rows[SHOW_VERSIONS:], className='doc-timeline small mt-1')])
            if len(rows) > SHOW_VERSIONS else None,
        ]), className='mb-2'))
    return html.Div(cards)
