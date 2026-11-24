#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
delivery_rollforward.py — 窄表交付按周滚动发布离线入口

给定上周的权威事实表、上周实际交付的窄表包，以及一批按业务行键定位的
insert / update / delete 变更，本入口：

  1. 先按既有无损验收协议确认「上周事实表」与「上周交付包」表示同一份整行
     集合（缺行、造行、结构非法都直接拒收，绝不静默修复后继续）；
  2. 在同一条业务行键上整体应用变更，得到本周事实集合；
  3. 按上周交付声明的表列与连接树，生成本周交付包：每张表恰好是本周事实行
     在该表列上的投影，并按集合语义去重；
  4. 输出新的 delivery.json、本周 fact.json 与一份可复核的变更报告；
     新包可直接交给现有 lossless_accept.py / fee_ledger.py 使用。

关键约束（与既有入口一致）：
  * 不枚举完整连接结果，也不物化随无关明细组合膨胀的中间行集；
  * 上周事实/交付的一致性判定只用「逐表投影存在性 + 自底向上精确计数」；
  * 交付行按「本周事实行的投影」集合生成，因此同一投影由多条事实行支持时，
    删除其中一条不会误删仍被其他事实行使用的交付行；
  * 连接键变更会自然反映到每张声明该列的表上；
  * 空值是可相等的显式字面值（None == None）；
  * 业务键不允许为空值，且在事实集合中必须唯一；
  * 整批要么全部接受并原子落盘，要么不写任何输出（失败时原交付包保持可用）；
  * 同一输入重复运行得到同一集合结果与逐字节一致的报告。

子命令：apply（别名 publish / rollforward）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 无论从哪个工作目录启动，都先定位到本脚本所在的 tool/ 目录以导入同包模块。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 复用验收入口的加载、结构校验与精确计数原语；绝不修改其行为。
import lossless_accept as la
from lossless_accept import (
    Acceptance,
    InputError,
    _read_json,
    is_scalar,
    type_name,
)

REPORT_VERSION = "1.0"
DELIVERY_VERSION = "1.0"

VALID_OPS = ("insert", "update", "delete")

# 每种操作允许出现的字段；未知字段视为结构非法。
ALLOWED_FIELDS = {
    "insert": frozenset({"op", "row", "key"}),
    "update": frozenset({"op", "row", "key"}),
    "delete": frozenset({"op", "key"}),
}

DEFAULT_DELIVERY_NAME = "delivery.json"
DEFAULT_REPORT_NAME = "change_report.json"
DEFAULT_FACT_NAME = "fact.json"


# --------------------------------------------------------------------------- #
# 错误、确定性排序与哈希
# --------------------------------------------------------------------------- #

class Rejection(Exception):
    """整批拒收。携带阶段与结构化原因；调用方不得写出任何新版本文件。"""

    def __init__(self, stage: str, reasons: List[str],
                 detail: Optional[Dict[str, Any]] = None):
        super().__init__("; ".join(reasons))
        self.stage = stage
        self.reasons = list(reasons)
        self.detail = detail or {}

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "report_version": REPORT_VERSION,
            "status": "REJECTED",
            "stage": self.stage,
            "reasons": self.reasons,
        }
        if self.detail:
            out["detail"] = self.detail
        return out


def value_sort_key(v: Any) -> Tuple[Any, ...]:
    """空值最前；整数按数值；字符串按 Unicode 码点（与台账入口同一值序）。"""
    if v is None:
        return (0, 0)
    if isinstance(v, bool):
        raise Rejection("internal", [f"不支持排序的值类型: {v!r}"])
    if isinstance(v, int):
        return (1, v)
    if isinstance(v, str):
        return (2, v)
    raise Rejection("internal", [f"不支持的排序值类型: {type_name(v)}"])


def row_sort_key(row: Sequence[Any]) -> Tuple[Tuple[Any, ...], ...]:
    return tuple(value_sort_key(v) for v in row)


