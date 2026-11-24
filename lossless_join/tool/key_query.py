#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
key_query.py — 交付窄表批量主键查询入口（不依赖原始事实表）

客服拿到一批业务主键后，可在只有 delivery.json（窄表 + 连接树 + 列目录并集）
的情况下导出这些键对应的完整事实行。

语义（与 lossless_accept / fee_ledger 完全一致）：
  * 空值是可相等的显式字面值（None == None）；
  * 值仅支持整数、字符串、null，bool/float/object/array 作为标量值均非法；
  * 整行集合去重；
  * 一条完整行可返回，当且仅当它在连接树的每张窄表中都有投影完全相容的实际行；
  * 相邻节点按全部共享列对齐；共享接口不相容时不会拼接不同局部值；
  * RIP 保证沿树传播即可保证全局相容。

实现使用请求键位掩码做自底向上/自顶向下剪枝，只枚举最终会出现的行组合：
不构造未请求键的全树连接，也不先物化可能爆炸的中间连接结果。

命令：
  key_query.py [query|lookup|batch-query] --delivery D \
      --key-col K [--key-col K2 ...] --keys keys.json \
      --fact-out fact.json --report-out query_report.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

# `python3 -I /path/to/tool/key_query.py` 时脚本目录仍可用于导入同目录模块；
# 显式插入也兼容从其他工作目录或作为命名空间包导入。
_TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOL_DIR not in sys.path:
    sys.path.insert(0, _TOOL_DIR)

from lossless_accept import InputError, _read_json, is_scalar, type_name  # noqa: E402
from fee_ledger import load_narrow_delivery, validate_tree_and_types  # noqa: E402

QUERY_VERSION = "1.0"
DEFAULT_FACT_NAME = "fact.json"
DEFAULT_REPORT_NAME = "query_report.json"
COMPAT_REPORT_NAME = "key_query_report.json"
DEFAULT_OUT_DIR = "key_query_out"

KNOWN_COMMANDS = {
    "query", "query-keys", "key-query", "batch-query", "batch_query",
    "lookup", "key-lookup", "key_lookup", "search", "fetch",
}

# 键请求文件中可能使用的批次字段。第一个存在的字段生效。
BATCH_FIELDS = (
    "keys", "key_values", "requested_keys", "batch_keys", "primary_keys",
    "pks", "queries", "requests", "values",
)
EMBEDDED_COL_FIELDS = (
    "key_columns", "key_cols", "primary_key_columns", "pk_columns",
    "column_names", "columns",
)


class QueryRejection(Exception):
    """输入或查询整批失败；调用方不得发布成功查询的结果组。"""

    def __init__(self, stage: str, reasons: List[str],
                 detail: Optional[Dict[str, Any]] = None):
        super().__init__("; ".join(reasons))
        self.stage = stage
        self.reasons = list(reasons)
        self.detail = detail or {}

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "query_version": QUERY_VERSION,
            "status": "REJECTED",
            "stage": self.stage,
            "reasons": self.reasons,
        }
        if self.detail:
            out["detail"] = self.detail
        return out


# --------------------------------------------------------------------------- #
# 确定性排序 / JSON
# --------------------------------------------------------------------------- #

def value_sort_key(v: Any) -> Tuple[Any, ...]:
    """null 最前，整数按数值，字符串按 Unicode 码点。"""
    if v is None:
        return (0, 0)
    if isinstance(v, bool):
        raise QueryRejection("internal", [f"不支持排序的布尔值: {v!r}"])
    if isinstance(v, int):
        return (1, v)
    if isinstance(v, str):
        return (2, v)
    raise QueryRejection("internal", [f"不支持排序的值类型: {type_name(v)}"])


def row_sort_key(row: Sequence[Any]) -> Tuple[Tuple[Any, ...], ...]:
    return tuple(value_sort_key(v) for v in row)


def key_object(key: Sequence[Any], key_cols: Sequence[str]) -> Dict[str, Any]:
    return {c: v for c, v in zip(key_cols, key)}


def dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def canonical_json(obj: Any) -> str:
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


# --------------------------------------------------------------------------- #
# 配置与键批次解析
# --------------------------------------------------------------------------- #

def normalize_key_columns(cli_values: Sequence[str],
                          csv_values: Sequence[str],
                          embedded: Optional[Sequence[str]] = None,
                          known_columns: Optional[Sequence[str]] = None
                          ) -> List[str]:
    known_set = set(known_columns or [])
    raw: List[Any] = list(cli_values or [])
    expanded: List[Any] = []
    for item in raw:
        if (isinstance(item, str) and "," in item and known_set
                and item not in known_set):
            parts = [p.strip() for p in item.split(",") if p.strip()]
            if all(p in known_set for p in parts):
                expanded.extend(parts)
                continue
        expanded.append(item)
    raw = expanded

    for item in csv_values or []:
        if isinstance(item, str):
            stripped = item.strip()
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, list):
                        raw.extend(parsed)
                        continue
                except json.JSONDecodeError:
                    pass
            raw.extend([part.strip() for part in item.split(",") if part.strip()])
        else:
            raw.append(item)

    if not raw and embedded is not None:
        raw = list(embedded)

    reasons: List[str] = []
    if not raw:
        raise QueryRejection("config", [
            "必须至少指定一个主键列（--key-col，可重复指定）"])

    cols: List[str] = []
    seen = set()
    for c in raw:
        if not isinstance(c, str) or not c:
            reasons.append(f"主键列名必须是非空字符串，发现: {c!r}")
            continue
        if c in seen:
            reasons.append(f"主键列 {c!r} 重复指定")
            continue
        seen.add(c)
        cols.append(c)
    if reasons:
        raise QueryRejection("config", reasons)
    return cols


