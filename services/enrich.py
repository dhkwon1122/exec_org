"""이력 원문 → LLM 요약·키워드 생성을 백그라운드에서 처리.

원문은 저장 즉시 DB 에 남고(ai_status=pending), 요약·키워드는 이 모듈의 스레드가
채운다. LLM 이 느리거나 죽어 있어도 기록 자체는 절대 잃지 않는다. 실패하면
ai_status=failed 와 오류 메시지가 남고 화면에서 "다시 생성"할 수 있다.
"""
import os
from concurrent.futures import ThreadPoolExecutor

from services import llm, store

_executor = ThreadPoolExecutor(max_workers=int(os.environ.get('LLM_MAX_CONCURRENT', '2')),
                               thread_name_prefix='enrich')


def enrich_entry(entry_id: int) -> None:
    """동기 처리 — 결과를 DB 에 저장한다. 예외는 밖으로 내보내지 않는다."""
    entry = store.get_history(entry_id)
    if entry is None:
        return
    task = store.get_task(entry['task_id'])
    try:
        result = llm.analyze_record(entry['content'], task['title'] if task else '', entry['entry_type'])
    except Exception as e:  # noqa: BLE001 — 어떤 실패든 기록에 남기고 화면에서 재시도
        store.set_history_ai(entry_id, 'failed', error=str(e)[:500])
        return
    store.set_history_ai(entry_id, 'done', **result)


def submit(entry_id: int) -> None:
    """AI 정리를 요청한다. LLM 미설정이면 아무것도 하지 않는다."""
    if not llm.is_configured():
        store.set_history_ai(entry_id, '')
        return
    store.set_history_ai(entry_id, 'pending')
    _executor.submit(enrich_entry, entry_id)
