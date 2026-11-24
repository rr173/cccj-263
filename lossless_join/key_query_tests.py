#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量主键查询入口的端到端断言。

运行：python3 -I key_query_tests.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(ROOT, "tool", "key_query.py")
ACCEPT = os.path.join(ROOT, "tool", "lossless_accept.py")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), name, detail)


def run_cli(*args):
    p = subprocess.run([sys.executable, "-I", TOOL, *args],
                       capture_output=True, text=True)
    parsed = None
    text = p.stdout.strip() or p.stderr.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        pass
    return p.returncode, parsed, p.stdout, p.stderr


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def query_case(case_dir, delivery, keys, key_cols=("id",), extra_args=()):
    dp = os.path.join(case_dir, "delivery.json")
    kp = os.path.join(case_dir, "keys.json")
    fp = os.path.join(case_dir, "out", "fact.json")
    rp = os.path.join(case_dir, "out", "query_report.json")
    write_json(dp, delivery)
    write_json(kp, keys)
    rc, cli_rep, out, err = run_cli(
        "query", "--delivery", dp, "--keys", kp,
        *sum((["--key-col", c] for c in key_cols), []),
        "--fact-out", fp, "--report-out", rp, *extra_args)
    rep = read_json(rp) if os.path.exists(rp) else cli_rep
    fact = read_json(fp) if os.path.exists(fp) else None
    return rc, rep, fact, fp, rp, err