def validate_key_columns_in_directory(key_cols: Sequence[str],
                                      directory: Sequence[str]) -> None:
    col_set = set(directory)
    unknown = [c for c in key_cols if c not in col_set]
    if unknown:
        raise QueryRejection("config", [
            f"主键列 {unknown} 不在交付包列目录 {list(directory)!r} 中"])


def normalize_one_key(spec: Any, key_cols: Sequence[str],
                      where: str) -> Tuple[Optional[Tuple[Any, ...]], List[str]]:
    reasons: List[str] = []

    # 兼容 [{"key": {...}}] / [{"primary_key": [...]}] 等包装形式。
    if isinstance(spec, dict) and not all(c in spec for c in key_cols):
        for wrap_field in ("key", "primary_key", "pk", "key_value", "values"):
            if wrap_field in spec:
                spec = spec[wrap_field]
                break

    values: Any
    if len(key_cols) == 1 and is_scalar(spec):
        values = [spec]
    elif isinstance(spec, dict):
        missing = [c for c in key_cols if c not in spec]
        extra = [c for c in spec if c not in key_cols]
        if missing:
            reasons.append(f"{where} 缺少主键列: {missing}")
        if extra:
            reasons.append(f"{where} 含多余字段: {sorted(extra)}")
        values = [spec.get(c) for c in key_cols]
    elif isinstance(spec, (list, tuple)):
        if len(spec) != len(key_cols):
            reasons.append(
                f"{where} 的主键长度 {len(spec)} 与主键列数 {len(key_cols)} 不符")
        values = list(spec)
    else:
        if len(key_cols) == 1:
            reasons.append(
                f"{where} 的主键值类型 {type_name(spec)} 不受支持"
                "（仅允许整数/字符串/显式 null）")
        else:
            reasons.append(
                f"{where} 必须是{len(key_cols)} 个标量组成的数组或列名对象")
        return None, reasons

    if not isinstance(values, list):
        return None, reasons

    for i, (c, v) in enumerate(zip(key_cols, values)):
        if not is_scalar(v):
            reasons.append(
                f"{where} 第 {i + 1} 个主键列 {c!r} 的值类型 "
                f"{type_name(v)} 不受支持（仅允许整数/字符串/显式 null）")
    if len(values) != len(key_cols) or reasons:
        return None, reasons
    return tuple(values), reasons


def extract_batch_raw(data: Any) -> Tuple[List[Any], Optional[List[str]]]:
    embedded_cols: Optional[List[str]] = None
    if isinstance(data, list):
        return data, None
    if not isinstance(data, dict):
        raise QueryRejection("keys", ["主键请求文件必须是数组或 JSON 对象"])

    for field in EMBEDDED_COL_FIELDS:
        if field in data:
            ec = data[field]
            if not isinstance(ec, list) or not all(isinstance(c, str) for c in ec):
                raise QueryRejection("keys", [f"{field} 必须是字符串数组"])
            embedded_cols = list(ec)
            break

    for field in BATCH_FIELDS:
        if field in data:
            raw = data[field]
            if not isinstance(raw, list):
                raise QueryRejection("keys", [f"主键批次字段 {field!r} 必须是数组"])
            return raw, embedded_cols

    # 复合键的单个对象：{"a": 1, "b": "x"}。
    if embedded_cols is not None and all(c in data for c in embedded_cols):
        return [data], embedded_cols

    raise QueryRejection("keys", [
        "主键请求文件必须包含 keys/key_values/requests 等数组字段，"
        "或直接给出主键数组/单个主键对象"])


def parse_key_batch(raw_requests: Sequence[Any],
                    key_cols: Sequence[str]
                    ) -> Tuple[List[Tuple[Any, ...]], int, int]:
    """返回 (按首次出现去重的键, 原始请求数, 重复请求数)。"""
    unique: List[Tuple[Any, ...]] = []
    seen = set()
    reasons: List[str] = []

    for i, spec in enumerate(raw_requests):
        key, key_reasons = normalize_one_key(
            spec, key_cols, f"主键批次第 {i} 项")
        if key_reasons:
            reasons.extend(key_reasons)
            continue
        assert key is not None
        if key not in seen:
            seen.add(key)
            unique.append(key)

    if reasons:
        raise QueryRejection("keys", reasons)

    submitted = len(raw_requests)
    duplicate_count = submitted - len(unique)
    return unique, submitted, duplicate_count


def read_key_batch_file(path: str
                        ) -> Tuple[List[Any], Optional[List[str]], Optional[str]]:
    try:
        data = _read_json(path)
    except InputError as exc:
        raise QueryRejection("keys", exc.reasons)
    raw, embedded = extract_batch_raw(data)
    file_sha = sha256_file(path)
    return raw, embedded, file_sha


