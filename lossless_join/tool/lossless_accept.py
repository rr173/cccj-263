#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lossless_accept.py — 窄表无损回拼离线验收工具

判定一组拟交付窄表按声明连接树回拼后，是否与完整原事实表（整行集合语义）
完全相等，并分别给出：
  * 原表有、回拼没有的事实（缺行 / 拼不回）
  * 原表没有、回拼却能拼出的组合（造行）

关键约束：
  * 不枚举、不落地整个回拼结果；
  * 不物化可能远大于全部输入的中间拼接行集；
  * 只按输入中实际出现的键值维护分组计数映射（大整数精确计数）；
  * 空值是可相等的显式字面值（None == None），不采用 SQL 未知比较；
  * 整行集合为准，重复行不增加份数。

输入为 JSON（见 README）。两个子命令：
  accept        执行验收，输出 JSON 报告
  verify-report 仅依据输入复核报告中引用的实际行证据（可单独运行）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import deque
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPORT_VERSION = "1.0"

# --------------------------------------------------------------------------- #
# 基础类型工具
# --------------------------------------------------------------------------- #

SCALAR_TYPES = (int, str, type(None))


def is_scalar(v: Any) -> bool:
    """协议值：整数、字符串、显式空值。bool 视为非法（Python 中 bool 是 int 子类）。"""
    if isinstance(v, bool):
        return False
    return isinstance(v, SCALAR_TYPES)


def type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, str):
        return "string"
    return type(v).__name__


def scalar_key(v: Any) -> Any:
    """可哈希键：None 自身可哈希且参与相等比较，符合空值字面值协议。"""
    return v


def project(row: Sequence[Any], cols: Sequence[str],
            idx: Dict[str, int]) -> Tuple[Any, ...]:
    return tuple(row[idx[c]] for c in cols)


# --------------------------------------------------------------------------- #
# 加载与结构校验
# --------------------------------------------------------------------------- #

