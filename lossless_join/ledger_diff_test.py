#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""费用台账随机差分测试 + 大隐式连接压力。

每个随机实例：
  1) 朴素枚举完整自然连接（小实例，可枚举）作为基准；
  2) 以「单据、分组、分摊单元」投影计算基准权重、最大余数分摊与待分摊；
  3) 调用 fee_ledger ledger 入口逐项比对；
  4) 再跑一次确认输出确定。
压力：多个无共享列分支各 700 行，隐式组合 3.4 亿，断言耗时与输入同阶。
"""
import itertools
import json
import os
import random
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(ROOT, "tool", "fee_ledger.py")
TMP = os.path.join(ROOT, "cases", "ledger_random")
os.makedirs(TMP, exist_ok=True)

ID_COL, AMT_COL, UNIT_COL = "doc_id", "amt_c", "unit"
GCOLS = ["g1", "g2"]
# 每列固定同质非空类型 + 允许显式空值，避免产生协议禁止的同名列类型冲突。
COL_DOMAINS = {
    UNIT_COL: ["u0", "u1", "u2", "u3", None],
    "g1": [0, 1, 2, None],
    "g2": ["a", "b", "c", None],
    "x1": [10, 20, 30, None],
    "x2": ["p", "q", None],
}


def brute_join(tables):
    """笛卡尔 + 合并过滤的朴素自然连接，返回全列 dict 行集合。"""
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
    return {tuple(sorted(m.items())) for m in results}


def sort_key(v):
    if v is None:
        return (0,)
    if isinstance(v, int):
        return (1, v)
    return (2, v)


def gkey_key(g):
    return tuple(sort_key(v) for v in g)


def oracle_allocate(amount, weights):
    total_w = sum(weights.values())
    mag = abs(amount)
    keys = list(weights.keys())
    bases = {k: mag * w // total_w for k, w in weights.items()}
    rems = {k: mag * w % total_w for k, w in weights.items()}
    order = sorted(keys, key=lambda k: (-rems[k], gkey_key(k)))
    for k in order[:mag - sum(bases.values())]:
        bases[k] += 1
    if amount < 0:
        bases = {k: -v for k, v in bases.items()}
    return bases


def random_instance(rng):
    """RIP 安全的随机窄表实例；amt 只在单据表 T0。

    每个列的声明块都取一棵 *以 T0 为根* 的连通子树，天然满足同名列连成一块；
    金额列块固定为 {T0}。因此任何配置列都能从单据表沿树到达。
    """
    nt = rng.randint(2, 4)
    names = [f"T{i}" for i in range(nt)]
    parent = {names[i]: names[rng.randrange(i)] for i in range(1, nt)}
    edges = [[parent[n], n] for n in names[1:]]
    children = {n: [] for n in names}
    for n, p in parent.items():
        children[p].append(n)

    def grow_block(root):
        block = {root}
        frontier = [root]
        while frontier and rng.random() < 0.65:
            u = rng.choice(frontier)
            nbrs = [x for x in ([parent.get(u)] + children[u])
                    if x and x not in block]
            if not nbrs:
                frontier.remove(u)
                continue
            w = rng.choice(nbrs)
            block.add(w)
            frontier.append(w)
        return block

    cols = [ID_COL, AMT_COL, UNIT_COL, *GCOLS, "x1", "x2"]
    table_cols = {n: set() for n in names}
    table_cols["T0"].add(AMT_COL)  # 金额列只允许在单据表
    for c in cols:
        if c == AMT_COL:
            continue
        block = grow_block("T0")
        for n in block:
            table_cols[n].add(c)
    for n in names:
        if n != "T0" and not table_cols[n]:
            # 继承父表已有列，保证该列节点块仍连通（不破坏 RIP）
            table_cols[n].add(next(iter(table_cols[parent[n]])))

    ndoc = rng.randint(1, 5)
    doc_ids = [f"D{i}" for i in range(ndoc)]
    amounts = {d: rng.choice([-500, -7, -1, 0, 1, 7, 101, 1000, 333])
               for d in doc_ids}

    tables = []
    for n in names:
        tcols = [c for c in cols if c in table_cols[n]]
        rows = []
        if n == "T0":
            for d in doc_ids:
                row = []
                for c in tcols:
                    if c == ID_COL:
                        row.append(d)
                    elif c == AMT_COL:
                        row.append(amounts[d])
                    else:
                        row.append(rng.choice(COL_DOMAINS[c]))
                if rng.random() < 0.3:  # 完全重复行
                    rows.append(list(row))
                rows.append(row)
        else:
            for _ in range(rng.randint(1, 6)):
                row = []
                for c in tcols:
                    if c == ID_COL:
                        row.append(rng.choice(doc_ids + ["ghost"]))
                    elif c == AMT_COL:
                        row.append(rng.randint(-9, 9))  # 结构上不会发生（amt 只在 T0）
                    else:
                        row.append(rng.choice(COL_DOMAINS[c]))
                rows.append(row)
        # 去重保持集合语义
        seen = set()
        uniq = []
        for r in rows:
            t = tuple(r)
            if t not in seen:
                seen.add(t)
                uniq.append(r)
        tables.append({"name": n, "columns": tcols, "rows": uniq})
    return {"tables": tables, "tree": edges}, doc_ids, amounts


def main():
    rng = random.Random(20261125)
    n_trials = 150
    failures = []
    for trial in range(n_trials):
        delivery, doc_ids, amounts = random_instance(rng)
        d = os.path.join(TMP, f"inst_{trial}")
        os.makedirs(d, exist_ok=True)
        dp = os.path.join(d, "delivery.json")
        out = os.path.join(d, "ledger.json")
        with open(dp, "w", encoding="utf-8") as fh:
            json.dump(delivery, fh)

        # ---- 基准：完整连接 → (id, gkey, unit) 投影
        joined = brute_join(delivery["tables"])
        proj = {}  # id -> gkey -> {unit}
        for tup in joined:
            m = dict(tup)
            d_id = m[ID_COL]
            if d_id not in amounts:
                continue  # 非单据表引入的幽灵编号不产生台账归属
            gk = tuple(m[c] for c in GCOLS)
            proj.setdefault(d_id, {}).setdefault(gk, set()).add(m[UNIT_COL])

        exp_totals = {}
        exp_pending = []
        for d_id in doc_ids:
            per = proj.get(d_id, {})
            if not per:
                exp_pending.append({"doc_id": d_id, "amount_cents": amounts[d_id]})
                continue
            weights = {gk: len(us) for gk, us in per.items()}
            alloc = oracle_allocate(amounts[d_id], weights)
            for gk, part in alloc.items():
                exp_totals[gk] = exp_totals.get(gk, 0) + part
        exp_pending.sort(key=lambda x: sort_key(x["doc_id"]))
        exp_out = [{"group": list(gk), "amount_cents": exp_totals[gk]}
                   for gk in sorted(exp_totals, key=gkey_key)]
        exp_doc_total = sum(amounts.values())
        exp_alloc = sum(exp_totals.values())
        exp_pend = sum(p["amount_cents"] for p in exp_pending)

        cfg = ["--doc-table", "T0", "--doc-id-col", ID_COL,
               "--amount-col", AMT_COL, "--unit-col", UNIT_COL]
        for c in GCOLS:
            cfg += ["--group-col", c]
        p = subprocess.run([sys.executable, "-I", TOOL, "ledger",
                            "--delivery", dp, "--out", out, *cfg],
                           capture_output=True, text=True)
        if p.returncode != 0:
            failures.append((trial, "工具拒绝了结构合法实例", p.stderr, delivery))
            continue
        with open(out, encoding="utf-8") as fh:
            got = json.load(fh)
        if got["group_totals"] != exp_out:
            failures.append((trial, "分组汇总不符", got["group_totals"], exp_out))
            continue
        if got["pending"] != exp_pending:
            failures.append((trial, "待分摊不符", got["pending"], exp_pending))
            continue
        rec = got["reconciliation"]
        if not (rec["balanced"] and rec["distinct_doc_amount_total"] == exp_doc_total
                and rec["allocated_total"] == exp_alloc
                and rec["pending_total"] == exp_pend
                and rec["doc_count"] == len(doc_ids)):
            failures.append((trial, "轧账字段不符", rec))
            continue
        # 确定性：再跑一次
        p2 = subprocess.run([sys.executable, "-I", TOOL, "ledger",
                             "--delivery", dp, *cfg],
                            capture_output=True, text=True)
        if json.loads(p2.stdout)["group_totals"] != exp_out:
            failures.append((trial, "输出不确定", p2.stdout[:300]))

    print(f"随机差分: {n_trials - len(failures)}/{n_trials} 个实例与朴素全枚举一致")
    for f in failures[:5]:
        print("FAIL", json.dumps(f, ensure_ascii=False)[:900])

    # ---------------- 压力：3 个无共享列分支各 700 行 => 700^3 = 343,000,000 组合
    N = 700
    stress = {
        "tables": [
            {"name": "T0", "columns": [ID_COL, AMT_COL],
             "rows": [["D0", 12345]]},
            {"name": "X", "columns": ["q"], "rows": [[i] for i in range(N)]},
            {"name": "Y", "columns": ["r"], "rows": [[str(i)] for i in range(N)]},
            {"name": "Z", "columns": ["s"], "rows": [[i] for i in range(N)]},
            {"name": "B", "columns": [ID_COL, UNIT_COL, *GCOLS],
             "rows": [["D0", "u1", 0, "a"], ["D0", "u2", 0, "a"],
                      ["D0", "u3", 1, "b"]]},
        ],
        "tree": [["T0", "X"], ["T0", "Y"], ["T0", "Z"], ["T0", "B"]],
    }
    d = os.path.join(TMP, "stress")
    os.makedirs(d, exist_ok=True)
    dp = os.path.join(d, "delivery.json")
    out = os.path.join(d, "ledger.json")
    with open(dp, "w") as fh:
        json.dump(stress, fh)
    cfg = ["--doc-table", "T0", "--doc-id-col", ID_COL,
           "--amount-col", AMT_COL, "--unit-col", UNIT_COL,
           "--group-col", GCOLS[0], "--group-col", GCOLS[1]]
    t0 = time.time()
    p = subprocess.run([sys.executable, "-I", TOOL, "ledger",
                        "--delivery", dp, "--out", out, *cfg],
                       capture_output=True, text=True)
    dt = time.time() - t0
    with open(out) as fh:
        got = json.load(fh)
    # 权重 g(0,a)=2、g(1,b)=1，12345 => 8230/4115
    expect = [{"group": [0, "a"], "amount_cents": 8230},
              {"group": [1, "b"], "amount_cents": 4115}]
    ok = p.returncode == 0 and got["group_totals"] == expect and dt < 10
    print(f"压力测试 700^3={N ** 3:,}: {'PASS' if ok else 'FAIL'}，"
          f"耗时 {dt:.2f}s（输入 {1 + 3 * N + 3} 行）")
    if not ok:
        print(p.stderr or json.dumps(got, ensure_ascii=False)[:600])
        failures.append(("stress",))

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
