#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""窄表滚动发布入口端到端测试（手工用例 + 非法拒收 + 原子性 + 幂等 + 压力）。

全部用 python3 -I 运行；测试数据写在独立 cases/roll_* 目录，脚本位于项目根。
"""
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(ROOT, "tool", "delivery_rollforward.py")
ACCEPT = os.path.join(ROOT, "tool", "lossless_accept.py")
LEDGER = os.path.join(ROOT, "tool", "fee_ledger.py")
CASES = os.path.join(ROOT, "cases")
results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), name, detail)


def write_dir(name, fact, delivery, changes):
    d = os.path.join(CASES, name)
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(d)
    paths = {}
    for key, obj in (("fact", fact), ("delivery", delivery), ("changes", changes)):
        p = os.path.join(d, key + ".json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
        paths[key] = p
    return d, paths


def run_roll(paths, out_dir, key_cols, extra=(), tool=TOOL):
    cmd = [sys.executable, "-I", tool, "apply",
           "--fact", paths["fact"], "--delivery", paths["delivery"],
           "--changes", paths["changes"], "--out-dir", out_dir]
    for k in key_cols:
        cmd += ["--key-col", k]
    cmd += list(extra)
    p = subprocess.run(cmd, capture_output=True, text=True)
    rep = None
    rp = os.path.join(out_dir, "change_report.json")
    if os.path.exists(rp):
        with open(rp, encoding="utf-8") as fh:
            rep = json.load(fh)
    if rep is None and p.stderr.strip():
        try:
            rep = json.loads(p.stderr)
        except json.JSONDecodeError:
            rep = None
    return p.returncode, rep, p.stdout, p.stderr


def run_accept(fact_path, delivery_path):
    p = subprocess.run([sys.executable, "-I", ACCEPT, "accept",
                        "--fact", fact_path, "--delivery", delivery_path],
                       capture_output=True, text=True)
    try:
        return p.returncode, json.loads(p.stdout or p.stderr)
    except json.JSONDecodeError:
        return p.returncode, None


FACT = {"columns": ["a", "b", "c"],
        "rows": [[1, "x", 10], [1, "y", 20], [2, "z", 30]]}
DELIVERY = {
    "tables": [
        {"name": "T1", "columns": ["a", "b"], "rows": [[1, "x"], [1, "y"], [2, "z"]]},
        {"name": "T2", "columns": ["b", "c"], "rows": [["x", 10], ["y", 20], ["z", 30]]},
    ],
    "tree": [["T1", "T2"]],
}

# ----------------------------------------------------------------- R01 主场景
d, paths = write_dir("roll_01_basic", FACT, DELIVERY, {"changes": [
    {"op": "insert", "row": [3, "w", 40]},
    {"op": "update", "key": {"b": "x"}, "row": [1, "x", 99]},
    {"op": "delete", "key": {"b": "z"}},
]})
out = os.path.join(d, "out")
rc, rep, _, err = run_roll(paths, out, ["b"])
check("R01 退出码0", rc == 0, err)
with open(os.path.join(out, "delivery.json"), encoding="utf-8") as fh:
    new_del = json.load(fh)
with open(os.path.join(out, "fact.json"), encoding="utf-8") as fh:
    new_fact = json.load(fh)
T = {t["name"]: {tuple(r) for r in t["rows"]} for t in new_del["tables"]}
check("R01 T1 投影集合正确", T["T1"] == {(1, "x"), (1, "y"), (3, "w")}, str(T))
check("R01 T2 投影集合正确（连接键更新同步到 c）",
      T["T2"] == {("x", 99), ("y", 20), ("w", 40)}, str(T))
check("R01 本周事实集合正确",
      {tuple(r) for r in new_fact["rows"]} == {(3, "w", 40), (1, "x", 99), (1, "y", 20)})
check("R01 报告: 提交3/接受3/完整应用",
      rep["changes"]["submitted_operation_count"] == 3
      and rep["changes"]["accepted_operation_count"] == 3
      and rep["changes"]["complete_applied"] is True)
check("R01 报告: 受影响键3", rep["changes"]["affected_business_key_count"] == 3)
check("R01 报告: 各表新增/移除去重行数",
      {(x["name"], x["added_distinct_row_count"], x["removed_distinct_row_count"])
       for x in rep["tables"]} == {("T1", 1, 1), ("T2", 2, 2)}, str(rep["tables"]))
check("R01 报告: 预检 sets_equal", rep["precheck"]["sets_equal"] is True)
check("R01 报告: 后检 sets_equal", rep["postcheck"]["sets_equal"] is True)
rc2, acc = run_accept(paths["fact"].replace("fact.json", "out/fact.json"),
                      os.path.join(out, "delivery.json"))
check("R01 新包可直接被现有验收入口 ACCEPTED",
      rc2 == 0 and acc and acc["status"] == "ACCEPTED")
p = subprocess.run([sys.executable, "-I", LEDGER, "ledger",
                    "--delivery", os.path.join(out, "delivery.json"),
                    "--doc-table", "T1", "--doc-id-col", "b",
                    "--amount-col", "a", "--unit-col", "c", "--group-col", "b"],
                   capture_output=True, text=True)
check("R01 新包可直接被台账入口消费", p.returncode == 0
      and json.loads(p.stdout)["status"] == "OK")

# ----------------------------------------------------------------- R02 同投影多事实行
# 两条事实行在 T1 上投影相同（1,x）；删除其中一条不得移除仍被另一条使用的交付行。
fact2 = {"columns": ["a", "b", "c"],
         "rows": [[1, "x", 10], [1, "x", 20], [2, "y", 30]]}
del2 = {
    "tables": [
        {"name": "T1", "columns": ["a", "b"], "rows": [[1, "x"], [2, "y"]]},
        {"name": "T2", "columns": ["b", "c"], "rows": [["x", 10], ["x", 20], ["y", 30]]},
    ],
    "tree": [["T1", "T2"]],
}
# 业务键 = (a,b)：删除键 (1,x) 是不允许的（两行同键）——先确认键唯一性拒收
d, paths = write_dir("roll_02_proj", fact2, del2, {"changes": []})
rc, rep, _, err = run_roll(paths, os.path.join(d, "out"), ["a", "b"])
check("R02 业务键不唯一被拒收", rc == 2 and rep["stage"] == "keys"
      and any("重复" in r for r in rep["reasons"]), err)
# 改用键 (b,c)：唯一；删除 (x,20) 后 T1 仍须保留 (1,x)
d, paths = write_dir("roll_02_proj", fact2, del2, {"changes": [
    {"op": "delete", "key": {"b": "x", "c": 20}}]})
out = os.path.join(d, "out")
rc, rep, _, err = run_roll(paths, out, ["b", "c"])
check("R02 删除同投影事实行: 退出码0", rc == 0, err)
with open(os.path.join(out, "delivery.json"), encoding="utf-8") as fh:
    nd = json.load(fh)
T = {t["name"]: {tuple(r) for r in t["rows"]} for t in nd["tables"]}
check("R02 共享投影 (1,x) 未被误删", T["T1"] == {(1, "x"), (2, "y")}, str(T))
check("R02 T2 仅移除 (x,20)", T["T2"] == {("x", 10), ("y", 30)}, str(T))
delta = {x["name"]: (x["added_distinct_row_count"], x["removed_distinct_row_count"])
         for x in rep["tables"]}
check("R02 报告 T1 无增删、T2 移除1", delta == {"T1": (0, 0), "T2": (0, 1)}, str(delta))

# ----------------------------------------------------------------- R03 修改连接键
# 修改业务键外的连接键 b：x -> q，T1 与 T2 的 b 列都必须同步变化
fact3 = {"columns": ["a", "b", "c"],
         "rows": [[1, "x", 10], [2, "y", 20]]}
del3 = {
    "tables": [
        {"name": "T1", "columns": ["a", "b"], "rows": [[1, "x"], [2, "y"]]},
        {"name": "T2", "columns": ["b", "c"], "rows": [["x", 10], ["y", 20]]},
    ],
    "tree": [["T1", "T2"]],
}
d, paths = write_dir("roll_03_joinkey", fact3, del3, {"changes": [
    {"op": "update", "key": {"a": 1}, "row": [1, "q", 10]}]})
out = os.path.join(d, "out")
rc, rep, _, err = run_roll(paths, out, ["a"])
check("R03 修改连接键: 退出码0", rc == 0, err)
with open(os.path.join(out, "delivery.json"), encoding="utf-8") as fh:
    nd = json.load(fh)
T = {t["name"]: {tuple(r) for r in t["rows"]} for t in nd["tables"]}
check("R03 T1 的 b 由 x 变 q", T["T1"] == {(1, "q"), (2, "y")}, str(T))
check("R03 T2 的 b 由 x 变 q", T["T2"] == {("q", 10), ("y", 20)}, str(T))

# ----------------------------------------------------------------- R04 修改不得改变业务键
d, paths = write_dir("roll_04_keychange", FACT, DELIVERY, {"changes": [
    {"op": "update", "key": {"b": "x"}, "row": [1, "z", 10]}]})
rc, rep, _, err = run_roll(paths, os.path.join(d, "out"), ["b"])
check("R04 修改改变业务键被拒收", rc == 2 and rep["stage"] == "changes"
      and any("业务键不能改变" in r for r in rep["reasons"]), err)

# ----------------------------------------------------------------- R05 变更非法族
def reject(name, changes, needle, stage="changes", key_cols=("b",), fact=FACT,
           delivery=DELIVERY):
    d, paths = write_dir(name, fact, delivery, changes)
    rc, rep, _, err = run_roll(paths, os.path.join(d, "out"), list(key_cols))
    ok = rc == 2 and rep and rep["status"] == "REJECTED" and \
        any(needle in r for r in rep["reasons"]) and rep.get("stage") == stage
    check(f"{name}: {needle}", ok, json.dumps(rep, ensure_ascii=False) if rep else err)


reject("roll_05_insert_exists",
       {"changes": [{"op": "insert", "row": [1, "x", 10]}]}, "当前已存在")
reject("roll_05_update_unknown",
       {"changes": [{"op": "update", "key": {"b": "nope"}, "row": [9, "nope", 1]}]},
       "当前不存在")
reject("roll_05_delete_unknown",
       {"changes": [{"op": "delete", "key": {"b": "nope"}}]}, "当前不存在")
reject("roll_05_bad_op",
       {"changes": [{"op": "upsert", "row": [9, "n", 1]}]}, "非法")
reject("roll_05_dup_key",
       {"changes": [{"op": "delete", "key": {"b": "x"}},
                    {"op": "update", "key": {"b": "x"}, "row": [1, "x", 11]}]},
       "重复或冲突")
reject("roll_05_dup_key_same_op",
       {"changes": [{"op": "delete", "key": {"b": "x"}},
                    {"op": "delete", "key": {"b": "x"}}]}, "重复或冲突")
reject("roll_05_null_key",
       {"changes": [{"op": "delete", "key": {"b": None}}]}, "空值")
reject("roll_05_null_key_in_insert",
       {"changes": [{"op": "insert", "row": [9, None, 1]}]}, "空值")
reject("roll_05_width",
       {"changes": [{"op": "insert", "row": [9, "n"]}]}, "宽度")
reject("roll_05_bad_value",
       {"changes": [{"op": "insert", "row": [9, "n", 1.5]}]}, "不支持的值类型")
reject("roll_05_bool_value",
       {"changes": [{"op": "insert", "row": [9, "n", True]}]}, "不支持的值类型")
reject("roll_05_unknown_col",
       {"changes": [{"op": "insert", "row": {"a": 9, "b": "n", "c": 1, "d": 2}}]},
       "未知列")
reject("roll_05_missing_col",
       {"changes": [{"op": "insert", "row": {"a": 9, "b": "n"}}]}, "缺少列")
reject("roll_05_extra_field",
       {"changes": [{"op": "delete", "key": {"b": "x"}, "row": [1, "x", 10]}]},
       "不允许的字段")
reject("roll_05_missing_row",
       {"changes": [{"op": "insert", "key": {"b": "n"}}]}, "完整 row")
reject("roll_05_missing_key",
       {"changes": [{"op": "delete"}]}, "业务键 key")
reject("roll_05_key_width",
       {"changes": [{"op": "delete", "key": ["x", 1]}]}, "长度")
reject("roll_05_key_extra_field",
       {"changes": [{"op": "delete", "key": {"b": "x", "z": 1}}]}, "多余字段")
reject("roll_05_not_object", {"changes": [42]}, "必须是对象")
reject("roll_05_changes_not_list", {"changes": {}}, "必须是数组")
reject("roll_05_unknown_top", {"changes": [], "junk": 1}, "未知顶层字段")
reject("roll_05_row_key_mismatch",
       {"changes": [{"op": "update", "key": {"b": "x"}, "row": [1, "y", 10]}]},
       "不一致")
reject("roll_05_key_col_unknown", {"changes": []}, "不在上周事实表",
       stage="config", key_cols=("nope",))

# ----------------------------------------------------------------- R06 上周基线非法：缺行/造行/结构非法都必须拒收且不静默修复
bad_missing = {
    "tables": [
        {"name": "T1", "columns": ["a", "b"], "rows": [[1, "x"], [1, "y"]]},
        {"name": "T2", "columns": ["b", "c"], "rows": [["x", 10], ["y", 20], ["z", 30]]},
    ],
    "tree": [["T1", "T2"]],
}
d, paths = write_dir("roll_06_missing_base", FACT, bad_missing,
                     {"changes": [{"op": "delete", "key": {"b": "z"}}]})
rc, rep, _, err = run_roll(paths, os.path.join(d, "out"), ["b"])
check("R06 上周交付缺行被拒收（预检 stage）",
      rc == 2 and rep["stage"] == "precheck"
      and any("缺行" in r for r in rep["reasons"]), err)
check("R06 预检给出精确计数",
      rep["detail"]["verdict"]["missing_row_count"] == 1
      and rep["detail"]["verdict"]["spurious_row_count"] == 0)
check("R06 拒收不写任何输出", not os.path.exists(os.path.join(d, "out")))

bad_spurious = {
    "tables": [
        {"name": "T1", "columns": ["a", "b"],
         "rows": [[1, "x"], [1, "y"], [2, "z"], [2, "y"]]},
        {"name": "T2", "columns": ["b", "c"],
         "rows": [["x", 10], ["y", 20], ["z", 30]]},
    ],
    "tree": [["T1", "T2"]],
}
d, paths = write_dir("roll_06_spurious_base", FACT, bad_spurious,
                     {"changes": []})
rc, rep, _, err = run_roll(paths, os.path.join(d, "out"), ["b"])
check("R06 上周交付造行被拒收",
      rc == 2 and rep["stage"] == "precheck"
      and any("造行" in r for r in rep["reasons"])
      and rep["detail"]["verdict"]["spurious_row_count"] >= 1, err)

bad_tree = {
    "tables": [
        {"name": "T1", "columns": ["a", "b"], "rows": [[1, "x"], [1, "y"], [2, "z"]]},
        {"name": "T2", "columns": ["b", "c"], "rows": [["x", 10], ["y", 20], ["z", 30]]},
        {"name": "T3", "columns": ["a"], "rows": [[1], [2]]},
    ],
    "tree": [["T1", "T2"]],
}
d, paths = write_dir("roll_06_bad_struct", FACT, bad_tree, {"changes": []})
rc, rep, _, err = run_roll(paths, os.path.join(d, "out"), ["b"])
check("R06 上周交付结构非法被拒收",
      rc == 2 and rep["stage"] == "delivery"
      and any("连通" in r or "n-1" in r for r in rep["reasons"]), err)

# ----------------------------------------------------------------- R07 原子性：失败保持原包可用
d, paths = write_dir("roll_07_atomic", FACT, DELIVERY, {"changes": [
    {"op": "delete", "key": {"b": "nope"}}]})
out = os.path.join(d, "out")
os.makedirs(out)
with open(os.path.join(out, "delivery.json"), "w", encoding="utf-8") as fh:
    fh.write('{"OLD": true}\n')
rc, rep, _, _ = run_roll(paths, out, ["b"])
with open(os.path.join(out, "delivery.json"), encoding="utf-8") as fh:
    old = fh.read()
check("R07 失败时原输出不被覆盖", rc == 2 and old == '{"OLD": true}\n')
check("R07 失败时无新报告", not os.path.exists(os.path.join(out, "change_report.json")))
check("R07 失败时无临时文件残留",
      not [f for f in os.listdir(out) if f.startswith(".rollforward")])

# ----------------------------------------------------------------- R07b 原子性：中途写失败必须回滚全部输出
import tempfile
sys.path.insert(0, os.path.join(ROOT, "tool"))
import delivery_rollforward as DRF  # noqa: E402

tmpd = tempfile.mkdtemp()
pa = os.path.join(tmpd, "a.json")
pb = os.path.join(tmpd, "b.json")
with open(pa, "w") as fh:
    fh.write("OLD_A")
with open(pb, "w") as fh:
    fh.write("OLD_B")
orig_replace = DRF.os.replace
fired = {"n": 0}


def flaky(src, dst):
    if dst == pb and fired["n"] == 0:
        fired["n"] += 1
        raise OSError("simulated failure")
    return orig_replace(src, dst)


DRF.os.replace = flaky
raised = False
try:
    DRF.atomic_write_outputs([(pa, "NEW_A"), (pb, "NEW_B")])
except OSError:
    raised = True
finally:
    DRF.os.replace = orig_replace
with open(pa) as fh:
    ra = fh.read()
with open(pb) as fh:
    rb = fh.read()
check("R07b 写失败回滚全部输出", raised and ra == "OLD_A" and rb == "OLD_B",
      f"{ra!r}/{rb!r}")
check("R07b 回滚后无临时残留",
      not [f for f in os.listdir(tmpd) if f.startswith(".rollforward")])
shutil.rmtree(tmpd)

# ----------------------------------------------------------------- R07c 输出路径互相重叠 => 拒收
d, paths = write_dir("roll_07c_outcollision", FACT, DELIVERY, {"changes": []})
outd = os.path.join(d, "out")
same = os.path.join(outd, "same.json")
p = subprocess.run([sys.executable, "-I", TOOL, "apply",
                    "--fact", paths["fact"], "--delivery", paths["delivery"],
                    "--changes", paths["changes"], "--key-col", "b",
                    "--out-dir", outd, "--delivery-out", same, "--report-out", same],
                   capture_output=True, text=True)
check("R07c 两个输出指向同一路径被拒收",
      p.returncode == 2 and "指向同一路径" in p.stderr)
check("R07c 拒收后不落盘任何输出", not os.path.exists(outd))

# ----------------------------------------------------------------- R08 幂等：重复运行同结果、同报告
d, paths = write_dir("roll_08_idem", FACT, DELIVERY, {"changes": [
    {"op": "insert", "row": [3, "w", 40]},
    {"op": "update", "key": {"b": "x"}, "row": [1, "x", 99]},
    {"op": "delete", "key": {"b": "z"}}]})
o1 = os.path.join(d, "out1")
o2 = os.path.join(d, "out2")
rc1, rep1, _, _ = run_roll(paths, o1, ["b"])
rc2, rep2, _, _ = run_roll(paths, o2, ["b"])
check("R08 两次运行均成功", rc1 == 0 and rc2 == 0)
first_bytes = {}
for fn in ("delivery.json", "fact.json", "change_report.json"):
    with open(os.path.join(o1, fn), "rb") as fh:
        a = fh.read()
    with open(os.path.join(o2, fn), "rb") as fh:
        b = fh.read()
    first_bytes[fn] = a
    check(f"R08 {fn} 逐字节一致", a == b)
# 同目录重复运行（覆盖自己）也应一致
rc3, rep3, _, _ = run_roll(paths, o1, ["b"])
check("R08 覆盖重复运行结果稳定", rc3 == 0 and all(
    open(os.path.join(o1, fn), "rb").read() == data
    for fn, data in first_bytes.items()))

# ----------------------------------------------------------------- R09 空变更：原样发布，报告零操作
d, paths = write_dir("roll_09_noop", FACT, DELIVERY, {"changes": []})
out = os.path.join(d, "out")
rc, rep, _, err = run_roll(paths, out, ["b"])
check("R09 空变更发布成功且零操作",
      rc == 0 and rep["changes"]["accepted_operation_count"] == 0
      and rep["changes"]["affected_business_key_count"] == 0, err)
with open(os.path.join(out, "delivery.json"), encoding="utf-8") as fh:
    nd = json.load(fh)
orig = {t["name"]: {tuple(r) for r in t["rows"]} for t in DELIVERY["tables"]}
new = {t["name"]: {tuple(r) for r in t["rows"]} for t in nd["tables"]}
check("R09 空变更交付集合不变", new == orig)

# ----------------------------------------------------------------- R10 空值字面相等 + 以数组形式给出 key
# 业务键 a 非空；非键列 b 的显式空值按字面值处理（None == None 成立）
fact10 = {"columns": ["a", "b"], "rows": [[1, None], [2, "z"], [3, "y"]]}
del10 = {
    "tables": [
        {"name": "T1", "columns": ["a"], "rows": [[1], [2], [3]]},
        {"name": "T2", "columns": ["a", "b"],
         "rows": [[1, None], [2, "z"], [3, "y"]]},
    ],
    "tree": [["T1", "T2"]],
}
# 事实表业务键含空值 => 拒收（换个确实含空键的键列）
d, paths = write_dir("roll_10_nullkey", fact10, del10, {"changes": []})
rc, rep, _, err = run_roll(paths, os.path.join(d, "out"), ["b"])
check("R10 事实表业务键含空值被拒收",
      rc == 2 and rep["stage"] == "keys" and any("空值" in r for r in rep["reasons"]), err)
# 用非空键 a，并以数组形式给出 key
d, paths = write_dir("roll_10_nullkey", fact10, del10, {"changes": [
    {"op": "delete", "key": [2]},
    {"op": "insert", "row": [4, None]}]})
out = os.path.join(d, "out")
rc, rep, _, err = run_roll(paths, out, ["a"])
check("R10 非空业务键可用数组形式且空值按字面值处理", rc == 0, err)
with open(os.path.join(out, "delivery.json"), encoding="utf-8") as fh:
    nd = json.load(fh)
T = {t["name"]: {tuple(r) for r in t["rows"]} for t in nd["tables"]}
check("R10 T2 空值行集正确",
      T["T2"] == {(1, None), (3, "y"), (4, None)}, str(T))
check("R10 T1 投影集合正确", T["T1"] == {(1,), (3,), (4,)}, str(T))

# ----------------------------------------------------------------- R11 类型冲突由变更引入 => 拒收
d, paths = write_dir("roll_11_typeclash", FACT, DELIVERY, {"changes": [
    {"op": "insert", "row": [3, "w", "not_int"]}]})
rc, rep, _, err = run_roll(paths, os.path.join(d, "out"), ["b"])
check("R11 变更引入列类型冲突被拒收",
      rc == 2 and rep["stage"] == "changes" and any("冲突类型" in r for r in rep["reasons"]), err)

# ----------------------------------------------------------------- R12 输出路径与输入重叠 => 拒收
d, paths = write_dir("roll_12_overlap", FACT, DELIVERY, {"changes": []})
p = subprocess.run([sys.executable, "-I", TOOL, "apply",
                    "--fact", paths["fact"], "--delivery", paths["delivery"],
                    "--changes", paths["changes"], "--key-col", "b",
                    "--delivery-out", paths["delivery"]],
                   capture_output=True, text=True)
check("R12 输出覆盖输入被拒收", p.returncode == 2 and "重叠" in p.stderr)
with open(paths["delivery"], encoding="utf-8") as fh:
    check("R12 输入文件保持原样", json.load(fh) == DELIVERY)

# ----------------------------------------------------------------- R13 大隐式连接：不生成连接结果
# 3 张无共享列分支各 400 行（隐式 6400 万组合）+ 一条事实行；变更 1 条新增。
N = 400
fact13 = {"columns": ["a", "b", "c", "k"],
          "rows": [[0, 0, 0, "k0"]]}
del13 = {
    "tables": [
        {"name": "T1", "columns": ["a", "k"], "rows": [[x, "k0"] for x in range(N)]},
        {"name": "T2", "columns": ["b"], "rows": [[x] for x in range(N)]},
        {"name": "T3", "columns": ["c"], "rows": [[x] for x in range(N)]},
    ],
    "tree": [["T1", "T2"], ["T1", "T3"]],
}
# 该上周包必然「造行」——预检应拒收，且必须在极短时间内精确计数而非枚举
d, paths = write_dir("roll_13_stress", fact13, del13, {"changes": []})
t0 = time.time()
rc, rep, _, err = run_roll(paths, os.path.join(d, "out"), ["k"])
dt = time.time() - t0
check("R13 亿级隐式连接预检快速且拒收（精确计数）",
      rc == 2 and rep["stage"] == "precheck"
      and rep["detail"]["verdict"]["reconstructed_distinct_row_count"] == N ** 3
      and dt < 15, f"{dt:.2f}s")

# 合法大包：让上周交付与事实精确一致（单表单列），再应用少量变更
fact13b = {"columns": ["a", "k"], "rows": [[x, "k0"] for x in range(N)]}
del13b = {
    "tables": [{"name": "T1", "columns": ["a", "k"],
                "rows": [[x, "k0"] for x in range(N)]}],
    "tree": [],
}
d, paths = write_dir("roll_13b_stress", fact13b, del13b, {"changes": [
    {"op": "insert", "row": [N, "new"]},
    {"op": "delete", "key": {"k": "k0"}}]})
# 键 k 不唯一 => 拒收；改键 a
d, paths = write_dir("roll_13b_stress", fact13b, del13b, {"changes": [
    {"op": "insert", "row": [N, "new"]},
    {"op": "delete", "key": {"a": 0}}]})
out = os.path.join(d, "out")
t0 = time.time()
rc, rep, _, err = run_roll(paths, out, ["a"])
dt = time.time() - t0
check("R13b 大输入少量变更快速完成",
      rc == 0 and rep["tables"][0]["removed_distinct_row_count"] == 1
      and rep["tables"][0]["added_distinct_row_count"] == 1 and dt < 15,
      f"{dt:.2f}s {err}")
check("R13b 无连接结果落地", not [f for f in os.listdir(out)
                                if "join" in f or "reconstruct" in f])

# ----------------------------------------------------------------- R14 多表链 + 批次综合
fact14 = {"columns": ["a", "b", "c", "d"],
          "rows": [[1, "x", 10, "p"], [2, "y", 20, "q"], [3, "z", 30, "r"]]}
del14 = {
    "tables": [
        {"name": "T1", "columns": ["a", "b"], "rows": [[1, "x"], [2, "y"], [3, "z"]]},
        {"name": "T2", "columns": ["b", "c"], "rows": [["x", 10], ["y", 20], ["z", 30]]},
        {"name": "T3", "columns": ["c", "d"], "rows": [[10, "p"], [20, "q"], [30, "r"]]},
    ],
    "tree": [["T1", "T2"], ["T2", "T3"]],
}
d, paths = write_dir("roll_14_chain", fact14, del14, {"changes": [
    {"op": "insert", "row": [4, "x", 40, "s"]},
    {"op": "update", "key": {"a": 2}, "row": [2, "w", 20, "q"]},
    {"op": "delete", "key": {"a": 3}},
]})
out = os.path.join(d, "out")
rc, rep, _, err = run_roll(paths, out, ["a"])
check("R14 三表链综合变更成功", rc == 0, err)
with open(os.path.join(out, "delivery.json"), encoding="utf-8") as fh:
    nd = json.load(fh)
T = {t["name"]: {tuple(r) for r in t["rows"]} for t in nd["tables"]}
check("R14 T1 集合", T["T1"] == {(1, "x"), (2, "w"), (4, "x")}, str(T["T1"]))
check("R14 T2 集合", T["T2"] == {("x", 10), ("w", 20), ("x", 40)}, str(T["T2"]))
check("R14 T3 集合", T["T3"] == {(10, "p"), (20, "q"), (40, "s")}, str(T["T3"]))
check("R14 报告表数3", len(rep["tables"]) == 3)
# 该连接树（a->b 与 c->d 非键连接）在变更后按协议投影会出现结构性造行，
# 工具不得私自改动交付集合，必须在后检与警告中如实报告。
check("R14 结构性造行被如实报告（不改写交付集合）",
      rep["postcheck"]["sets_equal"] is False
      and rep["postcheck"]["spurious_row_count"] == 2
      and any("WARNING" in w for w in rep["warnings"]), str(rep["postcheck"]))
# 新包仍可被现有验收入口直接复核（并给出诚实结论，而非崩溃/拒收格式）
rc2, acc = run_accept(os.path.join(out, "fact.json"), os.path.join(out, "delivery.json"))
check("R14 新包可被现有验收入口直接复核",
      rc2 == 2 and acc and acc["status"] == "REJECTED"
      and acc["verdict"]["spurious_row_count"] == 2)
# 严格模式应拒收这类结构性造行
d_strict = os.path.join(d, "out_strict")
cmd = [sys.executable, "-I", TOOL, "apply",
       "--fact", paths["fact"], "--delivery", paths["delivery"],
       "--changes", paths["changes"], "--key-col", "a",
       "--out-dir", d_strict, "--require-lossless"]
p = subprocess.run(cmd, capture_output=True, text=True)
check("R14 --require-lossless 拒收结构性造行且不落盘",
      p.returncode == 2 and "require-lossless" in p.stderr
      and not os.path.exists(d_strict))

# ----------------------------------------------------------------- R15 无损链式模式：连接键唯一
# 业务键 b 唯一 => T1(a,b) 中 b->a 单值，T2(b,c) 中 b->c 单值，投影包无损
fact15 = {"columns": ["a", "b", "c"],
          "rows": [[1, "x", 10], [2, "y", 20], [3, "z", 30]]}
del15 = {
    "tables": [
        {"name": "T1", "columns": ["a", "b"], "rows": [[1, "x"], [2, "y"], [3, "z"]]},
        {"name": "T2", "columns": ["b"], "rows": [["x"], ["y"], ["z"]]},
        {"name": "T3", "columns": ["b", "c"], "rows": [["x", 10], ["y", 20], ["z", 30]]},
    ],
    "tree": [["T1", "T2"], ["T2", "T3"]],
}
d, paths = write_dir("roll_15_lossless", fact15, del15, {"changes": [
    {"op": "insert", "row": [4, "w", 40]},
    {"op": "update", "key": {"b": "y"}, "row": [2, "y", 25]},
    {"op": "delete", "key": {"b": "z"}}]})
out = os.path.join(d, "out")
rc, rep, _, err = run_roll(paths, out, ["b"])
check("R15 唯一连接键链式变更后仍无损", rc == 0 and rep["postcheck"]["sets_equal"], err)
rc2, acc = run_accept(os.path.join(out, "fact.json"), os.path.join(out, "delivery.json"))
check("R15 新包被现有验收入口 ACCEPTED",
      rc2 == 0 and acc and acc["status"] == "ACCEPTED")

# R14 各表增量计数非负
check("R14 各表增量计数非负", all(x["added_distinct_row_count"] >= 0
                                   and x["removed_distinct_row_count"] >= 0
                                   for x in rep["tables"]))

# ----------------------------------------------------------------- 汇总
fails = [r for r in results if not r[1]]
print(f"\n==== {len(results) - len(fails)}/{len(results)} passed ====")
sys.exit(1 if fails else 0)
