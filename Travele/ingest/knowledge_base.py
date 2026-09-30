"""
知识库服务 - 使用本地 HuggingFace Embedding
"""
import os
import hashlib
from datetime import datetime

import config_data as config
from city_extract import extract_city          # 从正文提取城市（用于 metadata 过滤）
from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter


def check_md5(md5_str: str) -> bool:
    """检查传入的 md5 字符串是否已经被处理过"""
    if not os.path.exists(config.md5_path):
        # 文件不存在则创建空文件，并返回 False（未处理）
        open(config.md5_path, 'w', encoding='utf-8').close()
        return False
    else:
        with open(config.md5_path, 'r', encoding='utf-8') as f:
            for line in f.readlines():
                line = line.strip()
                if line == md5_str:
                    return True
        return False


def save_md5(md5_str: str) -> None:
    """将 md5 字符串记录到文件内保存"""
    with open(config.md5_path, "a", encoding="utf-8") as f:
        f.write(md5_str + '\n')


def get_string_md5(content: str) -> str:
    """计算字符串的 MD5 值（UTF-8 编码）"""
    str_bytes = content.encode(encoding='utf-8')   # 使用传入的 content
    md5_obj = hashlib.md5()
    md5_obj.update(str_bytes)
    return md5_obj.hexdigest()


class KnowledgeBaseService:
    def __init__(self):
        # 确保向量库持久化目录存在
        os.makedirs(config.persist_directory, exist_ok=True)

        # ---------- 使用本地 HuggingFace 嵌入 ----------
        self.embeddings = HuggingFaceEmbeddings(
            model_name = config.models_name,
        )

        # 实例化 Chroma 向量数据库
        # collection_metadata：显式指定**余弦度量**——在线检索按"1-距离"算相关度
        # 并做 min_score 阈值，L2 距离下不成立（集合已存在时该参数不生效、沿用原度量）
        self.chroma = Chroma(
            collection_name=config.collection_name,
            embedding_function=self.embeddings,          # 使用本地嵌入
            persist_directory=config.persist_directory,
            collection_metadata={"hnsw:space": "cosine"},
        )

        # 文本分割器
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
            separators=config.separators,
            length_function=len,
        )

    def upload_by_str(self, data: str, filename: str) -> str:
        """
        将传入的文本字符串向量化并存入向量库
        """
        md5_hex = get_string_md5(data)    # 传入 data 而非 date
        if check_md5(md5_hex):
            return "[跳过] 内容已存在知识库中"

        # 根据文本长度决定是否分割
        if len(data) > config.max_split_char_number:
            knowledge_chunks: list[str] = self.splitter.split_text(data)
        else:
            knowledge_chunks = [data]

        # 构造元数据：city 从正文自动提取（检索时按城市精确过滤要用它）
        city = extract_city(data, filename)
        metadata = {
            "city": city or "",
            "source": filename,
            "create_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "operator": "Ban-pa"
        }

        # 添加文本块和对应元数据（每个块共享同一份 metadata）
        self.chroma.add_texts(
            texts=knowledge_chunks,
            metadatas=[metadata for _ in knowledge_chunks],
        )

        save_md5(md5_hex)
        return f"[成功] 内容已载入向量库（识别城市：{city or '未识别'}）"