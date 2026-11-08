"""Parsing quality (tables, order, encodings, wrapped lines, scanned pages) and keyword search."""

import uuid
from pathlib import Path

import httpx
import pytest
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select

from app.db.session import get_async_sessionmaker
from app.models import Document
from app.services.chunking import chunk_document
from app.services.parsing import parse_document
from app.services.retrieval import _keyword_query, hybrid_search
from tests.conftest import SAMPLES, upload


def _pdf_with_table_and_blank_page(path: Path) -> Path:
    styles = getSampleStyleSheet()
    table = Table([["Coverage", "Limit"], ["Building", "$12,000,000"], ["Flood", "$2,000,000"]])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    SimpleDocTemplate(str(path), pagesize=letter).build(
        [
            Paragraph("Limits of Insurance", styles["Heading2"]),
            table,
            Paragraph("The limits above apply per occurrence.", styles["Normal"]),
            PageBreak(),
            Spacer(1, 10),  # page 2: no text at all (stands in for a scanned page)
            PageBreak(),
            Paragraph("Section III conditions apply to every claim made under this policy.", styles["Normal"]),
        ]
    )
    return path


class TestPdf:
    def test_table_appears_once_in_place(self, tmp_path: Path) -> None:
        parsed = parse_document(_pdf_with_table_and_blank_page(tmp_path / "t.pdf"), ".pdf")
        text = parsed.pages[0].text
        assert text.count("Building") == 1  # not also as loose text above the rows
        assert "Building | $12,000,000" in text
        assert text.index("Limits of Insurance") < text.index("Table:") < text.index("apply per occurrence")

    def test_pages_without_text_are_reported(self, tmp_path: Path) -> None:
        parsed = parse_document(_pdf_with_table_and_blank_page(tmp_path / "t.pdf"), ".pdf")
        assert [p.number for p in parsed.pages] == [1, 3]
        assert parsed.pages_without_text == [2]

    def test_wrapped_lines_are_joined_so_chunks_end_at_sentences(self) -> None:
        parsed = parse_document(SAMPLES / "acme_property_policy_2025.pdf", ".pdf")
        page3 = parsed.pages[2].text
        assert "Protective safeguards: the automatic sprinkler" in page3  # was split across two lines
        assert "Section IV - Endorsements" in page3.split("\n")  # headings keep their own line
        assert "Policy number CPP-4471-2025" in parsed.pages[0].text.split("\n")  # label lines too
        chunks = chunk_document(parsed, chunk_size=60, overlap=0)
        assert not any(c.content.startswith("safeguards") for c in chunks)


class TestDocx:
    def test_table_follows_its_heading(self) -> None:
        text = parse_document(SAMPLES / "northwind_general_liability_quote.docx", ".docx").pages[0].text
        assert "Proposed limits\nTable:\nCoverage | Limit\nEach occurrence | $1,000,000" in text
        assert text.index("Table:") < text.index("Premium and terms")


class TestTextEncodings:
    @pytest.mark.parametrize(
        ("encoding", "text"),
        [
            ("cp1252", "Prämie: 500 € – fällig"),  # what Windows editors save; was UTF-16 garbage
            ("utf-8-sig", "Prämie: 500 €"),  # UTF-8 with BOM: BOM removed
            ("utf-16", "Prämie: 500 €"),  # Python's UTF-16 always writes a BOM
            ("utf-8", "Prämie: 500 €"),
        ],
    )
    def test_decoding(self, tmp_path: Path, encoding: str, text: str) -> None:
        path = tmp_path / "notes.txt"
        path.write_bytes(f"{text} and enough text to pass the minimum length".encode(encoding))
        assert parse_document(path, ".txt").pages[0].text.startswith(text)

    def test_bytes_cp1252_cannot_decode_fall_back_to_latin1(self, tmp_path: Path) -> None:
        path = tmp_path / "notes.txt"
        path.write_bytes(b"\x81 Pr\xe4mie and enough text to pass the minimum length")  # 0x81 undefined in cp1252
        assert parse_document(path, ".txt").pages[0].text.startswith("\x81 Prämie")


# ------------------------------------------------------------------ keyword half of hybrid search
async def _policy(db, policy_id: str) -> Document:  # type: ignore[no-untyped-def]
    return (await db.execute(select(Document).where(Document.id == uuid.UUID(policy_id)))).scalar_one()


async def _keyword_rows(policy_id: str, query: str) -> list:
    async with get_async_sessionmaker()() as db:
        doc = await _policy(db, policy_id)
        return (await db.execute(_keyword_query(doc.owner_id, [doc.id], query, 24))).all()


async def test_natural_question_finds_keyword_matches(ready_docs: dict[str, str]) -> None:
    # Before: websearch_to_tsquery needed 'much' & 'flood' & 'deduct' in ONE chunk -> no rows at all.
    rows = await _keyword_rows(ready_docs["policy"], "How much is the flood deductible?")
    assert rows and rows[0].page_number == 1  # the declarations page ranks first
    assert rows[0].all_words is False  # 'much' isn't in it: a partial match does not pass the gate alone


async def test_all_words_present_sets_keyword_match(ready_docs: dict[str, str]) -> None:
    from app.llm import get_provider

    query = "flood deductible"
    embedding = (await get_provider().aembed([query])).vectors[0]
    async with get_async_sessionmaker()() as db:
        doc = await _policy(db, ready_docs["policy"])
        results = await hybrid_search(
            db, owner_id=doc.owner_id, document_ids=[doc.id], query=query, embedding=embedding, top_k=10
        )
    assert any(r.page_number == 1 and r.keyword_match for r in results)


async def test_scanned_page_warning_is_shown_on_document(client: httpx.AsyncClient, tmp_path: Path) -> None:
    pdf = _pdf_with_table_and_blank_page(tmp_path / "partly_scanned.pdf").read_bytes()
    response = await upload(client, "partly_scanned.pdf", pdf)
    doc = (await client.get(f"/documents/{response.json()['document']['id']}")).json()
    assert doc["status"] == "ready"
    assert doc["warnings"][0]["code"] == "pages_without_text"
    assert "Page 2 " in doc["warnings"][0]["message"]
