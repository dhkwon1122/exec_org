"""기간별 통합 보고서: 범위 선택 → 근거가 붙은 초안 → 편집·내려받기·메일 작성."""
from datetime import date, timedelta

import dash
import dash_bootstrap_components as dbc
import flask
from dash import Input, Output, State, callback, clientside_callback, dcc, html, no_update

from components import drafts, ui
from components.history_form import people_options, task_options
from services import llm, reports

dash.register_page(__name__, path='/reports', name='통합 보고서', title='업무 기록 · 통합 보고서')


def layout(**_):
    today = date.today()
    return html.Div([
        ui.page_head('통합 보고서', '여러 업무와 인물의 기록을 기간별로 정리합니다'),
        dbc.Card(dbc.CardBody([
            dbc.Row([
                dbc.Col([dbc.Label('시작일'), dbc.Input(id='rp-from', type='date', value=(today - timedelta(days=6)).isoformat())], md=3),
                dbc.Col([dbc.Label('종료일'), dbc.Input(id='rp-to', type='date', value=today.isoformat())], md=3),
                dbc.Col([dbc.Label('업무 (비우면 전체)'), dcc.Dropdown(id='rp-tasks', options=task_options(), multi=True)], md=6),
                dbc.Col([dbc.Label('임원·리더 (비우면 전체)'), dcc.Dropdown(id='rp-people', options=people_options(), multi=True)], md=8),
                dbc.Col([dbc.Checkbox(id='rp-children', label='하위 업무 포함', value=True),
                         dbc.Checkbox(id='rp-ai', label='AI로 문장 정리', value=False, disabled=not llm.is_configured())], md=4),
            ], className='g-3 mb-3'),
            html.Div('업무와 인물을 함께 고르면 두 조건에 모두 해당하는 기록을 모읍니다. '
                     '기간·대상을 바꾼 뒤에는 다시 생성해 주세요.', className='small text-muted mb-2'),
            dbc.Button('보고서 초안 만들기', id='rp-generate', color='primary'),
        ]), className='mb-3'),
        dcc.Loading(html.Div(id='rp-note'), type='dot'),
        drafts.widget('rp', 'report:latest'),
        dbc.Tabs([
            dbc.Tab(dbc.Textarea(id='rp-text', value='', rows=18, className='report-editor',
                                placeholder='보고서 초안을 생성하거나 저장된 초안을 불러오세요.'), label='초안 편집', tab_id='edit'),
            dbc.Tab(dcc.Markdown(id='rp-preview', className='report-preview p-3'), label='미리보기', tab_id='preview'),
        ], id='rp-tabs', active_tab='edit', className='mb-2'),
        html.Div([
            dbc.Button('Markdown 내려받기', id='rp-download-btn', color='secondary', outline=True),
            dbc.Button('메일로 작성', id='rp-mail', color='secondary', outline=True),
            dbc.Button('본문 복사', id='rp-copy', color='secondary', outline=True),
            html.Span(id='rp-copy-status', role='status', className='small text-muted'),
        ], className='d-flex flex-wrap align-items-center gap-2 mt-3'),
        dcc.Download(id='rp-download'),
    ])


drafts.register('rp', ['rp-text', 'rp-from', 'rp-to', 'rp-tasks', 'rp-people', 'rp-children', 'rp-ai'])


@callback(Output('rp-text', 'value', allow_duplicate=True), Output('rp-note', 'children'),
          Input('rp-generate', 'n_clicks'), State('rp-from', 'value'), State('rp-to', 'value'),
          State('rp-tasks', 'value'), State('rp-people', 'value'), State('rp-children', 'value'), State('rp-ai', 'value'),
          prevent_initial_call=True, running=[(Output('rp-generate', 'disabled'), True, False)])
def generate(_n, first, last, tasks, people, children, ai):
    try:
        snapshot = reports.collect(first, last, tasks, people, bool(children))
        report = reports.render(snapshot, base_url=flask.request.host_url.rstrip('/'), ai=bool(ai))
    except ValueError as error:
        return no_update, dbc.Alert(str(error), color='danger')
    note = report['note'] or '원문과 실제 업무 상태로 만든 기본 초안입니다. 내용을 확인하고 공유하세요.'
    if snapshot['limited']:
        note += ' 표시 한도에 도달했습니다. 기간이나 범위를 줄여 주세요.'
    return report['text'], dbc.Alert(note, color='warning' if snapshot['limited'] else 'info', className='small')


@callback(Output('rp-preview', 'children'), Output('rp-copy', 'disabled'), Output('rp-download-btn', 'disabled'),
          Output('rp-mail', 'disabled'), Input('rp-text', 'value'))
def preview(text):
    return text or '', not bool(text and text.strip()), not bool(text and text.strip()), not bool(text and text.strip())


clientside_callback("""async function(n, text) {
    if (!n || !text) return dash_clientside.no_update;
    try {
        if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
        else {
            const field = document.createElement('textarea'); field.value = text;
            field.style.position = 'fixed'; field.style.top = '0'; document.body.appendChild(field);
            field.select(); const ok = document.execCommand('copy'); field.remove();
            if (!ok) throw new Error('copy');
        }
        return '본문을 복사했습니다.';
    } catch (_) { return '복사하지 못했습니다. Markdown 내려받기를 이용해 주세요.'; }
}""", Output('rp-copy-status', 'children'), Input('rp-copy', 'n_clicks'), State('rp-text', 'value'),
    prevent_initial_call=True)


@callback(Output('rp-download', 'data'), Input('rp-download-btn', 'n_clicks'), State('rp-text', 'value'),
          prevent_initial_call=True)
def download(_, text):
    return dcc.send_string(text, f'업무보고_{date.today().isoformat()}.md') if text else no_update


@callback(Output('mail-draft', 'data', allow_duplicate=True), Input('rp-mail', 'n_clicks'),
          State('rp-text', 'value'), prevent_initial_call=True)
def mail(_, text):
    if not text or not text.strip():
        return no_update
    return {'subject': text.splitlines()[0].lstrip('# ').strip()[:300], 'text': text,
            'table_html': '', 'to': '', 'attachments': [], 'related_kind': 'report', 'related_id': ''}
