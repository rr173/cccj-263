#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""费用台账入口端到端测试（手算用例 + 非法拒绝 + 无膨胀压力）。

全部用 python3 -I 运行；测试数据写在独立 cases/ledger_* 目录，脚本本身位于项目根。
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(ROOT, "tool", "fee_ledger.py")
CASES = os.path.join(ROOT, "cases")
results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), name, detail)


def j(obj):
    """tuple 键安全的展示（tuple 键转字符串）。"""
    try:
        return json.dumps(obj, ensure_ascii=False)
    except TypeError:
        return str(obj)


def write_case(name, delivery):
    d = os.path.join(CASES, name)
    os.makedirs(d, exist_ok=True)
    dp = os.path.join(d, "delivery.json")
    out = os.path.join(d, "ledger.json")
    with open(dp, "w", encoding="utf-8") as fh:
        json.dump(delivery, fh, ensure_ascii=False)
    return dp, out


def run_ledger(dp, out, *cfg):
    cmd = [sys.executable, "-I", TOOL, "ledger",
           "--delivery", dp, "--out", out, *cfg]
    p = subprocess.run(cmd, capture_output=True, text=True)
    parsed = None
    if os.path.exists(out):
        with open(out, encoding="utf-8") as fh:
            try:
                parsed = json.load(fh)
            except json.JSONDecodeError:
                parsed = None
    if parsed is None and p.stderr.strip():
        try:
            parsed = json.loads(p.stderr)
        except json.JSONDecodeError:
            pass
    return p.returncode, parsed, p.stdout, p.stderr


CFG = ["--doc-table", "DOC", "--doc-id-col", "id", "--amount-col", "amt",
       "--unit-col", "unit", "--group-col", "bu", "--group-col", "team"]

# ----------------------------------------------------------------- L01 主场景：
# D1=1000 分；bu1/team1 单元 {u1,u2} 权重2，bu2/team2 单元 {u3} 权重1
del_main = {
    "tables": [
        {"name": "DOC", "columns": ["id", "amt"],
         "rows": [["D1", 1000]]},
        {"name": "B", "columns": ["id", "bu", "unit"],
         "rows": [["D1", "bu1", "u1"], ["D1", "bu1", "u2"], ["D1", "bu2", "u3"]]},
        {"name": "T", "columns": ["bu", "team"],
         "rows": [["bu1", "team1"], ["bu2", "team2"]]},
    ],
    "tree": [["DOC", "B"], ["B", "T"]],
}
dp, out = write_case("ledger_01_main", del_main)
rc, led, _, err = run_ledger(dp, out, *CFG)
check("L01 退出码0", rc == 0, err)
totals = {tuple(x["group"]): x["amount_cents"] for x in led["group_totals"]}
check("L01 667/333 分摊", totals == {("bu1", "team1"): 667, ("bu2", "team2"): 333},
      j(totals))
check("L01 无待分摊", led["pending"] == [])
check("L01 轧账平衡 1000", led["reconciliation"] == {
    "doc_count": 1, "distinct_doc_amount_total": 1000,
    "allocated_total": 1000, "pending_total": 0, "balanced": True})
check("L01 列目录为并集", led["column_directory"] == ["id", "amt", "bu", "unit", "team"])

# 同一业务单元多条明细不得多分：B 中 (D1,bu1,u1) 出现多次 + 完全重复单据行
del_dupe = {
    "tables": [
        {"name": "DOC", "columns": ["id", "amt"],
         "rows": [["D1", 1000], ["D1", 1000]]},  # 完全重复单据行只算一次
        {"name": "B", "columns": ["id", "bu", "unit"],
         "rows": [["D1", "bu1", "u1"], ["D1", "bu1", "u1"],
                  ["D1", "bu1", "u2"], ["D1", "bu2", "u3"], ["D1", "bu2", "u3"]]},
        {"name": "T", "columns": ["bu", "team"],
         "rows": [["bu1", "team1"], ["bu2", "team2"]]},
    ],
    "tree": [["DOC", "B"], ["B", "T"]],
}
dp, out = write_case("ledger_02_dupes", del_dupe)
rc, led, _, err = run_ledger(dp, out, *CFG)
check("L02 重复明细/重复单据行不改变结果", rc == 0 and
      {tuple(x["group"]): x["amount_cents"] for x in led["group_totals"]} ==
      {("bu1", "team1"): 667, ("bu2", "team2"): 333}, err)
