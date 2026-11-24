#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fee_ledger.py — 窄表费用分摊台账离线入口（不依赖原事实表）

财务下游只能拿到供应商拆开的窄表。本入口直接从窄表（沿用 delivery.json 的
表与连接树格式，以所声明列的并集为列目录）出具按业务分组的费用台账：

  * 调用者指定单据表、单据编号列、有符号整数金额列（单位：分）、一个分摊单元
    列和非空有序分组列清单；
  * 一张单据对一个分组的权重 = 能与该单据同时相容（在全部窄表中都存在相容行，
    RIP 下等价于存在一条完整拼接）的 *不同分摊单元值* 个数；同一单元经多条路径
    或多种无关明细出现只算一次；单元确实落在多个分组时各分组各计一次；
  * 按单据独立分摊：|金额| × 权重占比，先逐组取整到分，余下的分按小数余数
    从大到小每组补一分，平手按分组列顺序比较分组值（空值最前，整数按数值，
    字符串按 Unicode 码点）；负金额对同一分配结果取负；
  * 没有任何有效归属的单据整笔进待分摊项。

关键复杂度约束（与验收工具一致）：
  * 不枚举、不落地完整拼接结果；
  * 不生成随无关明细组合膨胀的中间行集——连接传播时只保留分组列与分摊单元列
    上的投影，中间结构规模只随确实存在的「单据、分组、分摊单元」组合增长；
  * 不逐一试探三者值域的笛卡尔积：沿连接树按实际行的相容键传播，
    不相通的单据/分组/单元天然不会相遇；
  * 空值是可相等的显式字面值（None == None），可直接作为哈希键。

子命令：ledger
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import deque
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 无论从哪个工作目录启动，都先定位到本脚本所在的 tool/ 目录以导入同包模块。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 与验收工具同目录；以 `python3 -I tool/fee_ledger.py` 运行时脚本目录仍在 sys.path。
from lossless_accept import (
    InputError,
    _check_columns,
    _read_json,
    is_scalar,
    type_name,
)

LEDGER_VERSION = "1.0"


# --------------------------------------------------------------------------- #
# 加载（无原事实表：列目录取所声明列的并集）
# --------------------------------------------------------------------------- #

def load_narrow_delivery(path: str
                         ) -> Tuple[List[Dict[str, Any]], List[Any], List[str], List[str]]:
    """读取并做行级校验。返回 (表描述列表, tree 原始边, 硬错误, 通知)。"""
    data = _read_json(path)
    if not isinstance(data, dict):
        raise InputError(["交付文件必须是 JSON 对象"])
    tables_raw = data.get("tables")
    tree_raw = data.get("tree")
    if not isinstance(tables_raw, list) or not tables_raw:
        raise InputError(["交付文件 tables 必须是非空数组"])
    if not isinstance(tree_raw, list):
        raise InputError(["交付文件 tree 必须是边数组"])

    reasons: List[str] = []
    notices: List[str] = []
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
                reasons.append(
                    f"交付表 {name!r} 第 {ri} 行含不支持的值类型 {bad}"
                    f"（仅支持整数/字符串/显式空值）")
                continue
            t = tuple(row)
            if t in distinct:
                distinct[t].append(ri)
                n_dup += 1
            else:
                distinct[t] = [ri]
        if n_dup:
            notices.append(
                f"NOTICE:交付表 {name!r} 存在 {n_dup} 条重复行，已按集合语义去重")
        tables.append({
            "name": name,
            "columns": list(cols),
            "rows": list(distinct.keys()),
            "row_indices": list(distinct.values()),
        })
    return tables, tree_raw, reasons, notices


# --------------------------------------------------------------------------- #
# 结构校验：连接树、RIP、同名列类型一致（协议同验收工具，但不依赖原表）
# --------------------------------------------------------------------------- #