def parse_cli_scalar(text: str) -> Any:
    """解析命令行标量；无法按 JSON 解析时按原字符串处理。"""
    try:
        v = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text
    if not is_scalar(v):
        # 例如 true 在协议中是非法 bool；更可能调用者想传字符串 "true"。
        if isinstance(v, bool):
            return text
        raise QueryRejection("config", [
            f"命令行主键值 {text!r} 的类型 {type_name(v)} 不受支持"])
    return v


def parse_smart_key_token(token: str, directory: Sequence[str]
                          ) -> Tuple[Optional[str], Optional[Any]]:
    """把 --key 参数智能区分为列名或键值。

    若 token 正好是交付包列目录中的列名，视为主键列；否则尝试解析为键值。
    支持 id=123 / id="x" 的单列对象写法。
    """
    if token in set(directory):
        return token, None
    if "=" in token:
        name, raw_value = token.split("=", 1)
        name = name.strip()
        if name in set(directory):
            return None, {name: parse_cli_scalar(raw_value)}
    if token and token[0] in "[{\"" or token in ("null", "true", "false") \
            or (token[:1].isdigit() or token[:1] == "-"):
        return None, parse_cli_scalar(token)
    # 不是 JSON 形态且不是已知列：仍可能是字符串主键值。
    return None, token


# --------------------------------------------------------------------------- #
# 核心查询：请求键位掩码 + 树上剪枝 + 输出规模枚举
# --------------------------------------------------------------------------- #

def build_tree_order(tables: Sequence[Dict[str, Any]],
                     adj: Sequence[Sequence[int]]
                     ) -> Tuple[List[int], List[int], List[List[int]]]:
    n = len(tables)
    parent = [-1] * n
    parent[0] = -2
    children: List[List[int]] = [[] for _ in range(n)]
    order: List[int] = []
    queue = [0]
    head = 0
    while head < len(queue):
        u = queue[head]
        head += 1
        order.append(u)
        for v in adj[u]:
            if parent[v] != -1:
                continue
            parent[v] = u
            children[u].append(v)
            queue.append(v)
    if len(order) != n:
        # validate_tree_and_types 已经拒收非连通包；这里是防御性检查。
        raise QueryRejection("delivery", ["连接树不连通，无法执行主键查询"])
    return parent, order, children