check("L02 去重通知存在", any("重复行" in w for w in led["warnings"]))
check("L02 单据计数为1", led["reconciliation"]["doc_count"] == 1)

# 同编号不同单据行 => 拒收（金额不同）
del_conflict = {
    "tables": [
        {"name": "DOC", "columns": ["id", "amt"], "rows": [["D1", 1000], ["D1", 900]]},
        {"name": "B", "columns": ["id", "bu", "unit"], "rows": [["D1", "bu1", "u1"]]},
        {"name": "T", "columns": ["bu", "team"], "rows": [["bu1", "team1"]]},
    ],
    "tree": [["DOC", "B"], ["B", "T"]],
}
dp, out = write_case("ledger_03_conflict", del_conflict)
rc, led, _, _ = run_ledger(dp, out, *CFG)
check("L03 同编号不同单据行拒收", rc == 2 and led["status"] == "REJECTED"
      and any("同一单据编号" in r for r in led["reasons"]))
check("L03 拒收不写台账文件", not os.path.exists(out))

# ----------------------------------------------------------------- L04 最大余数平手：
# 三组各权重1，金额 101 => 33/34/34；平手按分组列顺序，null 最前，字符串按码点
del_tie = {
    "tables": [
        {"name": "DOC", "columns": ["id", "amt"], "rows": [["D1", 101]]},
        {"name": "B", "columns": ["id", "bu", "unit"],
         "rows": [["D1", "b", "u1"], ["D1", "a", "u2"], ["D1", None, "u3"]]},
        {"name": "T", "columns": ["bu", "team"],
         "rows": [["b", "t"], ["a", "t"], [None, "t"]]},
    ],
    "tree": [["DOC", "B"], ["B", "T"]],
}
dp, out = write_case("ledger_04_tie", del_tie)
rc, led, _, err = run_ledger(dp, out, *CFG)
order = [tuple(x["group"]) for x in led["group_totals"]]
amts = [x["amount_cents"] for x in led["group_totals"]]
check("L04 平手顺序 null < 'a' < 'b'", rc == 0 and
      order == [(None, "t"), ("a", "t"), ("b", "t")], json.dumps(order, ensure_ascii=False))
check("L04 余数补在前两组 34/34/33", amts == [34, 34, 33], str(amts))

# ----------------------------------------------------------------- L05 负金额：
del_neg = {
    "tables": [
        {"name": "DOC", "columns": ["id", "amt"], "rows": [["D1", -1000]]},
        {"name": "B", "columns": ["id", "bu", "unit"],
         "rows": [["D1", "bu1", "u1"], ["D1", "bu1", "u2"], ["D1", "bu2", "u3"]]},
        {"name": "T", "columns": ["bu", "team"],
         "rows": [["bu1", "team1"], ["bu2", "team2"]]},
    ],
    "tree": [["DOC", "B"], ["B", "T"]],
}
dp, out = write_case("ledger_05_negative", del_neg)
rc, led, _, err = run_ledger(dp, out, *CFG)
totals = {tuple(x["group"]): x["amount_cents"] for x in led["group_totals"]}
check("L05 负金额对同样结果取负 -667/-333", rc == 0 and
      totals == {("bu1", "team1"): -667, ("bu2", "team2"): -333}, err)
check("L05 轧账 -1000", led["reconciliation"]["allocated_total"] == -1000
      and led["reconciliation"]["balanced"])

