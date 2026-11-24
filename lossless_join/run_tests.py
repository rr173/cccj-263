#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端测试：生成各场景输入，调用验收工具与证据复核入口并断言结论。
注意：用 python -I 运行；测试数据写在独立 cases 目录。"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(ROOT, "tool", "lossless_accept.py")
CASES = os.path.join(ROOT, "cases")
os.makedirs(CASES, exist_ok=True)


def write_case(name, fact, delivery):
    d = os.path.join(CASES, name)
    os.makedirs(d, exist_ok=True)
    fp = os.path.join(d, "fact.json")
    dp = os.path.join(d, "delivery.json")
    rp = os.path.join(d, "report.json")
    with open(fp, "w", encoding="utf-8") as fh:
        json.dump(fact, fh, ensure_ascii=False)
    with open(dp, "w", encoding="utf-8") as fh:
        json.dump(delivery, fh, ensure_ascii=False)
    return fp, dp, rp


def run(*args):
    p = subprocess.run([sys.executable, "-I", TOOL, *args],
                       capture_output=True, text=True)
    out = p.stdout.strip()
    err = p.stderr.strip()
    try:
        parsed = json.loads(out) if out else json.loads(err)
    except json.JSONDecodeError:
        parsed = None
    return p.returncode, parsed, out, err


def run_accept(fp, dp, rp):
    """accept 带 --out 时 stdout 为空，从报告文件读取。"""
    rc, parsed, out, err = run("accept", "--fact", fp, "--delivery", dp, "--out", rp)
    if parsed is None and os.path.exists(rp):
        with open(rp, encoding="utf-8") as fh:
            parsed = json.load(fh)
    return rc, parsed, out, err


results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("PASS" if cond else "FAIL"), name, detail)


# ---------------------------------------------------------------- 1. 干净无损
# b 为唯一键（b->a, b->c 成立），按 b 拆分可无损回拼
fact1 = {
    "columns": ["a", "b", "c"],
    "rows": [[1, "x", 10], [1, "y", 20], [2, "z", 30]],
}
del1 = {
    "tables": [
        {"name": "T1", "columns": ["a", "b"], "rows": [[1, "x"], [1, "y"], [2, "z"]]},
        {"name": "T2", "columns": ["b", "c"], "rows": [["x", 10], ["y", 20], ["z", 30]]},
    ],
    "tree": [["T1", "T2"]],
}
fp, dp, rp = write_case("01_clean", fact1, del1)
rc, rep, _, err = run_accept(fp, dp, rp)
check("01 干净无损: 退出码0", rc == 0, err)
check("01 干净无损: ACCEPTED", rep and rep["status"] == "ACCEPTED")
check("01 回拼行数=3", rep and rep["verdict"]["reconstructed_distinct_row_count"] == 3)
rc, ver, _, err = run("verify-report", "--fact", fp, "--delivery", dp, "--report", rp)
check("01 证据复核 VERIFIED", rc == 0 and ver and ver["status"] == "VERIFIED", err)

# ---------------------------------------------------------------- 2. 造行
# 事实回到非键场景：T1 多出 (2,y)，与 T2 的 (y,20) 拼出 (2,y,20)
fact1 = {
    "columns": ["a", "b", "c"],
    "rows": [[1, "x", 10], [1, "y", 20], [2, "x", 30]],
}
del2 = {
    "tables": [
        {"name": "T1", "columns": ["a", "b"], "rows": [[1, "x"], [1, "y"], [2, "x"], [2, "y"]]},
        {"name": "T2", "columns": ["b", "c"], "rows": [["x", 10], ["y", 20], ["x", 30]]},
    ],
    "tree": [["T1", "T2"]],
}
fp, dp, rp = write_case("02_spurious", fact1, del2)
rc, rep, _, err = run_accept(fp, dp, rp)
check("02 造行: 拒绝", rc == 2 and rep["status"] == "REJECTED", err)
v = rep["verdict"]
# 自然连接: b=x 组 2*2=4，b=y 组 2*1=2，共 6；交集 3；造 3；缺 0
check("02 回拼6/交集3/造行3/缺0",
      (v["reconstructed_distinct_row_count"], v["intersection_row_count"],
       v["spurious_row_count"], v["missing_row_count"]) == (6, 3, 3, 0),
      json.dumps(v))
se = rep["spurious_evidence"]
check("02 造行证据不在原表且两表坐标齐全",
      se and tuple(se["assembled_row"]) not in {tuple(r) for r in fact1["rows"]}
      and len(se["witnesses"]) == 2,
      json.dumps(se, ensure_ascii=False))