def query_keys_in_memory(tables: Sequence[Dict[str, Any]],
                         adj: Sequence[Sequence[int]],
                         directory: Sequence[str],
                         key_cols: Sequence[str],
                         requested_keys: Sequence[Tuple[Any, ...]]
                         ) -> Tuple[List[Tuple[Any, ...]],
                                    Dict[Tuple[Any, ...], int]]:
    """执行查询并返回 (排序去重完整行, 每请求键行数)。

    requested_keys 应由调用方去重；顺序任意。返回的计数覆盖每个请求键。
    """
    # 即使程序化调用方传入重复键，也严格按一个请求键处理。
    keys = sorted(set(requested_keys), key=row_sort_key)
    q_count = len(keys)
    all_mask = (1 << q_count) - 1 if q_count else 0
    key_pos_in_directory = [directory.index(c) for c in key_cols]

    parent, order, children = build_tree_order(tables, adj)
    n = len(tables)

    dir_index = {c: i for i, c in enumerate(directory)}
    table_index = [{c: i for i, c in enumerate(t["columns"])} for t in tables]
    global_positions = [
        [dir_index[c] for c in t["columns"]] for t in tables
    ]

    # 每条父子边的共享接口列。RIP 保证非相邻同名列也会逐边传播。
    edge_cols: List[List[str]] = [[] for _ in range(n)]
    edge_parent_pos: List[List[int]] = [[] for _ in range(n)]
    edge_child_pos: List[List[int]] = [[] for _ in range(n)]
    for v in range(1, n):
        u = parent[v]
        u_set = set(tables[u]["columns"])
        cols = [c for c in directory
                if c in u_set and c in table_index[v]]
        edge_cols[v] = cols
        edge_parent_pos[v] = [table_index[u][c] for c in cols]
        edge_child_pos[v] = [table_index[v][c] for c in cols]

    row_orders: List[List[int]] = []
    for t in tables:
        rows = t["rows"]
        row_orders.append(sorted(range(len(rows)),
                                 key=lambda i: row_sort_key(rows[i])))

    # 1) 每张表的本地请求键掩码：只检查该表实际声明的主键列投影。
    subtree_masks: List[List[int]] = []
    for ti, t in enumerate(tables):
        rows = t["rows"]
        present = [(kid, table_index[ti][c])
                   for kid, c in enumerate(key_cols)
                   if c in table_index[ti]]
        if not present:
            subtree_masks.append([all_mask] * len(rows))
            continue

        part_to_mask: Dict[Tuple[Any, ...], int] = {}
        for qid, key in enumerate(keys):
            part = tuple(key[kid] for kid, _ in present)
            part_to_mask[part] = part_to_mask.get(part, 0) | (1 << qid)

        masks: List[int] = []
        for row in rows:
            part = tuple(row[pos] for _, pos in present)
            masks.append(part_to_mask.get(part, 0))
        subtree_masks.append(masks)

    # 2) 自底向上：孩子子树必须在共享接口上支持该请求。
    for u in reversed(order):
        for v in children[u]:
            support_by_interface: Dict[Tuple[Any, ...], int] = {}
            v_rows = tables[v]["rows"]
            cpos = edge_child_pos[v]
            for j, mask in enumerate(subtree_masks[v]):
                if not mask:
                    continue
                interf = tuple(v_rows[j][p] for p in cpos)
                support_by_interface[interf] = (
                    support_by_interface.get(interf, 0) | mask)

            u_rows = tables[u]["rows"]
            ppos = edge_parent_pos[v]
            masks_u = subtree_masks[u]
            for i, local_mask in enumerate(masks_u):
                if not local_mask:
                    continue
                interf = tuple(u_rows[i][p] for p in ppos)
                masks_u[i] = local_mask & support_by_interface.get(interf, 0)

    # 3) 自顶向下：加入祖先/旁支约束，得到每行对每个请求的全局可行掩码。
    global_masks: List[List[int]] = [[] for _ in range(n)]
    global_masks[0] = list(subtree_masks[0])
    for u in order:
        u_rows = tables[u]["rows"]
        for v in children[u]:
            ppos = edge_parent_pos[v]
            outside_by_interface: Dict[Tuple[Any, ...], int] = {}
            for i, mask in enumerate(global_masks[u]):
                if not mask:
                    continue
                interf = tuple(u_rows[i][p] for p in ppos)
                outside_by_interface[interf] = (
                    outside_by_interface.get(interf, 0) | mask)

            v_rows = tables[v]["rows"]
            cpos = edge_child_pos[v]
            out_masks: List[int] = [0] * len(v_rows)
            for j, sub_mask in enumerate(subtree_masks[v]):
                if not sub_mask:
                    continue
                interf = tuple(v_rows[j][p] for p in cpos)
                out_masks[j] = sub_mask & outside_by_interface.get(interf, 0)
            global_masks[v] = out_masks

    # 4) 只为全局可行的 (请求, 行, 接口) 建索引。每个这样的对都由至少一条
    #    最终结果见证，因此索引规模受输出行数 × 表数约束，而不是全连接大小。
    root_by_request: Dict[int, List[int]] = {}
    for i in row_orders[0]:
        bits = global_masks[0][i]
        while bits:
            bit = bits & -bits
            qid = bit.bit_length() - 1
            root_by_request.setdefault(qid, []).append(i)
            bits ^= bit

    child_index: List[Dict[Tuple[Any, Tuple[Any, ...]], List[int]]] = [
        {} for _ in range(n)
    ]
    for v in range(1, n):
        cpos = edge_child_pos[v]
        v_rows = tables[v]["rows"]
        index = child_index[v]
        for j in row_orders[v]:
            bits = global_masks[v][j]
            if not bits:
                continue
            interf = tuple(v_rows[j][p] for p in cpos)
            while bits:
                bit = bits & -bits
                qid = bit.bit_length() - 1
                index.setdefault((qid, interf), []).append(j)
                bits ^= bit

    # 5) 输出规模的回溯枚举。沿边接口取候选，绝不让不相容局部值相遇。
    found_rows: set[Tuple[Any, ...]] = set()
    values: List[Any] = [None] * len(directory)
    needed_recursion = max(1000, n * 4 + 100)
    if sys.getrecursionlimit() < needed_recursion:
        sys.setrecursionlimit(needed_recursion)

    def assign_row(u: int, i: int) -> None:
        row = tables[u]["rows"][i]
        for gp, val in zip(global_positions[u], row):
            values[gp] = val

    def walk(u: int, row_i: int, qid: int, continuation: Any) -> None:
        assign_row(u, row_i)
        kids = children[u]
        u_row = tables[u]["rows"][row_i]

        def continue_child(k: int) -> None:
            if k == len(kids):
                # 当前节点子树已完成；把控制权交还父节点继续下一个兄弟。
                # 只有根节点的最终 continuation 才落地一条完整行。
                continuation()
                return

            v = kids[k]
            ppos = edge_parent_pos[v]
            interf = tuple(u_row[p] for p in ppos)
            candidates = child_index[v].get((qid, interf), ())

            for j in candidates:
                def after_child(k_next: int = k + 1) -> None:
                    continue_child(k_next)

                walk(v, j, qid, after_child)

        continue_child(0)

    for qid, root_ids in root_by_request.items():
        for i in root_ids:
            walk(0, i, qid, lambda: found_rows.add(tuple(values)))

    sorted_rows = sorted(found_rows, key=row_sort_key)

    counts: Dict[Tuple[Any, ...], int] = {k: 0 for k in keys}
    for row in sorted_rows:
        key = tuple(row[p] for p in key_pos_in_directory)
        # 位掩码保证 key 必为某个请求键；防御性忽略理论上的 None。
        if key in counts:
            counts[key] += 1
    return sorted_rows, counts


# --------------------------------------------------------------------------- #
# 报告编排
# --------------------------------------------------------------------------- #