# ----------------------------------------------------------------- L06 待分摊：
# D2 在 B 中完全没有相容行 => 整笔待分摊，不猜分组；同组正负相抵保留零合计组
del_pending = {
    "tables": [
        {"name": "DOC", "columns": ["id", "amt"],
         "rows": [["D1", 100], ["D2", 250], ["D3", -100]]},
        {"name": "B", "columns": ["id", "bu", "unit"],
         "rows": [["D1", "g", "u1"], ["D3", "g", "u2"]]},
        {"name": "T", "columns": ["bu", "team"], "rows": [["g", "tm"]]},
    ],
    "tree": [["DOC", "B"], ["B", "T"]],
}
dp, out = write_case("ledger_06_pending", del_pending)
rc, led, _, err = run_ledger(dp, out, *CFG)
check("L06 退出码0", rc == 0, err)
totals = {tuple(x["group"]): x["amount_cents"] for x in led["group_totals"]}
check("L06 零合计组保留", totals == {("g", "tm"): 0}, j(totals))
check("L06 D2 整笔待分摊", led["pending"] == [{"doc_id": "D2", "amount_cents": 250}])
check("L06 轧账 0 + 250 == 250", led["reconciliation"]["balanced"]
      and led["reconciliation"]["distinct_doc_amount_total"] == 250
      and led["reconciliation"]["pending_total"] == 250)

# ----------------------------------------------------------------- L07 多路径/无关明细：
# 同一单元经多种无关明细组合重复出现，权重仍只算不同单元值；
# B 的无关列 x、E 的无关列 y 会把朴素拼接放大，但 P 投影后全部收敛。
del_diamond = {
    "tables": [
        {"name": "DOC", "columns": ["id", "amt"], "rows": [["D1", 100]]},
        {"name": "B", "columns": ["id", "unit", "x"],
         "rows": [["D1", "u1", "x1"], ["D1", "u1", "x2"], ["D1", "u2", "x3"]]},
        {"name": "E", "columns": ["unit", "bu", "y"],
         "rows": [["u1", "g1", "y1"], ["u1", "g1", "y2"], ["u2", "g1", "y3"]]},
        {"name": "T", "columns": ["bu", "team"], "rows": [["g1", "tm"]]},
    ],
    "tree": [["DOC", "B"], ["B", "E"], ["E", "T"]],
}
dp, out = write_case("ledger_07_diamond", del_diamond)
rc, led, _, err = run_ledger(dp, out, *CFG)
check("L07 链状树结构合法", rc == 0, err)
totals = {tuple(x["group"]): x["amount_cents"] for x in led["group_totals"]}
# 朴素拼接中 u1 有 2x2=4 种明细组合，但不同单元只有 u1/u2 两个 => 权重2，单组独占 100
check("L07 多路径/无关明细不重复计数 100", totals == {("g1", "tm"): 100},
      j(totals))

# ----------------------------------------------------------------- L08 跨分支不相通：
# 单元 u1/u2 只属于 g1，u3 只属于 g2，不能把 u3 分给 g1
del_split = {
    "tables": [
        {"name": "DOC", "columns": ["id", "amt"], "rows": [["D1", 300]]},
        {"name": "B", "columns": ["id", "bu", "unit"],
         "rows": [["D1", "g1", "u1"], ["D1", "g1", "u2"], ["D1", "g2", "u3"]]},
        {"name": "T", "columns": ["bu", "team"],
         "rows": [["g1", "tm1"], ["g2", "tm2"]]},
    ],
    "tree": [["DOC", "B"], ["B", "T"]],
}
dp, out = write_case("ledger_08_split", del_split)
rc, led, _, err = run_ledger(dp, out, *CFG)
totals = {tuple(x["group"]): x["amount_cents"] for x in led["group_totals"]}
# 权重 2:1 => 200:100
check("L08 分组权重 2:1 => 200/100", rc == 0 and
      totals == {("g1", "tm1"): 200, ("g2", "tm2"): 100}, err)