def main():
    tmp_root = tempfile.mkdtemp(prefix="key-query-tests-", dir=ROOT)
    try:
        # ------------------------------------------------------------ Q01
        d1 = {
            "tables": [
                {"name": "K", "columns": ["id", "a"],
                 "rows": [[1, "x"], [2, "y"], [1, "x"]]},
                {"name": "A", "columns": ["id", "u"],
                 "rows": [[1, "u1"], [1, "u2"], [2, "u3"]]},
                {"name": "B", "columns": ["id", "v"],
                 "rows": [[1, "v1"], [1, "v2"], [2, "v4"], [9, "ghost"]]},
            ],
            "tree": [["K", "A"], ["K", "B"]],
        }
        case = os.path.join(tmp_root, "q01")
        rc, rep, fact, fp, rp, err = query_case(
            case, d1, {"keys": [1, 2, 3, 1]})
        check("Q01 查询退出码0", rc == 0, err)
        check("Q01 列目录为首声明并集",
              fact and fact["columns"] == ["id", "a", "u", "v"])
        check("Q01 事实行=5且确定排序", fact and fact["rows"] == [
            [1, "x", "u1", "v1"], [1, "x", "u1", "v2"],
            [1, "x", "u2", "v1"], [1, "x", "u2", "v2"],
            [2, "y", "u3", "v4"]], json.dumps(fact))
        check("Q01 请求4原始/去重3/重复1", rep and (
            rep["submitted_key_count"], rep["requested_key_count"],
            rep["duplicate_request_count"]) == (4, 3, 1))
        by = {tuple(x["key_values"]): x for x in rep["results"]}
        check("Q01 逐键计数 4/1/0", rep and (
            by[(1,)]["row_count"], by[(2,)]["row_count"],
            by[(3,)]["hit"]) == (4, 1, False), json.dumps(rep))
        check("Q01 总数字段一致", rep and rep["total_row_count"] == 5
              and rep["returned_row_count"] == 5)
        check("Q01 未命中键为3", rep and rep["missing_keys"] == [{"id": 3}])

        # 请求全部真实连接键时，生成事实表可直接通过无损验收。
        accept_rep = os.path.join(case, "accept.json")
        p = subprocess.run([
            sys.executable, "-I", ACCEPT, "accept", "--fact", fp,
            "--delivery", os.path.join(case, "delivery.json"),
            "--out", accept_rep], capture_output=True, text=True)
        ar = read_json(accept_rep)
        check("Q01 生成事实行可直接作为验收入口原表并 ACCEPTED",
              p.returncode == 0 and ar["status"] == "ACCEPTED"
              and ar["verdict"]["sets_equal"] is True, p.stderr)

        # ------------------------------------------------------------ Q02
        # 空命中合法，输出空 rows，而不是报错或省略文件。
        case = os.path.join(tmp_root, "q02_empty")
        rc, rep, fact, fp, rp, err = query_case(case, d1, {"keys": [404]})
        check("Q02 空命中退出0", rc == 0, err)
        check("Q02 fact rows=[]", fact == {"columns": ["id", "a", "u", "v"],
                                          "rows": []})
        check("Q02 报告计数", rep and rep["total_row_count"] == 0
              and rep["missing_key_count"] == 1
              and rep["results"][0]["hit"] is False)

        # 空批次也按确定的空结果处理。
        case = os.path.join(tmp_root, "q02b_empty_batch")
        rc, rep, fact, _, _, err = query_case(case, d1, {"keys": []})
        check("Q02b 空批次合法且全零", rc == 0 and fact["rows"] == []
              and rep["requested_key_count"] == 0
              and rep["results"] == [], err)

        # ------------------------------------------------------------ Q03
        # 显式 null 是可相等字面值，可作为请求键。
        dnull = {
            "tables": [
                {"name": "T0", "columns": ["id", "a"],
                 "rows": [[None, "n"], [1, "x"]]},
                {"name": "T1", "columns": ["id", "b"],
                 "rows": [[None, "b0"], [1, "b1"]]},
            ],
            "tree": [["T0", "T1"]],
        }
        case = os.path.join(tmp_root, "q03_null")
        rc, rep, fact, _, _, err = query_case(
            case, dnull, {"key_columns": ["id"], "keys": [None]})
        check("Q03 null 键命中", rc == 0
              and fact["rows"] == [[None, "n", "b0"]]
              and rep["results"][0]["hit"] is True, err + json.dumps(fact))

        # ------------------------------------------------------------ Q04
        # 复合键跨两张表；必须经共享 x 传播相关性。
        dcomp = {
            "tables": [
                {"name": "A", "columns": ["a", "x"],
                 "rows": [[1, "x1"], [2, "x2"]]},
                {"name": "B", "columns": ["x", "b"],
                 "rows": [["x1", "b1"], ["x1", "b2"], ["x2", "b3"]]},
            ],
            "tree": [["A", "B"]],
        }
        case = os.path.join(tmp_root, "q04_composite")
        rc, rep, fact, _, _, err = query_case(
            case, dcomp,
            [{"a": 1, "b": "b2"}, {"a": 1, "b": "b3"}],
            key_cols=("a", "b"))
        check("Q04 跨表复合键只命中相容相关值", rc == 0
              and fact["rows"] == [[1, "x1", "b2"]]
              and [x["hit"] for x in rep["results"]] == [True, False],
              err + json.dumps(fact))

        # ------------------------------------------------------------ Q05
        # 共享接口不相容时，不能把 B 的 y=p 与 C 的 y=q 局部值拼起来。
        dbadif = {
            "tables": [
                {"name": "A", "columns": ["id", "x"], "rows": [[1, 7]]},
                {"name": "B", "columns": ["x", "y"], "rows": [[7, "p"]]},
                {"name": "C", "columns": ["y", "z"], "rows": [["q", 9]]},
            ],
            "tree": [["A", "B"], ["B", "C"]],
        }
        case = os.path.join(tmp_root, "q05_interface")
        rc, rep, fact, _, _, err = query_case(case, dbadif, {"keys": [1]})
        check("Q05 接口不相容返回空且不拼局部值", rc == 0
              and fact["rows"] == [] and rep["total_row_count"] == 0, err)

        # 相容接口时同链可返回。
        dok = json.loads(json.dumps(dbadif))
        dok["tables"][2]["rows"] = [["p", 9]]
        case = os.path.join(tmp_root, "q05b_interface_ok")
        rc, _, fact, _, _, err = query_case(case, dok, {"keys": [1]})
        check("Q05b 接口相容返回完整行", rc == 0
              and fact["rows"] == [[1, 7, "p", 9]], err + json.dumps(fact))

        # ------------------------------------------------------------ Q06
        # 非法键列/值整批拒收，且不留下新的 fact/report。
        case = os.path.join(tmp_root, "q06_bad_col")
        rc, rep, fact, fp, rp, err = query_case(
            case, d1, {"keys": [1]}, key_cols=("missing",))
        check("Q06 未知键列 rc2", rc == 2 and rep and rep["status"] == "REJECTED")
        check("Q06 失败无输出文件", not os.path.exists(fp) and not os.path.exists(rp))

        case = os.path.join(tmp_root, "q06b_bad_value")
        rc, rep, fact, fp, rp, err = query_case(
            case, d1, {"keys": [{"id": True}]})
        check("Q06b bool 键值拒收", rc == 2 and rep["status"] == "REJECTED", err)
        check("Q06b 无半成品", not os.path.exists(fp) and not os.path.exists(rp))

        # ------------------------------------------------------------ Q07
        # 已有成功输出在失败查询后必须保留；新路径失败不得产生文件。
        case = os.path.join(tmp_root, "q07_atomic")
        os.makedirs(case, exist_ok=True)
        fp, rp = os.path.join(case, "fact.json"), os.path.join(case, "report.json")
        write_json(fp, {"old": True})
        write_json(rp, {"old": True})
        dp = os.path.join(case, "delivery.json")
        kp = os.path.join(case, "bad.json")
        write_json(dp, d1)
        write_json(kp, {"keys": [1.25]})
        rc, _, _, err = run_cli(
            "--delivery", dp, "--key-col", "id", "--keys", kp,
            "--fact-out", fp, "--report-out", rp)
        check("Q07 非法查询 rc2", rc == 2, err)
        check("Q07 旧成组输出保留", read_json(fp) == {"old": True}
              and read_json(rp) == {"old": True})

        # ------------------------------------------------------------ Q08
        # 相同输入重复执行字节一致。
        case = os.path.join(tmp_root, "q08_idem")
        rc1, _, fact1, fp1, rp1, err = query_case(case, d1, {"keys": [1, 2, 3, 1]})
        bytes_f1 = open(fp1, "rb").read()
        bytes_r1 = open(rp1, "rb").read()
        rc2, _, fact2, fp2, rp2, err = query_case(case, d1, {"keys": [1, 2, 3, 1]})
        check("Q08 两次成功", rc1 == rc2 == 0, err)
        check("Q08 fact/report 字节一致",
              bytes_f1 == open(fp2, "rb").read()
              and bytes_r1 == open(rp2, "rb").read())

        # ------------------------------------------------------------ Q09
        # 兼容入口 batch_key_query.py 与位置参数/内嵌 key_columns。
        alias_tool = os.path.join(ROOT, "tool", "batch_key_query.py")
        case = os.path.join(tmp_root, "q09_alias")
        os.makedirs(case, exist_ok=True)
        dp = os.path.join(case, "delivery.json")
        kp = os.path.join(case, "keys.json")
        outdir = os.path.join(case, "out")
        write_json(dp, d1)
        write_json(kp, {"key_columns": ["id"], "keys": [2]})
        p = subprocess.run([
            sys.executable, "-I", alias_tool, dp, kp,
            "--out-dir", outdir, "--quiet"], capture_output=True, text=True)
        f = read_json(os.path.join(outdir, "fact.json"))
        check("Q09 别名入口/位置参数/内嵌键列可用",
              p.returncode == 0 and len(f["rows"]) == 1
              and f["rows"][0][0] == 2, p.stderr)

        # lossless_accept query 兼容子命令。
        p = subprocess.run([
            sys.executable, "-I", ACCEPT, "query", "--delivery", dp,
            "--keys", kp, "--out-dir", os.path.join(case, "accept_q"),
            "--quiet"], capture_output=True, text=True)
        check("Q09 lossless_accept query 兼容子命令",
              p.returncode == 0 and os.path.exists(
                  os.path.join(case, "accept_q", "fact.json")), p.stderr)

    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    fails = [r for r in results if not r[1]]
    print(f"\n==== {len(results) - len(fails)}/{len(results)} passed ====")
    if fails:
        for name, _, detail in fails:
            print("FAILED:", name, detail)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
