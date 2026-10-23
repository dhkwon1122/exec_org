"""첨부 문서에서 글자 뽑기 — AI 요약·태그(services/doc_summary.py)와 문서 검색에 쓴다.

docx·pptx·hwpx 는 압축(zip) 안의 XML 을 직접 읽고(추가 패키지 없음), xlsx 는 openpyxl, pdf 는 pypdf(없으면 건너뜀).
스캔한 pdf·이미지·예전 형식(doc/xls/ppt/hwp)은 글자를 뽑지 못하고 그 이유를 note 로 돌려준다.
"""
import io
import re
import zipfile
from datetime import date, datetime
from xml.etree import ElementTree as ET

MAX_CHARS = 200_000          # 저장하는 본문 최대 길이
MAX_XML_BYTES = 50 * 1024 * 1024  # 압축 안 XML 하나의 최대 크기(압축 폭탄 방지)
MAX_SHEET_ROWS = 2000

SUPPORTED = {'docx', 'xlsx', 'xlsm', 'pptx', 'pdf', 'hwpx', 'txt', 'csv'}
_OLD = {'doc': 'docx', 'xls': 'xlsx', 'ppt': 'pptx', 'hwp': 'hwpx'}

_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
_A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'


def ext_of(filename: str) -> str:
    return filename.rsplit('.', 1)[-1].lower() if '.' in (filename or '') else ''


def supported(filename: str) -> bool:
    return ext_of(filename) in SUPPORTED


def extract(filename: str, data: bytes) -> tuple[str, str]:
    """(본문, 안내). 본문을 못 뽑으면 본문은 '' 이고 안내에 이유."""
    ext = ext_of(filename)
    if ext not in SUPPORTED:
        if ext in _OLD:
            return '', f'예전 형식(.{ext})은 읽지 못합니다 — .{_OLD[ext]} 로 저장한 파일이면 읽을 수 있습니다.'
        return '', f'글자를 읽을 수 없는 형식입니다(.{ext or "확장자 없음"}).'
    try:
        text = _READERS[ext](data)
    except _Missing as e:
        return '', str(e)
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, OSError) as e:
        return '', f'파일을 열지 못했습니다({type(e).__name__}).'
    except Exception as e:  # noqa: BLE001 — 깨진 파일 하나로 메일 처리 전체가 멈추지 않게
        return '', f'파일을 읽다가 오류가 났습니다({type(e).__name__}).'
    text = _tidy(text)
    if not text:
        return '', '글자가 없습니다' + (' — 스캔한 이미지 pdf 일 수 있습니다.' if ext == 'pdf' else '.')
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + '\n…(이하 생략)'
    return text, ''


class _Missing(RuntimeError):
    pass


def _tidy(text: str) -> str:
    text = text.replace('\x00', '').replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'[ \t ]+\n', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _xml(z: zipfile.ZipFile, name: str) -> ET.Element:
    if z.getinfo(name).file_size > MAX_XML_BYTES:
        raise ValueError('too large')
    return ET.fromstring(z.read(name))


def _num_key(name: str) -> int:
    m = re.search(r'(\d+)\.xml$', name)
    return int(m.group(1)) if m else 0


# ── 워드 ──────────────────────────────────────────────────────────────

def _w_para(p: ET.Element) -> str:
    out = []
    for el in p.iter():
        if el.tag == _W + 't':
            out.append(el.text or '')
        elif el.tag == _W + 'tab':
            out.append('\t')
        elif el.tag in (_W + 'br', _W + 'cr'):
            out.append('\n')
    return ''.join(out)


def _w_block(el: ET.Element, lines: list[str]) -> None:
    if el.tag == _W + 'p':
        lines.append(_w_para(el))
    elif el.tag == _W + 'tbl':  # 표는 한 행을 ' | ' 로 이어 한 줄로
        for tr in el.iter(_W + 'tr'):
            cells = [' '.join(_w_para(p) for p in tc.iter(_W + 'p')).strip() for tc in tr.findall(_W + 'tc')]
            if any(cells):
                lines.append(' | '.join(cells))
        lines.append('')
    elif el.tag == _W + 'sdt':  # 내용 컨트롤(목차 등) 안쪽
        for content in el.findall(_W + 'sdtContent'):
            for child in content:
                _w_block(child, lines)