# 同一单元确实出现在多个分组 => 各分组各计一次
del_shared = {
    "tables": [
        {"name": "DOC", "columns": ["id", "amt"], "rows": [["D1", 200]]},
        {"name": "B", "columns": ["id", "bu", "unit"],
         "rows": [["D1", "g1", "u1"], ["D1", "g2", "u1"]]},
        {"name": "T", "columns": ["bu", "team"],
         "rows": [["g1", "tm1"], ["g2", "tm2"]]},
    ],
    "tree": [["DOC", "B"], ["B", "T"]],
}
dp, out = write_case("ledger_08b_shared_unit", del_shared)
rc, led, _, err = run_ledger(dp, out, *CFG)
totals = {tuple(x["group"]): x["amount_cents"] for x in led["group_totals"]}
check("L08b 同单元跨组各计一次 100/100", rc == 0 and
      totals == {("g1", "tm1"): 100, ("g2", "tm2"): 100}, err)

# ----------------------------------------------------------------- L09 无关明细不膨胀（压力）：
# DOC 仅 1 张单据；K(id,k) 500 个无关 k 值；X(q)、W(r) 与主链无共享列（交叉连接）；
# U(unit) 500 个单元、T(bu,team) 500 组；仅 B 把 id/unit/bu 真正关联（4 条）。
# 朴素完整拼接至少 500^3 = 1.25 亿组合，台账中间结构不得随之膨胀。
N = 500
del_stress = {
    "tables": [
        {"name": "DOC", "columns": ["id", "amt"], "rows": [["D1", 1000]]},
        {"name": "K", "columns": ["id", "k"],
         "rows": [["D1", f"k{i}"] for i in range(N)]},
        {"name": "X", "columns": ["q"], "rows": [[f"q{i}"] for i in range(N)]},
        {"name": "W", "columns": ["r"], "rows": [[f"r{i}"] for i in range(N)]},
        {"name": "B", "columns": ["id", "unit", "bu"],
         "rows": [["D1", "u1", "g1"], ["D1", "u2", "g1"],
                  ["D1", "u3", "g2"], ["D1", "u4", "g2"]]},
        {"name": "U", "columns": ["unit"],
         "rows": [[f"u{i}"] for i in range(N)]},
        {"name": "T", "columns": ["bu", "team"],
         "rows": [[f"g{i}", f"tm{i}"] for i in range(N)]},
    ],
    "tree": [["DOC", "K"], ["DOC", "X"], ["DOC", "W"],
             ["DOC", "B"], ["B", "U"], ["B", "T"]],
}
dp, out = write_case("ledger_09_stress", del_stress)
t0 = time.time()
rc, led, _, err = run_ledger(dp, out, *CFG)
dt = time.time() - t0
totals = {tuple(x["group"]): x["amount_cents"] for x in led["group_totals"]}
check("L09 压力用例快速完成且结果正确（权重2:2 => 500/500）",
      rc == 0 and totals == {("g1", "tm1"): 500, ("g2", "tm2"): 500}
      and dt < 10, f"{dt:.2f}s {err}")
check("L09 无中间落地文件", not [f for f in os.listdir(os.path.join(CASES, "ledger_09_stress"))
                                 if "join" in f or "tmp" in f])
check("L09 轧账平衡", led["reconciliation"]["balanced"])

# ----------------------------------------------------------------- L10 非法配置/数据
base = {
    "tables": [
        {"name": "DOC", "columns": ["id", "amt"], "rows": [["D1", 100]]},
        {"name": "B", "columns": ["id", "bu", "unit"], "rows": [["D1", "g1", "u1"]]},
        {"name": "T", "columns": ["bu", "team"], "rows": [["g1", "tm1"]]},
    ],
    "tree": [["DOC", "B"], ["B", "T"]],
}

def reject_case(name, delivery, cfg, needle):
    dp, out = write_case(name, delivery)
    rc, led, _, err = run_ledger(dp, out, *cfg)
    ok = rc == 2 and led and led["status"] == "REJECTED" and \
        any(needle in r for r in led["reasons"])
    check(f"{name} 拒绝: {needle}", ok,
          json.dumps(led, ensure_ascii=False) if led else err)

