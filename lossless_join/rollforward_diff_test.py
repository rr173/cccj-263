#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""滚动发布入口随机差分测试。

对每个随机实例：
  1) 用朴素方法直接计算「本周事实集合」与「每表投影去重集合」作为基准；
  2) 调用滚动发布入口，逐表比对交付行集合与事实行集合；
  3) 核对报告的操作数、受影响键、各表增/删去重行数；
  4) 校验新包能被现有 lossless_accept 入口按预期复核；
  5) 重复运行确认输出逐字节一致。
"""
import json
import os
import random
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(ROOT, "tool", "delivery_rollforward.py")
ACCEPT = os.path.join(ROOT, "tool", "lossless_accept.py")
TMP = os.path.join(ROOT, "cases", "roll_random")
os.makedirs(TMP, exist_ok=True)


def brute_apply(fact_cols, fact_rows, delivery, key_cols, ops):
    """朴素基准：事实字典按业务键应用变更，再逐表取事实投影集合。"""
    kidx = [fact_cols.index(c) for c in key_cols]
    fmap = {tuple(r[i] for i in kidx): tuple(r) for r in fact_rows}
    for op in ops:
        row = tuple(op["row"]) if "row" in op else None
        if "key" in op:
            spec = op["key"]
            key = (tuple(spec[c] for c in key_cols) if isinstance(spec, dict)
                   else tuple(spec))
        else:
            key = tuple(row[i] for i in kidx)
        if op["op"] == "insert":
            assert key not in fmap
            fmap[key] = row
        elif op["op"] == "update":
            assert key in fmap
            fmap[key] = row
        else:
            assert key in fmap
            del fmap[key]
    new_rows = list(fmap.values())
    new_tables = []
    fidx = {c: i for i, c in enumerate(fact_cols)}
    for t in delivery["tables"]:
        proj = sorted({tuple(r[fidx[c]] for c in t["columns"]) for r in new_rows},
                      key=lambda x: repr(x))
        new_tables.append({"name": t["name"], "columns": t["columns"], "rows": proj})
    return set(new_rows), new_tables


def random_instance(rng):
    ncol = rng.randint(2, 4)
    cols = [f"c{i}" for i in range(ncol)]
    # 业务键取一列，值域非空且互不相同，保证事实行键唯一
    key_col = cols[rng.randrange(ncol)]
    key_idx = cols.index(key_col)
    domains = {}
    for j, c in enumerate(cols):
        if j == key_idx:
            domains[c] = list(range(20))
        elif rng.random() < 0.5:
            domains[c] = [0, 1, 2, None]        # 整数列（可含显式空值）
        else:
            domains[c] = ["a", "b", "s", None]  # 字符串列（可含显式空值）
    # 业务键列若被随机选成字符串域，替换为整数键域
    domains[key_col] = list(range(20))
    nfact = rng.randint(1, 8)
    used = set()
    rows = []
    while len(rows) < nfact:
        r = [rng.choice(domains[c]) for c in cols]
        if r[key_idx] in used:
            continue
        used.add(r[key_idx])
        rows.append(r)

    # 随机树 + 连通列块（同 online 验收随机测试），保证 RIP 与列覆盖
    nt = rng.randint(1, 4)
    names = [f"T{i}" for i in range(nt)]
    parent = {names[i]: names[rng.randrange(i)] for i in range(1, nt)}
    edges = [[parent[n], n] for n in names[1:]]
    owner = {c: rng.choice(names) for c in cols}
    table_cols = {n: set() for n in names}
    for c, root in owner.items():
        block = {root}
        frontier = [root]
        while frontier and rng.random() < 0.6:
            u = rng.choice(frontier)
            nbrs = [x for x in ([parent.get(u)] + [k for k, v in parent.items() if v == u])
                    if x and x not in block]
            if not nbrs:
                frontier.remove(u)
                continue
            w = rng.choice(nbrs)
            block.add(w)
            frontier.append(w)
        for n in block:
            table_cols[n].add(c)
    # 保证每张表至少声明一列，且不破坏 RIP：把最近的非空节点的某一列沿树上
    # 路径补到空节点及其间所有节点（沿路径扩充同列节点块，连通性保持）。
    adj = {n: set() for n in names}
    for a, b in edges:
        adj[a].add(b)
        adj[b].add(a)
    for n in names:
        if table_cols[n]:
            continue
        # BFS 找最近非空节点
        prev = {n: None}
        queue = [n]
        target = None
        while queue and target is None:
            nxt = []
            for u in queue:
                if table_cols[u] and u != n:
                    target = u
                    break
                for v in adj[u]:
                    if v not in prev:
                        prev[v] = u
                        nxt.append(v)
            queue = nxt
        if target is None:
            continue
        path = []
        cur = target
        while cur is not None:
            path.append(cur)
            cur = prev[cur]
        chosen = sorted(table_cols[target])[0]
        for u in path:
            table_cols[u].add(chosen)

    fidx = {c: i for i, c in enumerate(cols)}
    tables = []
    for n in names:
        tcols = [c for c in cols if c in table_cols[n]]
        proj = sorted({tuple(r[fidx[c]] for c in tcols) for r in rows},
                      key=lambda x: tuple((0, "") if v is None else (1, type(v).__name__) + (0,) if isinstance(v, int) else (2, v) for v in x))
        tables.append({"name": n, "columns": tcols, "rows": [list(p) for p in proj]})
    fact = {"columns": cols, "rows": rows}
    # 基线必须是「无损投影」：若随机树把同一业务键的多值列留在 T1 侧导致投影
    # 回拼造行，则退化为单表交付（单表投影必然无损），以便随机实例集中检验
    # 变更应用与投影增量逻辑，而不是集中在预检拒收路径。
    delivery = {"tables": tables, "tree": edges}
    if not projection_is_lossless(fact, delivery):
        delivery = {"tables": [{"name": "ONLY", "columns": list(cols),
                                "rows": [list(r) for r in rows]}], "tree": []}

    # 变更：对随机子集做 update/delete，再插入若干新键
    ops = []
    existing = list(rows)
    rng.shuffle(existing)
    n_mod = rng.randint(0, len(existing))
    touched = set()
    for r in existing[:n_mod]:
        key_val = r[key_idx]
        if key_val in touched:
            continue
        touched.add(key_val)
        if rng.random() < 0.5:
            new_row = list(r)
            for j in range(ncol):
                if j != key_idx:
                    new_row[j] = rng.choice(domains[cols[j]])
            ops.append({"op": "update", "key": {key_col: key_val}, "row": new_row})
        else:
            ops.append({"op": "delete", "key": {key_col: key_val}})
    next_key = max(used) + 1
    for _ in range(rng.randint(0, 3)):
        new_row = [rng.choice(domains[c]) for c in cols]
        new_row[key_idx] = next_key
        next_key += 1
        ops.append({"op": "insert", "row": new_row})
    rng.shuffle(ops)
    return fact, delivery, [key_col], ops


def brute_join(tables):
    """小实例朴素自然连接（RIP 下与树连接等价），返回全列行元组集合。"""
    all_cols = []
    for t in tables:
        for c in t["columns"]:
            if c not in all_cols:
                all_cols.append(c)
    results = [{}]
    for t in tables:
        nxt = []
        for acc in results:
            for row in t["rows"]:
                m = dict(acc)
                ok = True
                for c, v in zip(t["columns"], row):
                    if c in m and m[c] != v:
                        ok = False
                        break
                    m[c] = v
                if ok:
                    nxt.append(m)
        results = nxt
    return all_cols, {tuple(m.get(c) for c in all_cols) for m in results}


def projection_is_lossless(fact, delivery):
    cols, joined = brute_join(delivery["tables"])
    ordered = {tuple(r[cols.index(c)] for c in fact["columns"]) for r in joined}
    return ordered == {tuple(r) for r in fact["rows"]}


def main():
    rng = random.Random(20261125)
    n_trials = 150
    failures = []
    for trial in range(n_trials):
        fact, delivery, key_cols, ops = random_instance(rng)
        d = os.path.join(TMP, f"inst_{trial}")
        os.makedirs(d, exist_ok=True)
        fp = os.path.join(d, "fact.json")
        dp = os.path.join(d, "delivery.json")
        cp = os.path.join(d, "changes.json")
        with open(fp, "w", encoding="utf-8") as fh:
            json.dump(fact, fh)
        with open(dp, "w", encoding="utf-8") as fh:
            json.dump(delivery, fh)
        with open(cp, "w", encoding="utf-8") as fh:
            json.dump({"changes": ops}, fh)
        out = os.path.join(d, "out")
        cmd = [sys.executable, "-I", TOOL, "apply", "--fact", fp,
               "--delivery", dp, "--changes", cp, "--out-dir", out]
        for k in key_cols:
            cmd += ["--key-col", k]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            failures.append((trial, "入口拒绝", p.stderr[:600], fact, delivery, ops))
            continue

        want_rows, want_tables = brute_apply(
            fact["columns"], [tuple(r) for r in fact["rows"]],
            delivery, key_cols, ops)
        with open(os.path.join(out, "delivery.json"), encoding="utf-8") as fh:
            got_del = json.load(fh)
        with open(os.path.join(out, "fact.json"), encoding="utf-8") as fh:
            got_fact = json.load(fh)
        with open(os.path.join(out, "change_report.json"), encoding="utf-8") as fh:
            rep = json.load(fh)

        got_rows = {tuple(r) for r in got_fact["rows"]}
        if got_rows != want_rows:
            failures.append((trial, "事实集合不符",
                             (sorted(got_rows, key=str), sorted(want_rows, key=str))))
            continue
        got_tables = {t["name"]: {tuple(r) for r in t["rows"]}
                      for t in got_del["tables"]}
        want_map = {t["name"]: {tuple(r) for r in t["rows"]} for t in want_tables}
        if got_tables != want_map:
            failures.append((trial, "交付投影集合不符", (got_tables, want_map)))
            continue
        # 报告一致性
        if rep["changes"]["accepted_operation_count"] != len(ops):
            failures.append((trial, "接受操作数不符", (rep["changes"], len(ops))))
            continue
        if rep["changes"]["affected_business_key_count"] != len(ops):
            failures.append((trial, "受影响键数不符（每键至多一操作）", rep["changes"]))
            continue
        want_delta = {}
        for t in delivery["tables"]:
            before = {tuple(r) for r in t["rows"]}
            after = want_map[t["name"]]
            want_delta[t["name"]] = (len(after - before), len(before - after))
        got_delta = {x["name"]: (x["added_distinct_row_count"],
                                 x["removed_distinct_row_count"]) for x in rep["tables"]}
        if got_delta != want_delta:
            failures.append((trial, "增删去重行数不符", (got_delta, want_delta)))
            continue
        # 新包可被现有验收入口复核
        p2 = subprocess.run([sys.executable, "-I", ACCEPT, "accept",
                             "--fact", os.path.join(out, "fact.json"),
                             "--delivery", os.path.join(out, "delivery.json")],
                            capture_output=True, text=True)
        try:
            acc = json.loads(p2.stdout or p2.stderr)
        except json.JSONDecodeError:
            failures.append((trial, "验收入口输出无法解析", p2.stdout + p2.stderr))
            continue
        v = acc.get("verdict", {})
        if v.get("fact_row_count") != len(want_rows):
            failures.append((trial, "验收入口事实行数不符", v))
            continue
        if v.get("missing_row_count", 1) != 0:
            failures.append((trial, "新包出现缺行（不允许）", v))
            continue
        # 独立核对：用朴素自然连接重算新包的整行集合与各精确计数，
        # 必须与（a）本周事实集合的交集/缺行、（b）工具报告的后检完全一致。
        # 注意：投影包按协议生成，若连接树本身使投影不可回拼，允许存在造行，
        # 但工具必须如实报告，不得私自增删交付行。
        base_cols, base_join = brute_join(got_del["tables"])
        ordered = {tuple(r[base_cols.index(c)] for c in fact["columns"])
                   for r in base_join}
        want_inter = len(ordered & want_rows)
        want_missing = len(want_rows) - want_inter
        want_spurious = len(ordered) - want_inter
        if want_missing != 0:
            failures.append((trial, "投影包出现缺行（不允许）", want_missing))
            continue
        if (v.get("missing_row_count"), v.get("spurious_row_count"),
                v.get("intersection_row_count")) != (want_missing, want_spurious, want_inter):
            failures.append((trial, "验收入口计数与朴素基准不一致",
                             (v, want_inter, want_missing, want_spurious)))
            continue
        pc = rep["postcheck"]
        if (pc["missing_row_count"], pc["spurious_row_count"],
                pc["intersection_row_count"], pc["sets_equal"]) != (
                want_missing, want_spurious, want_inter, want_spurious == 0):
            failures.append((trial, "报告后检与朴素基准不一致", (pc, want_spurious)))
            continue
        if want_spurious == 0 and not (v.get("sets_equal") is True and not rep["warnings"]):
            failures.append((trial, "无损情形验收结论应 ACCEPTED 且无警告", (v, rep["warnings"])))
            continue
        # 重复运行字节一致
        out2 = os.path.join(d, "out2")
        cmd2 = [c if c != out else out2 for c in cmd]
        subprocess.run(cmd2, capture_output=True, text=True)
        for fn in ("delivery.json", "fact.json", "change_report.json"):
            with open(os.path.join(out, fn), "rb") as fh:
                a = fh.read()
            with open(os.path.join(out2, fn), "rb") as fh:
                b = fh.read()
            if a != b:
                failures.append((trial, f"{fn} 重复运行不一致", None))
                break

    print(f"滚动发布随机差分: {n_trials - len(failures)}/{n_trials} 与朴素基准一致")
    for f in failures[:5]:
        print("FAIL", json.dumps(f, ensure_ascii=False, default=str)[:800])
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
