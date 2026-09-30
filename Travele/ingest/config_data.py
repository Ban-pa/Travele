"""离线攻略管道（功能①：入库）的配置。

约定（重要）：
- collection_name 必须是**余弦度量**的集合（rag_cosine），因为在线检索
  （Travele/retrieval/recall.py）用"1 - 距离"当相关度、用 min_score 做阈值；
  L2 距离下这个算法不成立。建库时会带 hnsw:space=cosine，集合已存在则沿用原度量。
- persist_directory / md5_path 是**相对启动目录**解析的：请从本目录
  （Travele/ingest）启动上传脚本，否则会写到别的地方。
"""
import os

md5_path = "./md5.text"

collection_name = "rag_cosine"          # 与在线检索 Travele/retrieval/recall.py 保持一致
persist_directory = "./chroma_db"

chunk_size = 1000
chunk_overlap = 100
separators = ["\n\n", "\n", ".", "。", ",", "，", ";", "!", " "]
max_split_char_number = 1000            # 文本分割的阈值

models_name = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model", "bge-small-zh-v1.5")