# 金额列出现在非单据表（X 与 DOC 相邻且都含 amt，RIP 仍成立，但配置规则拒绝）
d = json.loads(json.dumps(base))
d["tables"].append({"name": "X", "columns": ["amt"], "rows": [[100]]})
d["tree"].append(["DOC", "X"])
reject_case("ledger_10_amount_elsewhere", d, CFG, "只能出现在单据表")

# 金额列兼任分组列
reject_case("ledger_10_amount_as_group", base,
            ["--doc-table", "DOC", "--doc-id-col", "id", "--amount-col", "amt",
             "--unit-col", "unit", "--group-col", "amt"], "不能同时作为分组列")

# 金额列兼任分摊单元
reject_case("ledger_10_amount_as_unit", base,
            ["--doc-table", "DOC", "--doc-id-col", "id", "--amount-col", "amt",
             "--unit-col", "amt", "--group-col", "bu"], "分摊单元列")

# 空编号
d = json.loads(json.dumps(base))
d["tables"][0]["rows"] = [[None, 100]]
reject_case("ledger_10_null_id", d, CFG, "单据编号为空")

# 非整数金额（字符串 / 浮点 / 布尔）
d = json.loads(json.dumps(base))
d["tables"][0]["rows"] = [["D1", "100"]]
reject_case("ledger_10_string_amount", d, CFG, "不是整数")
d = json.loads(json.dumps(base))
d["tables"][0]["rows"] = [["D1", 1.5]]
reject_case("ledger_10_float_amount", d, CFG, "不支持的值类型")
d = json.loads(json.dumps(base))
d["tables"][0]["rows"] = [["D1", True]]
reject_case("ledger_10_bool_amount", d, CFG, "不支持的值类型")

# 未知列 / 未知表
reject_case("ledger_10_unknown_col", base,
            ["--doc-table", "DOC", "--doc-id-col", "nope", "--amount-col", "amt",
             "--unit-col", "unit", "--group-col", "bu"], "未在任何窄表中声明")
reject_case("ledger_10_unknown_table", base,
            ["--doc-table", "NOPE", "--doc-id-col", "id", "--amount-col", "amt",
             "--unit-col", "unit", "--group-col", "bu"], "不在交付表清单")

# 分组列为空 / 分组列重复
p = subprocess.run([sys.executable, "-I", TOOL, "ledger",
                    "--delivery", write_case("ledger_10_no_group", base)[0],
                    "--doc-table", "DOC", "--doc-id-col", "id", "--amount-col", "amt",
                    "--unit-col", "unit"],
                   capture_output=True, text=True)
check("L10 缺少 --group-col 由 argparse 拒绝", p.returncode != 0)

# 同名列类型冲突（bu 既有字符串又有整数）
d = json.loads(json.dumps(base))
d["tables"][2]["rows"] = [[1, "tm1"]]
reject_case("ledger_10_type_clash", d, CFG, "冲突类型")

# 非树（不连通）
d = json.loads(json.dumps(base))
d["tables"].append({"name": "Z", "columns": ["z"], "rows": [[1]]})
reject_case("ledger_10_bad_tree", d, CFG, "连接树")

# RIP 破坏：R 含 bu，却挂在不含 bu 的 DOC 下 => bu 的节点块被隔断
d = json.loads(json.dumps(base))
d["tables"].append({"name": "R", "columns": ["bu"], "rows": [["g1"]]})
d["tree"].append(["DOC", "R"])
reject_case("ledger_10_bad_rip", d, CFG, "连成一块")

# 交叉连接语义：U2(unit2) 挂在 DOC 下且与 DOC 无共享列 => unit2 对单据无约束，
# 每个单据都与全部 unit2 值相容（结构合法，不属于“不可达”配置错误）
d = json.loads(json.dumps(base))
d["tables"].append({"name": "U2", "columns": ["unit2"],
                    "rows": [["z1"], ["z2"], ["z3"]]})
d["tree"].append(["DOC", "U2"])
dp, out = write_case("ledger_10_crossjoin", d)
rc, led, _, err = run_ledger(
    dp, out, "--doc-table", "DOC", "--doc-id-col", "id", "--amount-col", "amt",
    "--unit-col", "unit2", "--group-col", "bu")
