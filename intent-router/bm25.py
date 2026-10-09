"""bm25.py — 字符二元组 BM25 相似度（第 2 层的模糊匹配）。

作用：词典能覆盖「同义词」，但覆盖不了「换个说法问同一件事」。BM25 用每个意图的
**范例句**做小语料，对当前输入打分，从而抗语序变化、同义改写与轻微错字。

分词：中文按**字符二元组**（免分词，也就不依赖 jieba；CJK 单字成串时自然成对），
ASCII 按词切分（``ora-01555`` 这类整体保留）。若环境恰好装了 jieba 也不启用——
本插件坚持零第三方依赖，行为在所有部署上一致。

IDF 在小语料上偏噪，所以这里的 BM25 分数只作**一路证据**（见 scorer.py）：
关键词证据权重更高，最终是否表态由阈值 + margin + 证据条数三重门控决定。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

K1 = 1.5
B = 0.75

# ASCII 词元（含内部连字符/点/下划线，错误码整体保留）
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._\-]*")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def tokenize(text: str) -> List[str]:
    """混合分词：ASCII 按词、CJK 按字符二元组（长度 1 的串按单字）。

    内部先 casefold：契约上调用方应传归一化文本（normalize.py），但分词是纯函数、
    被多处复用，自己兜住大小写更稳（``ORA-01555`` 与 ``ora-01555`` 必须同形）。
    """
    if not text:
        return []
    text = text.casefold()
    tokens: List[str] = []
    for match in _ASCII_TOKEN_RE.finditer(text):
        token = match.group(0)
        if len(token) > 1:
            tokens.append(token)
    # CJK：按连续块取 bigram（跨标点/ASCII 不组对，避免造出无意义组合）
    run: List[str] = []
    for ch in text:
        if _CJK_RE.match(ch):
            run.append(ch)
            continue
        _flush_run(run, tokens)
        run = []
    _flush_run(run, tokens)
    return tokens


def _flush_run(run: List[str], tokens: List[str]) -> None:
    if not run:
        return
    if len(run) == 1:
        tokens.append(run[0])
        return
    for i in range(len(run) - 1):
        tokens.append(run[i] + run[i + 1])


@dataclass
class BM25Index:
    """范例句小语料索引。``docs`` 是 (doc_id, 文本) 序列；doc_id 可重复（同一意图多条范例句）。"""

    k1: float = K1
    b: float = B
    _doc_ids: List[str] = field(default_factory=list)
    _doc_tokens: List[List[str]] = field(default_factory=list)
    _df: Dict[str, int] = field(default_factory=dict)
    _idf: Dict[str, float] = field(default_factory=dict)
    _avgdl: float = 0.0

    @classmethod
    def build(cls, docs: Sequence[Tuple[str, str]]) -> "BM25Index":
        index = cls()
        index._doc_ids = [str(doc_id) for doc_id, _text in docs]
        index._doc_tokens = [tokenize(text) for _doc_id, text in docs]
        total_terms = 0
        for tokens in index._doc_tokens:
            total_terms += len(tokens)
            for token in set(tokens):
                index._df[token] = index._df.get(token, 0) + 1
        n_docs = max(1, len(index._doc_tokens))
        index._avgdl = (total_terms / n_docs) if n_docs else 0.0
        for token, df in index._df.items():
            index._idf[token] = math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)
        return index

    @property
    def doc_count(self) -> int:
        return len(self._doc_tokens)

    @property
    def avgdl(self) -> float:
        return self._avgdl

    def score_docs(self, query: str) -> List[float]:
        """返回每个 doc 的原始 BM25 分数（与 ``build`` 时的 docs 顺序一致）。"""
        query_tokens = tokenize(query)
        if not query_tokens:
            return [0.0] * len(self._doc_tokens)
        scores: List[float] = []
        for tokens in self._doc_tokens:
            scores.append(self._score_one(query_tokens, tokens))
        return scores

    def _score_one(self, query_tokens: Sequence[str], doc_tokens: Sequence[str]) -> float:
        if not doc_tokens:
            return 0.0
        length = len(doc_tokens)
        counts: Dict[str, int] = {}
        for token in doc_tokens:
            counts[token] = counts.get(token, 0) + 1
        avgdl = self._avgdl or 1.0
        total = 0.0
        for token in query_tokens:
            freq = counts.get(token, 0)
            if not freq:
                continue
            idf = self._idf.get(token, 0.0)
            if idf <= 0.0:
                continue
            denominator = freq + self.k1 * (1.0 - self.b + self.b * length / avgdl)
            total += idf * (freq * (self.k1 + 1.0)) / denominator
        return total

    def score_by_id(self, query: str) -> Dict[str, float]:
        """按 doc_id 取最大分（同一意图多条范例句时取最相似的那条）。

        返回**所有** doc_id（无命中的记 0.0），形状稳定——调用方不必处理 KeyError。
        """
        best: Dict[str, float] = {doc_id: 0.0 for doc_id in self._doc_ids}
        for doc_id, score in zip(self._doc_ids, self.score_docs(query)):
            if score > best[doc_id]:
                best[doc_id] = score
        return best


def normalize_scores(scores: Dict[str, float]) -> Dict[str, float]:
    """把一组分数线性归一到 [0,1]（除以最大值）。"""
    if not scores:
        return {}
    peak = max(scores.values())
    if peak <= 0.0:
        return {key: 0.0 for key in scores}
    return {key: value / peak for key, value in scores.items()}