def build_key_report(delivery_path: Optional[str],
                     key_cols: Sequence[str],
                     directory: Sequence[str],
                     unique_keys: Sequence[Tuple[Any, ...]],
                     submitted_count: int,
                     duplicate_count: int,
                     rows: Sequence[Tuple[Any, ...]],
                     counts: Dict[Tuple[Any, ...], int],
                     notices: Sequence[str],
                     key_file_path: Optional[str] = None,
                     key_file_sha256: Optional[str] = None) -> Dict[str, Any]:
    sorted_keys = sorted(unique_keys, key=row_sort_key)

    entries: List[Dict[str, Any]] = []
    all_key_objects: List[Dict[str, Any]] = []
    hit_objects: List[Dict[str, Any]] = []
    missing_objects: List[Dict[str, Any]] = []
    missing_values: List[List[Any]] = []
    hit_count = 0

    for key in sorted_keys:
        n = int(counts.get(key, 0))
        hit = n > 0
        obj = key_object(key, key_cols)
        values = list(key)
        all_key_objects.append(obj)
        if hit:
            hit_count += 1
            hit_objects.append(obj)
        else:
            missing_objects.append(obj)
            missing_values.append(list(key))
        entries.append({
            "key": obj,
            "key_object": obj,
            "key_values": values,
            "values": values,
            "hit": hit,
            "is_hit": hit,
            "matched": hit,
            "found": hit,
            "exists": hit,
            "row_count": n,
            "rows_count": n,
            "n_rows": n,
            "number_of_rows": n,
            "returned_row_count": n,
            "returned_rows": n,
            "fact_row_count": n,
            "count": n,
        })

    entry_by_key = {key: entry for key, entry in zip(sorted_keys, entries)}
    request_order_entries = [
        entry_by_key[key] for key in unique_keys if key in entry_by_key
    ]

    total_rows = len(rows)
    missing_count = len(sorted_keys) - hit_count
    fact_doc = {
        "columns": list(directory),
        "rows": [list(r) for r in rows],
    }

    report: Dict[str, Any] = {
        "query_version": QUERY_VERSION,
        "report_version": QUERY_VERSION,
        "version": QUERY_VERSION,
        "status": "OK",
        "tool": "key_query",
        "key_columns": list(key_cols),
        "column_directory": list(directory),
        "requested_keys": all_key_objects,
        "all_keys": all_key_objects,

        # 最直接的计数契约。
        "requested_key_count": len(sorted_keys),
        "requested_keys_count": len(sorted_keys),
        "keys_requested": len(sorted_keys),
        "distinct_requested_key_count": len(sorted_keys),
        "unique_key_count": len(sorted_keys),
        "key_count": len(sorted_keys),
        "total_key_count": len(sorted_keys),
        "submitted_key_count": submitted_count,
        "raw_key_count": submitted_count,
        "duplicate_request_count": duplicate_count,
        "duplicate_key_count": duplicate_count,

        "total_row_count": total_rows,
        "total_rows": total_rows,
        "rows_total": total_rows,
        "row_total": total_rows,
        "rows_count": total_rows,
        "returned_row_count": total_rows,
        "returned_rows": total_rows,
        "fact_row_count": total_rows,
        "row_count": total_rows,

        "hit_key_count": hit_count,
        "matched_key_count": hit_count,
        "found_key_count": hit_count,
        "hit_keys": hit_objects,
        "matched_keys": hit_objects,
        "found_keys": hit_objects,
        "missing_key_count": missing_count,
        "unmatched_key_count": missing_count,
        "not_found_key_count": missing_count,
        "summary": {
            "requested_key_count": len(sorted_keys),
            "submitted_key_count": submitted_count,
            "duplicate_request_count": duplicate_count,
            "hit_key_count": hit_count,
            "missing_key_count": missing_count,
            "total_row_count": total_rows,
        },
        "counts": {
            "requested_keys": len(sorted_keys),
            "submitted_keys": submitted_count,
            "duplicate_requests": duplicate_count,
            "hit_keys": hit_count,
            "missing_keys": missing_count,
            "total_rows": total_rows,
        },

        "results": entries,
        "requests": request_order_entries,
        "request_order_results": request_order_entries,
        "key_results": entries,
        "key_reports": entries,
        "per_key_results": entries,
        "reports": entries,
        "per_key": entries,
        "keys": entries,
        "queries": entries,

        # 未命中键的两种常用表示。
        "missing": missing_objects,
        "unmatched": missing_objects,
        "missed": missing_objects,
        "missing_keys": missing_objects,
        "missing_key_objects": missing_objects,
        "unmatched_keys": missing_objects,
        "missed_keys": missing_objects,
        "not_found_keys": missing_objects,
        "missing_key_values": missing_values,
        "unmatched_key_values": missing_values,

        "outputs": {
            "fact_content_sha256": sha256_canonical(fact_doc),
            "fact_row_count": total_rows,
        },
        "warnings": list(notices),
    }

    inputs: Dict[str, Any] = {}
    if delivery_path:
        inputs["delivery"] = {"file_sha256": sha256_file(delivery_path)}
    if key_file_path and key_file_sha256:
        inputs["keys"] = {"file_sha256": key_file_sha256}
    if inputs:
        report["inputs"] = inputs
    return report


