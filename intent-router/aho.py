"""aho.py — Aho-Corasick 多模式匹配（第 2 层的词表扫描）。

为什么不用「N 条正则遍历」：词表会长到几百上千条别名，逐条 ``re.search`` 是
O(词表 × 文本) 且每条正则都是一次独立回溯；Aho-Corasick 把词表编译成一台自动机，
对文本**单次线性扫描**输出全部命中，延迟基本与词表规模无关，也没有回溯风险——
这一点对「hook 在对话关键路径上」的场景是硬要求。

纯 stdlib 实现（trie + fail 指针 + 输出继承），构建语义与经典 AC 一致；
``tests/test_algorithms.py`` 用暴力匹配做逐例对齐验证。
"""
from __future__ import annotations

from collections import deque
from typing import Dict, Iterable, Iterator, List, Sequence, Tuple


class Automaton:
    """把一组模式编译成 AC 自动机；模式与文本都必须已归一化（见 normalize.py）。

    **索引语义**：空模式无法匹配，构建时直接跳过且不占索引——因此「模式索引 i」对应的
    是**过滤掉空串之后**的第 i 个模式。调用方要么保证不含空串，要么按同样规则过滤后
    再拿索引去查表（taxonomy.build_indexes 用后者，并单独保存 key 顺序）。
    """

    __slots__ = ("goto", "fail", "out", "_lengths", "pattern_count")

    def __init__(self, patterns: Sequence[str]) -> None:
        self.goto: List[Dict[str, int]] = [{}]
        self.fail: List[int] = [0]
        self.out: List[List[int]] = [[]]
        lengths: List[int] = []
        count = 0
        for pattern in patterns:
            if not pattern:
                continue
            lengths.append(len(pattern))
            state = 0
            for ch in pattern:
                nxt = self.goto[state].get(ch)
                if nxt is None:
                    nxt = len(self.goto)
                    self.goto.append({})
                    self.fail.append(0)
                    self.out.append([])
                    self.goto[state][ch] = nxt
                state = nxt
            self.out[state].append(count)
            count += 1
        self._lengths = lengths
        self.pattern_count = count
        self._build_fail()

    def _build_fail(self) -> None:
        """BFS 建 fail 指针，并把失配状态的输出并入自身（输出继承）。"""
        queue: deque = deque()
        for ch, nxt in self.goto[0].items():
            self.fail[nxt] = 0
            queue.append(nxt)
        while queue:
            state = queue.popleft()
            for ch, nxt in self.goto[state].items():
                f = self.fail[state]
                while f and ch not in self.goto[f]:
                    f = self.fail[f]
                target = self.goto[f].get(ch, 0)
                # 深度 ≥2 的状态不可能指回自己（goto[0] 只有深度 1 的状态），
                # 这里仍显式防一手，避免未来改动引入自环。
                self.fail[nxt] = 0 if target == nxt else target
                self.out[nxt].extend(self.out[self.fail[nxt]])
                queue.append(nxt)

    def iter_matches(self, text: str) -> Iterator[Tuple[int, int, int]]:
        """产出 ``(pattern_index, start, end)``，按 end 升序；end 为开区间。"""
        state = 0
        lengths = self._lengths
        goto, fail, out = self.goto, self.fail, self.out
        for i, ch in enumerate(text):
            while state and ch not in goto[state]:
                state = fail[state]
            state = goto[state].get(ch, 0)
            for index in out[state]:
                yield index, i - lengths[index] + 1, i + 1

    def find(self, text: str) -> List[Tuple[int, int, int]]:
        return list(self.iter_matches(text))

    def has_match(self, text: str) -> bool:
        for _ in self.iter_matches(text):
            return True
        return False


def distinct_patterns(matches: Iterable[Tuple[int, int, int]]) -> Dict[int, int]:
    """把命中折叠成 ``{pattern_index: 出现次数}``，同一模式同一位置只计一次。"""
    seen: Dict[int, int] = {}
    positions: Dict[int, set] = {}
    for index, start, _end in matches:
        bucket = positions.setdefault(index, set())
        if start in bucket:
            continue
        bucket.add(start)
        seen[index] = seen.get(index, 0) + 1
    return seen
