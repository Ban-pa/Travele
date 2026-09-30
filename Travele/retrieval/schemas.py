"""召回功能的数据结构 —— 从攻略库里捞出来的一块正文。
"""
from pydantic import BaseModel, ConfigDict, Field


class DocChunk(BaseModel):
    """召回命中的一块攻略正文。"""

    model_config = ConfigDict(extra="ignore")

    text: str                                   # 正文
    doc: str                                    # 出处：哪一篇攻略（入库时的文件名）
    chunk_index: int | None = None              # 是这篇里的第几块（入库时没存，检索时数出来）
    score: float = Field(default=0.0, ge=0.0)   # 相关度 = 1 - 余弦距离，越大越相关
    city: str = ""                              # 入库时打的城市标签，检索按它过滤