# 证人在共享列 b 上必须一致（否则不是合法拼接）
bvals = {w["row"][w["columns"].index("b")] for w in se["witnesses"]}
check("02 证人共享列 b 取值一致", bvals == {"y"} or bvals == {"x"}, str(bvals))
rc, ver, _, err = run("verify-report", "--fact", fp, "--delivery", dp, "--report", rp)
check("02 证据复核通过", rc == 0 and ver["status"] == "VERIFIED", err)

# ---------------------------------------------------------------- 3. 缺行
# T1 缺 (2,x)，原行 (2,x,30) 拼不回；T1 仅剩 (1,x),(1,y)
del3 = {
    "tables": [
        {"name": "T1", "columns": ["a", "b"], "rows": [[1, "x"], [1, "y"]]},
        {"name": "T2", "columns": ["b", "c"], "rows": [["x", 10], ["y", 20], ["x", 30]]},
    ],
    "tree": [["T1", "T2"]],
}
fp, dp, rp = write_case("03_missing", fact1, del3)
rc, rep, _, err = run_accept(fp, dp, rp)
v = rep["verdict"]
# 连接: (1,x,10),(1,x,30),(1,y,20) 共3行；交集 2；缺 1；造 1（(1,x,30)）
check("03 缺行: 回拼3/交集2/缺1/造1（两类并存不抵销）",
      (v["reconstructed_distinct_row_count"], v["intersection_row_count"],
       v["spurious_row_count"], v["missing_row_count"]) == (3, 2, 1, 1),
      json.dumps(v))
me = rep["missing_evidence"]
check("03 缺行证据原行=(2,x,30)", me and me["fact_row"] == [2, "x", 30])
check("03 缺失片段坐标=T1 [2,x]",
      me and me["missing_fragment"]["table"] == "T1"
      and me["missing_fragment"]["expected_row"] == [2, "x"])
rc, ver, _, _ = run("verify-report", "--fact", fp, "--delivery", dp, "--report", rp)
check("03 证据复核通过", rc == 0 and ver["status"] == "VERIFIED")

# ---------------------------------------------------------------- 4. 纯缺行（无造行）：用键场景删一个片段
fact4 = {
    "columns": ["a", "b", "c"],
    "rows": [[1, "x", 10], [1, "y", 20], [2, "z", 30]],
}
del4 = {
    "tables": [
        {"name": "T1", "columns": ["a", "b"], "rows": [[1, "x"], [1, "y"]]},  # 缺 (2,z)
        {"name": "T2", "columns": ["b", "c"], "rows": [["x", 10], ["y", 20], ["z", 30]]},
    ],
    "tree": [["T1", "T2"]],
}
fp, dp, rp = write_case("04_missing_only", fact4, del4)
rc, rep, _, err = run_accept(fp, dp, rp)
v = rep["verdict"]
check("04 纯缺行: 回拼2/交集2/缺1/造0",
      (v["reconstructed_distinct_row_count"], v["intersection_row_count"],
       v["spurious_row_count"], v["missing_row_count"]) == (2, 2, 0, 1),
      json.dumps(v))
check("04 仅缺行证据", rep["missing_evidence"] and rep["spurious_evidence"] is None)
rc, ver, _, _ = run("verify-report", "--fact", fp, "--delivery", dp, "--report", rp)
check("04 证据复核通过", rc == 0 and ver["status"] == "VERIFIED")

# ---------------------------------------------------------------- 5. 非法：RIP 破坏
fact5 = {"columns": ["a", "b", "c"], "rows": [[1, "x", 10]]}
del5 = {
    "tables": [
        {"name": "T1", "columns": ["a", "b"], "rows": [[1, "x"]]},
        {"name": "T2", "columns": ["b", "c"], "rows": [["x", 10]]},
        {"name": "T3", "columns": ["a", "c"], "rows": [[1, 10]]},
    ],
    "tree": [["T1", "T2"], ["T2", "T3"]],  # a 出现在 T1,T3 但路径经 T2（不含 a）
}
fp, dp, _ = write_case("05_bad_rip", fact5, del5)
rc, rep, _, err = run("accept", "--fact", fp, "--delivery", dp)
check("05 RIP 破坏被拒绝", rc == 2 and rep and rep["status"] == "REJECTED"
      and any("连成一块" in r for r in rep["reasons"]), json.dumps(rep, ensure_ascii=False))

# ---------------------------------------------------------------- 6. 非法：树不连通/边数错
del6 = {
    "tables": [
        {"name": "T1", "columns": ["a", "b"], "rows": [[1, "x"]]},
        {"name": "T2", "columns": ["b", "c"], "rows": [["x", 10]]},
        {"name": "T3", "columns": ["c"], "rows": [[10]]},
    ],
    "tree": [["T1", "T2"]],
}
fp, dp, _ = write_case("06_bad_tree", fact5, del6)
rc, rep, _, _ = run("accept", "--fact", fp, "--delivery", dp)
check("06 非树结构被拒绝", rc == 2 and rep["status"] == "REJECTED"
      and any("连通" in r or "n-1" in r for r in rep["reasons"]))

