"""Fast unit tests for pure logic (no network)."""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.errors import PayloadTooLargeError, UnsupportedMediaError, ValidationFailedError
from app.llm.base import Usage
from app.services.chat import PreparedQuestion, extract_citations
from app.services.chunking import chunk_document, count_tokens
from app.services.documents import validate_upload
from app.services.parsing import Page, ParsedDocument, ParseError, parse_document
from app.services.retrieval import RetrievedChunk, _fuse
from app.services.usage import estimate_cost_usd
from tests.conftest import SAMPLES


class TestParsing:
    def test_pdf_keeps_page_numbers_and_tables(self) -> None:
        parsed = parse_document(SAMPLES / "acme_property_policy_2025.pdf", ".pdf")
        assert parsed.page_count == 3
        assert parsed.pages[0].number == 1
        assert "$25,000" in parsed.pages[0].text  # flood deductible on the declarations page
        assert "CPP-4471-2025" in parsed.text_with_page_markers

    def test_docx_includes_table_cells(self) -> None:
        parsed = parse_document(SAMPLES / "northwind_general_liability_quote.docx", ".docx")
        assert "Each occurrence" in parsed.pages[0].text and "$1,000,000" in parsed.pages[0].text

    def test_markdown(self) -> None:
        parsed = parse_document(SAMPLES / "bayline_broker_service_agreement.md", ".md")
        assert "$15,000" in parsed.pages[0].text

    @pytest.mark.parametrize(
        ("name", "code"),
        [("edge-cases/scanned_no_text.pdf", "no_extractable_text"), ("edge-cases/corrupt.pdf", "corrupt_file")],
    )
    def test_bad_pdfs_fail_with_clear_codes(self, name: str, code: str) -> None:
        with pytest.raises(ParseError) as exc:
            parse_document(SAMPLES / name, ".pdf")
        assert exc.value.code == code


class TestValidation:
    def test_rejects_unknown_extension(self) -> None:
        with pytest.raises(UnsupportedMediaError):
            validate_upload("virus.exe", b"MZ")

    def test_rejects_content_that_does_not_match_extension(self) -> None:
        with pytest.raises(UnsupportedMediaError):
            validate_upload("report.pdf", b"MZ\x90 not a pdf")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationFailedError):
            validate_upload("notes.txt", b"")

    def test_rejects_too_large(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "max_upload_mb", 0)
        with pytest.raises(PayloadTooLargeError):
            validate_upload("notes.txt", b"hello")

    def test_normalises_extension(self) -> None:
        assert validate_upload("Policy.PDF", b"%PDF-1.7 ...") == ".pdf"


class TestChunking:
    def test_chunks_respect_size_and_keep_pages(self) -> None:
        text = " ".join(f"Sentence number {i} about coverage." for i in range(400))
        parsed = ParsedDocument(pages=[Page(1, text), Page(2, "Short second page.")])
        chunks = chunk_document(parsed, chunk_size=100, overlap=20)
        assert len(chunks) > 5
        assert all(c.token_count <= 100 for c in chunks)
        assert chunks[-1].page_number == 2
        assert [c.index for c in chunks] == list(range(len(chunks)))

    def test_count_tokens(self) -> None:
        assert count_tokens("hello world") == 2


class TestRetrievalFusion:
    def _row(self, cid: uuid.UUID, similarity: float = 0.5, all_words: bool = True) -> SimpleNamespace:
        return SimpleNamespace(
            id=cid,
            document_id=uuid.uuid4(),
            chunk_index=0,
            page_number=1,
            content="x",
            similarity=similarity,
            all_words=all_words,
        )

    def test_rrf_rewards_chunks_found_by_both_searches(self) -> None:
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        fused = _fuse([self._row(a), self._row(b)], [self._row(b), self._row(c, all_words=False)], top_k=3)
        assert fused[0].chunk_id == b
        assert fused[0].keyword_match is True  # contains every word of the question
        assert {f.chunk_id for f in fused} == {a, b, c}
        assert next(f for f in fused if f.chunk_id == c).keyword_match is False  # only some words


class TestCitations:
    def test_maps_numbers_to_chunks_and_ignores_invented_ones(self) -> None:
        doc_id = uuid.uuid4()
        chunk = RetrievedChunk(uuid.uuid4(), doc_id, 3, 7, "The flood deductible is $25,000.", 0.8, 0.1)
        prepared = PreparedQuestion(
            question="q", standalone_question="q", documents=[SimpleNamespace(id=doc_id, title="Policy")], history=[]
        )
        prepared.chunks = [chunk]
        citations = extract_citations("It is $25,000 [1]. Also [1] and [9].", prepared)
        assert len(citations) == 1
        assert citations[0]["page_number"] == 7 and citations[0]["document_title"] == "Policy"

    def test_not_found_answer_cites_only_what_it_refers_to(self) -> None:
        doc_id = uuid.uuid4()
        prepared = PreparedQuestion(
            question="q", standalone_question="q", documents=[SimpleNamespace(id=doc_id, title="Policy")], history=[]
        )
        prepared.chunks = [RetrievedChunk(uuid.uuid4(), doc_id, 0, 1, "text", 0.5, 0.1)]
        # It says what the documents DO say, with a mark: the mark must open its source (#80; it used to
        # be dropped, leaving a [1] that led nowhere). The answer still counts as "not found".
        answer = "I couldn't find this in the selected documents. They mention Acme [1]."
        assert [c["number"] for c in extract_citations(answer, prepared)] == [1]
        assert extract_citations("I couldn't find this in the selected documents.", prepared) == []


class TestCost:
    def test_cost_uses_base_model_price_for_dated_ids(self) -> None:
        usage = Usage(model="gpt-5.4-mini-2026-03-17", prompt_tokens=1_000_000, completion_tokens=1_000_000)
        assert estimate_cost_usd(usage) == Decimal("5.25")

    def test_unknown_model_costs_zero(self) -> None:
        assert estimate_cost_usd(Usage(model="mystery", prompt_tokens=10)) == Decimal("0.0")


def test_chat_prompt_asks_for_the_working_before_each_result() -> None:
    """After two earlier turns, chat-v6 stated results before its working and got them wrong in 19 of
    20 runs ("$3,650 higher before the policy fee, or $3,900 including it"). chat-v7 asks for the
    working first and each result once; measured 0 of 30 on the same passages. Keep both parts."""
    from app.llm.prompts import chat as chat_prompt

    rule = chat_prompt.SYSTEM[chat_prompt.SYSTEM.index("7. Calculate") : chat_prompt.SYSTEM.index("8. Be concise")]
    words = " ".join(rule.split())
    assert "Write the working first" in words
    assert "give each computed result only once, at the end of its working line" in words
    assert "never put a computed value in quotation marks" in words
    assert chat_prompt.VERSION == "chat-v7"