def validate_tree_and_types(tables: List[Dict[str, Any]],
                            tree_raw: List[Any]
                            ) -> Tuple[List[List[int]], List[str], List[str]]:
    reasons: List[str] = []
    n = len(tables)
    name_to_id = {t["name"]: i for i, t in enumerate(tables)}

    # 列目录（并集，按首次声明排序）与每列出现的表
    directory: List[str] = []
    seen_cols = set()
    coverage: Dict[str, List[int]] = {}
    for ti, t in enumerate(tables):
        for c in t["columns"]:
            if c not in seen_cols:
                seen_cols.add(c)
                directory.append(c)
                coverage[c] = []
            coverage[c].append(ti)

    adj: List[List[int]] = [[] for _ in range(n)]
    edge_set = set()
    for ei, edge in enumerate(tree_raw):
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

    if not reasons:
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

        # RIP：凡含同名列的节点必须在树上连成一块
        for c, where in coverage.items():
            if len(where) <= 1:
                continue
            marked = set(where)
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
                reasons.append(
                    f"列 {c!r} 出现在不连通的多个树块中，违反同名列连成一块的要求")

    # 同名列类型一致（空值不参与类型判定）
    col_types: Dict[str, set] = {c: set() for c in directory}
    for t in tables:
        idx = {c: j for j, c in enumerate(t["columns"])}
        for c in t["columns"]:
            ts = col_types[c]
            for row in t["rows"]:
                v = row[idx[c]]
                if v is not None:
                    ts.add(type_name(v))
    for c, ts in col_types.items():
        if len(ts) > 1:
            reasons.append(f"列 {c!r} 出现冲突类型: {sorted(ts)}")

    return adj, reasons, directory


# --------------------------------------------------------------------------- #
# 配置校验 + 单据表专项规则
# --------------------------------------------------------------------------- #

def validate_config(tables: List[Dict[str, Any]],
                    directory: List[str],
                    doc_table: str,
                    doc_id_col: str,
                    amount_col: str,
                    unit_col: str,
                    group_cols: List[str]
                    ) -> Tuple[int, Dict[str, Any], List[str]]:
    reasons: List[str] = []
    name_to_id = {t["name"]: i for i, t in enumerate(tables)}
    col_set = set(directory)

    if doc_table not in name_to_id:
        reasons.append(f"单据表 {doc_table!r} 不在交付表清单中")
        doc_u = -1
    else:
        doc_u = name_to_id[doc_table]

    if not isinstance(doc_id_col, str) or not doc_id_col:
        reasons.append("单据编号列必须是非空列名")
    if not isinstance(amount_col, str) or not amount_col:
        reasons.append("金额列必须是非空列名")
    if not isinstance(unit_col, str) or not unit_col:
        reasons.append("分摊单元列必须是非空列名")
    if not group_cols:
        reasons.append("分组列清单必须非空")
    elif any(not isinstance(c, str) or not c for c in group_cols):
        reasons.append("分组列名必须是非空字符串")
    elif len(set(group_cols)) != len(group_cols):
        reasons.append(f"分组列清单存在重复: {group_cols}")

    for label, c in (("单据编号列", doc_id_col), ("金额列", amount_col),
                     ("分摊单元列", unit_col)):
        if c and c not in col_set:
            reasons.append(f"{label} {c!r} 未在任何窄表中声明")
    for c in group_cols:
        if isinstance(c, str) and c and c not in col_set:
            reasons.append(f"分组列 {c!r} 未在任何窄表中声明")

    if doc_u >= 0:
        doc_cols = set(tables[doc_u]["columns"])
        if doc_id_col and doc_id_col not in doc_cols:
            reasons.append(f"单据编号列 {doc_id_col!r} 必须声明在单据表 {doc_table!r} 中")
        if amount_col and amount_col not in doc_cols:
            reasons.append(f"金额列 {amount_col!r} 必须声明在单据表 {doc_table!r} 中")

    # 金额列只能出现在单据表，且不能兼任分组/分摊单元
    amount_where = [t["name"] for t in tables if amount_col in t["columns"]]
    if amount_col and amount_where and any(i != doc_u for i in
                                           (name_to_id[n] for n in amount_where)):
        reasons.append(f"金额列 {amount_col!r} 只能出现在单据表 {doc_table!r} 中，"
                       f"实际出现于 {amount_where}")
    if amount_col:
        if amount_col == doc_id_col:
            reasons.append("单据编号列与金额列不能是同一列")
        if amount_col == unit_col:
            reasons.append("金额列不能同时作为分摊单元列")
        if amount_col in group_cols:
            reasons.append("金额列不能同时作为分组列")
    if unit_col in group_cols:
        reasons.append(f"分摊单元列 {unit_col!r} 不能同时出现在分组列清单中")

    # 配置列可达性说明：RIP 已保证每个配置列的全部声明节点连成一个连通块，
    # 而连接树整体连通，故该块一定可从单据表节点到达；块与单据表之间的边若
    # 不共享该列即为交叉连接（无约束，属于合法协议），传播引擎能正确处理。
    # 因此除已被 RIP 校验覆盖的情形外，不存在额外的“不可达”配置错误。

    info: Dict[str, Any] = {}
    if not reasons and doc_u >= 0:
        doc = tables[doc_u]
        idx = {c: j for j, c in enumerate(doc["columns"])}
        id_j, amt_j = idx[doc_id_col], idx[amount_col]
        id_to_row: Dict[Any, Tuple[Any, ...]] = {}
        conflicts: List[Any] = []
        for ri, row in enumerate(doc["rows"]):
            idv = row[id_j]
            amt = row[amt_j]
            if idv is None:
                reasons.append(f"单据表 {doc_table!r} 第 {ri} 行单据编号为空")
            if amt is None:
                reasons.append(f"单据表 {doc_table!r} 第 {ri} 行金额为空（金额必须是有符号整数）")
            elif isinstance(amt, bool) or not isinstance(amt, int):
                reasons.append(
                    f"单据表 {doc_table!r} 第 {ri} 行金额 {amt!r} 不是整数"
                    f"（实际类型 {type_name(amt)}）")
            if idv is not None:
                if idv in id_to_row:
                    if id_to_row[idv] != row:
                        conflicts.append(idv)
                else:
                    id_to_row[idv] = row
        if conflicts:
            shown = json.dumps(conflicts[:10], ensure_ascii=False)
            reasons.append(
                f"同一单据编号对应了不同单据行，拒收: {shown}"
                + (" ..." if len(conflicts) > 10 else ""))
        info = {"doc_u": doc_u, "id_j": id_j, "amt_j": amt_j,
                "id_to_row": id_to_row}
    return doc_u, info, reasons


