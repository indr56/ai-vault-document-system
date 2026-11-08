"""Token-aware chunking that never crosses page boundaries, so every chunk has one page number
for precise citations."""

from dataclasses import dataclass
from functools import lru_cache

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.services.parsing import ParsedDocument

ENCODING = "cl100k_base"  # tokenizer used by OpenAI embedding models


@dataclass
class ChunkData:
    index: int
    page_number: int | None
    content: str
    token_count: int


@lru_cache
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding(ENCODING)


def count_tokens(text: str) -> int:
    return len(_encoding().encode(text, disallowed_special=()))


def split_by_tokens(text: str, max_tokens: int) -> list[str]:
    """Hard cut into pieces of at most max_tokens (for blocks too big for one model call)."""
    tokens = _encoding().encode(text, disallowed_special=())
    return [_encoding().decode(tokens[i : i + max_tokens]) for i in range(0, len(tokens), max_tokens)]


def chunk_document(parsed: ParsedDocument, chunk_size: int, overlap: int) -> list[ChunkData]:
    # Separators prefer paragraph, then line, then sentence boundaries before splitting words.
    splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        encoding_name=ENCODING,
        chunk_size=chunk_size,
        chunk_overlap=overlap,
        separators=["\n\n", "\n", ". ", "; ", ", ", " ", ""],
    )
    chunks: list[ChunkData] = []
    for page in parsed.pages:
        for piece in splitter.split_text(page.text):
            piece = piece.strip()
            if not piece:
                continue
            chunks.append(
                ChunkData(
                    index=len(chunks),
                    page_number=page.number,
                    content=piece,
                    token_count=count_tokens(piece),
                )
            )
    return chunks
