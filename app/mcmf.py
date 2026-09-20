"""最小费用最大流（MCMF）。

使用 SPFA（Bellman-Ford 队列优化）在残量网络上反复找最短路并增广。
图规模小（稿件数 × 评审人数级别），该实现足够快且无第三方依赖。

费用约定：所有边费用非负。残量网络中的反向边费用为负，
因此必须使用能处理负权边的 SPFA，而不能用 Dijkstra（无势能标号时）。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class Edge:
    to: int
    rev: int          # 反向边在邻接表中的下标
    cap: int
    cost: int
    tag: dict | None = None  # 业务标记，仅正向边携带


class MinCostMaxFlow:
    def __init__(self, n: int):
        self.n = n
        self.g: list[list[Edge]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: int, cost: int, tag: dict | None = None) -> None:
        fwd = Edge(to=v, rev=len(self.g[v]), cap=cap, cost=cost, tag=tag)
        bak = Edge(to=u, rev=len(self.g[u]), cap=0, cost=-cost, tag=None)
        self.g[u].append(fwd)
        self.g[v].append(bak)

    def solve(self, s: int, t: int, required_flow: int | None = None) -> tuple[int, int]:
        """返回 (实际流量, 最小费用)。required_flow=None 表示求最大流。"""
        INF = 10**18
        flow = 0
        cost = 0

        while required_flow is None or flow < required_flow:
            dist = [INF] * self.n
            pv = [-1] * self.n       # 前驱节点
            pe = [-1] * self.n       # 前驱边下标
            in_queue = [False] * self.n
            dist[s] = 0
            q = deque([s])
            in_queue[s] = True

            while q:
                u = q.popleft()
                in_queue[u] = False
                for i, e in enumerate(self.g[u]):
                    if e.cap > 0 and dist[u] + e.cost < dist[e.to]:
                        dist[e.to] = dist[u] + e.cost
                        pv[e.to] = u
                        pe[e.to] = i
                        if not in_queue[e.to]:
                            q.append(e.to)
                            in_queue[e.to] = True

            if dist[t] == INF:
                break  # 无可增广路，已达到最大流

            # 沿最短路计算可增广量
            aug = INF if required_flow is None else required_flow - flow
            v = t
            while v != s:
                u = pv[v]
                aug = min(aug, self.g[u][pe[v]].cap)
                v = u
            if aug <= 0:
                break

            v = t
            while v != s:
                u = pv[v]
                e = self.g[u][pe[v]]
                e.cap -= aug
                self.g[v][e.rev].cap += aug
                cost += aug * e.cost
                v = u
            flow += aug

        return flow, cost

    def used_tags(self) -> list[tuple[dict, int]]:
        """收集所有携带业务标记且有流量经过的边 (tag, flow)。

        有流量 = 正向边残量小于初始容量；初始容量记录在 tag['cap'] 中。
        """
        out: list[tuple[dict, int]] = []
        for u in range(self.n):
            for e in self.g[u]:
                if e.tag is not None:
                    init_cap = e.tag.get("cap")
                    if init_cap is not None and e.cap < init_cap:
                        out.append((e.tag, init_cap - e.cap))
        return out
