import pytest

from components.history_form import TEMPLATES, save_history
from services import db, store


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    db.configure('sqlite:///' + str(tmp_path / 'test.db'))
    yield


def test_parse_links_and_urls_in_text():
    links = store.parse_links('회의록 https://conf.example.com/pages/123\n'
                              'https://a.example.com/c.\n'
                              '\\\\fs01\\팀\\보고서.pptx 최종본\n'
                              '링크 없는 줄은 무시\n'
                              'www.example.com 참고')
    assert links == [
        {'title': '회의록', 'url': 'https://conf.example.com/pages/123'},
        {'title': 'https://a.example.com/c', 'url': 'https://a.example.com/c'},
        {'title': '최종본', 'url': '\\\\fs01\\팀\\보고서.pptx'},
        {'title': '참고', 'url': 'www.example.com'},
    ]
    assert store.urls_in_text('자료 https://x.example/a?b=1. 그리고 (https://z.example/q) 또 https://x.example/a?b=1') == [
        'https://x.example/a?b=1', 'https://z.example/q']


def test_save_history_attaches_links_urls_and_files():
    t = store.create_task('장비 도입')
    data = 'data:text/plain;base64,' + __import__('base64').b64encode('견적'.encode()).decode()
    h = save_history(t['id'], '회의', '공급사 회의. 자료는 https://conf.example.com/p/1 참고', None, False,
                     '회의록 https://conf.example.com/p/1\n공유폴더 \\\\fs01\\장비\\비교표.xlsx',
                     [data], ['견적서.txt'], '권대현')
    refs = store.get_history(h['id'])['refs']
    kinds = sorted((r['kind'], r['title']) for r in refs)
    # 입력한 링크와 원문 속 같은 링크는 한 번만, 공유폴더 경로는 https 를 붙이지 않는다
    assert kinds == [('file', '견적서.txt'), ('link', '공유폴더'), ('link', '회의록')]
    unc = next(r for r in refs if r['title'] == '공유폴더')
    assert unc['url'] == '\\\\fs01\\장비\\비교표.xlsx'
    # 이력에 붙은 자료는 '자료' 자동 이력을 따로 남기지 않는다
    assert not any(x['entry_type'] == '자료' for x in store.list_history(t['id']))
    # 업무 참고자료 목록에도 나온다 (이력 표시와 함께)
    assert all(r['history_id'] == h['id'] for r in store.list_references(task_id=t['id']))


def test_search_finds_history_by_attached_material():
    t = store.create_task('a')
    h = store.add_history(t['id'], '주간 회의')
    store.attach_links(t['id'], h['id'], [{'title': '예산 계획서', 'url': 'https://drive.example/budget'}])
    assert [x['id'] for x in store.search_history('계획서')] == [h['id']]
    assert store.search_history('계획서')[0]['refs'][0]['title'] == '예산 계획서'
    assert [x['id'] for x in store.list_tasks('계획서')] == [t['id']]


def test_attach_links_skips_duplicates_and_validates_entry():
    a = store.create_task('a')
    b = store.create_task('b')
    h = store.add_history(a['id'], 'x')
    assert store.attach_links(a['id'], h['id'], [{'url': 'www.example.com'}]) == 1
    assert store.attach_links(a['id'], h['id'], [{'url': 'www.example.com'}, {'url': 'https://www.example.com'}]) == 0
    with pytest.raises(ValueError):  # 다른 업무의 이력에는 붙일 수 없다
        store.add_reference(b['id'], 'link', url='https://x.example', history_id=h['id'])


def test_deleting_history_keeps_its_material_as_task_reference():
    t = store.create_task('a')
    h = store.add_history(t['id'], 'x')
    store.attach_links(t['id'], h['id'], [{'title': '근거', 'url': 'https://x.example'}])
    store.delete_history(h['id'])
    refs = store.list_references(task_id=t['id'])
    assert [(r['title'], r['history_id']) for r in refs] == [('근거', None)]


def test_templates_cover_entry_types():
    assert set(TEMPLATES) == set(store.ENTRY_TYPES)


def test_attachment_failure_rolls_back_history_links_and_written_files(tmp_path, monkeypatch):
    import builtins
    from pathlib import Path
    from services import enrich
    t = store.create_task('장비 도입')
    person = store.create_person('김임원')
    data = 'data:text/plain;base64,YWJj'
    real_open = builtins.open

    def fail_second_file(path, mode='r', *args, **kwargs):
        f = real_open(path, mode, *args, **kwargs)
        if mode == 'wb' and str(path).endswith('_second.txt'):
            f.write(b'partial')
            f.close()
            raise OSError('디스크 가득 참')
        return f

    with monkeypatch.context() as m:
        m.setattr(builtins, 'open', fail_second_file)
        m.setattr(enrich, 'submit', lambda *_: pytest.fail('롤백된 기록에 AI 를 요청하면 안 됨'))
        with pytest.raises(ValueError, match='첨부 파일을 저장하지 못했습니다'):
            save_history(t['id'], '회의', '김임원 회의', None, True, '자료 https://example.com',
                         [data, data], ['first.txt', 'second.txt'], '담당자', [person['id']])
    assert store.search_history('김임원') == [] and store.list_references() == []
    assert list(Path(db.UPLOAD_DIR).rglob('*.txt')) == []
    h = save_history(t['id'], '회의', '김임원 회의', None, False, '', [data], ['first.txt'], '담당자')
    assert len(store.search_history('김임원')) == 1
    assert len(h['refs']) == 1 and Path(store.upload_abspath(h['refs'][0]['file_path'])).read_bytes() == b'abc'


def test_db_failure_after_file_write_cleans_upload(tmp_path, monkeypatch):
    from pathlib import Path
    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError
    from services.db import Reference
    t = store.create_task('업무')

    def fail_insert(*_):
        raise IntegrityError('insert failed', {}, RuntimeError('DB 오류'))

    event.listen(Reference, 'before_insert', fail_insert)
    try:
        with pytest.raises(IntegrityError):
            save_history(t['id'], '메모', '저장 취소될 원문', None, False, '',
                         ['data:text/plain;base64,YWJj'], ['test.txt'], '담당자')
    finally:
        event.remove(Reference, 'before_insert', fail_insert)
    assert store.search_history('저장 취소될 원문') == []
    assert list(Path(db.UPLOAD_DIR).rglob('*.txt')) == []


def test_edit_and_new_materials_roll_back_together(monkeypatch):
    from components import history_actions
    from services import auth
    from types import SimpleNamespace
    t = store.create_task('업무')
    h = store.add_history(t['id'], '이전 원문', title='이전 제목')
    monkeypatch.setattr(history_actions, 'ctx', SimpleNamespace(triggered_id='he-save'))
    monkeypatch.setattr(auth, 'current_name', lambda: '담당자')
    add = store.add_reference

    def fail_file(*args, **kwargs):
        if args[1] == 'file':
            raise ValueError('첨부 실패')
        return add(*args, **kwargs)

    monkeypatch.setattr(store, 'add_reference', fail_file)
    result = history_actions.edit_history(None, None, 1, None, None, h['id'], '메모', None,
        '새 제목', '', '', '새 원문', '자료 https://example.com',
        ['data:text/plain;base64,YWJj'], ['test.txt'], [], [t['id']], 0)
    assert result[0] is True and result[8] == '첨부 실패'
    got = store.get_history(h['id'])
    assert got['content'] == '이전 원문' and got['title'] == '이전 제목' and got['refs'] == []