# ---------------------------------------------------------------- 7. 非法：列覆盖缺失 + 类型冲突
del7 = {
    "tables": [
        {"name": "T1", "columns": ["a", "b"], "rows": [[1, "x"]]},
        {"name": "T2", "columns": ["b"], "rows": [["x"]]},
    ],
    "tree": [["T1", "T2"]],
}
fp, dp, _ = write_case("07_bad_coverage", fact5, del7)
rc, rep, _, _ = run("accept", "--fact", fp, "--delivery", dp)
check("07 列未覆盖被拒绝", rc == 2 and any("没有任何交付表声明" in r for r in rep["reasons"]))

fact7b = {"columns": ["a", "b"], "rows": [[1, "x"]]}
del7b = {
    "tables": [
        {"name": "T1", "columns": ["a", "b"], "rows": [[1, "x"]]},
        {"name": "T2", "columns": ["a"], "rows": [["1"]]},  # 字符串 vs 整数
    ],
    "tree": [["T1", "T2"]],
}
fp, dp, _ = write_case("07b_type_clash", fact7b, del7b)
rc, rep, _, _ = run("accept", "--fact", fp, "--delivery", dp)
check("07b 同名列类型冲突被拒绝", rc == 2
      and any("冲突类型" in r for r in rep["reasons"]), json.dumps(rep, ensure_ascii=False))

# ---------------------------------------------------------------- 8. 空值字面相等
fact8 = {"columns": ["a", "b"], "rows": [[1, None], [None, "z"]]}
del8 = {
    "tables": [
        {"name": "T1", "columns": ["a"], "rows": [[1], [None]]},
        {"name": "T2", "columns": ["a", "b"], "rows": [[1, None], [None, "z"]]},
    ],
    "tree": [["T1", "T2"]],
}
fp, dp, rp = write_case("08_nulls", fact8, del8)
rc, rep, _, err = run_accept(fp, dp, rp)
check("08 空值字面相等且无损", rc == 0 and rep["status"] == "ACCEPTED", err)
check("08 回拼行数=2", rep["verdict"]["reconstructed_distinct_row_count"] == 2)

# 空值造行：T1 多一个 None 已存在；改 T2 多 [None,"q"] 与 T1 的 None 拼出 (None,q)
del8b = {
    "tables": [
        {"name": "T1", "columns": ["a"], "rows": [[1], [None]]},
        {"name": "T2", "columns": ["a", "b"], "rows": [[1, None], [None, "z"], [None, "q"]]},
    ],
    "tree": [["T1", "T2"]],
}
fp, dp, rp = write_case("08b_null_spurious", fact8, del8b)
rc, rep, _, _ = run_accept(fp, dp, rp)
check("08b 空值造行被识别", rep["verdict"]["spurious_row_count"] == 1
      and rep["spurious_evidence"]["assembled_row"] == [None, "q"])
rc, ver, _, _ = run("verify-report", "--fact", fp, "--delivery", dp, "--report", rp)
check("08b 证据复核通过", rc == 0 and ver["status"] == "VERIFIED")

# ---------------------------------------------------------------- 9. 隐式大连接：输入小、回拼极大
# T1(a) 300 行, T2(b) 300 行, T3(c) 300 行，无相邻共享列（星形交叉连接）
big = list(range(300))
fact9 = {"columns": ["a", "b", "c"], "rows": [[0, 0, 0]]}  # 原表很小
del9 = {
    "tables": [
        {"name": "T1", "columns": ["a"], "rows": [[x] for x in big]},
        {"name": "T2", "columns": ["b"], "rows": [[x] for x in big]},
        {"name": "T3", "columns": ["c"], "rows": [[x] for x in big]},
    ],
    "tree": [["T1", "T2"], ["T1", "T3"]],
}
fp, dp, rp = write_case("09_huge_implicit", fact9, del9)
rc, rep, out, err = run_accept(fp, dp, rp)
check("09 大连接不爆炸且精确计数 27,000,000",
      rep and rep["verdict"]["reconstructed_distinct_row_count"] == 300 ** 3,
      err or out)
check("09 造行数=26,999,999 且不落地回拼表",
      rep["verdict"]["spurious_row_count"] == 300 ** 3 - 1)
check("09 证据搜索仍给出一条拼成行",
      rep["spurious_evidence"] is not None
      and len(rep["spurious_evidence"]["assembled_row"]) == 3)
# 目录中不应出现回拼表落地文件
leftover = [f for f in os.listdir(os.path.join(CASES, "09_huge_implicit"))
            if "join" in f or "reconstruct" in f]
