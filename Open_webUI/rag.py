"""
title: QC Knowledge Search
requirements: qdrant-client,FlagEmbedding,transformers
version: 0.3.0
"""

from collections import Counter

from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct
from FlagEmbedding import BGEM3FlagModel

_embed_model = None


def get_embed_model():
    global _embed_model
    if _embed_model is None:
        _embed_model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False)
    return _embed_model


class Tools:
    class Valves(BaseModel):
        QDRANT_URL: str = Field(
            default="http://qdrant:6333",
            description="URL Qdrant с проиндексированными документами",
        )
        COLLECTION: str = Field(
            default="doc_chunks", description="Имя коллекции в Qdrant"
        )
        TOP_K: int = Field(
            default=4, description="Сколько релевантных фрагментов возвращать"
        )

    def __init__(self):
        self.valves = self.Valves()

    def search_knowledge_base(self, query: str) -> str:
        """
        Ищет наиболее релевантные фрагменты в базе знаний (проиндексированные PDF/Word
        документы — регламенты, SOP, инструкции) по смысловому сходству с запросом.
        Используй этот тул, когда вопрос касается СОДЕРЖАНИЯ внутренней документации —
        например, нужно процитировать, найти определение или объяснить процедуру.
        Если вопрос про то, КАКИЕ документы вообще загружены в базу — используй
        list_indexed_files, а не этот тул.

        :param query: вопрос или тема для поиска по базе знаний
        """
        try:
            model = get_embed_model()
            query_emb = model.encode([query])["dense_vecs"][0].tolist()

            client = QdrantClient(url=self.valves.QDRANT_URL)
            result = client.query_points(
                collection_name=self.valves.COLLECTION,
                query=query_emb,
                with_payload=True,
                limit=self.valves.TOP_K,
            ).points
        except Exception as e:
            return f"Ошибка поиска по базе знаний: {e}"

        if not result:
            return "В базе знаний ничего релевантного не найдено."

        parts = [
            f"[Источник: {p.payload['source_file']}, фрагмент {p.payload['chunk_index']}, "
            f"релевантность {p.score:.2f}]\n{p.payload['content']}"
            for p in result
        ]
        return "\n\n---\n\n".join(parts)

    def list_indexed_files(self) -> str:
        """
        Возвращает список всех документов (имена файлов), которые сейчас проиндексированы
        в базе знаний, вместе с количеством чанков по каждому. Используй этот тул, когда
        пользователь спрашивает "какие документы загружены", "что есть в базе знаний",
        "сколько файлов проиндексировано" и т.п. — то есть мета-вопрос о СОСТАВЕ базы,
        а не о её содержании. Не путай с отдельной встроенной функцией списка баз знаний
        Open WebUI — она к этой базе не относится, здесь используется собственное
        Qdrant-хранилище проекта.
        """
        try:
            client = QdrantClient(url=self.valves.QDRANT_URL)

            if not client.collection_exists(self.valves.COLLECTION):
                return f"Коллекция '{self.valves.COLLECTION}' в Qdrant ещё не создана — документы не индексировались."

            counts = Counter()
            next_offset = None
            while True:
                points, next_offset = client.scroll(
                    collection_name=self.valves.COLLECTION,
                    with_payload=["source_file"],
                    with_vectors=False,
                    limit=256,
                    offset=next_offset,
                )
                for p in points:
                    counts[p.payload.get("source_file", "неизвестно")] += 1
                if next_offset is None:
                    break
        except Exception as e:
            return f"Ошибка получения списка документов: {e}"

        if not counts:
            return "В базе знаний пока нет ни одного проиндексированного документа."

        total_chunks = sum(counts.values())
        lines = [
            f"Проиндексировано файлов: {len(counts)}, всего чанков: {total_chunks}",
            "",
        ]
        for fname, n in sorted(counts.items()):
            lines.append(f"  - {fname} ({n} чанков)")
        return "\n".join(lines)