def query_batch(delivery_path: str,
                key_columns: Optional[Sequence[str]] = None,
                keys_path: Optional[str] = None,
                keys_data: Any = None,
                inline_keys: Optional[Sequence[Any]] = None
                ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """语义化别名：返回 (fact_document, report)。"""
    return run_key_query(
        delivery_path, key_columns=key_columns, keys_path=keys_path,
        keys_data=keys_data, inline_keys=inline_keys)


def query_keys(delivery_path: str,
               key_columns: Optional[Sequence[str]] = None,
               keys: Any = None,
               keys_path: Optional[str] = None
               ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """宽松 API 别名。keys 可传已解析批次，keys_path 可传批次文件。"""
    if isinstance(keys, str) and keys_path is None and os.path.exists(keys):
        keys_path = keys
        keys_data = None
    else:
        keys_data = keys
    return run_key_query(
        delivery_path, key_columns=key_columns,
        keys_path=keys_path, keys_data=keys_data)


def run_key_query(delivery_path: str,
                  key_columns: Optional[Sequence[str]] = None,
                  keys_path: Optional[str] = None,
                  keys_data: Any = None,
                  inline_keys: Optional[Sequence[Any]] = None
                  ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """纯编排 API：返回 (fact_document, report)。任何非法输入抛 QueryRejection。

    keys_data 可为已经读出的 list/dict；否则使用 keys_path。inline_keys
    可追加程序化/命令行给出的原始键项。
    """
    if isinstance(key_columns, str):
        key_columns = [key_columns]

    try:
        tables, tree_raw, reasons, notices = load_narrow_delivery(delivery_path)
    except InputError as exc:
        raise QueryRejection("delivery", exc.reasons)
    except OSError as exc:
        raise QueryRejection("delivery", [f"交付文件无法读取: {exc}"])

    if not tables:
        reasons = reasons + ["没有合法交付表"]
    adj: List[List[int]] = []
    directory: List[str] = []
    if tables:
        adj, tree_reasons, directory = validate_tree_and_types(tables, tree_raw)
        reasons = reasons + tree_reasons
    if reasons:
        raise QueryRejection("delivery", reasons)

    # 键列可能内嵌在键请求文件中，因此先读文件再最终规范化。
    raw_requests: List[Any] = []
    embedded_cols: Optional[List[str]] = None
    key_file_sha: Optional[str] = None
    actual_keys_path: Optional[str] = None

    if keys_path:
        raw_file, embedded_cols, key_file_sha = read_key_batch_file(keys_path)
        actual_keys_path = keys_path
        raw_requests.extend(raw_file)

    if keys_data is not None:
        raw_data, embedded_data_cols = extract_batch_raw(keys_data)
        raw_requests.extend(raw_data)
        if embedded_data_cols is not None:
            embedded_cols = embedded_data_cols

    if inline_keys:
        raw_requests.extend(inline_keys)

    key_cols = normalize_key_columns(
        list(key_columns or []), [],
        embedded_cols if not key_columns else None,
        known_columns=directory)
    validate_key_columns_in_directory(key_cols, directory)

    unique_keys, submitted_count, duplicate_count = parse_key_batch(
        raw_requests, key_cols)

    rows, counts = query_keys_in_memory(
        tables, adj, directory, key_cols, unique_keys)

    fact_doc = {
        "columns": list(directory),
        "rows": [list(r) for r in rows],
    }
    report = build_key_report(
        delivery_path, key_cols, directory, unique_keys,
        submitted_count, duplicate_count, rows, counts, notices,
        key_file_path=actual_keys_path, key_file_sha256=key_file_sha)
    return fact_doc, report


# --------------------------------------------------------------------------- #
# 成组原子写
# --------------------------------------------------------------------------- #

def atomic_write_outputs(files: Sequence[Tuple[str, str]]) -> None:
    """把 [(path, text)] 成组发布；失败时恢复旧文件，不留半成品。"""
    staged: List[Tuple[str, str]] = []
    backups: List[Tuple[str, Optional[str]]] = []
    placed: List[str] = []
    try:
        for path, text in files:
            directory = os.path.dirname(os.path.abspath(path)) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".key-query-tmp-", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            staged.append((path, tmp))

        for path, _ in files:
            if os.path.exists(path):
                path_dir = os.path.dirname(os.path.abspath(path)) or "."
                fd, bak = tempfile.mkstemp(prefix=".key-query-bak-", dir=path_dir)
                os.close(fd)
                try:
                    os.replace(path, bak)
                except BaseException:
                    try:
                        os.remove(bak)
                    except OSError:
                        pass
                    raise
                backups.append((path, bak))
            else:
                backups.append((path, None))

        for (path, tmp), (_, _bak) in zip(staged, backups):
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


def ensure_distinct_output_paths(paths: Sequence[str]) -> None:
    seen: Dict[str, str] = {}
    for label_path in paths:
        label, path = label_path.split("=", 1)
        real = os.path.realpath(os.path.abspath(path))
        if real in seen:
            raise QueryRejection("config", [
                f"输出 {label} 与输出 {seen[real]} 指向同一路径 {path!r}"])
        seen[real] = label


def ensure_outputs_not_inputs(outputs: Sequence[Tuple[str, str]],
                               input_paths: Sequence[Optional[str]]) -> None:
    input_reals = {
        os.path.realpath(os.path.abspath(p))
        for p in input_paths if p
    }
    for label, path in outputs:
        if os.path.realpath(os.path.abspath(path)) in input_reals:
            raise QueryRejection("config", [
                f"输出 {label} 路径 {path!r} 与输入文件重叠；"
                "为避免破坏输入，请指定不同输出路径"])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="key_query.py",
        description="从交付窄表包按一批主键导出完整事实行和逐键查询报告")
    p.add_argument("--delivery", "--delivery-file", "-d", dest="delivery",
                   help="delivery.json 路径")

    p.add_argument("--key-col", "--key-column", "--primary-key-col",
                   "--primary-key-column", "--pk-col",
                   dest="key_col", action="append", default=[],
                   help="主键列，可重复指定；顺序即复合键顺序")
    p.add_argument("--key-cols", "--key-columns", "--primary-key-cols",
                   "--primary-key-columns", "--pk-cols", "--pk-columns",
                   dest="key_cols_csv", action="append", default=[],
                   help="逗号分隔或 JSON 数组形式的多个主键列（兼容写法）")
    p.add_argument("--key", "--pk", "--primary-key", dest="key_tokens",
                   action="append", default=[],
                   help="若值是列目录中的列名则视为主键列，否则视为追加的键值")

    p.add_argument("--keys", "--keys-file", "--key-file", "--query-keys",
                   "--primary-keys", "--primary-key-file", "--pk-file",
                   "--batch-file", "--requests", "--request-file",
                   "--queries-file", "--query-file", "--input", "--input-file",
                   "--batch",
                   dest="keys", help="主键批次 JSON 文件")
    p.add_argument("--keys-json", "--keys-inline", dest="keys_json",
                   help="直接给出口径同 keys 文件的 JSON 文本")
    p.add_argument("--key-value", "--request-key", dest="inline_key_values",
                   action="append", default=[],
                   help="追加单个键值；复合键请传 JSON 数组/对象")

    p.add_argument("--out-dir", "--output-dir", dest="out_dir",
                   help=f"未分别指定输出时使用的目录（默认 {DEFAULT_OUT_DIR}）")
    p.add_argument("--fact-out", "--out-fact", "--fact-output", "--output-fact",
                   "--fact", "--fact-file", "--rows-out", "--rows-file",
                   "--rows", "--result", "--result-out", "--results-out",
                   "--result-file", "--output", "-o",
                   dest="fact_out", help="完整事实行 JSON 输出路径")
    p.add_argument("--report-out", "--out-report", "--report-output",
                   "--output-report", "--report", "--query-report",
                   "--report-file", "--report-json",
                   dest="report_out", help="查询报告 JSON 输出路径")
    p.add_argument("--out", dest="out_legacy",
                   help="未显式指定结果/报告时的兼容输出路径；"
                        "文件名含 report 时作为报告，否则作为事实行文件")
    p.add_argument("--report-stdout", action="store_true",
                   help="成功后把完整报告打印到 stdout")
    p.add_argument("--quiet", action="store_true",
                   help="不打印成功摘要")
    p.add_argument("positionals", nargs="*",
                   help="兼容位置参数：delivery.json keys.json fact.json report.json")
    return p


def _split_legacy_out(value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not value:
        return None, None
    base = os.path.basename(value).lower()
    if "report" in base:
        return None, value
    return value, None


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    argv_list = list(sys.argv[1:] if argv is None else argv)
    if argv_list and not argv_list[0].startswith("-"):
        first = argv_list[0]
        looks_like_command = (
            first in KNOWN_COMMANDS
            or (
                "." not in first
                and os.sep not in first
                and (os.altsep is None or os.altsep not in first)
                and not os.path.exists(first)
            )
        )
        if looks_like_command:
            argv_list = argv_list[1:]
    return build_parser().parse_args(argv_list)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    # 位置参数兼容：delivery keys fact-out report-out。
    pos = list(args.positionals or [])
    delivery = args.delivery
    keys_path = args.keys
    fact_out = args.fact_out
    report_out = args.report_out

    if pos and not delivery:
        delivery = pos.pop(0)
    if pos and not keys_path:
        keys_path = pos.pop(0)
    if pos and not fact_out:
        fact_out = pos.pop(0)
    if pos and not report_out:
        report_out = pos.pop(0)
    if pos:
        print(json.dumps({
            "query_version": QUERY_VERSION,
            "status": "REJECTED",
            "stage": "config",
            "reasons": [f"无法识别的额外位置参数: {pos}"],
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    # --keys 按项目惯例是文件路径；也兼容直接传 JSON 文本。
    keys_data: Any = None
    if keys_path and not os.path.exists(keys_path):
        stripped = keys_path.lstrip()
        if stripped[:1] in "[{":
            try:
                keys_data = json.loads(keys_path)
                keys_path = None
            except json.JSONDecodeError:
                # 保留为路径，后续按“文件不存在/JSON 解析失败”拒收。
                keys_data = None

    legacy_out = args.out_legacy
    out_dir = args.out_dir
    if not out_dir and legacy_out and (os.path.isdir(legacy_out)
                                       or legacy_out.endswith(("/", os.sep))):
        out_dir = legacy_out
        legacy_fact = legacy_report = None
    else:
        legacy_fact, legacy_report = _split_legacy_out(legacy_out)
    fact_out = fact_out or legacy_fact
    report_out = report_out or legacy_report

    out_dir = (out_dir
               or os.path.dirname(fact_out or "")
               or os.path.dirname(report_out or "")
               or DEFAULT_OUT_DIR)
    if not fact_out:
        fact_out = os.path.join(out_dir, DEFAULT_FACT_NAME)
    if not report_out:
        report_out = os.path.join(out_dir, DEFAULT_REPORT_NAME)

    if not delivery:
        print(json.dumps({
            "query_version": QUERY_VERSION,
            "status": "REJECTED",
            "stage": "config",
            "reasons": ["必须通过 --delivery 指定 delivery.json"],
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    input_paths: List[Optional[str]] = [delivery, keys_path]
    try:
        ensure_distinct_output_paths([f"fact={fact_out}", f"report={report_out}"])
        ensure_outputs_not_inputs(
            [("fact", fact_out), ("report", report_out)], input_paths)
    except QueryRejection as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2),
              file=sys.stderr)
        return 2

    # 先做一次交付包校验，以便智能识别 --key 列名/值并验证列目录。
    try:
        tables, tree_raw, reasons, notices = load_narrow_delivery(delivery)
        if not tables:
            reasons.append("没有合法交付表")
            adj, directory = [], []
        else:
            adj, tree_reasons, directory = validate_tree_and_types(tables, tree_raw)
            reasons = reasons + tree_reasons
        if reasons:
            raise QueryRejection("delivery", reasons)

        raw_key_col_args = list(args.key_col or []) + list(args.key_cols_csv or [])
        if raw_key_col_args:
            key_cols = normalize_key_columns(
                args.key_col, args.key_cols_csv, known_columns=directory)
        else:
            # 可能由 --keys 文件中的 key_columns 提供；run_key_query 会处理。
            key_cols = []

        key_columns_explicit = bool(raw_key_col_args)
        inline_raw: List[Any] = []
        for token in args.key_tokens or []:
            if key_columns_explicit:
                # 明确给过 --key-col 后，--key 按请求键值处理，
                # 即使字符串值恰好与某列同名也不能误判成列。
                if token and token[0] in "[{\"" or token in (
                        "null", "true", "false"):
                    inline_raw.append(parse_cli_scalar(token))
                elif "=" in token and not token.lstrip().startswith("-"):
                    name, raw_value = token.split("=", 1)
                    inline_raw.append(
                        {name.strip(): parse_cli_scalar(raw_value)})
                else:
                    inline_raw.append(parse_cli_scalar(token))
                continue

            col, value = parse_smart_key_token(token, directory)
            if col is not None:
                if col not in key_cols:
                    key_cols.append(col)
            else:
                inline_raw.append(value)
        for text in args.inline_key_values or []:
            # 允许传 @path？不必；这里按 JSON/标量解析。
            if text and text[0] in "[{\"" or text in ("null", "true", "false"):
                inline_raw.append(parse_cli_scalar(text))
            elif "=" in text and not text.lstrip().startswith("-"):
                name, raw_value = text.split("=", 1)
                inline_raw.append({name.strip(): parse_cli_scalar(raw_value)})
            else:
                inline_raw.append(parse_cli_scalar(text))

        validate_key_columns_in_directory(key_cols, directory)

        if args.keys_json:
            try:
                json_keys = json.loads(args.keys_json)
            except json.JSONDecodeError as exc:
                raise QueryRejection("config", [f"--keys-json 不是合法 JSON: {exc}"])
            if keys_data is None:
                keys_data = json_keys
            else:
                inline_raw.extend(extract_batch_raw(json_keys)[0])

        fact_doc, report = run_key_query(
            delivery, key_columns=key_cols, keys_path=keys_path,
            keys_data=keys_data, inline_keys=inline_raw)
    except QueryRejection as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2),
              file=sys.stderr)
        return 2
    except InputError as exc:
        print(json.dumps({
            "query_version": QUERY_VERSION,
            "status": "REJECTED",
            "stage": "input",
            "reasons": exc.reasons,
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except OSError as exc:
        print(json.dumps({
            "query_version": QUERY_VERSION,
            "status": "REJECTED",
            "stage": "input",
            "reasons": [f"输入文件无法读取: {exc}"],
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    fact_text = dump_json(fact_doc)
    # 用最终落盘字节的哈希覆盖内容哈希，保证报告可复核且确定。
    report["outputs"]["fact_file_sha256"] = hashlib.sha256(
        fact_text.encode("utf-8")).hexdigest()
    report_text = dump_json(report)

    try:
        atomic_write_outputs([(fact_out, fact_text), (report_out, report_text)])
    except OSError as exc:
        print(json.dumps({
            "query_version": QUERY_VERSION,
            "status": "REJECTED",
            "stage": "write",
            "reasons": [f"写出结果/报告失败，已回滚且未留下成功查询半成品: {exc}"],
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    if args.report_stdout:
        print(report_text, end="")
    elif not args.quiet:
        print(json.dumps({
            "status": "OK",
            "fact_out": fact_out,
            "report_out": report_out,
            "requested_key_count": report["requested_key_count"],
            "total_row_count": report["total_row_count"],
            "missing_key_count": report["missing_key_count"],
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
