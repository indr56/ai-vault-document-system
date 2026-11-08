"""Text extraction for PDF, DOCX, TXT and Markdown, keeping page numbers for citations."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
from docx import Document as DocxDocument
from docx.table import Table as DocxTable
from pdfminer.pdfdocument import PDFPasswordIncorrect


@dataclass
class Page:
    number: int | None  # None for formats without real pages (docx, txt, md)
    text: str


@dataclass
class ParsedDocument:
    pages: list[Page]
    # PDF pages that had no extractable text (e.g. scanned pages inside an otherwise digital PDF).
    # They are left out of `pages`, so the document is still searchable, but the user is warned.
    pages_without_text: list[int] = field(default_factory=list)

    @property
    def page_count(self) -> int | None:
        numbers = [p.number for p in self.pages if p.number is not None]
        return max(numbers) if numbers else None

    @property
    def text_with_page_markers(self) -> str:
        return "\n\n".join(f"[Page {p.number}]\n{p.text}" if p.number else p.text for p in self.pages)


class ParseError(Exception):
    """Non-retryable: the file itself is the problem, retrying will not help."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


MIN_TEXT_CHARS = 20


def _clean(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# A line counts as "wrapped" (the sentence continues on the next line) when it is at least this
# share of the page's longest line and does not end a sentence.
WRAPPED_LINE_RATIO = 0.6
_LIST_OR_HEADING = re.compile(r"^\s*([-*\u2022#|>]|\d+[.)]\s|\([a-z0-9]\)\s|[A-Z][A-Z0-9 &/-]{2,}$)")


def _join_wrapped_lines(text: str) -> str:
    """Re-join lines that a PDF broke only because the printed line was full.

    "... the insurer may cancel. Protective\nsafeguards: the sprinkler ..." becomes one line, so the
    chunker splits at sentence ends instead of in the middle of a clause. Blank lines, list items,
    headings and short label lines ("Policy number CPP-4471-2025") keep their line breaks.
    """
    lines = text.split("\n")
    longest = max((len(line.strip()) for line in lines), default=0)
    out: list[str] = []
    for line in lines:
        line = line.strip()
        if out and out[-1] and line and not _LIST_OR_HEADING.match(line):
            prev = out[-1]
            ends_sentence = prev[-1] in ".!?:;"
            wrapped = len(prev) >= WRAPPED_LINE_RATIO * longest and not ends_sentence
            if (line[0].islower() and not prev.endswith(":")) or wrapped:
                out[-1] = f"{prev} {line}"
                continue
        out.append(line)
    return "\n".join(out)


def _table_text(rows: list[list[str | None]]) -> str:
    lines = [" | ".join((cell or "").replace("\n", " ").strip() for cell in row) for row in rows if row]
    return "Table:\n" + "\n".join(lines) if lines else ""


def _center_inside(obj: dict, bbox: tuple[float, float, float, float]) -> bool:
    x = (obj["x0"] + obj["x1"]) / 2
    y = (obj["top"] + obj["bottom"]) / 2
    return bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]


def _pdf_page_text(page) -> str:  # type: ignore[no-untyped-def]
    """Page text in reading order, with each table written ONCE, as rows, where it appears.

    Text inside a table's box is removed from the flowing text (otherwise the table's words appear
    twice: once as loose text, once as "A | B" rows). The page is then read top to bottom in bands:
    text above table 1, table 1's rows, text between table 1 and 2, ... text below the last table.
    """
    tables = sorted(page.find_tables(), key=lambda t: t.bbox[1])
    body = page.filter(lambda obj: not any(_center_inside(obj, t.bbox) for t in tables)) if tables else page
    x0, top, x1, bottom = page.bbox
    parts: list[str] = []
    cursor = top
    for table in tables:
        t_top, t_bottom = max(table.bbox[1], cursor), min(table.bbox[3], bottom)
        if t_top - cursor >= 1:
            parts.append(_join_wrapped_lines(body.crop((x0, cursor, x1, t_top)).extract_text() or ""))
        parts.append(_table_text(table.extract()))
        cursor = max(cursor, t_bottom)
    if bottom - cursor >= 1:
        parts.append(_join_wrapped_lines(body.crop((x0, cursor, x1, bottom)).extract_text() or ""))
    return "\n\n".join(part for part in parts if part.strip())


def _parse_pdf(path: Path) -> ParsedDocument:
    pages: list[Page] = []
    empty: list[int] = []
    try:
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                text = _clean(_pdf_page_text(page))
                if text:
                    pages.append(Page(number=i, text=text))
                else:
                    empty.append(i)
    except PDFPasswordIncorrect as exc:
        raise ParseError("password_protected", "The PDF is password-protected.") from exc
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError("corrupt_file", "The PDF could not be read; it may be corrupted.") from exc

    if (pages or empty) and sum(len(p.text) for p in pages) < MIN_TEXT_CHARS:
        raise ParseError(
            "no_extractable_text",
            "No text could be extracted. The PDF looks scanned (image-only); OCR is not supported yet.",
        )
    return ParsedDocument(pages=pages, pages_without_text=empty)


def _parse_docx(path: Path) -> ParsedDocument:
    try:
        doc = DocxDocument(str(path))
    except Exception as exc:
        raise ParseError("corrupt_file", "The Word document could not be read.") from exc
    # Walk the body in order, so each table stays right after the heading/paragraph that explains it.
    parts: list[str] = []
    for block in doc.iter_inner_content():
        if isinstance(block, DocxTable):
            rows = [[cell.text for cell in row.cells] for row in block.rows]
            if table := _table_text(rows):
                parts.append(table)
        elif block.text.strip():
            parts.append(block.text)
    return ParsedDocument(pages=[Page(number=None, text=_clean("\n".join(parts)))])


def _decode_text(raw: bytes) -> str:
    """UTF-8 (with or without BOM) -> UTF-16 only when it has a BOM -> Windows-1252 -> Latin-1.

    UTF-16 is never guessed: almost any even-length byte string "decodes" as UTF-16 garbage.
    Windows-1252 comes before Latin-1 because it is what Windows editors save (curly quotes, euro
    sign); Latin-1 accepts every byte, so it is the last resort and never fails.
    """
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _parse_text(path: Path) -> ParsedDocument:
    try:
        text = _decode_text(path.read_bytes())
    except UnicodeDecodeError as exc:  # a BOM followed by invalid UTF-16
        raise ParseError("encoding_error", "The text file encoding is not supported.") from exc
    return ParsedDocument(pages=[Page(number=None, text=_clean(text))])


PARSERS = {".pdf": _parse_pdf, ".docx": _parse_docx, ".txt": _parse_text, ".md": _parse_text}


def parse_document(path: Path, extension: str) -> ParsedDocument:
    parser = PARSERS.get(extension)
    if parser is None:
        raise ParseError("unsupported_file_type", f"Unsupported file type: {extension}")
    parsed = parser(path)
    parsed.pages = [p for p in parsed.pages if p.text]
    if sum(len(p.text) for p in parsed.pages) < MIN_TEXT_CHARS:
        raise ParseError("empty_document", "The document contains no readable text.")
    return parsed
