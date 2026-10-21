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


def enrich_entry(entry_id: int, token: str | None = None) -> None:
    """동기 처리 — 결과를 DB 에 저장한다. 예외는 밖으로 내보내지 않는다."""
    token = token or store.begin_history_ai(entry_id)
    entry = store.get_history(entry_id)
    if entry is None or entry['ai_token'] != token:
        return
    task = store.get_task(entry['task_id']) if entry['task_id'] else None
    try:
        people = [f"{p['name']} {p['position']}".strip() for p in entry['people']]
        result = llm.analyze_record(entry['content'], task['title'] if task else '', entry['entry_type'], people,
                                   reference_date=entry['occurred_at'][:10] or None)
    except Exception as e:  # noqa: BLE001 — 어떤 실패든 기록에 남기고 화면에서 재시도
        store.set_history_ai(entry_id, 'failed', error=str(e)[:500], token=token)
        return
    store.set_history_ai(entry_id, 'done', token=token, reference_date=entry['occurred_at'][:10], **result)


def submit(entry_id: int) -> None:
    """AI 정리를 요청한다. LLM 미설정이면 아무것도 하지 않는다."""
    configured = llm.is_configured()
    token = store.begin_history_ai(entry_id, 'pending' if configured else '')
    if not configured or token is None:
        return
    try:
        _executor.submit(enrich_entry, entry_id, token)
    except RuntimeError as e:
        store.set_history_ai(entry_id, 'failed', error=str(e)[:500], token=token)