class InputError(Exception):
    def __init__(self, reasons: List[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _read_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise InputError([f"文件不存在: {path}"])
    except json.JSONDecodeError as exc:
        raise InputError([f"JSON 解析失败({path}): {exc}"])
    except OSError as exc:
        raise InputError([f"文件无法读取({path}): {exc}"])


def _check_columns(obj: Any, owner: str, reasons: List[str]) -> Optional[List[str]]:
    if not isinstance(obj, list) or not obj:
        reasons.append(f"{owner} 的 columns 必须是非空数组")
        return None
    cols: List[str] = []
    seen = set()
    for c in obj:
        if not isinstance(c, str) or not c:
            reasons.append(f"{owner} 的列名必须是非空字符串，发现: {c!r}")
            continue
        if c in seen:
            reasons.append(f"{owner} 的列 {c!r} 重复声明")
        seen.add(c)
        cols.append(c)
    return cols if not any(False for _ in ()) else cols


def load_fact(path: str) -> Tuple[List[str], List[Tuple[Any, ...]], List[str]]:
    """返回 (列, 去重行集合, 校验原因)。"""
    reasons: List[str] = []
    data = _read_json(path)
    if not isinstance(data, dict):
        raise InputError(["原表文件必须是 JSON 对象"])
    cols = _check_columns(data.get("columns"), "原表", reasons)
    rows_raw = data.get("rows")
    if not isinstance(rows_raw, list):
        reasons.append("原表 rows 必须是数组")
        rows_raw = []
    rows: List[Tuple[Any, ...]] = []
    seen_rows = set()
    n_dup = 0
    if cols is not None:
        width = len(cols)
        for i, row in enumerate(rows_raw):
            if not isinstance(row, list) or len(row) != width:
                reasons.append(f"原表第 {i} 行不是长度为 {width} 的数组")
                continue
            if any(not is_scalar(v) for v in row):
                bad = next(type_name(v) for v in row if not is_scalar(v))
                reasons.append(f"原表第 {i} 行含不支持的值类型 {bad}（仅支持整数/字符串/显式空值）")
                continue
            t = tuple(row)
            if t in seen_rows:
                n_dup += 1
                continue
            seen_rows.add(t)
            rows.append(t)
    return (cols or []), rows, reasons + ([f"NOTICE:原表存在 {n_dup} 条重复行，已按集合语义去重"] if n_dup else [])


def load_deliveries(path: str, fact_cols: List[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """返回交付表描述列表与校验原因。每张表: name, columns, rows(去重), row_indices(每组去重行的原始下标列表)。"""
    reasons: List[str] = []
    data = _read_json(path)
    if not isinstance(data, dict):
        raise InputError(["交付文件必须是 JSON 对象"])
    tables_raw = data.get("tables")
    tree_raw = data.get("tree")
    if not isinstance(tables_raw, list) or not tables_raw:
        raise InputError(["交付文件 tables 必须是非空数组"])
    if not isinstance(tree_raw, list):
        raise InputError(["交付文件 tree 必须是边数组"])

    fact_set = set(fact_cols)
    tables: List[Dict[str, Any]] = []
    names_seen = set()
    for ti, tobj in enumerate(tables_raw):
        if not isinstance(tobj, dict):
            reasons.append(f"第 {ti} 张交付表不是对象")
            continue
        name = tobj.get("name")
        if not isinstance(name, str) or not name:
            reasons.append(f"第 {ti} 张交付表缺少合法 name")
            name = f"__table_{ti}"
        if name in names_seen:
            reasons.append(f"交付表名 {name!r} 重复")
        names_seen.add(name)
        cols = _check_columns(tobj.get("columns"), f"交付表 {name!r}", reasons)
        if cols is None:
            continue
        unknown = [c for c in cols if c not in fact_set]
        if unknown:
            reasons.append(f"交付表 {name!r} 含原表不存在的列: {unknown}")
        rows_raw = tobj.get("rows", [])
        if not isinstance(rows_raw, list):
            reasons.append(f"交付表 {name!r} 的 rows 必须是数组")
            rows_raw = []
        distinct: Dict[Tuple[Any, ...], List[int]] = {}
        width = len(cols)
        n_dup = 0
        for ri, row in enumerate(rows_raw):
            if not isinstance(row, list) or len(row) != width:
                reasons.append(f"交付表 {name!r} 第 {ri} 行不是长度为 {width} 的数组")
                continue
            if any(not is_scalar(v) for v in row):
                bad = next(type_name(v) for v in row if not is_scalar(v))
                reasons.append(f"交付表 {name!r} 第 {ri} 行含不支持的值类型 {bad}")
                continue
            t = tuple(row)
            if t in distinct:
                distinct[t].append(ri)
                n_dup += 1
            else:
                distinct[t] = [ri]
        if n_dup:
            reasons.append(f"NOTICE:交付表 {name!r} 存在 {n_dup} 条重复行，已按集合语义去重")
        tables.append({
            "name": name,
            "columns": list(cols),
            "rows": list(distinct.keys()),
            "row_indices": list(distinct.values()),
        })

    return tables, reasons


def validate_structure(fact_cols: List[str],
                       fact_rows: List[Tuple[Any, ...]],
                       tables: List[Dict[str, Any]]) -> Tuple[List[List[int]], List[str]]:
    """校验列覆盖、连接树合法、RIP（同名列节点连成一块）、同名列类型一致。
    返回邻接表（按表下标）与原因列表。"""
    reasons: List[str] = []
    n = len(tables)
    name_to_id = {t["name"]: i for i, t in enumerate(tables)}

    # 列覆盖：每个源列至少出现一次
    coverage: Dict[str, List[int]] = {c: [] for c in fact_cols}
    for ti, t in enumerate(tables):
        for c in t["columns"]:
            if c in coverage:
                coverage[c].append(ti)
    missing_cols = [c for c, where in coverage.items() if not where]
    if missing_cols:
        reasons.append(f"以下源列没有任何交付表声明: {missing_cols}")

    # 类型一致：同名列在原表与各表实际值上的非空类型集合必须单一
    fact_idx_global = {c: i for i, c in enumerate(fact_cols)}
    col_types: Dict[str, set] = {}
    for c in fact_cols:
        ts = {type_name(r[fact_idx_global[c]]) for r in fact_rows
              if r[fact_idx_global[c]] is not None}
        if ts:
            col_types[c] = set(ts)
    for c in fact_cols:
        ts = {type_name(r[fact_idx_global[c]]) for r in fact_rows
              if r[fact_idx_global[c]] is not None}
        if ts:
            col_types[c] = set(ts)
    for t in tables:
        idx = {c: j for j, c in enumerate(t["columns"])}
        for c in t["columns"]:
            ts = set()
            for row in t["rows"]:
                v = row[idx[c]]
                if v is not None:
                    ts.add(type_name(v))
            col_types.setdefault(c, set()).update(ts)
    for c, ts in col_types.items():
        if len(ts) > 1:
            reasons.append(f"列 {c!r} 出现冲突类型: {sorted(ts)}")

    return _validate_tree(tables, name_to_id, coverage, reasons)


def _validate_tree(tables: List[Dict[str, Any]],
                   name_to_id: Dict[str, int],
                   coverage: Dict[str, List[int]],
                   reasons: List[str]) -> Tuple[List[List[int]], List[str]]:
    n = len(tables)
    # tree 边由调用方通过 tables 顺序之外的输入给出；这里从模块级缓存读取
    edges_raw = _CURRENT_TREE_RAW  # type: ignore[name-defined]
    adj: List[List[int]] = [[] for _ in range(n)]
    edge_set = set()
    for ei, edge in enumerate(edges_raw):
        if not isinstance(edge, list) or len(edge) != 2:
            reasons.append(f"tree 第 {ei} 条边必须是两个表名组成的数组")
            continue
        a, b = edge
        if a not in name_to_id or b not in name_to_id:
            reasons.append(f"tree 第 {ei} 条边引用了未知表: {edge!r}")
            continue
        u, v = name_to_id[a], name_to_id[b]
        if u == v:
            reasons.append(f"tree 不允许自环: {edge!r}")
            continue
        key = (min(u, v), max(u, v))
        if key in edge_set:
            reasons.append(f"tree 存在重复边: {edge!r}")
            continue
        edge_set.add(key)
        adj[u].append(v)
        adj[v].append(u)

    if reasons:
        return adj, reasons

    # 必须是树：n-1 条边且连通（无环由连通 + 边数保证）
    if len(edge_set) != n - 1:
        reasons.append(f"连接树必须恰好含 n-1={n - 1} 条边，实际 {len(edge_set)} 条")
    seen = {0}
    dq = deque([0])
    while dq:
        u = dq.popleft()
        for v in adj[u]:
            if v not in seen:
                seen.add(v)
                dq.append(v)
    if len(seen) != n:
        reasons.append("连接树不连通，所有交付表必须在同一棵树上")

    # RIP：凡含同名列的节点必须在树上连成一块（去掉该列节点后不得分裂成多个连通块）
    for c, where in coverage.items():
        if len(where) <= 1:
            continue
        marked = set(where)
        # 从任一含该列的节点出发，仅沿“含该列”节点可达的集合必须等于 marked
        start = where[0]
        reached = {start}
        dq = deque([start])
        while dq:
            u = dq.popleft()
            for v in adj[u]:
                if v in marked and v not in reached:
                    reached.add(v)
                    dq.append(v)
        if reached != marked:
            reasons.append(f"列 {c!r} 出现在不连通的多个树块中，违反同名列连成一块的要求")

    return adj, reasons


_CURRENT_TREE_RAW: List[Any] = []


# --------------------------------------------------------------------------- #
# 验收核心
# --------------------------------------------------------------------------- #

class Acceptance:
    def __init__(self, fact_cols: List[str], fact_rows: List[Tuple[Any, ...]],
                 tables: List[Dict[str, Any]], adj: List[List[int]]):
        self.fact_cols = fact_cols
        self.fact_rows = fact_rows
        self.fact_set = set(fact_rows)
        self.fact_idx = {c: i for i, c in enumerate(fact_cols)}
        self.tables = tables
        self.adj = adj
        self.n = len(tables)
        self.cols = [t["columns"] for t in tables]
        self.col_idx = [{c: j for j, c in enumerate(t["columns"])} for t in tables]
        self.rows = [t["rows"] for t in tables]
        self.row_indices = [t["row_indices"] for t in tables]
        # 去重行 -> 表内编号
        self.row_number = [{row: k for k, row in enumerate(t["rows"])} for t in tables]
        self._root_tree()

    # ---------------- 树布局 ---------------- #
    def _root_tree(self) -> None:
        self.parent = [-1] * self.n
        self.children: List[List[int]] = [[] for _ in range(self.n)]
        self.order: List[int] = []  # 前序
        dq = deque([0])
        self.parent[0] = -2
        while dq:
            u = dq.popleft()
            self.order.append(u)
            for v in self.adj[u]:
                if v == self.parent[u]:
                    continue
                self.parent[v] = u
                self.children[u].append(v)
                dq.append(v)
        self.postorder = list(reversed(self.order))
        self.pos = {u: k for k, u in enumerate(self.order)}
        # 父子共享列（按父表列序），可能为空
        self.shared: List[List[str]] = [[] for _ in range(self.n)]
        for v in range(self.n):
            u = self.parent[v]
            if u < 0:
                continue
            self.shared[v] = [c for c in self.cols[u] if c in self.col_idx[v]]

    # ---------------- 局部相容 ---------------- #
    def _compatible(self, u: int, trow: Tuple[Any, ...],
                    fact_row: Tuple[Any, ...]) -> bool:
        idx = self.col_idx[u]
        for c, j in idx.items():
            if trow[j] != fact_row[self.fact_idx[c]]:
                return False
        return True

    def precompute_compatibility(self) -> List[List[List[int]]]:
        """compat[u] = 表 u 每行相容的原表行下标列表。"""
        compat: List[List[List[int]]] = []
        for u in range(self.n):
            per_row: List[List[int]] = []
            for trow in self.rows[u]:
                hits = [i for i, fr in enumerate(self.fact_rows)
                        if self._compatible(u, trow, fr)]
                per_row.append(hits)
            compat.append(per_row)
        return compat

    # ---------------- 自底向上精确计数（不物化拼接行） ---------------- #
    def bottom_up_counts(self) -> List[Dict[Tuple[Any, ...], int]]:
        """f[u]: 以 u 为根的子树，在父子接口列 self.shared[u] 的每个实际键值上
        可产生的 *不同* 完整拼接行数。
        只对输入中实际出现的键建映射；空共享列（交叉连接）退化为单键常量。"""
        f: List[Dict[Tuple[Any, ...], int]] = [dict() for _ in range(self.n)]
        for u in self.postorder:
            # g_u(row_of_u) = 各孩子 f 在该孩子接口键上的取值之积
            child_data = []
            for v in self.children[u]:
                shared_v = self.shared[v]
                v_idx = self.col_idx[v]
                fv = f[v]
                if not shared_v:
                    # 交叉连接：f[v] 只有 () 一个键
                    child_data.append((shared_v, v_idx, fv, fv.get((), 0)))
                else:
                    child_data.append((shared_v, v_idx, fv, None))

            local: Dict[Tuple[Any, ...], int] = {}
            u_idx = self.col_idx[u]
            for row in self.rows[u]:
                g = 1
                for shared_v, v_idx, fv, const in child_data:
                    if const is not None:
                        g *= const
                    else:
                        key = tuple(row[u_idx[c]] for c in shared_v)
                        g *= fv.get(key, 0)
                out_key = tuple(row[u_idx[c]] for c in self.shared[u]) if self.shared[u] else ()
                local[out_key] = local.get(out_key, 0) + g
            f[u] = local
        return f

    def total_join_size(self, f: List[Dict[Tuple[Any, ...], int]]) -> int:
        root = self.order[0]
        if not self.shared[root] and not self.children[root]:
            pass
        # 根接口为空：f[root] 至多一个 () 键
        return sum(f[root].values())

    # ---------------- 交集：原表中可由回拼产生的行 ---------------- #
    def intersection_rows(self) -> List[Tuple[Any, ...]]:
        """原表行 r 属于回拼结果 ⇔ 每张表都存在与 r 局部相容的行。
        RIP 保证局部相容沿树传播即全局相容。"""
        result = []
        self.missing_detail: List[Tuple[Tuple[Any, ...], int, Tuple[Any, ...]]] = []
        for fr in self.fact_rows:
            bad_u = -1
            for u in range(self.n):
                found = False
                for trow in self.rows[u]:
                    if self._compatible(u, trow, fr):
                        found = True
                        break
                if not found:
                    bad_u = u
                    break
            if bad_u < 0:
                result.append(fr)
            else:
                frag = project(fr, self.cols[bad_u], self.fact_idx)
                self.missing_detail.append((fr, bad_u, frag))
        return result

    # ---------------- 造行证据：计数引导的剪枝 DFS（不枚举回拼表） ---------------- #
    def find_spurious(self, f: List[Dict[Tuple[Any, ...], int]],
                      compat: List[List[List[int]]]
                      ) -> Optional[Tuple[Tuple[Any, ...], List[Dict[str, Any]]]]:
        """
        沿前序逐表选行。状态 (i, assigned, S)：
          N = 已选前缀下，剩余子森林仍可产生的不同回拼数（由 f 精确给出）
          G = 其中能落在原表集合中的元组数（用 compat 求交，候选集 S）
        N>G ⇔ 当前前缀下必有造行；据此选分支，绝不遍历整个回拼结果。
        """
        # 初始：N = 总回拼行数，G = 交集大小
        init_S = set(range(len(self.fact_rows)))
        state = {"i": 0, "assigned": [None] * self.n, "S": init_S}

        def remaining_N(i: int, assigned: List[Optional[Tuple[Any, ...]]]) -> int:
            """前序位置 i 起、各未处理子树根在已赋值接口下的 f 乘积。
            前序中父必先于子，故位置 >=i 的子树根恰为父位置 <i 的节点。"""
            total = 1
            for k in range(i, self.n):
                v = self.order[k]
                p = self.parent[v]
                if p >= 0 and self.pos[p] >= i:
                    continue  # 非子树根：父尚未处理，跳过
                shared_v = self.shared[v]
                if not shared_v:
                    total *= f[v].get((), 0)
                    continue
                prow = assigned[p]
                if prow is None:
                    return 0
                p_idx = self.col_idx[p]
                key = tuple(prow[p_idx[c]] for c in shared_v)
                total *= f[v].get(key, 0)
            return total

        def remaining_G(i: int, S: set) -> int:
            """S 中能通过所有未处理表局部相容检查的原表行数。"""
            if not S:
                return 0
            live = set(S)
            for k in range(i, self.n):
                v = self.order[k]
                allowed: set = set()
                for ri, row in enumerate(self.rows[v]):
                    allowed.update(compat[v][ri])
                live &= allowed
                if not live:
                    return 0
            return len(live)

        def edge_compatible(u: int, row: Tuple[Any, ...],
                            assigned: List[Optional[Tuple[Any, ...]]]) -> bool:
            """行 row（节点 u）必须与所有已赋值邻居在共享列上逐值相等。
            前序中已赋值邻居只会是父节点；空值按字面值相等（== 语义）。"""
            u_idx = self.col_idx[u]
            for w in self.adj[u]:
                wrow = assigned[w]
                if wrow is None:
                    continue
                shared = [c for c in self.cols[u] if c in self.col_idx[w]]
                w_idx = self.col_idx[w]
                for c in shared:
                    if row[u_idx[c]] != wrow[w_idx[c]]:
                        return False
            return True

        def dfs(i: int, assigned: List[Optional[Tuple[Any, ...]]],
                S: set, picks: List[int]) -> bool:
            if i == self.n:
                # S 为空即该拼成行不在原表中
                return not S
            u = self.order[i]
            for ri, row in enumerate(self.rows[u]):
                if not edge_compatible(u, row, assigned):
                    continue
                new_S = {fi for fi in S if fi in compat[u][ri]}
                assigned[u] = row
                Np = remaining_N(i + 1, assigned)
                Gp = remaining_G(i + 1, new_S)
                if Np > Gp:
                    picks.append(ri)
                    if dfs(i + 1, assigned, new_S, picks):
                        return True
                    picks.pop()
                assigned[u] = None
            return False

        picks: List[int] = []
        ok = dfs(0, state["assigned"], init_S, picks)
        if not ok:
            return None
        # 组装完整拼成行与各表坐标
        merged: Dict[str, Any] = {}
        witnesses: List[Dict[str, Any]] = []
        for k, u in enumerate(self.order):
            ri = picks[k]
            row = self.rows[u][ri]
            for c, j in self.col_idx[u].items():
                merged[c] = row[j]
            orig = self.row_indices[u][ri]
            witnesses.append({
                "table": self.tables[u]["name"],
                "table_index": u,
                "row_number": ri,
                "row_indices_in_file": orig,
                "row": list(row),
                "columns": list(self.cols[u]),
            })
        full = tuple(merged[c] for c in self.fact_cols)
        return full, witnesses


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #

def build_report(fact_path: str, delivery_path: str) -> Dict[str, Any]:
    warnings: List[str] = []
    fact_cols, fact_rows, w = load_fact(fact_path)
    warnings.extend(w)
    delivery_raw = _read_json(delivery_path)
    if not isinstance(delivery_raw, dict):
        raise InputError(["交付文件必须是 JSON 对象"])
    global _CURRENT_TREE_RAW
    _CURRENT_TREE_RAW = delivery_raw.get("tree", [])
    tables, w2 = load_deliveries(delivery_path, fact_cols)
    warnings.extend(w2)

    all_messages = warnings + w2
    notices = [r for r in all_messages if r.startswith("NOTICE:")]
    hard_reasons = [r for r in all_messages if not r.startswith("NOTICE:")]
    if tables and not hard_reasons:
        adj, structural_reasons = validate_structure(fact_cols, fact_rows, tables)
        hard_reasons.extend(structural_reasons)
    elif not tables:
        hard_reasons.append("没有合法交付表")
    if hard_reasons:
        return {
            "report_version": REPORT_VERSION,
            "status": "REJECTED",
            "reasons": hard_reasons,
            "warnings": notices,
        }

    acc = Acceptance(fact_cols, fact_rows, tables, adj)
    compat = acc.precompute_compatibility()
    f = acc.bottom_up_counts()
    join_count = acc.total_join_size(f)
    inter = acc.intersection_rows()
    inter_count = len(inter)
    fact_count = len(fact_rows)
    missing_count = fact_count - inter_count
    spurious_count = join_count - inter_count

    report: Dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "status": "ACCEPTED" if missing_count == 0 and spurious_count == 0 else "REJECTED",
        "verdict": {
            "fact_row_count": fact_count,
            "reconstructed_distinct_row_count": join_count,
            "intersection_row_count": inter_count,
            "missing_row_count": missing_count,
            "spurious_row_count": spurious_count,
            "sets_equal": missing_count == 0 and spurious_count == 0,
        },
        "warnings": notices,
        "missing_evidence": None,
        "spurious_evidence": None,
    }

    if missing_count > 0:
        fr, u, frag = acc.missing_detail[0]
        report["missing_evidence"] = {
            "fact_row": list(fr),
            "fact_columns": list(fact_cols),
            "missing_fragment": {
                "table": tables[u]["name"],
                "table_index": u,
                "expected_columns": list(acc.cols[u]),
                "expected_row": list(frag),
            },
        }

    if spurious_count > 0:
        found = acc.find_spurious(f, compat)
        if found is None:
            # 理论不可达：计数已判定 N>G。保护性失败。
            report["status"] = "REJECTED"
            report.setdefault("reasons", []).append("内部错误：计数显示存在造行但证据搜索失败")
        else:
            full, witnesses = found
            report["spurious_evidence"] = {
                "assembled_row": list(full),
                "fact_columns": list(fact_cols),
                "witnesses": witnesses,
            }
    return report


# --------------------------------------------------------------------------- #
# 报告证据复核（独立入口，只核对报告里引用的实际行）
# --------------------------------------------------------------------------- #

def verify_report(fact_path: str, delivery_path: str, report_path: str) -> Dict[str, Any]:
    report = _read_json(report_path)
    if not isinstance(report, dict):
        raise InputError(["报告文件必须是 JSON 对象"])
    fact_cols, fact_rows, _ = load_fact(fact_path)
    fact_set = set(fact_rows)
    fact_idx = {c: i for i, c in enumerate(fact_cols)}
    delivery_raw = _read_json(delivery_path)
    global _CURRENT_TREE_RAW
    _CURRENT_TREE_RAW = delivery_raw.get("tree", [])
    tables, _ = load_deliveries(delivery_path, fact_cols)
    by_name = {t["name"]: t for t in tables}

    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    # 计数自洽
    v = report.get("verdict", {})
    if isinstance(v, dict):
        ok = (v.get("fact_row_count") == len(fact_rows)
              and v.get("missing_row_count", 0) >= 0
              and v.get("spurious_row_count", 0) >= 0
              and v.get("intersection_row_count") ==
                  len(fact_rows) - v.get("missing_row_count", 0)
              and v.get("reconstructed_distinct_row_count") ==
                  v.get("intersection_row_count") + v.get("spurious_row_count", 0))
        add("计数自洽", ok,
            f"原表 {len(fact_rows)} 行；报告计数 {json.dumps(v, ensure_ascii=False)}")

    # 缺行证据
    me = report.get("missing_evidence")
    if me is None:
        add("缺行证据（报告声明无缺行）", report.get("verdict", {}).get("missing_row_count", 0) == 0,
            "报告未提供缺行证据")
    else:
        fr = tuple(me.get("fact_row", []))
        add("缺行证据: 原行真实存在", fr in fact_set,
            f"行 {list(fr)} 是否出现在原表集合中")
        frag = me.get("missing_fragment", {})
        tname = frag.get("table")
        t = by_name.get(tname)
        if t is None:
            add("缺行证据: 片段坐标合法", False, f"未知交付表 {tname!r}")
        else:
            cols = frag.get("expected_columns")
            expected = tuple(frag.get("expected_row", []))
            shape_ok = cols == t["columns"] and len(expected) == len(t["columns"])
            proj_ok = shape_ok and expected == project(fr, t["columns"], fact_idx)
            absent_ok = shape_ok and expected not in set(t["rows"])
            add("缺行证据: 片段等于原行在该表列上的投影", proj_ok,
                f"表 {tname!r} 列 {cols}")
            add("缺行证据: 该片段在交付表中确实不存在", absent_ok,
                f"期望片段 {list(expected)}")

    # 造行证据
    se = report.get("spurious_evidence")
    if se is None:
        add("造行证据（报告声明无造行）", report.get("verdict", {}).get("spurious_row_count", 0) == 0,
            "报告未提供造行证据")
    else:
        assembled = tuple(se.get("assembled_row", []))
        add("造行证据: 拼成行确实不在原表", assembled not in fact_set and len(assembled) == len(fact_cols),
            f"拼成行 {list(assembled)}")
        witnesses = se.get("witnesses", [])
        covered: Dict[str, Any] = {}
        all_w_ok = True
        detail_msgs = []
        for w in witnesses:
            tname = w.get("table")
            t = by_name.get(tname)
            if t is None:
                all_w_ok = False
                detail_msgs.append(f"未知表 {tname!r}")
                continue
            rn = w.get("row_number")
            if not isinstance(rn, int) or not (0 <= rn < len(t["rows"])):
                all_w_ok = False
                detail_msgs.append(f"表 {tname!r} 行号越界: {rn}")
                continue
            actual = t["rows"][rn]
            file_idxs = t["row_indices"][rn]
            coord_ok = w.get("row_indices_in_file") == file_idxs and list(actual) == w.get("row")
            if not coord_ok:
                all_w_ok = False
                detail_msgs.append(f"表 {tname!r} 行坐标与实际数据不符")
                continue
            for c, val in zip(t["columns"], actual):
                if c in covered and covered[c] != val:
                    all_w_ok = False
                    detail_msgs.append(f"列 {c!r} 上各表取值冲突")
                covered[c] = val
        all_cols = set(fact_cols)
        cover_ok = set(covered.keys()) == all_cols
        merge_ok = cover_ok and tuple(covered[c] for c in fact_cols) == assembled
        add("造行证据: 坐标均指向交付表真实行", all_w_ok, "; ".join(detail_msgs) or "全部命中")
        add("造行证据: 各行合并即所报拼成行", merge_ok,
            f"覆盖列 {sorted(covered.keys())}")
    return {
        "report_version": REPORT_VERSION,
        "status": "VERIFIED" if all(c["ok"] for c in checks) else "VERIFICATION_FAILED",
        "checks": checks,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: Optional[List[str]] = None) -> int:
    argv_list = list(sys.argv[1:] if argv is None else argv)

    # 批量主键查询是同项目的独立入口；这里保留兼容子命令转发。
    # 既有 accept / verify-report 不走此分支，参数与退出码完全不变。
    if (argv_list and not argv_list[0].startswith("-")
            and argv_list[0] not in {"accept", "verify-report"}):
        tool_dir = os.path.dirname(os.path.abspath(__file__))
        if tool_dir not in sys.path:
            sys.path.insert(0, tool_dir)
        from key_query import main as key_query_main
        return key_query_main(["query", *argv_list[1:]])

    parser = argparse.ArgumentParser(
        description="窄表无损回拼离线验收工具（不物化回拼结果）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_acc = sub.add_parser("accept", help="执行验收并输出 JSON 报告")
    p_acc.add_argument("--fact", required=True, help="完整原事实表 JSON")
    p_acc.add_argument("--delivery", required=True, help="交付表与连接树 JSON")
    p_acc.add_argument("--out", help="报告输出路径（缺省打印到 stdout）")

    p_ver = sub.add_parser("verify-report", help="独立复核报告中的实际行证据")
    p_ver.add_argument("--fact", required=True)
    p_ver.add_argument("--delivery", required=True)
    p_ver.add_argument("--report", required=True)

    args = parser.parse_args(argv)
    try:
        if args.cmd == "accept":
            report = build_report(args.fact, args.delivery)
            text = json.dumps(report, ensure_ascii=False, indent=2)
            if args.out:
                with open(args.out, "w", encoding="utf-8") as fh:
                    fh.write(text + "\n")
            else:
                print(text)
            return 0 if report.get("status") == "ACCEPTED" else 2
        report = verify_report(args.fact, args.delivery, args.report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "VERIFIED" else 3
    except InputError as exc:
        print(json.dumps({
            "report_version": REPORT_VERSION,
            "status": "REJECTED",
            "reasons": exc.reasons,
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