def canonical_json(obj: Any) -> str:
    """与空白/键序无关的规范 JSON，用于内容哈希与确定性比较。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def sha256_canonical(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


# --------------------------------------------------------------------------- #
# 加载上周事实表与交付包（与验收入口共享同一协议）
# --------------------------------------------------------------------------- #

def _split_messages(messages: Sequence[str]) -> Tuple[List[str], List[str]]:
    notices = [m for m in messages if m.startswith("NOTICE:")]
    hard = [m for m in messages if not m.startswith("NOTICE:")]
    return hard, notices


def load_week_fact(path: str) -> Tuple[List[str], List[Tuple[Any, ...]], List[str]]:
    """返回 (列, 去重行列表, 通知)。任何硬错误都整批拒收。"""
    try:
        cols, rows, messages = la.load_fact(path)
    except InputError as exc:
        raise Rejection("fact", exc.reasons)
    hard, notices = _split_messages(messages)
    if hard:
        raise Rejection("fact", hard)
    return cols, rows, notices


def load_week_delivery(path: str, fact_cols: List[str],
                       fact_rows: List[Tuple[Any, ...]]
                       ) -> Tuple[List[Dict[str, Any]], List[Any], List[List[int]], List[str]]:
    """加载并按验收协议校验上周交付包。返回 (表描述, tree 原始边, 邻接表, 通知)。"""
    try:
        data = _read_json(path)
    except InputError as exc:
        raise Rejection("delivery", exc.reasons)
    if not isinstance(data, dict):
        raise Rejection("delivery", ["交付文件必须是 JSON 对象"])
    tree_raw = data.get("tree")
    if not isinstance(tree_raw, list):
        raise Rejection("delivery", ["交付文件 tree 必须是边数组"])

    # validate_structure 通过模块级变量读取当前 tree；与验收工具用法一致。
    la._CURRENT_TREE_RAW = tree_raw
    try:
        tables, messages = la.load_deliveries(path, fact_cols)
    except InputError as exc:
        raise Rejection("delivery", exc.reasons)
    hard, notices = _split_messages(messages)
    if not tables:
        hard.append("没有合法交付表")
    if hard:
        raise Rejection("delivery", hard)

    adj, struct_reasons = la.validate_structure(fact_cols, fact_rows, tables)
    if struct_reasons:
        raise Rejection("delivery", struct_reasons)
    return tables, tree_raw, adj, notices


# --------------------------------------------------------------------------- #
# 上周事实集合 vs 交付包：同一整行集合判定（不物化回拼结果）
# --------------------------------------------------------------------------- #

def delivered_sets(tables: List[Dict[str, Any]]) -> List[set]:
    """各交付表实际交付的去重行集合；只随输入行数增长。"""
    return [set(t["rows"]) for t in tables]


def fact_projection_counts(tables: List[Dict[str, Any]], fact_cols: List[str],
                           fact_rows: Sequence[Tuple[Any, ...]]
                           ) -> List[Dict[Tuple[Any, ...], int]]:
    """每张交付表列上，「事实行投影 -> 支持该投影的去重事实行数」。"""
    fact_idx = {c: i for i, c in enumerate(fact_cols)}
    out: List[Dict[Tuple[Any, ...], int]] = []
    for t in tables:
        idx = {c: j for j, c in enumerate(t["columns"])}
        counts: Dict[Tuple[Any, ...], int] = {}
        for row in fact_rows:
            p = tuple(row[fact_idx[c]] for c in t["columns"])
            counts[p] = counts.get(p, 0) + 1
        out.append(counts)
    return out


def check_fact_delivery(fact_cols: List[str],
                        fact_rows: Sequence[Tuple[Any, ...]],
                        tables: List[Dict[str, Any]],
                        adj: List[List[int]],
                        sample_limit: int = 5
                        ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """按验收协议计算精确计数，返回 (计数, 缺行样例)。

    * 交集：事实行在每张交付表都存在一条完全相同投影的**实际交付行**
      （RIP 已由结构校验保证局部相容 ⇔ 全局可拼）；
    * 回拼行数：Acceptance 的自底向上精确计数，只对实际出现的键建映射；
    * 缺行 = |F| - |I|；造行 = |J| - |I|；两者互不抵销。
    """
    if not isinstance(fact_rows, list):
        fact_rows = list(fact_rows)
    fact_idx = {c: i for i, c in enumerate(fact_cols)}
    dsets = delivered_sets(tables)
    inter = 0
    missing_samples: List[Dict[str, Any]] = []
    for fr in fact_rows:
        missing_here = None
        for ti, t in enumerate(tables):
            p = tuple(fr[fact_idx[c]] for c in t["columns"])
            if p not in dsets[ti]:
                missing_here = (t, p)
                break
        if missing_here is None:
            inter += 1
        elif len(missing_samples) < sample_limit:
            t, p = missing_here
            missing_samples.append({
                "fact_row": list(fr),
                "missing_table": t["name"],
                "missing_row": list(p),
            })

    acc = Acceptance(fact_cols, list(fact_rows), tables, adj)
    join_count = acc.total_join_size(acc.bottom_up_counts())
    fact_count = len(fact_rows)
    missing = fact_count - inter
    spurious = join_count - inter
    if missing < 0 or spurious < 0:
        raise Rejection("internal", [
            f"内部错误：计数不自洽 fact={fact_count} join={join_count} inter={inter}"])
    counts = {
        "fact_row_count": fact_count,
        "reconstructed_distinct_row_count": join_count,
        "intersection_row_count": inter,
        "missing_row_count": missing,
        "spurious_row_count": spurious,
        "sets_equal": missing == 0 and spurious == 0,
    }
    return counts, missing_samples


# --------------------------------------------------------------------------- #
# 业务行键与上周事实索引
# --------------------------------------------------------------------------- #

def validate_key_columns(fact_cols: List[str],
                         key_cols_raw: Sequence[str]) -> List[str]:
    reasons: List[str] = []
    if not key_cols_raw:
        reasons.append("必须通过 --key-col 至少指定一个业务行键列")
    cols: List[str] = []
    for c in key_cols_raw:
        if not isinstance(c, str) or not c:
            reasons.append(f"业务行键列名必须是非空字符串，发现: {c!r}")
            continue
        if c in cols:
            reasons.append(f"业务行键列 {c!r} 重复指定")
            continue
        if c not in fact_cols:
            reasons.append(f"业务行键列 {c!r} 不在上周事实表的列中")
            continue
        cols.append(c)
    if reasons:
        raise Rejection("config", reasons)
    return cols


def build_key_index(fact_cols: List[str],
                    fact_rows: Sequence[Tuple[Any, ...]],
                    key_cols: List[str]
                    ) -> Tuple[Dict[Tuple[Any, ...], Tuple[Any, ...]],
                               List[int]]:
    """事实集合按业务行键建唯一索引。键为空值或键值不唯一都拒收。"""
    idx = [fact_cols.index(c) for c in key_cols]
    mapping: Dict[Tuple[Any, ...], Tuple[Any, ...]] = {}
    null_rows = 0
    duplicate_rows: List[Tuple[Any, ...]] = []
    for row in fact_rows:
        key = tuple(row[i] for i in idx)
        if any(v is None for v in key):
            null_rows += 1
            continue
        if key in mapping:
            duplicate_rows.append(key)
        else:
            mapping[key] = row
    reasons: List[str] = []
    detail: Dict[str, Any] = {}
    if null_rows:
        reasons.append(
            f"上周事实表有 {null_rows} 行的业务行键含空值；业务行键必须非空，无法定位变更")
        detail["null_key_row_count"] = null_rows
    if duplicate_rows:
        shown = [list(k) for k in duplicate_rows[:10]]
        reasons.append(
            f"上周事实表有 {len(duplicate_rows)} 个业务行键重复（不唯一），无法按行键定位变更: "
            f"{json.dumps(shown, ensure_ascii=False)}"
            + (" ..." if len(duplicate_rows) > 10 else ""))
        detail["duplicate_key_count"] = len(duplicate_rows)
        detail["duplicate_keys_sample"] = shown
    if reasons:
        raise Rejection("keys", reasons, detail)
    return mapping, idx


# --------------------------------------------------------------------------- #
# 变更文件解析与整批校验
# --------------------------------------------------------------------------- #

def _norm_key(spec: Any, key_cols: List[str], where: str,
              reasons: List[str]) -> Optional[Tuple[Any, ...]]:
    why = len(reasons)
    if isinstance(spec, dict):
        missing = [c for c in key_cols if c not in spec]
        extra = [c for c in spec if c not in key_cols]
        if missing:
            reasons.append(f"{where} 的 key 缺少业务行键列: {missing}")
        if extra:
            reasons.append(f"{where} 的 key 含多余字段: {sorted(extra)}")
        values = [spec.get(c) for c in key_cols]
    elif isinstance(spec, list):
        if len(spec) != len(key_cols):
            reasons.append(
                f"{where} 的 key 长度 {len(spec)} 与业务行键列数 {len(key_cols)} 不符")
        values = list(spec)
    else:
        reasons.append(f"{where} 的 key 必须是对象或数组")
        return None

    for c, v in zip(key_cols, values):
        if not is_scalar(v):
            reasons.append(
                f"{where} 的业务行键列 {c!r} 含不支持的值类型 {type_name(v)}")
        elif v is None:
            reasons.append(f"{where} 的业务行键列 {c!r} 的值为空；业务键不能是空值")
    if len(reasons) != why or len(values) != len(key_cols):
        return None
    return tuple(values)


def _norm_row(spec: Any, fact_cols: List[str], where: str,
              reasons: List[str]) -> Optional[Tuple[Any, ...]]:
    why = len(reasons)
    if isinstance(spec, list):
        if len(spec) != len(fact_cols):
            reasons.append(
                f"{where} 的 row 宽度为 {len(spec)}，应为 {len(fact_cols)}")
        values = list(spec)
    elif isinstance(spec, dict):
        missing = [c for c in fact_cols if c not in spec]
        extra = [c for c in spec if c not in fact_cols]
        if missing:
            reasons.append(f"{where} 的 row 缺少列: {missing}")
        if extra:
            reasons.append(f"{where} 的 row 含未知列: {sorted(extra)}")
        values = [spec.get(c) for c in fact_cols]
    else:
        reasons.append(f"{where} 的 row 必须是完整行（数组或列值对象）")
        return None

    for c, v in zip(fact_cols, values):
        if not is_scalar(v):
            reasons.append(
                f"{where} 的列 {c!r} 含不支持的值类型 {type_name(v)}"
                f"（仅支持整数/字符串/显式空值）")
    if len(reasons) != why or len(values) != len(fact_cols):
        return None
    return tuple(values)


def load_changes(path: str, fact_cols: List[str], key_cols: List[str],
                 base_keys: Dict[Tuple[Any, ...], Tuple[Any, ...]]
                 ) -> Tuple[List[Dict[str, Any]], int]:
    """解析并整体校验一批变更。返回 (规范化的操作列表, 提交的操作数)。

    规范化操作: {"op","key","row"(可选),"index"(文件内下标)}。
    任何结构/取值/存在性/重复问题都整批拒收，不返回部分结果。
    """
    try:
        data = _read_json(path)
    except InputError as exc:
        raise Rejection("changes", exc.reasons)

    if isinstance(data, list):
        raw_ops = data
    elif isinstance(data, dict):
        extra = [k for k in data if k not in ("changes_version", "changes", "operations")]
        if extra:
            raise Rejection("changes", [f"变更文件含未知顶层字段: {sorted(extra)}"])
        present = [k for k in ("changes", "operations") if k in data]
        if not present:
            raise Rejection("changes", ["变更文件必须包含 changes 数组"])
        if len(present) > 1:
            raise Rejection("changes", ["变更文件不能同时包含 changes 与 operations"])
        raw_ops = data[present[0]]
        if not isinstance(raw_ops, list):
            raise Rejection("changes", [f"变更文件的 {present[0]} 必须是数组"])
    else:
        raise Rejection("changes", ["变更文件必须是数组或含 changes 数组的对象"])

    reasons: List[str] = []
    ops: List[Dict[str, Any]] = []
    for oi, raw in enumerate(raw_ops):
        where = f"变更第 {oi} 条"
        if not isinstance(raw, dict):
            reasons.append(f"{where} 必须是对象")
            continue
        op = raw.get("op")
        if op not in VALID_OPS:
            reasons.append(
                f"{where} 的操作 {op!r} 非法（仅支持 insert/update/delete）")
            continue
        allowed = ALLOWED_FIELDS[op]
        extra = [k for k in raw if k not in allowed]
        if extra:
            reasons.append(f"{where}（{op}）含不允许的字段: {sorted(extra)}")
            continue
        if op in ("insert", "update") and "row" not in raw:
            reasons.append(f"{where}（{op}）必须给出修改后的完整 row")
            continue
        if op in ("update", "delete") and "key" not in raw:
            reasons.append(f"{where}（{op}）必须给出业务键 key")
            continue

        row: Optional[Tuple[Any, ...]] = None
        if op in ("insert", "update"):
            row = _norm_row(raw.get("row"), fact_cols, where, reasons)
        key: Optional[Tuple[Any, ...]] = None
        if "key" in raw:
            key = _norm_key(raw["key"], key_cols, where, reasons)
        elif row is not None:
            kidx = [fact_cols.index(c) for c in key_cols]
            key = tuple(row[i] for i in kidx)
            if any(v is None for v in key):
                reasons.append(f"{where} 的行在业务键列上含空值；业务键不能是空值")
                key = None
        if key is None:
            continue
        if op in ("insert", "update") and row is None:
            continue
        if op in ("insert", "update"):
            kidx = [fact_cols.index(c) for c in key_cols]
            if tuple(row[i] for i in kidx) != key:
                reasons.append(
                    f"{where}（{op}）的 row 业务键 {list(tuple(row[i] for i in kidx))} "
                    f"与 key {list(key)} 不一致；业务键不能改变")
                continue
        ops.append({"op": op, "key": key, "row": row, "index": oi})

    if reasons:
        raise Rejection("changes", reasons)

    # 同一批次：同一业务键不得重复或互相冲突
    seen: Dict[Tuple[Any, ...], List[int]] = {}
    for o in ops:
        seen.setdefault(o["key"], []).append(o["index"])
    repeated = {k: v for k, v in seen.items() if len(v) > 1}
    if repeated:
        shown = [{"key": list(k), "operation_indices": v}
                 for k, v in sorted(repeated.items(), key=lambda kv: row_sort_key(kv[0]))]
        raise Rejection(
            "changes",
            [f"同一批次对 {len(repeated)} 个业务键出现重复或冲突操作: "
             f"{json.dumps(shown[:10], ensure_ascii=False)}"
             + (" ..." if len(shown) > 10 else "")],
            {"duplicate_key_operations": shown[:10],
             "duplicate_key_count": len(shown)})

    # 存在性：新增键必须当前不存在；修改/删除键必须当前存在。
    # 新增同时必须是当前不存在的完整行——行已存在则其业务键必已存在，故由键检查覆盖；
    # 业务键必须保持唯一，否则下一周无法再按行键定位修改/删除。
    bad_insert = [o for o in ops if o["op"] == "insert" and o["key"] in base_keys]
    bad_existing = [o for o in ops
                    if o["op"] in ("update", "delete") and o["key"] not in base_keys]
    if bad_insert or bad_existing:
        reasons2: List[str] = []
        detail: Dict[str, Any] = {}
        if bad_insert:
            keys = [list(o["key"]) for o in bad_insert[:10]]
            reasons2.append(
                f"{len(bad_insert)} 条新增使用了当前已存在的业务键（新增必须是当前不存在的"
                f"完整行，且业务键必须保持唯一）: "
                f"{json.dumps(keys, ensure_ascii=False)}"
                + (" ..." if len(bad_insert) > 10 else ""))
            detail["insert_existing_keys"] = keys
        if bad_existing:
            keys = [{"key": list(o["key"]), "op": o["op"]}
                    for o in bad_existing[:10]]
            reasons2.append(
                f"{len(bad_existing)} 条修改/删除使用了当前不存在的业务键: "
                f"{json.dumps(keys, ensure_ascii=False)}"
                + (" ..." if len(bad_existing) > 10 else ""))
            detail["update_delete_unknown_keys"] = keys
        raise Rejection("changes", reasons2, detail)
    return ops, len(raw_ops)


# --------------------------------------------------------------------------- #
# 应用变更：事实集合 + 各交付表投影计数（不物化连接结果）
# --------------------------------------------------------------------------- #

def apply_changes(fact_cols: List[str],
                  fact_rows: Sequence[Tuple[Any, ...]],
                  tables: List[Dict[str, Any]],
                  base_proj_counts: List[Dict[Tuple[Any, ...], int]],
                  base_keys: Dict[Tuple[Any, ...], Tuple[Any, ...]],
                  key_cols: List[str],
                  ops: List[Dict[str, Any]]
                  ) -> Tuple[List[Tuple[Any, ...]],
                             List[Dict[Tuple[Any, ...], int]],
                             List[Dict[str, Any]]]:
    """返回 (本周去重事实行(按业务键排序), 各表投影计数, 受影响键清单)。

    投影只维护「投影 -> 支持它的去重事实行数」，因此删除一条事实行仅在计数
    归零时才移除该交付行；修改连接键会同时反映到所有声明该列的表。
    """
    kidx = [fact_cols.index(c) for c in key_cols]
    fact_idx = {c: i for i, c in enumerate(fact_cols)}
    ordering = sorted(ops, key=lambda o: row_sort_key(o["key"]))
    fact_map = dict(base_keys)
    proj_counts = [{k: int(v) for k, v in c.items()} for c in base_proj_counts]
    table_cols = [list(t["columns"]) for t in tables]

    def bump(row: Tuple[Any, ...], sign: int, reason: str) -> None:
        for ti, cols_t in enumerate(table_cols):
            p = tuple(row[fact_idx[c]] for c in cols_t)
            old = proj_counts[ti].get(p, 0)
            new = old + sign
            if new < 0:
                raise Rejection("internal", [
                    f"内部错误：交付表 {tables[ti]['name']!r} 投影 {list(p)} "
                    f"计数为负（{reason}）"])
            if new == 0:
                proj_counts[ti].pop(p, None)
            else:
                proj_counts[ti][p] = new

    affected: List[Dict[str, Any]] = []
    for o in ordering:
        key, op = o["key"], o["op"]
        if op == "insert":
            if key in fact_map:
                raise Rejection("internal", ["内部错误：新增键在校验后仍已存在"])
            fact_map[key] = o["row"]
            bump(o["row"], +1, "insert")
            affected.append({"key": key, "op": op, "row_changed": True})
        elif op == "update":
            if key not in fact_map:
                raise Rejection("internal", ["内部错误：修改键在校验后仍不存在"])
            old_row = fact_map[key]
            new_row = o["row"]
            if tuple(new_row[i] for i in kidx) != key:
                raise Rejection("internal", ["内部错误：修改改变了业务键"])
            if old_row != new_row:
                bump(old_row, -1, "update-before")
                bump(new_row, +1, "update-after")
                fact_map[key] = new_row
                affected.append({"key": key, "op": op, "row_changed": True})
            else:
                affected.append({"key": key, "op": op, "row_changed": False})
        else:  # delete
            if key not in fact_map:
                raise Rejection("internal", ["内部错误：删除键在校验后仍不存在"])
            old_row = fact_map.pop(key)
            bump(old_row, -1, "delete")
            affected.append({"key": key, "op": op, "row_changed": True})

    # 结果按业务键规范排序，保证相同输入得到相同事实集合与交付包
    ordered_keys = sorted(fact_map.keys(), key=row_sort_key)
    new_rows = [fact_map[k] for k in ordered_keys]
    return new_rows, proj_counts, affected


def check_column_types(fact_cols: List[str],
                       fact_rows: Sequence[Tuple[Any, ...]]) -> None:
    """本周事实集合中每一列的非空值类型必须单一，否则下游验收入口会拒收。"""
    reasons: List[str] = []
    for j, c in enumerate(fact_cols):
        ts = {type_name(r[j]) for r in fact_rows if r[j] is not None}
        if len(ts) > 1:
            reasons.append(f"变更后列 {c!r} 出现冲突类型: {sorted(ts)}")
    if reasons:
        raise Rejection("changes", reasons)


# --------------------------------------------------------------------------- #
# 生成本周交付包（每表 = 本周事实行在该表列上的投影，集合去重）
# --------------------------------------------------------------------------- #

def build_new_tables(tables: List[Dict[str, Any]],
                     proj_counts: List[Dict[Tuple[Any, ...], int]]
                     ) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for ti, t in enumerate(tables):
        rows = sorted(proj_counts[ti].keys(), key=row_sort_key)
        out.append({
            "name": t["name"],
            "columns": list(t["columns"]),
            "rows": [list(r) for r in rows],
        })
    return out


def build_delivery_json(tables: List[Dict[str, Any]],
                        tree_raw: List[Any]) -> Dict[str, Any]:
    return {
        "delivery_version": DELIVERY_VERSION,
        "tables": tables,
        "tree": json.loads(json.dumps(tree_raw)),
    }


def build_fact_json(fact_cols: List[str],
                    rows: Sequence[Tuple[Any, ...]]) -> Dict[str, Any]:
    return {"columns": list(fact_cols),
            "rows": [list(r) for r in rows]}


# --------------------------------------------------------------------------- #
# 报告：确定性、不含时间戳/绝对路径
# --------------------------------------------------------------------------- #

def _key_obj(key: Tuple[Any, ...], key_cols: List[str]) -> Dict[str, Any]:
    return {c: v for c, v in zip(key_cols, key)}


def _precheck_tables(tables: List[Dict[str, Any]],
                     projection_counts: List[Dict[Tuple[Any, ...], int]]
                     ) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for ti, t in enumerate(tables):
        delivered_rows = set(t["rows"])
        proj_rows = set(projection_counts[ti])
        out.append({
            "name": t["name"],
            "delivered_row_count": len(delivered_rows),
            "fact_projection_row_count": len(proj_rows),
            "delivered_rows_outside_projection": len(delivered_rows - proj_rows),
            "projection_rows_not_delivered": len(proj_rows - delivered_rows),
        })
    return out


def _table_deltas(tables: List[Dict[str, Any]],
                  proj_counts: List[Dict[Tuple[Any, ...], int]]
                  ) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for ti, t in enumerate(tables):
        before = set(t["rows"])
        after = set(proj_counts[ti])
        out.append({
            "name": t["name"],
            "columns": list(t["columns"]),
            "before_row_count": len(before),
            "after_row_count": len(after),
            "added_distinct_row_count": len(after - before),
            "removed_distinct_row_count": len(before - after),
            "unchanged_row_count": len(after & before),
        })
    return out


# --------------------------------------------------------------------------- #
# 原子落盘：任一文件写入失败则回滚，不留下可被当作新版本的部分结果
# --------------------------------------------------------------------------- #

def atomic_write_outputs(files: Sequence[Tuple[str, str]]) -> None:
    """把 [(path, text)] 作为一组原子发布：失败时恢复原有文件。"""
    staged: List[Tuple[str, str]] = []
    backups: List[Tuple[str, Optional[str]]] = []
    placed: List[str] = []
    try:
        for path, text in files:
            directory = os.path.dirname(os.path.abspath(path)) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".rollforward-", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            staged.append((path, tmp))
        for path, _ in files:
            if os.path.exists(path):
                directory = os.path.dirname(os.path.abspath(path)) or "."
                fd, bak = tempfile.mkstemp(prefix=".rollforward-bak-", dir=directory)
                os.close(fd)
                os.replace(path, bak)
                backups.append((path, bak))
            else:
                backups.append((path, None))
        for (path, tmp), (_, bak) in zip(staged, backups):
            os.replace(tmp, path)
            placed.append(path)
    except BaseException:
        for path in reversed(placed):
            try:
                os.remove(path)
            except OSError:
                pass
        for path, bak in backups:
            if bak is not None and os.path.exists(bak):
                try:
                    os.replace(bak, path)
                except OSError:
                    pass
        for _, tmp in staged:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        raise
    else:
        for _, bak in backups:
            if bak is not None and os.path.exists(bak):
                try:
                    os.remove(bak)
                except OSError:
                    pass


# === SECTION 3 ===


# --------------------------------------------------------------------------- #
# 编排：预检 -> 变更 -> 生成 -> 后检 -> 报告 -> 原子落盘
# --------------------------------------------------------------------------- #

def run_rollforward(fact_path: str, delivery_path: str, changes_path: str,
                    key_cols_raw: Sequence[str],
                    require_lossless: bool = False
                    ) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """返回 (交付 JSON, 事实 JSON, 报告 JSON)。任何拒收都抛出 Rejection。"""
    # ---------------- 上周输入加载（沿用验收协议） ---------------- #
    fact_cols, fact_rows, notices = load_week_fact(fact_path)
    key_cols = validate_key_columns(fact_cols, key_cols_raw)
    tables, tree_raw, adj, dnotices = load_week_delivery(
        delivery_path, fact_cols, fact_rows)
    notices = notices + dnotices

    # ---------------- 业务行键唯一性 ---------------- #
    base_keys, _kidx = build_key_index(fact_cols, fact_rows, key_cols)

    # ---------------- 预检：上周事实集合 == 上周交付回拼集合 ---------------- #
    precheck, missing_samples = check_fact_delivery(
        fact_cols, fact_rows, tables, adj)
    if not precheck["sets_equal"]:
        reasons = []
        if precheck["missing_row_count"]:
            reasons.append(
                f"上周交付包缺行 {precheck['missing_row_count']} 条："
                f"这些事实行无法由交付窄表回拼")
        if precheck["spurious_row_count"]:
            reasons.append(
                f"上周交付包造行 {precheck['spurious_row_count']} 条："
                f"回拼结果中存在上周事实表没有的完整行")
        reasons.append("上周交付包与上周事实表不是同一份整行集合，拒绝在错误基线上滚动")
        raise Rejection("precheck", reasons, {
            "verdict": precheck,
            "missing_samples": missing_samples,
        })

    # 上周交付行集合必须恰好等于事实投影集合（sets_equal 时必然成立，防御性核对）
    base_proj_counts = fact_projection_counts(tables, fact_cols, fact_rows)
    for ti, t in enumerate(tables):
        if set(base_proj_counts[ti]) != set(t["rows"]):
            raise Rejection("internal", [
                f"内部错误：交付表 {t['name']!r} 的去重行与事实投影不一致"])
    precheck["tables"] = _precheck_tables(tables, base_proj_counts)

    # ---------------- 变更解析与整批校验 ---------------- #
    ops, submitted = load_changes(changes_path, fact_cols, key_cols, base_keys)

    # ---------------- 应用变更得到本周事实与投影 ---------------- #
    new_rows, proj_counts, affected = apply_changes(
        fact_cols, fact_rows, tables, base_proj_counts, base_keys, key_cols, ops)
    check_column_types(fact_cols, new_rows)

    # ---------------- 生成本周交付包 ---------------- #
    new_tables = build_new_tables(tables, proj_counts)
    delivery_json = build_delivery_json(new_tables, tree_raw)
    fact_json = build_fact_json(fact_cols, new_rows)

    # ---------------- 后检：新包按同一协议复核（信息性，供报告与严格模式） ---------------- #
    check_tables = [
        {"name": t["name"], "columns": list(t["columns"]),
         "rows": [tuple(r) for r in t["rows"]],
         "row_indices": [[i] for i in range(len(t["rows"]))]}
        for t in new_tables
    ]
    # Acceptance 需要邻接表；沿用上周结构（表集合与连接树未改变）
    postcheck, _ = check_fact_delivery(fact_cols, new_rows, check_tables, adj)
    warnings = list(notices)
    if not postcheck["sets_equal"]:
        warnings.append(
            "WARNING:本周投影包按上周表列与连接树无法无损回拼本周事实集合"
            f"（缺行 {postcheck['missing_row_count']}、造行 "
            f"{postcheck['spurious_row_count']}）；请下游据此复核")
        if require_lossless:
            raise Rejection("postcheck", [
                "使用 --require-lossless 时，本周投影包必须无损回拼本周事实集合，"
                f"实际缺行 {postcheck['missing_row_count']}、"
                f"造行 {postcheck['spurious_row_count']}"], {"verdict": postcheck})

    # ---------------- 报告 ---------------- #
    by_op = {op: 0 for op in VALID_OPS}
    for o in ops:
        by_op[o["op"]] += 1
    affected_view = [
        {"key": _key_obj(a["key"], key_cols), "operation": a["op"],
         "row_changed": a["row_changed"]}
        for a in affected
    ]
    table_deltas = _table_deltas(tables, proj_counts)

    report: Dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "status": "OK",
        "tool": "delivery_rollforward",
        "inputs": {
            "fact": {"file_sha256": sha256_file(fact_path),
                     "fact_row_count": len(fact_rows)},
            "delivery": {"file_sha256": sha256_file(delivery_path),
                         "delivery_content_sha256": sha256_canonical(
                             {"tables": [{"name": t["name"],
                                          "columns": t["columns"],
                                          "rows": [list(r) for r in t["rows"]]}
                                         for t in tables],
                              "tree": tree_raw})},
            "changes": {"file_sha256": sha256_file(changes_path),
                        "submitted_operation_count": submitted},
        },
        "key_columns": list(key_cols),
        "precheck": precheck,
        "changes": {
            "complete_applied": True,
            "submitted_operation_count": submitted,
            "accepted_operation_count": len(ops),
            "by_operation": by_op,
            "affected_business_key_count": len(affected),
            "affected_business_keys": affected_view,
        },
        "tables": table_deltas,
        "postcheck": postcheck,
        "outputs": {
            "delivery_content_sha256": sha256_canonical(delivery_json),
            "fact_content_sha256": sha256_canonical(fact_json),
            "delivery_row_count_by_table": [
                {"name": t["name"], "row_count": len(t["rows"])}
                for t in new_tables],
            "fact_row_count": len(new_rows),
        },
        "warnings": warnings,
    }
    return delivery_json, fact_json, report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _is_within_output(path: str, inputs: Sequence[str]) -> bool:
    a = os.path.realpath(os.path.abspath(path))
    for p in inputs:
        if os.path.realpath(os.path.abspath(p)) == a:
            return True
    return False


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="窄表交付按周滚动发布离线入口（应用变更、发布下一版窄表快照）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("apply", "publish", "rollforward"):
        p = sub.add_parser(name, help="应用变更并发布下一版窄表快照")
        p.add_argument("--fact", required=True, help="上周权威事实表 JSON")
        p.add_argument("--delivery", required=True, help="上周实际交付包 JSON")
        p.add_argument("--changes", required=True, help="本周变更文件 JSON")
        p.add_argument("--key-col", required=True, action="append",
                       help="业务行键列，可重复指定；顺序即报告中的键字段顺序")
        p.add_argument("--out-dir", default="rollforward_out",
                       help="输出目录（默认 rollforward_out）")
        p.add_argument("--delivery-out", help="新交付包路径（默认 <out-dir>/delivery.json）")
        p.add_argument("--report-out", help="变更报告路径（默认 <out-dir>/change_report.json）")
        p.add_argument("--fact-out", help="本周事实表路径（默认 <out-dir>/fact.json）")
        p.add_argument("--report-stdout", action="store_true",
                       help="同时把报告打印到 stdout")
        p.add_argument("--require-lossless", action="store_true",
                       help="本周投影包必须无损回拼本周事实集合，否则整批拒收")

    args = parser.parse_args(argv)
    out_dir = args.out_dir
    delivery_out = args.delivery_out or os.path.join(out_dir, DEFAULT_DELIVERY_NAME)
    report_out = args.report_out or os.path.join(out_dir, DEFAULT_REPORT_NAME)
    fact_out = args.fact_out or os.path.join(out_dir, DEFAULT_FACT_NAME)

    inputs = [args.fact, args.delivery, args.changes]
    outputs = (("delivery", delivery_out), ("report", report_out),
               ("fact", fact_out))
    for label, path in outputs:
        if _is_within_output(path, inputs):
            print(json.dumps({
                "report_version": REPORT_VERSION,
                "status": "REJECTED",
                "stage": "config",
                "reasons": [f"输出路径 {path!r}（{label}）与输入文件重叠；"
                            "为避免破坏输入，请使用不同的输出路径"],
            }, ensure_ascii=False, indent=2), file=sys.stderr)
            return 2
    # 三个输出彼此不得重叠，否则其中一个会覆盖另一个的发布内容
    seen_out: Dict[str, str] = {}
    for label, path in outputs:
        key = os.path.realpath(os.path.abspath(path))
        if key in seen_out:
            print(json.dumps({
                "report_version": REPORT_VERSION,
                "status": "REJECTED",
                "stage": "config",
                "reasons": [f"输出 {label} 与输出 {seen_out[key]} 指向同一路径 {path!r}"],
            }, ensure_ascii=False, indent=2), file=sys.stderr)
            return 2
        seen_out[key] = label

    try:
        delivery_json, fact_json, report = run_rollforward(
            args.fact, args.delivery, args.changes, list(args.key_col),
            require_lossless=args.require_lossless)
    except Rejection as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2),
              file=sys.stderr)
        return 2

    delivery_text = dump_json(delivery_json)
    fact_text = dump_json(fact_json)
    report["outputs"]["delivery_file_sha256"] = hashlib.sha256(
        delivery_text.encode("utf-8")).hexdigest()
    report["outputs"]["fact_file_sha256"] = hashlib.sha256(
        fact_text.encode("utf-8")).hexdigest()
    report_text = dump_json(report)

    try:
        atomic_write_outputs([
            (delivery_out, delivery_text),
            (fact_out, fact_text),
            (report_out, report_text),
        ])
    except OSError as exc:
        print(json.dumps({
            "report_version": REPORT_VERSION,
            "status": "REJECTED",
            "stage": "write",
            "reasons": [f"写入输出失败，已回滚且未留下新版本: {exc}"],
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    if args.report_stdout:
        print(report_text, end="")
    else:
        print(json.dumps({
            "status": "OK",
            "delivery": delivery_out,
            "fact": fact_out,
            "report": report_out,
            "accepted_operation_count": report["changes"]["accepted_operation_count"],
            "affected_business_key_count": report["changes"]["affected_business_key_count"],
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())