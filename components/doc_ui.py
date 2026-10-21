"""메일 첨부 문서의 판(버전) 보기 — 받은 메일 상세와 업무 상세가 같이 쓴다 (services/doc_versions.py)."""
import dash_bootstrap_components as dbc
from dash import dcc, html

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
            ]))
        if n > 1 and not any(f['latest'] for f in g['files']):
            head.append(html.Div(['최신 판: ', _file_link(latest),
                                  html.Span(f" ({latest['mail_date']} 메일)", className='text-muted')]))
        items.append(dbc.ListGroupItem([
            *head,
            html.Details([
                html.Summary(f"'{g['base']}' 판 이력 {len(g['versions'])}건", className='text-muted'),
                html.Ul([_version_row(v, current_mail) for v in reversed(g['versions'])],
                        className='list-unstyled mt-1 mb-0 ps-2'),
            ]) if len(g['versions']) > 1 else None,
        ], className='small'))
    return dbc.ListGroup(items)