# --------------------------------------------------------------------------- #
# 核心引擎：沿连接树传播「分组列 + 分摊单元列」投影
# --------------------------------------------------------------------------- #

class LedgerEngine:
    """
    对树上每个节点 u、每个去重行 r 维护：

        Q(u, r) = 以 r 为 u 的选定行时，u 子树能产生的全部完整拼接在
                  P = 分组列 ∪ {分摊单元列} 上的 *不同* 投影集合
                  （每个投影是 (列, 值) 对的 frozenset）

    投影只保留 P 列：无关明细列与重复路径在集合语义下自然消失，中间结构
    只随确实可达的不同（分组、分摊单元）组合增长，绝不随隐式拼接行数膨胀。
    RIP 保证：存在一条全树相容选行 ⇔ 每张表都有与该行局部相容的行，
    因此自底向上按边共享列等值传播即可，无需枚举全局拼接。
    """

    def __init__(self, tables: List[Dict[str, Any]], adj: List[List[int]],
                 root_u: int, id_col: str,
                 group_cols: List[str], unit_col: str):
        self.tables = tables
        self.adj = adj
        self.n = len(tables)
        self.root = root_u
        self.id_col = id_col
        self.group_cols = list(group_cols)
        self.unit_col = unit_col
        self.proj_cols = set(group_cols) | {unit_col}

        self.cols = [t["columns"] for t in tables]
        self.cidx = [{c: j for j, c in enumerate(t["columns"])} for t in tables]
        self.rows = [t["rows"] for t in tables]
        self._root_tree()
        self._build_edge_indexes()

    def _root_tree(self) -> None:
        self.parent = [-1] * self.n
        self.children: List[List[int]] = [[] for _ in range(self.n)]
        self.order: List[int] = []
        dq = deque([self.root])
        self.parent[self.root] = -2
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
        # 父子共享列（按父表列序），可能为空 => 交叉连接
        self.shared: List[List[str]] = [[] for _ in range(self.n)]
        for v in range(self.n):
            u = self.parent[v]
            if u >= 0:
                self.shared[v] = [c for c in self.cols[u] if c in self.cidx[v]]

    def _build_edge_indexes(self) -> None:
        """孩子侧按父子共享列建实际键 → 行列表；无共享列时为 None（全相容）。"""
        self.cindex: List[Optional[Dict[Tuple[Any, ...], List[Tuple[Any, ...]]]]] = \
            [None] * self.n
        for v in range(self.n):
            u = self.parent[v]
            if u < 0 or not self.shared[v]:
                continue
            vi = self.cidx[v]
            shared = self.shared[v]
            index: Dict[Tuple[Any, ...], List[Tuple[Any, ...]]] = {}
            for rv in self.rows[v]:
                key = tuple(rv[vi[c]] for c in shared)
                index.setdefault(key, []).append(rv)
            self.cindex[v] = index

    def _compatible_child_rows(self, u: int, row: Tuple[Any, ...],
                               v: int) -> Sequence[Tuple[Any, ...]]:
        shared = self.shared[v]
        if not shared:
            return self.rows[v]  # 交叉连接：孩子任意行都相容
        ui = self.cidx[u]
        key = tuple(row[ui[c]] for c in shared)
        assert self.cindex[v] is not None
        return self.cindex[v].get(key, ())

    def propagate(self) -> List[Dict[Tuple[Any, ...], set]]:
        P = self.proj_cols
        Q: List[Dict[Tuple[Any, ...], set]] = [dict() for _ in range(self.n)]
        for u in self.postorder:
            ui = self.cidx[u]
            own_p_cols = [c for c in self.cols[u] if c in P]
            for row in self.rows[u]:
                p0 = frozenset((c, row[ui[c]]) for c in own_p_cols)
                combos = {p0}
                for v in self.children[u]:
                    opts: set = set()
                    for rv in self._compatible_child_rows(u, row, v):
                        opts |= Q[v][rv]
                    if not opts:
                        combos = set()  # 某孩子无相容行 => 该行无任何完整拼接
                        break
                    merged = set()
                    for a in combos:
                        for b in opts:
                            merged.add(a | b)
                    combos = merged
                Q[u][row] = combos
        return Q

    def attributions(self, Q: List[Dict[Tuple[Any, ...], set]]
                     ) -> Tuple[Dict[Any, Dict[Tuple[Any, ...], set]], List[Any]]:
        """
        返回:
          attrib: 单据编号 -> 分组键 -> 相容分摊单元值集合
          pending: 没有任何完整拼接（无有效归属）的单据编号
        每个根行投影都覆盖 P 的全部列（P 中每列都在树的某节点声明且连通可达）。
        """
        root = self.root
        rj = self.cidx[root]
        id_j = rj[self.id_col]
        attrib: Dict[Any, Dict[Tuple[Any, ...], set]] = {}
        pending: List[Any] = []
        gcols = self.group_cols
        ucol = self.unit_col
        for rrow in self.rows[root]:
            doc_id = rrow[id_j]
            qs = Q[root][rrow]
            if not qs:
                pending.append(doc_id)
                continue
            per_doc = attrib.setdefault(doc_id, {})
            for q in qs:
                m = dict(q)
                gkey = tuple(m[c] for c in gcols)
                per_doc.setdefault(gkey, set()).add(m[ucol])
        return attrib, pending


