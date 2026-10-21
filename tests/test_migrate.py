import os

import pytest

from scripts import migrate_to_postgres as mig
from services import db, store


@pytest.fixture(autouse=True)
def src_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'UPLOAD_DIR', str(tmp_path / 'uploads'))
    url = 'sqlite:///' + str(tmp_path / 'old.db')
    db.configure(url)
    yield url


def _fill():
    t = store.create_task('조직개편 준비')
    kim = store.create_person('김영수', '부사장', aliases='김부사장')
    store.add_history(t['id'], '김부사장 피드백: 일정 당겨라', '피드백', person_ids=[kim['id']])
    store.add_history(None, '김부사장 해외 출장', '동향', person_ids=[kim['id']])
    todo = store.create_todo('보고서 초안', task_id=t['id'])
    store.set_todo_done(todo['id'], True)
    return t, kim


def test_migrate_copies_everything_with_ids(src_db, tmp_path):
    t, kim = _fill()
    before = mig._counts(db.engine)
    dst = 'sqlite:///' + str(tmp_path / 'new.db')
    assert mig.migrate(src_db, dst) == before

    db.configure(dst)  # 옮긴 DB 로 앱이 그대로 동작
    assert store.get_task(t['id'])['title'] == '조직개편 준비'
    assert [h['entry_type'] for h in store.search_history('', person_id=kim['id'])] == ['동향', '피드백']
    assert store.list_todos(task_id=t['id'])[0]['done'] is True
    new = store.add_history(None, '새 동향', '동향', person_ids=[kim['id']])
    assert new['id'] > max(h['id'] for h in store.search_history('', limit=50) if h['id'] != new['id'])


def test_migrate_refuses_non_empty_target(src_db, tmp_path):
    _fill()
    dst = 'sqlite:///' + str(tmp_path / 'new.db')
    mig.migrate(src_db, dst)
    with pytest.raises(mig.MigrateError, match='이미 기록'):
        mig.migrate(src_db, dst)
    with pytest.raises(mig.MigrateError, match='같은 DB'):
        mig.migrate(src_db, src_db)


def test_main_checks_arguments(tmp_path, capsys):
    assert mig.main(['--source', 'sqlite:///' + str(tmp_path / 'none.db'), '--target', 'sqlite:///x.db']) == 1
    assert '원본 파일이 없습니다' in capsys.readouterr().out
    assert not os.path.exists('x.db')
