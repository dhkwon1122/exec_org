"""업무 이력·참고자료 관리 시스템 (exec_org).

실행:  python app.py   → http://localhost:8050
운영:  gunicorn app:server -b 0.0.0.0:8050 --workers 2
"""
import os

import dash
import dash_bootstrap_components as dbc
import flask
from dash import Input, Output, dcc, html

from services import store

app = dash.Dash(
    __name__,
    use_pages=True,
    external_stylesheets=[dbc.themes.BOOTSTRAP, dbc.icons.BOOTSTRAP],
    suppress_callback_exceptions=True,
    title='업무 기록',
)
server = app.server


@server.route('/files/<int:ref_id>')
def download_file(ref_id: int):
    ref = store.get_reference(ref_id)
    path = store.upload_abspath(ref['file_path']) if ref and ref['kind'] == 'file' else None
    if not path:
        flask.abort(404)
    return flask.send_file(path, as_attachment=True, download_name=ref['file_name'] or os.path.basename(path))


navbar = dbc.Navbar(
    dbc.Container([
        dbc.NavbarBrand([html.I(className='bi bi-journal-text me-2'), '업무 기록'], href='/'),
        dbc.Nav([
            dbc.NavItem(dbc.NavLink('업무', href='/', active='exact')),
            dbc.NavItem(dbc.NavLink('참고자료', href='/references', active='exact')),
        ], navbar=True, className='me-auto'),
        dbc.InputGroup([
            dbc.InputGroupText(html.I(className='bi bi-person')),
            dbc.Input(id='author-input', placeholder='작성자 이름', debounce=True, style={'maxWidth': '140px'}),
        ], size='sm', style={'width': 'auto'}),
    ], fluid=True),
    color='dark', dark=True, className='mb-3',
)

app.layout = html.Div([
    # 작성자 이름은 브라우저에 기억해 두고 이력·자료 기록 시 함께 남긴다
    dcc.Store(id='author-store', storage_type='local'),
    navbar,
    dbc.Container(dash.page_container, fluid=True, className='pb-5'),
])


@app.callback(Output('author-store', 'data'), Input('author-input', 'value'), prevent_initial_call=True)
def _save_author(v):
    return (v or '').strip()


@app.callback(Output('author-input', 'value'), Input('author-store', 'modified_timestamp'),
              dash.State('author-store', 'data'))
def _load_author(_, data):
    return data or ''


if __name__ == '__main__':
    app.run(debug=os.environ.get('DASH_DEBUG', '1') == '1', host='0.0.0.0', port=int(os.environ.get('PORT', 8050)))
