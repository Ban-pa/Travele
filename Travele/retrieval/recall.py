"""攻略召回 —— 在线侧唯一的检索入口。
"""
import logging
import pathlib

from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings

from Travele.retrieval.schemas import DocChunk

logger = logging.getLogger("travele")

_INGEST_DIR = pathlib.Path(__file__).resolve().parents[1] / "ingest"
VECTOR_DIR = _INGEST_DIR / "chroma_db"
MODEL_DIR = _INGEST_DIR / "model" / "bge-small-zh-v1.5"
COLLECTION = "rag_cosine"

# bge 中文模型的官方检索指令：查询侧要加这句前缀，文档侧不加
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："

TOP_K = 5
MIN_SCORE = 0.25        # 相关度低于这个就当成"没召回"，宁可空着也不塞不相关的

_store: Chroma | None = None


def load_store() -> Chroma:
    """懒加载向量库（连带 bge 嵌入模型一起加载）。"""
    global _store
    if _store is None:
        logger.info("正在加载嵌入模型与向量库…")
        embeddings = HuggingFaceEmbeddings(model_name=str(MODEL_DIR))
        _store = Chroma(
            collection_name=COLLECTION,
            embedding_function=embeddings,
            persist_directory=str(VECTOR_DIR),
            collection_metadata={"hnsw:space": "cosine"},
        )
    return _store


def warm_up() -> bool:
    """预热：启动时调一次，把十几秒的模型加载挪到用户来之前。失败只记日志，不拦启动。"""
    try:
        load_store()
        logger.info("召回已预热：%s / 集合 %s", VECTOR_DIR, COLLECTION)
        return True
    except Exception:
        logger.exception("召回预热失败（攻略库或嵌入模型可能还没准备好）")
        return False


def compose_keywords(themes: list[str], must_visit: list[str],
                     center_place: str | None, remarks: str) -> str:
    """把用户需求里能当检索线索的部分拼成查询词。"""
    parts = [" ".join(themes), " ".join(must_visit), center_place or "", remarks]
    return " ".join(part.strip() for part in parts if part and part.strip())


def retrieve(city: str, keywords: str = "",
             top_k: int = TOP_K, min_score: float = MIN_SCORE) -> list[DocChunk]:
    """检索这个城市的攻略块，按相关度从高到低返回。
    """
    store = load_store()
    query = QUERY_PREFIX + " ".join(part for part in (city, keywords) if part and part.strip())

    hits = store.similarity_search_with_relevance_scores(
        query, k=top_k, filter={"city": city} if city else None)

    indexes: dict[str, dict[str, int]] = {}
    chunks: list[DocChunk] = []
    for document, score in hits:
        if score < min_score:
            continue
        doc_name = str(document.metadata.get("source", ""))
        if doc_name not in indexes:
            indexes[doc_name] = _chunk_indexes(store, doc_name)
        chunks.append(DocChunk(
            text=document.page_content,
            doc=doc_name,
            chunk_index=indexes[doc_name].get(document.page_content),
            score=round(float(score), 4),
            city=str(document.metadata.get("city", "")),
        ))
    return chunks


def _chunk_indexes(store: Chroma, doc: str) -> dict[str, int]:
    """某一篇攻略里"正文 → 块序号"的映射。
    """
    try:
        stored = store.get(where={"source": doc})
    except Exception:
        logger.exception("读 %s 的块序号失败", doc)
        return {}
    documents = stored.get("documents") or []
    return {text: position for position, text in enumerate(documents)}