# 唯一组 g1 与 3 个单元都相容 => 单组独占整笔 100
check("L10 交叉连接: 无约束单元全部相容，单组独占 100", rc == 0 and
      {tuple(x["group"]): x["amount_cents"] for x in led["group_totals"]}
      == {("g1",): 100}, err)

# ----------------------------------------------------------------- L11 单表场景：
# 单据表自身含全部所需列
single = {
    "tables": [
        {"name": "ONLY", "columns": ["id", "amt", "bu", "team", "unit"],
         "rows": [["D1", 100, "g1", "tm", "u1"],
                  ["D2", 300, "g2", "tm", "u2"]]},
    ],
    "tree": [],
}
dp, out = write_case("ledger_11_single", single)
rc, led, _, err = run_ledger(
    dp, out, "--doc-table", "ONLY", "--doc-id-col", "id", "--amount-col", "amt",
    "--unit-col", "unit", "--group-col", "bu", "--group-col", "team")
check("L11 单表台账正确", rc == 0 and
      {tuple(x["group"]): x["amount_cents"] for x in led["group_totals"]} ==
      {("g1", "tm"): 100, ("g2", "tm"): 300} and led["pending"] == [], err)

# 空表（无单据行）
empty = {"tables": [{"name": "ONLY", "columns": ["id", "amt", "bu", "team", "unit"],
                     "rows": []}], "tree": []}
dp, out = write_case("ledger_12_empty", empty)
rc, led, _, err = run_ledger(
    dp, out, "--doc-table", "ONLY", "--doc-id-col", "id", "--amount-col", "amt",
    "--unit-col", "unit", "--group-col", "bu", "--group-col", "team")
check("L12 空输入给出空台账且轧账为0", rc == 0 and
      led["group_totals"] == [] and led["pending"] == []
      and led["reconciliation"]["doc_count"] == 0
      and led["reconciliation"]["balanced"], err)

# ----------------------------------------------------------------- L13 零金额无归属：
# 零金额单据若无任何相容行，仍整笔（0 分）进待分摊；编号列兼任分组列是允许的
del_zero = {
    "tables": [
        {"name": "DOC", "columns": ["id", "amt"],
         "rows": [["D0", 0], ["D1", 50]]},
        {"name": "B", "columns": ["id", "bu", "unit"],
         "rows": [["D1", "g1", "u1"]]},
        {"name": "T", "columns": ["bu", "team"], "rows": [["g1", "tm"]]},
    ],
    "tree": [["DOC", "B"], ["B", "T"]],
}
dp, out = write_case("ledger_13_zero_pending", del_zero)
rc, led, _, err = run_ledger(dp, out, *CFG)
check("L13 零金额无归属单据进入待分摊", rc == 0 and
      led["pending"] == [{"doc_id": "D0", "amount_cents": 0}]
      and {tuple(x["group"]): x["amount_cents"]
           for x in led["group_totals"]} == {("g1", "tm"): 50}, err)

# 编号列与金额列同名 => 配置非法
reject_case("ledger_13_id_is_amount", base,
            ["--doc-table", "DOC", "--doc-id-col", "amt", "--amount-col", "amt",
             "--unit-col", "unit", "--group-col", "bu"], "不能是同一列")

# 编号列允许同时作为分组列
d = json.loads(json.dumps(base))
dp, out = write_case("ledger_13_id_as_group", d)
rc, led, _, err = run_ledger(
    dp, out, "--doc-table", "DOC", "--doc-id-col", "id", "--amount-col", "amt",
    "--unit-col", "unit", "--group-col", "id", "--group-col", "bu")
check("L13 编号列可兼任分组列", rc == 0 and
      {tuple(x["group"]): x["amount_cents"]
       for x in led["group_totals"]} == {("D1", "g1"): 100}, err)

# ----------------------------------------------------------------- 汇总
fails = [r for r in results if not r[1]]
print(f"\n==== {len(results) - len(fails)}/{len(results)} passed ====")
sys.exit(1 if fails else 0)
