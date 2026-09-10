"""Text extraction from documents. Pure functions, run in the media worker."""
import csv
import io
import json
import zipfile
from pathlib import Path

MAX_CHARS = 300_000


def _cap(text: str, meta: dict) -> str:
    if len(text) > MAX_CHARS:
        meta["truncated"] = True
        return text[:MAX_CHARS]
    return text


def extract_pdf(path: Path, meta: dict) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    meta["pages"] = len(reader.pages)
    parts = []
    for i, page in enumerate(reader.pages):
        txt = page.extract_text() or ""
        if txt.strip():
            parts.append(f"--- page {i + 1} ---\n{txt}")
    text = "\n".join(parts)
    if not text.strip():
        meta["scanned"] = True
    return text


def extract_docx(path: Path, meta: dict) -> str:
    import docx

    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for t_i, table in enumerate(d.tables):
        parts.append(f"--- table {t_i + 1} ---")
        for row in table.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    meta["paragraphs"] = len(d.paragraphs)
    return "\n".join(parts)


def extract_xlsx(path: Path, meta: dict, max_rows: int = 2000) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    parts = []
    meta["sheets"] = wb.sheetnames
    for ws in wb.worksheets:
        parts.append(f"--- sheet {ws.title} ---")
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= max_rows:
                parts.append(f"... ({max_rows} rows shown)")
                break
            if any(v is not None for v in row):
                parts.append("\t".join("" if v is None else str(v) for v in row))
    return "\n".join(parts)


def extract_txt(path: Path, meta: dict) -> str:
    raw = path.read_bytes()[: MAX_CHARS * 4]
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_csv(path: Path, meta: dict, max_rows: int = 2000) -> str:
    text = extract_txt(path, meta)
    rows = list(csv.reader(io.StringIO(text)))
    meta["rows"] = len(rows)
    return "\n".join("\t".join(r) for r in rows[:max_rows])


def extract_json(path: Path, meta: dict) -> str:
    text = extract_txt(path, meta)
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, indent=1)
    except Exception:
        return text


def extract_zip(path: Path, meta: dict) -> str:
    with zipfile.ZipFile(str(path)) as z:
        names = z.namelist()
    meta["entries"] = len(names)
    return "zip contents:\n" + "\n".join(names[:500])


EXTRACTORS = {
    "pdf": extract_pdf, "docx": extract_docx, "xlsx": extract_xlsx, "txt": extract_txt, "md": extract_txt,
    "csv": extract_csv, "json": extract_json, "zip": extract_zip,
}


def extract(path: Path, kind: str) -> tuple[str, dict]:
    meta: dict = {"kind": kind}
    fn = EXTRACTORS.get(kind)
    if fn is None:
        raise ValueError(f"no extractor for {kind}")
    return _cap(fn(path, meta), meta), meta
