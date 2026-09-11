"""
ingest_docs.py — индексация PDF/DOCX в Qdrant с эмбеддингами BGE-M3.

Запуск:
    docker exec -it open-webui pip install pypdf python-docx qdrant-client FlagEmbedding --break-system-packages
    docker exec -it open-webui python3 /app/rag_documents/ingest_docs.py
"""

import os
import re
import sys
import uuid

from pypdf import PdfReader
import docx
from transformers import AutoTokenizer
from FlagEmbedding import BGEM3FlagModel
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue

DOCS_DIR = "/app/rag_documents"
QDRANT_URL = "http://qdrant:6333"
COLLECTION = "doc_chunks"
VECTOR_SIZE = 1024  # размерность плотного эмбеддинга BGE-M3
MAX_TOKENS = 450
OVERLAP_TOKENS = 60

_tokenizer = AutoTokenizer.from_pretrained("BAAI/bge-m3")
_embed_model = None


def get_embed_model():
    global _embed_model
    if _embed_model is None:
        print("Загружаю BGE-M3 (первый раз — скачает ~2.3 ГБ, дальше из кэша)...")
        _embed_model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False)  # fp16 смысла на CPU не имеет
    return _embed_model


def embed_texts(texts):
    model = get_embed_model()
    return model.encode(texts, batch_size=8, max_length=MAX_TOKENS + OVERLAP_TOKENS)["dense_vecs"]


def extract_text(path: str) -> str:
    if path.lower().endswith(".pdf"):
        reader = PdfReader(path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    elif path.lower().endswith(".docx"):
        d = docx.Document(path)
        return "\n".join(p.text for p in d.paragraphs)
    return ""


def _n_tokens(text: str) -> int:
    return len(_tokenizer.encode(text, add_special_tokens=False))


def _split_long_paragraph(text: str, max_tokens: int):
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, current = [], ""
    for sent in sentences:
        candidate = f"{current} {sent}".strip()
        if _n_tokens(candidate) <= max_tokens:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = sent
    if current:
        chunks.append(current)
    return chunks


def chunk_text(text: str, max_tokens=MAX_TOKENS, overlap_tokens=OVERLAP_TOKENS):
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, current = [], ""
    for para in paragraphs:
        if _n_tokens(para) > max_tokens:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_paragraph(para, max_tokens))
            continue
        candidate = f"{current}\n\n{para}".strip()
        if _n_tokens(candidate) <= max_tokens:
            current = candidate
        else:
            chunks.append(current)
            current = para
    if current:
        chunks.append(current)

    overlapped = []
    for i, c in enumerate(chunks):
        if i == 0:
            overlapped.append(c)
            continue
        prev_tail = _tokenizer.decode(
            _tokenizer.encode(chunks[i - 1], add_special_tokens=False)[-overlap_tokens:]
        )
        overlapped.append(f"{prev_tail} {c}")
    return overlapped


def main():
    client = QdrantClient(url=QDRANT_URL)
    if not client.collection_exists(COLLECTION):
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )

    if not os.path.isdir(DOCS_DIR):
        print(f"Папка {DOCS_DIR} не найдена — проверьте volume в docker-compose Open WebUI.")
        sys.exit(1)

    files = [f for f in os.listdir(DOCS_DIR) if f.lower().endswith((".pdf", ".docx"))]
    if not files:
        print(f"В {DOCS_DIR} не найдено PDF/DOCX файлов.")
        sys.exit(0)

    for fname in files:
        path = os.path.join(DOCS_DIR, fname)
        print(f"Обрабатываю {fname}...")
        text = extract_text(path)
        if not text.strip():
            print(f"  Не удалось извлечь текст (похоже на скан — нужен OCR, отдельный шаг).")
            continue

        chunks = chunk_text(text)
        print(f"  {len(chunks)} чанков, считаю эмбеддинги...")
        embeddings = embed_texts(chunks)

        # идемпотентность: удаляем старые чанки этого файла перед перезаливкой
        client.delete(
            collection_name=COLLECTION,
            points_selector=Filter(
                must=[FieldCondition(key="source_file", match=MatchValue(value=fname))]
            ),
        )

        points = [
            PointStruct(
                id=str(uuid.uuid4()),
                vector=emb.tolist(),
                payload={"source_file": fname, "chunk_index": i, "content": chunk},
            )
            for i, (chunk, emb) in enumerate(zip(chunks, embeddings))
        ]
        client.upsert(collection_name=COLLECTION, points=points, wait=True)

    print("\nГотово. Индексация завершена.")


if __name__ == "__main__":
    main()