# --------------------------------------------------------------------------- #
# 分摊计算（按单据独立，最大余数法，负金额取负）
# --------------------------------------------------------------------------- #

def value_sort_key(v: Any) -> Tuple[Any, ...]:
    """空值最前；整数按数值；字符串按 Unicode 码点。"""
    if v is None:
        return (0,)
    if isinstance(v, bool):  # 理论不可达：布尔值在加载期已被拒
        raise InputError([f"不支持的分组值类型: {v!r}"])
    if isinstance(v, int):
        return (1, v)
    if isinstance(v, str):
        return (2, v)
    raise InputError([f"不支持的分组值类型: {v!r}"])


def group_sort_key(gkey: Tuple[Any, ...]) -> Tuple[Any, ...]:
    return tuple(value_sort_key(v) for v in gkey)


def allocate_one(amount: int,
                 weights: List[Tuple[Tuple[Any, ...], int]]
                 ) -> Dict[Tuple[Any, ...], int]:
    """weights: [(分组键, 正权重)]；返回各分组分到的有符号整数金额（分）。"""
    total_w = sum(w for _, w in weights)
    if total_w <= 0:
        raise InputError(["内部错误：权重之和必须为正"])
    magnitude = abs(amount)
    bases: List[int] = []
    rems: List[int] = []
    for _, w in weights:
        num = magnitude * w
        bases.append(num // total_w)
        rems.append(num % total_w)  # 同分母，余数大小即小数余数大小
    # 余数从大到小补一分；平手按分组列顺序比较分组值（小者先得）
    order = sorted(range(len(weights)),
                   key=lambda i: (-rems[i], group_sort_key(weights[i][0])))
    leftover = magnitude - sum(bases)  # 0 <= leftover < 分组数
    alloc = {gkey: base for (gkey, _), base in zip(weights, bases)}
    for k in order[:leftover]:
        alloc[weights[k][0]] += 1
    if amount < 0:
        alloc = {gkey: -v for gkey, v in alloc.items()}
    return alloc


def build_ledger(tables: List[Dict[str, Any]],
                 adj: List[List[int]],
                 directory: List[str],
                 cfg: Dict[str, Any],
                 info: Dict[str, Any],
                 notices: List[str]) -> Dict[str, Any]:
    engine = LedgerEngine(tables, adj, info["doc_u"], cfg["doc_id_col"],
                          cfg["group_cols"], cfg["unit_col"])
    Q = engine.propagate()
    attrib, pending_ids = engine.attributions(Q)

    id_to_row = info["id_to_row"]
    amt_j = info["amt_j"]
    doc_u = info["doc_u"]
    amount_of = {doc_id: row[amt_j] for doc_id, row in id_to_row.items()}

    group_totals: Dict[Tuple[Any, ...], int] = {}
    for doc_id, per_groups in attrib.items():
        amount = amount_of[doc_id]
        weights = [(gkey, len(unit_set)) for gkey, unit_set in per_groups.items()]
        alloc = allocate_one(amount, weights)
        for gkey, part in alloc.items():
            group_totals[gkey] = group_totals.get(gkey, 0) + part

    pending = [{"doc_id": doc_id, "amount_cents": amount_of[doc_id]}
               for doc_id in pending_ids]
    pending.sort(key=lambda item: value_sort_key(item["doc_id"]))

    totals_out = [
        {"group": list(gkey), "amount_cents": group_totals[gkey]}
        for gkey in sorted(group_totals.keys(), key=group_sort_key)
    ]
    pending_total = sum(item["amount_cents"] for item in pending)
    allocated_total = sum(group_totals.values())
    doc_total = sum(amount_of.values())

    if allocated_total + pending_total != doc_total:
        # 理论不可达：每张单据的分摊分量之和恒等于其原金额。保护性失败，不出台账。
        raise InputError([
            "内部错误：台账轧账不平 "
            f"(分摊 {allocated_total} + 待分摊 {pending_total} != 单据合计 {doc_total})"])

    return {
        "ledger_version": LEDGER_VERSION,
        "status": "OK",
        "config": {
            "doc_table": cfg["doc_table"],
            "doc_id_column": cfg["doc_id_col"],
            "amount_column": cfg["amount_col"],
            "unit_column": cfg["unit_col"],
            "group_columns": list(cfg["group_cols"]),
        },
        "column_directory": list(directory),
        "doc_table_index": doc_u,
        "group_totals": totals_out,
        "pending": pending,
        "reconciliation": {
            "doc_count": len(id_to_row),
            "distinct_doc_amount_total": doc_total,
            "allocated_total": allocated_total,
            "pending_total": pending_total,
            "balanced": allocated_total + pending_total == doc_total,
        },
        "warnings": notices,
    }


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def run_ledger(delivery_path: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    tables, tree_raw, reasons, notices = load_narrow_delivery(delivery_path)
    if not tables:
        reasons.append("没有合法交付表")
    adj: List[List[int]] = []
    directory: List[str] = []
    if tables:
        adj, tree_reasons, directory = validate_tree_and_types(tables, tree_raw)
        reasons.extend(tree_reasons)
        doc_u, info, cfg_reasons = validate_config(
            tables, directory,
            cfg["doc_table"], cfg["doc_id_col"], cfg["amount_col"],
            cfg["unit_col"], cfg["group_cols"])
        reasons.extend(cfg_reasons)
    else:
        info = {}
    if reasons:
        raise InputError(reasons)
    return build_ledger(tables, adj, directory, cfg, info, notices)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="窄表费用分摊台账离线入口（不依赖原事实表，不物化拼接结果）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ledger", help="从窄表直接生成按组分摊的费用台账")
    p.add_argument("--delivery", required=True, help="交付表与连接树 JSON")
    p.add_argument("--doc-table", required=True, help="单据所在表名")
    p.add_argument("--doc-id-col", required=True, help="单据编号列（编号不能为空）")
    p.add_argument("--amount-col", required=True,
                   help="有符号整数金额列（单位：分），仅允许出现在单据表")
    p.add_argument("--unit-col", required=True, help="分摊单元列")
    p.add_argument("--group-col", required=True, action="append",
                   help="分组列，可重复指定；先后顺序即平手裁决顺序")
    p.add_argument("--out", help="台账输出路径（缺省打印到 stdout）")
    args = parser.parse_args(argv)

    cfg = {
        "doc_table": args.doc_table,
        "doc_id_col": args.doc_id_col,
        "amount_col": args.amount_col,
        "unit_col": args.unit_col,
        "group_cols": list(args.group_col),
    }
    try:
        ledger = run_ledger(args.delivery, cfg)
        text = json.dumps(ledger, ensure_ascii=False, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        else:
            print(text)
        return 0
    except InputError as exc:
        print(json.dumps({
            "ledger_version": LEDGER_VERSION,
            "status": "REJECTED",
            "reasons": exc.reasons,
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
