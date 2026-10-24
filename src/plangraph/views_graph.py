"""Deterministic graph algorithms behind :mod:`plangraph.views`.

Pure helpers over an explicit node/predicate model, so the public Views API
stays small and each file stays under the 400-line limit.  No I/O, no globals.

``preds[v]`` is the set of nodes that must precede ``v`` (a dependency edge
``u -> v`` means ``u`` satisfies ``v``).  ``succ`` is the mirror image.
"""
from __future__ import annotations

from graphlib import CycleError, TopologicalSorter

__all__ = [
    "topo_order",
    "layers",
    "longest_paths",
    "strongly_connected",
    "find_cycles",
    "ancestors",
]


def topo_order(nodes, preds):
    """A deterministic topological order of *nodes* (predecessors first).

    A cycle (which a valid plan never has) is tolerated: ``graphlib`` reports
    it, and the still-unordered nodes are appended in sorted order so callers
    always get a total order instead of an exception.
    """
    graph = {v: set(preds.get(v, ())) for v in nodes}
    ts = TopologicalSorter(graph)
    order = []
    try:
        ts.prepare()
    except CycleError:
        return _kahn_leftover(nodes, graph)
    while ts.is_active():
        ready = sorted(ts.get_ready())
        order.extend(ready)
        ts.done(*ready)
    return order


def layers(nodes, preds):
    """Partition *nodes* into topological layers (wave 0 = no predecessors).

    Uses ``graphlib.TopologicalSorter.get_ready`` so every node in a layer has
    all its predecessors in earlier layers.  Falls back to a deterministic
    Kahn variant if the graph contains a cycle.
    """
    graph = {v: set(preds.get(v, ())) for v in nodes}
    try:
        ts = TopologicalSorter(graph)
        ts.prepare()
        out = []
        while ts.is_active():
            ready = sorted(ts.get_ready())
            out.append(ready)
            ts.done(*ready)
        return out
    except CycleError:
        return _kahn_layers(nodes, graph)


def longest_paths(nodes, preds, weight):
    """Longest weighted path ending at each node.

    Returns ``(hours, path)`` dicts.  *path[v]* is the prerequisite-to-``v``
    key sequence; ties on hours are broken by the lexicographically smallest
    key sequence, so the result is deterministic.
    """
    order = topo_order(nodes, preds)
    hours = {}
    path = {}
    for v in order:
        best_h = None
        best_p = None
        for u in sorted(preds.get(v, ())):
            if u not in hours:
                continue
            h = hours[u]
            if best_h is None or h > best_h or (h == best_h and path[u] < best_p):
                best_h, best_p = h, path[u]
        w = float(weight(v))
        if best_h is None:
            hours[v], path[v] = w, [v]
        else:
            hours[v], path[v] = best_h + w, best_p + [v]
    return hours, path


def strongly_connected(nodes, succ):
    """Tarjan SCCs as sorted key lists (iterative; no recursion limit)."""
    index = {}
    low = {}
    on_stack = {}
    stack = []
    comps = []
    counter = 0
    for root in sorted(nodes):
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            v, i = work[-1]
            if i == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on_stack[v] = True
            nbrs = sorted(succ.get(v, ()))
            advanced = False
            while i < len(nbrs):
                w = nbrs[i]
                i += 1
                if w not in index:
                    work[-1] = (v, i)
                    work.append((w, 0))
                    advanced = True
                    break
                if on_stack.get(w) and index[w] < low[v]:
                    low[v] = index[w]
            if advanced:
                continue
            work[-1] = (v, i)
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    comp.append(w)
                    if w == v:
                        break
                comps.append(sorted(comp))
            work.pop()
            if work:
                pv = work[-1][0]
                if low[v] < low[pv]:
                    low[pv] = low[v]
    return comps


def find_cycles(nodes, succ):
    """One representative cycle per strongly-connected component > 1 node.

    Self-loops are intentionally not reported here; ``views.validate`` uses a
    dedicated ``self_dep`` problem for those.
    """
    out = []
    for comp in strongly_connected(nodes, succ):
        if len(comp) > 1:
            out.append(_representative_cycle(comp, succ))
    out.sort()
    return out


def ancestors(starts, preds):
    """All nodes that can reach any of *starts* by following ``preds``."""
    seen = set(starts)
    stack = list(starts)
    while stack:
        v = stack.pop()
        for u in preds.get(v, ()):
            if u not in seen:
                seen.add(u)
                stack.append(u)
    return seen


# --------------------------------------------------------------- internals

def _representative_cycle(comp, succ):
    """A simple cycle through ``min(comp)`` inside an SCC."""
    comp_set = set(comp)
    start = min(comp)
    path = [start]
    seen = {start}

    def walk(v):
        for w in sorted(succ.get(v, ())):
            if w not in comp_set:
                continue
            if w == start:
                return True
            if w in seen:
                continue
            seen.add(w)
            path.append(w)
            if walk(w):
                return True
            path.pop()
            seen.discard(w)
        return False

    walk(start)
    return path


def _kahn_leftover(nodes, preds):
    indeg = {}
    succ = {v: [] for v in nodes}
    for v in nodes:
        indeg[v] = 0
    for v in nodes:
        for u in preds.get(v, ()):
            if u in indeg:
                indeg[v] += 1
                succ[u].append(v)
    order = []
    ready = sorted(v for v in nodes if indeg[v] == 0)
    while ready:
        v = ready.pop(0)
        order.append(v)
        for w in succ[v]:
            indeg[w] -= 1
            if indeg[w] == 0:
                ready.append(w)
        ready.sort()
    placed = set(order)
    order.extend(sorted(v for v in nodes if v not in placed))
    return order


def _kahn_layers(nodes, preds):
    indeg = {v: 0 for v in nodes}
    succ = {v: [] for v in nodes}
    for v in nodes:
        for u in preds.get(v, ()):
            if u in indeg:
                indeg[v] += 1
                succ[u].append(v)
    out = []
    current = sorted(v for v in nodes if indeg[v] == 0)
    placed = set()
    while current:
        out.append(current)
        placed.update(current)
        nxt = []
        for u in current:
            for w in succ[u]:
                indeg[w] -= 1
                if indeg[w] == 0:
                    nxt.append(w)
        current = sorted(nxt)
    leftover = sorted(v for v in nodes if v not in placed)
    if leftover:
        out.append(leftover)
    return out
