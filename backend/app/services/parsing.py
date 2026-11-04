"""Text extraction for PDF, DOCX, TXT and Markdown, keeping page numbers for citations."""

import re
from dataclasses import dataclass
from pathlib import Path

import pdfplumber
from docx import Document as DocxDocument
from pdfminer.pdfdocument import PDFPasswordIncorrect


@dataclass
class Page:
    number: int | None  # None for formats without real pages (docx, txt, md)
    text: str


@dataclass
class ParsedDocument:
    pages: list[Page]

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


def _parse_pdf(path: Path) -> list[Page]:
    pages: list[Page] = []
    try:
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                text = page.extract_text(layout=False) or ""
                # Tables are extracted separately so rows stay together for retrieval.
                for table in page.extract_tables() or []:
                    rows = [" | ".join(cell or "" for cell in row) for row in table if row]
                    if rows:
                        text += "\n\nTable:\n" + "\n".join(rows)
                pages.append(Page(number=i, text=_clean(text)))
    except PDFPasswordIncorrect as exc:
        raise ParseError("password_protected", "The PDF is password-protected.") from exc
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError("corrupt_file", "The PDF could not be read; it may be corrupted.") from exc

    if pages and sum(len(p.text) for p in pages) < MIN_TEXT_CHARS:
        raise ParseError(
            "no_extractable_text",
            "No text could be extracted. The PDF looks scanned (image-only); OCR is not supported yet.",
        )
    return [p for p in pages if p.text]


def _parse_docx(path: Path) -> list[Page]:
    try:
        doc = DocxDocument(str(path))
    except Exception as exc:
        raise ParseError("corrupt_file", "The Word document could not be read.") from exc
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
        parts.append("Table:\n" + "\n".join(rows))
    return [Page(number=None, text=_clean("\n".join(parts)))]


def _parse_text(path: Path) -> list[Page]:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return [Page(number=None, text=_clean(raw.decode(encoding)))]
        except UnicodeDecodeError:
            continue
    raise ParseError("encoding_error", "The text file encoding is not supported.")  # pragma: no cover


PARSERS = {".pdf": _parse_pdf, ".docx": _parse_docx, ".txt": _parse_text, ".md": _parse_text}


def parse_document(path: Path, extension: str) -> ParsedDocument:
    parser = PARSERS.get(extension)
    if parser is None:
        raise ParseError("unsupported_file_type", f"Unsupported file type: {extension}")
    pages = parser(path)
    if sum(len(p.text) for p in pages) < MIN_TEXT_CHARS:
        raise ParseError("empty_document", "The document contains no readable text.")
    return ParsedDocument(pages=pages)
