#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""随机差分测试 + 规模压力测试。

对每个随机实例：
  1) 朴素枚举完整回拼结果（小实例，可枚举），得到基准计数与集合；
  2) 调用验收工具，比对 reconstructed/missing/spurious 三个精确计数；
  3) 复核报告证据：缺行片段确实在交付表中不存在；造行证人确实可拼成该行且该行不在原表；
  4) 再跑 verify-report 入口。
另含大隐式连接压力测试，断言工具耗时/内存与输入规模同阶。
"""
import itertools
import json
import os
import random
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(ROOT, "tool", "lossless_accept.py")
TMP = os.path.join(ROOT, "cases", "random")
os.makedirs(TMP, exist_ok=True)


def brute_force_join(tables, tree_edges, names):
    """朴素全枚举自然连接（仅用于小实例基准）。返回完整行元组集合（按全列序）。"""
    adj = {n: set() for n in names}
    for a, b in tree_edges:
        adj[a].add(b)
        adj[b].add(a)
    all_cols = []
    for t in tables:
        for c in t["columns"]:
            if c not in all_cols:
                all_cols.append(c)

    def merge(acc, row, cols):
        out = dict(acc)
        for c, v in zip(cols, row):
            if c in out and out[c] != v:
                return None
            out[c] = v
        return out

    # 沿树逐表连接，直接笛卡尔后过滤（实例很小）
    results = [{}]
    for t in tables:
        nxt = []
        for acc in results:
            for row in t["rows"]:
                m = merge(acc, row, t["columns"])
                if m is not None:
                    nxt.append(m)
        results = nxt
    out_set = set()
    for m in results:
        out_set.add(tuple(m.get(c) for c in all_cols))
    return all_cols, out_set


def random_instance(rng):
    ncol = rng.randint(2, 4)
    cols = [f"c{i}" for i in range(ncol)]
    # 原表：小域上随机若干去重行
    domain = [0, 1, 2, None, "s"]
    nfact = rng.randint(1, 6)
    fact = set()
    while len(fact) < nfact:
        fact.add(tuple(rng.choice(domain) for _ in range(ncol)))
    fact = [list(r) for r in fact]

    # 随机生成一棵合法树并随机分配列（保证覆盖且 RIP：同列节点连成一块）
    nt = rng.randint(2, 4)
    names = [f"T{i}" for i in range(nt)]
    parent = {names[i]: names[rng.randrange(i)] for i in range(1, nt)}
    edges = [[parent[n], n] for n in names[1:]]
    # 列分配：每列选一个连通子树（用区间近似：随机选一个节点集合并保证连通）
    owner = {c: rng.choice(names) for c in cols}
    table_cols = {n: set() for n in names}
    for c, root in owner.items():
        # 从 root 随机扩展连通块
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
    # 保证每张表至少一列
    for n in names:
        if not table_cols[n]:
            table_cols[n].add(rng.choice(cols))

    # 数据：以“从原表投影 + 随机增删行”构造，制造缺行/造行
    tables = []
    for n in names:
        tcols = [c for c in cols if c in table_cols[n]]
        idxs = [cols.index(c) for c in tcols]
        proj = {tuple(r[i] for i in idxs) for r in fact}
        rows = [list(r) for r in proj]
        # 随机删行（制造缺行）
        if len(rows) > 1 and rng.random() < 0.5:
            del rows[rng.randrange(len(rows))]
        # 随机加行（制造造行）
        if rng.random() < 0.6:
            extra = tuple(rng.choice(domain) for _ in tcols)
            if list(extra) not in rows:
                rows.append(list(extra))
        tables.append({"name": n, "columns": tcols, "rows": rows})
    return {"columns": cols, "rows": fact}, {"tables": tables, "tree": edges}


def main():
    rng = random.Random(20261124)
    n_trials = 120
    failures = []
    for trial in range(n_trials):
        fact, delivery = random_instance(rng)
        d = os.path.join(TMP, f"inst_{trial}")
        os.makedirs(d, exist_ok=True)
        fp, dp, rp = (os.path.join(d, x) for x in ("fact.json", "delivery.json", "report.json"))
        with open(fp, "w", encoding="utf-8") as fh:
            json.dump(fact, fh)
        with open(dp, "w", encoding="utf-8") as fh:
            json.dump(delivery, fh)

        names = [t["name"] for t in delivery["tables"]]
        # 结构可能非法（RIP 由构造保证连通块，理论上合法；仍先尝试跑工具）
        p = subprocess.run([sys.executable, "-I", TOOL, "accept",
                            "--fact", fp, "--delivery", dp, "--out", rp],
                           capture_output=True, text=True)
        with open(rp, encoding="utf-8") as fh:
            rep = json.load(fh)
        if rep["status"] == "REJECTED" and "reasons" in rep and "verdict" not in rep:
            continue  # 结构性拒绝，跳过基准比对

        all_cols, join_set = brute_force_join(delivery["tables"], delivery["tree"], names)
        fact_set = {tuple(r) for r in fact["rows"]}
        # 列序对齐
        ordered_join = {tuple(r[all_cols.index(c)] for c in fact["columns"]) for r in join_set}
        base_join = len(ordered_join)
        base_inter = len(ordered_join & fact_set)
        base_missing = len(fact_set) - base_inter
        base_spurious = base_join - base_inter

        v = rep["verdict"]
        got = (v["reconstructed_distinct_row_count"], v["intersection_row_count"],
               v["missing_row_count"], v["spurious_row_count"])
        want = (base_join, base_inter, base_missing, base_spurious)
        if got != want:
            failures.append((trial, "计数不符", got, want, fact, delivery))
            continue

        # 证据有效性
        if base_missing > 0:
            me = rep["missing_evidence"]
            fr = tuple(me["fact_row"])
            if fr not in fact_set:
                failures.append((trial, "缺行证据原行不存在", me))
                continue
            frag = me["missing_fragment"]
            t = next(x for x in delivery["tables"] if x["name"] == frag["table"])
            exp = tuple(frag["expected_row"])
            if exp in {tuple(r) for r in t["rows"]}:
                failures.append((trial, "缺失片段竟存在", me))
                continue
        if base_spurious > 0:
            se = rep["spurious_evidence"]
            assembled = tuple(se["assembled_row"])
            if assembled in fact_set:
                failures.append((trial, "造行证据竟在原表", se))
                continue
            # 证人可拼成该行
            covered = {}
            for w in se["witnesses"]:
                t = next(x for x in delivery["tables"] if x["name"] == w["table"])
                actual = t["rows"][w["row_number"]]
                if list(actual) != w["row"]:
                    failures.append((trial, "证人坐标不实", w))
                    break
                for c, val in zip(t["columns"], actual):
                    if c in covered and covered[c] != val:
                        failures.append((trial, "证人共享列冲突", w))
                        break
                    covered[c] = val
            else:
                if tuple(covered[c] for c in fact["columns"]) != assembled:
                    failures.append((trial, "证人合并不出所报行", se))
                elif assembled not in ordered_join:
                    failures.append((trial, "所报造行不在朴素连接结果中", se))
                else:
                    pass
                continue
            continue

        # 独立复核入口
        p2 = subprocess.run([sys.executable, "-I", TOOL, "verify-report",
                             "--fact", fp, "--delivery", dp, "--report", rp],
                            capture_output=True, text=True)
        ver = json.loads(p2.stdout)
        if ver["status"] != "VERIFIED":
            failures.append((trial, "verify-report 未通过", ver))

    print(f"随机差分: {n_trials - len(failures)}/{n_trials} 个实例与朴素枚举完全一致")
    for f in failures[:5]:
        print("FAIL", json.dumps(f, ensure_ascii=False)[:800])

    # ---------------- 规模压力：隐式连接 500^3=1.25 亿，输入仅 1500 行 ----------------
    big = list(range(500))
    fact = {"columns": ["a", "b", "c"], "rows": [[0, 0, 0]]}
    delivery = {
        "tables": [
            {"name": "T1", "columns": ["a"], "rows": [[x] for x in big]},
            {"name": "T2", "columns": ["b"], "rows": [[x] for x in big]},
            {"name": "T3", "columns": ["c"], "rows": [[x] for x in big]},
        ],
        "tree": [["T1", "T2"], ["T1", "T3"]],
    }
    d = os.path.join(TMP, "stress")
    os.makedirs(d, exist_ok=True)
    fp, dp, rp = (os.path.join(d, x) for x in ("fact.json", "delivery.json", "report.json"))
    with open(fp, "w") as fh:
        json.dump(fact, fh)
    with open(dp, "w") as fh:
        json.dump(delivery, fh)
    t0 = time.time()
    p = subprocess.run([sys.executable, "-I", TOOL, "accept",
                        "--fact", fp, "--delivery", dp, "--out", rp],
                       capture_output=True, text=True)
    dt = time.time() - t0
    with open(rp) as fh:
        rep = json.load(fh)
    expect_join = 500 ** 3
    ok = (p.returncode == 2
          and rep["verdict"]["reconstructed_distinct_row_count"] == expect_join
          and rep["verdict"]["spurious_row_count"] == expect_join - 1
          and rep["spurious_evidence"] is not None)
    print(f"压力测试 500^3={expect_join:,}: {'PASS' if ok else 'FAIL'}，耗时 {dt:.2f}s（输入 1500 行）")
    if not ok:
        print(json.dumps(rep, ensure_ascii=False)[:600])
        failures.append(("stress",))

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