check("09 无回拼中间落地文件", not leftover, str(leftover))
rc, ver, _, _ = run("verify-report", "--fact", fp, "--delivery", dp, "--report", rp)
check("09 证据复核通过", rc == 0 and ver["status"] == "VERIFIED")

# ---------------------------------------------------------------- 10. 重复行集合语义 + 通知
fact10 = {"columns": ["a", "b"], "rows": [[1, "x"], [1, "x"], [2, "y"]]}
del10 = {
    "tables": [
        {"name": "T1", "columns": ["a"], "rows": [[1], [1], [2]]},
        {"name": "T2", "columns": ["a", "b"], "rows": [[1, "x"], [2, "y"], [2, "y"]]},
    ],
    "tree": [["T1", "T2"]],
}
fp, dp, rp = write_case("10_dupes", fact10, del10)
rc, rep, _, err = run_accept(fp, dp, rp)
check("10 重复行不影响无损结论", rc == 0 and rep["status"] == "ACCEPTED", err)
check("10 原表去重后2行、回拼2行",
      rep["verdict"]["fact_row_count"] == 2
      and rep["verdict"]["reconstructed_distinct_row_count"] == 2)
check("10 重复行有通知但不作废", any("重复行" in w for w in rep["warnings"]))
# 证据坐标应指向文件原始下标（含重复位置）
rc, ver, _, _ = run("verify-report", "--fact", fp, "--delivery", dp, "--report", rp)
check("10 复核通过", rc == 0 and ver["status"] == "VERIFIED")

# ---------------------------------------------------------------- 11. 非法值/形状
fact11 = {"columns": ["a", "b"], "rows": [[1, 1.5]]}  # 浮点不支持
del11 = {
    "tables": [
        {"name": "T1", "columns": ["a", "b"], "rows": [[1, 1.5]]},
    ],
    "tree": [],
}
fp, dp, _ = write_case("11_bad_value", fact11, del11)
rc, rep, _, _ = run("accept", "--fact", fp, "--delivery", dp)
check("11 非法值类型被拒绝", rc == 2 and any("不支持的值类型" in r for r in rep["reasons"]))

# ---------------------------------------------------------------- 12. 三表链 + 多共享列，造行证据跨三表
fact12 = {"columns": ["a", "b", "c", "d"],
          "rows": [[1, "x", 10, "p"], [1, "x", 20, "q"], [2, "y", 10, "r"]]}
del12 = {
    "tables": [
        {"name": "T1", "columns": ["a", "b"], "rows": [[1, "x"], [2, "y"], [2, "x"]]},
        {"name": "T2", "columns": ["b", "c"], "rows": [["x", 10], ["x", 20], ["y", 10]]},
        {"name": "T3", "columns": ["c", "d"], "rows": [[10, "p"], [20, "q"], [10, "r"]]},
    ],
    "tree": [["T1", "T2"], ["T2", "T3"]],
}
fp, dp, rp = write_case("12_chain", fact12, del12)
rc, rep, _, err = run_accept(fp, dp, rp)
v = rep["verdict"]
# 手工连接共 8 行，与原表交集 3（全部原行均可回拼），造行 5、缺行 0
check("12 链状三表计数: 回拼8 交集3 造5 缺0",
      (v["reconstructed_distinct_row_count"], v["intersection_row_count"],
       v["spurious_row_count"], v["missing_row_count"]) == (8, 3, 5, 0),
      json.dumps(v))
se = rep["spurious_evidence"]
check("12 证据三表坐标齐全且拼成行不在原表",
      se and len(se["witnesses"]) == 3
      and tuple(se["assembled_row"]) not in {tuple(r) for r in fact12["rows"]},
      json.dumps(se, ensure_ascii=False))
rc, ver, _, _ = run("verify-report", "--fact", fp, "--delivery", dp, "--report", rp)
check("12 复核通过", rc == 0 and ver["status"] == "VERIFIED")

# ---------------------------------------------------------------- 13. 篡改报告应复核失败
with open(rp, encoding="utf-8") as fh:
    tampered = json.load(fh)
tampered["spurious_evidence"]["assembled_row"][0] = 999
rp_bad = rp.replace("report.json", "report_tampered.json")
with open(rp_bad, "w", encoding="utf-8") as fh:
    json.dump(tampered, fh, ensure_ascii=False)
rc, ver, _, _ = run("verify-report", "--fact", fp, "--delivery", dp, "--report", rp_bad)
check("13 篡改报告被复核驳回", rc == 3 and ver["status"] == "VERIFICATION_FAILED")

# ---------------------------------------------------------------- 汇总
fails = [r for r in results if not r[1]]
print(f"\n==== {len(results) - len(fails)}/{len(results)} passed ====")
sys.exit(1 if fails else 0)