def _docx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        body = _xml(z, 'word/document.xml').find(_W + 'body')
        lines: list[str] = []
        for el in body if body is not None else []:
            _w_block(el, lines)
    return '\n'.join(lines)


# ── 엑셀 ──────────────────────────────────────────────────────────────

def _cell(v) -> str:
    if v is None:
        return ''
    if isinstance(v, datetime):
        return v.strftime('%Y-%m-%d %H:%M') if (v.hour or v.minute) else v.strftime('%Y-%m-%d')
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else f'{v:.4g}' if abs(v) < 1 else f'{v:,.2f}'.rstrip('0').rstrip('.')
    return str(v).strip()


def _xlsx(data: bytes) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts = []
    try:
        for ws in wb.worksheets:
            if getattr(ws, 'sheet_state', 'visible') != 'visible':
                continue
            rows, more = [], 0
            for row in ws.iter_rows(values_only=True):
                cells = [_cell(v) for v in row]
                while cells and not cells[-1]:
                    cells.pop()
                if not any(cells):
                    continue
                if len(rows) >= MAX_SHEET_ROWS:
                    more += 1
                    continue
                rows.append(' | '.join(cells))
            if rows:
                parts.append(f'[시트: {ws.title}]\n' + '\n'.join(rows) + (f'\n…(이하 {more}행 생략)' if more else ''))
    finally:
        wb.close()
    return '\n\n'.join(parts)


# ── 파워포인트 ────────────────────────────────────────────────────────

def _a_paras(root: ET.Element) -> list[str]:
    out = []
    for p in root.iter(_A + 'p'):
        t = ''.join((r.text or '') if r.tag == _A + 't' else '\n' if r.tag == _A + 'br' else ''
                    for r in p.iter() if r.tag in (_A + 't', _A + 'br'))
        if t.strip():
            out.append(t)
    return out


def _pptx(data: bytes) -> str:
    parts = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        slides = sorted((n for n in names if re.fullmatch(r'ppt/slides/slide\d+\.xml', n)), key=_num_key)
        for i, name in enumerate(slides, 1):
            lines = _a_paras(_xml(z, name))
            note = f'ppt/notesSlides/notesSlide{_num_key(name)}.xml'
            notes = [t for t in _a_paras(_xml(z, note)) if not t.strip().isdigit()] if note in names else []
            if lines or notes:
                parts.append(f'[슬라이드 {i}]\n' + '\n'.join(lines) + ('\n(메모) ' + ' '.join(notes) if notes else ''))
    return '\n\n'.join(parts)


# ── pdf · hwpx · 글 ──────────────────────────────────────────────────

def _pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise _Missing('pdf 를 읽으려면 서버에 pypdf 가 필요합니다 (pip install pypdf).') from e
    reader = PdfReader(io.BytesIO(data))
    parts = []
    for i, page in enumerate(reader.pages, 1):
        t = (page.extract_text() or '').strip()
        if t:
            parts.append(f'[{i}쪽]\n{t}')
    return '\n\n'.join(parts)


def _hwpx(data: bytes) -> str:
    lines = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in sorted((n for n in z.namelist() if re.fullmatch(r'Contents/section\d+\.xml', n)), key=_num_key):
            for p in _xml(z, name).iter():
                if p.tag.endswith('}p'):  # 표 속 문단도 따로 돌므로 이 문단의 글자(run 바로 아래 t)만
                    t = ''.join(el.text or '' for run in p if run.tag.endswith('}run')
                                for el in run if el.tag.endswith('}t'))
                    if t.strip():
                        lines.append(t)
    return '\n'.join(lines)


def _plain(data: bytes) -> str:
    for enc in ('utf-8-sig', 'cp949'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode('latin-1', errors='replace')


_READERS = {'docx': _docx, 'xlsx': _xlsx, 'xlsm': _xlsx, 'pptx': _pptx, 'pdf': _pdf, 'hwpx': _hwpx,
            'txt': _plain, 'csv': _plain}
