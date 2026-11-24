# 窄表无损回拼验收 / 费用分摊台账 / 按周滚动发布 / 批量主键查询工具

本项目提供四个相互独立的离线入口，共享同一套窄表协议（表与连接树格式、
空值字面相等、RIP、整行集合语义）：

1. **验收入口** `tool/lossless_accept.py`：给定完整原事实表，判定窄表回拼是否无损；
2. **台账入口** `tool/fee_ledger.py`：**不需要原事实表**，直接从窄表生成按业务
   分组的费用台账与待分摊清单；
3. **滚动发布入口** `tool/delivery_rollforward.py`：给定上周事实表与交付包及一批
   按业务行键定位的变更，应用变更并发布下一版窄表快照与变更报告（见第二节）；
4. **批量主键查询入口** `tool/key_query.py`：只有交付窄表包和一批主键时，导出这些
   键对应的完整事实行集合与逐键查询报告（见第三节）。

---
# 一、无损回拼验收（`lossless_accept.py`）

给定**完整原事实表**与供应商**实际拟交付的若干窄表**（含各自声明的列集合、实际数据，
以及连接树），离线判定按连接树回拼后的整行集合是否与原表**完全相等**，并分别独立给出：

- **缺行（原行拼不回）**：原表存在、但回拼结果中不存在的事实；
- **造行（凭空拼出）**：原表不存在、但回拼能拼出的完整组合。

两类问题分别计数、同时存在时互不抵销。工具**不枚举、不落地回拼结果**，
也不物化可能远大于全部输入的中间拼接行集；回拼行数用精确大整数给出。

## 1. 判定模型

协议规定：

- 每个源列至少在一张交付表中出现；凡含同名列的表节点在连接树上必须连成一块
  （**运行交集性质 RIP**）；
- 相邻节点按所有共享列做等值对齐；没有共享列的边不限制（交叉连接）；
- 值仅支持整数、字符串、显式空值 `null`；**空值是可相等的字面值**
  （`null == null` 成立，不采用 SQL 的三值逻辑）；
- 以**整行集合**为准，重复行不增加份数。

RIP 下成立关键等价关系：一个全量行元组能被回拼出来，当且仅当它在**每张**窄表中
都存在一个在该表列上完全相容的行——局部相容沿树传播即全局相容，无需枚举全局组合。

### 精确计数（不物化拼接结果）

对连接树自底向上，为每个节点 `u` 维护映射

```
f_u(键) = 以 u 为根的子树，在 u 与父节点共享列的“该实际键值”上可产生的不同完整拼接行数
```

- 叶节点：每个键的计数为该键在表中的去重行数；
- 内部节点：对本表每行，取各孩子子树在对应接口键上的计数相乘，再按本节点输出键汇总；
- 无共享列的边（交叉连接）退化为单键常量乘数；
- 映射的键**只来自输入中实际出现的值**，因此全部中间结构规模受输入行数约束，
  与隐式连接结果大小无关；计数使用 Python 任意精度整数。

根处求和即回拼结果的精确行数 `|J|`。

### 三类集合与两个独立结论

- `I = J ∩ F`（交集，逐表局部相容判定，RIP 保证正确）；
- 缺行数 `= |F| − |I|`；造行数 `= |J| − |I|`；
- 两者可同时为正，不做抵销；`sets_equal ⇔ 两者均为 0`。

### 造行证据：计数引导的剪枝搜索（仍不枚举回拼表）

沿树的前序逐表选行，维护：

- `N`：当前已选前缀下，剩余子森林还能产生的不同回拼数（由 `f` 映射直接算出）；
- `G`：其中能落在原表集合中的元组数（用预计算的“窄表行→相容原表行”求交）。

`N > G` 当且仅当当前前缀下必然存在造行。搜索始终沿 `N>G` 的分支深入，并强制
相邻表所选行在共享列上逐值相容，因此复杂度只随输入规模增长，绝不遍历整个回拼结果；
最终给出一条实际可拼成、且确实不在原表中的完整行，以及各窄表支持它的**行坐标**
（表内去重行号 + 该去重行在输入文件中的全部原始下标）。

## 2. 输入格式

### 原表 `fact.json`

```json
{
  "columns": ["a", "b", "c"],
  "rows": [[1, "x", 10], [2, null, 20]]
}
```

### 交付文件 `delivery.json`

```json
{
  "tables": [
    {"name": "T1", "columns": ["a", "b"], "rows": [[1, "x"]]},
    {"name": "T2", "columns": ["b", "c"], "rows": [["x", 10]]}
  ],
  "tree": [["T1", "T2"]]
}
```

- `tree` 为表名无向边数组；必须恰好连通且无环（n-1 条边）；
- 不假设窄表是原表的精确投影——增行、删行、重复、脏数据都按实际数据判定。

### 非法即拒绝验收（REJECTED，不出判定结论）

JSON/结构错误、非树（不连通、自环、重复边、未知表）、源列未被任何表声明、
同名列节点未连成一块（违反 RIP）、同名列出现冲突类型、不支持的值类型
（浮点、布尔、对象等）、行宽不符、列名重复等。

重复行不属非法：按集合语义去重后验收，并在报告 `warnings` 中给出 `NOTICE`。

## 3. 用法

所有读取输入数据的命令均用 `python3 -I` 运行（隔离工作目录，防止输入目录中的
同名模块被导入）。

```bash
# 验收（退出码 0=ACCEPTED，2=REJECTED）
python3 -I tool/lossless_accept.py accept \
    --fact cases/01_clean/fact.json \
    --delivery cases/01_clean/delivery.json \
    --out report.json

# 单独复核报告中的实际行证据（退出码 0=VERIFIED，3=VERIFICATION_FAILED）
python3 -I tool/lossless_accept.py verify-report \
    --fact cases/01_clean/fact.json \
    --delivery cases/01_clean/delivery.json \
    --report report.json
```

`verify-report` 不重新验收，只核对报告引用的证据：缺行原行是否真实存在、
所报缺失片段是否等于该原行投影且确实不在对应窄表中；造行是否确实不在原表、
各证人坐标是否指向窄表真实行、合并后是否恰好得到所报拼成行。篡改报告会被驳回。

## 4. 报告字段

```json
{
  "status": "REJECTED",
  "verdict": {
    "fact_row_count": 3,
    "reconstructed_distinct_row_count": 6,
    "intersection_row_count": 3,
    "missing_row_count": 0,
    "spurious_row_count": 3,
    "sets_equal": false
  },
  "missing_evidence": null,
  "spurious_evidence": {
    "assembled_row": [1, "x", 30],
    "fact_columns": ["a", "b", "c"],
    "witnesses": [
      {"table": "T1", "table_index": 0, "row_number": 0,
       "row_indices_in_file": [0], "row": [1, "x"], "columns": ["a", "b"]},
      {"table": "T2", "table_index": 1, "row_number": 2,
       "row_indices_in_file": [2], "row": ["x", 30], "columns": ["b", "c"]}
    ]
  }
}
```

缺行证据给出一条实际原行与缺失交付片段坐标（哪张表、应有哪些列、应有的行值）；
造行证据给出一条实际拼成的完整行与各窄表支持行坐标。报告不包含回拼表本身。

## 5. 费用分摊台账入口（`fee_ledger.py`，不需要原事实表）

财务下游只能拿到供应商拆开的窄表时，直接从窄表按业务分组出具费用台账。
列目录取各窄表**所声明列的并集**，输入中没有原事实表。

### 调用者配置

| 参数 | 含义 |
| --- | --- |
| `--doc-table` | 单据所在窄表 |
| `--doc-id-col` | 单据编号列（编号不能为空；同编号的完全重复单据行只算一次，同编号不同单据行直接拒收） |
| `--amount-col` | 有符号整数金额列，单位为**分**；只能出现在单据表，不能兼任分组列或分摊单元列 |
| `--unit-col` | 分摊单元列（一个） |
| `--group-col` | 分组列，可重复指定；参数先后顺序即有序分组列清单，也是平手裁决顺序 |

窄表仍按既有协议校验列、值与连接树（非树、RIP 破坏、同名列类型冲突、
不支持的值类型、行宽不符、列名重复等一律拒收）；空值是可相等的字面值。

### 归属与权重

只有能在**全部窄表**中同时找到相容行的关系（即存在一条完整拼接，RIP 下等价于
逐表局部相容）才算有效归属。对一张单据 `d`、一个分组 `g`：

```
weight(d, g) = 能与 d 同时相容的 *不同分摊单元值* 的数量
```

- 同一单元经多条路径或多种无关明细出现只算一次；
- 一个单元确实落在多个分组里时，在各分组各计一次；
- 没有任何有效归属的单据整笔列入**待分摊**，不猜分组。

### 分摊规则（逐单据独立）

1. 以 `|金额| × 该组权重 / 权重总和`，先对每组向下取整到分；
2. 余下的分按小数余数从大到小，每组补一分；
3. 余数平手时，按调用者声明的分组列顺序逐列比较分组值：
   空值排最前，整数按数值，字符串按 Unicode 码点；
4. 负金额对同样的分配结果整体取负；
5. 有有效归属的分组即使合计为零也保留。

### 不膨胀保证

传播时只保留 `分组列 ∪ {分摊单元列}` 上的**不同投影**，无关明细列与重复路径在
集合语义下自然消失；中间结构规模只随确实存在的不同「单据、分组、分摊单元」
组合增长，既不枚举完整拼接，也不生成随无关明细组合膨胀的中间行集，更不逐一
试探三者值域的笛卡尔积（沿连接树按实际行的相容键传播，不相通的单据/分组/单元
不会相遇）。

### 用法与输出

```bash
python3 -I tool/fee_ledger.py ledger \
    --delivery cases/ledger_01_main/delivery.json \
    --doc-table DOC --doc-id-col id --amount-col amt \
    --unit-col unit --group-col bu --group-col team \
    --out ledger.json
```

```json
{
  "ledger_version": "1.0",
  "status": "OK",
  "config": { "...": "回显调用配置" },
  "column_directory": ["id", "amt", "bu", "unit", "team"],
  "group_totals": [
    {"group": ["bu1", "team1"], "amount_cents": 667},
    {"group": ["bu2", "team2"], "amount_cents": 333}
  ],
  "pending": [],
  "reconciliation": {
    "doc_count": 1,
    "distinct_doc_amount_total": 1000,
    "allocated_total": 1000,
    "pending_total": 0,
    "balanced": true
  },
  "warnings": []
}
```

`group_totals` 为按组汇总的整数金额（分，有符号）；`pending` 给出待分摊单据的
编号与原金额。恒等关系：`allocated_total + pending_total =
distinct_doc_amount_total`（去重后单据金额之和）。任何非法数据或配置都以
`status=REJECTED` 明确报错，**不输出部分台账**（`--out` 不会落盘）。

---
# 二、按周滚动发布（`delivery_rollforward.py`）

上游按周发布窄表快照：上周给出完整事实表与实际交付包，本周只给一批按业务行键
定位的变更。本入口在**不改动**上述两个入口的前提下，离线应用变更并发布下一版
窄表快照与变更报告。

## 1. 流程

1. **预检**：先用验收协议精确判定「上周事实表」与「上周交付包」是否表示同一份
   整行集合。上周交付若缺行、造行或结构非法，直接整批拒收（`stage=precheck`
   或 `stage=delivery`），**绝不静默修复后继续**。
2. **应用变更**：在同一套业务行键上应用 `insert` / `update` / `delete`，得到本周
   事实集合。业务键不允许空值，且在事实集合中必须唯一（否则无法按行键定位）。
3. **生成快照**：每张表恰好是本周事实行在其声明列上的投影，按集合语义去重。
   因此同一投影由多条事实行共同支持时，删除其中一条不会误删仍被其他事实行使用
   的交付行；修改连接键会同步改变所有声明该列的表。内部只维护
   「投影 → 支持它的去重事实行数」与本周事实字典，规模随事实行、变更行和实际
   受影响的投影行增长，**不生成完整连接结果**。
4. **后检**：用同一验收协议复核新包，把精确计数写入报告。若上周的列划分与连接
   树使「投影包」本身无法无损回拼（结构性问题，例如同一业务键的多值列被拆到
   两侧），工具**不会私自增删交付行**，而是如实写入 `postcheck` 与 `warnings`；
   需要「无损否则失败」时加 `--require-lossless`。
5. **原子落盘**：`delivery.json`、`fact.json`、`change_report.json` 作为一组原子
   发布；任一写入失败则回滚，不留下可被当作新版本的部分结果，原交付包保持可用。

## 2. 变更文件格式

```json
{"changes": [
  {"op": "insert", "row": [3, "w", 40]},
  {"op": "update", "key": {"b": "x"}, "row": [1, "x", 99]},
  {"op": "delete", "key": {"b": "z"}}
]}
```

- `op` 只能是 `insert` / `update` / `delete`；
- `row` 是完整行（与事实表同宽的数组，或按列名的对象）；
- `key` 是业务行键（`{列: 值}` 对象，或按 `--key-col` 顺序的数组）；
  `insert` 可省略 `key`（由 `row` 推导）；
- 新增必须是当前不存在的完整行；修改/删除的键必须当前存在；修改后的 `row`
  在业务键列上必须与原键一致（业务键不能改变）；
- 同一批次对同一业务键不得出现重复或冲突操作；
- 上述任一不满足、行宽/列名/值类型/操作结构非法、业务键为空，整批拒收
  （`stage=changes`），不写任何输出。

## 3. 用法

```bash
python3 -I tool/delivery_rollforward.py apply \
    --fact last_week/fact.json \
    --delivery last_week/delivery.json \
    --changes this_week/changes.json \
    --key-col b \
    --out-dir this_week
# 产物: this_week/delivery.json、this_week/fact.json、this_week/change_report.json
```

`--key-col` 可重复指定多个非空业务行键列；`apply` 亦可写作 `publish` /
`rollforward`。`--delivery-out` / `--fact-out` / `--report-out` 可分别覆盖默认
路径（不得与输入或彼此重叠）。新包可直接交给第一节的验收、第五节台账入口。

```bash
python3 -I tool/lossless_accept.py accept \
    --fact this_week/fact.json --delivery this_week/delivery.json
```

## 4. 报告字段（`change_report.json`）

- `inputs`：三个输入文件的 `sha256`（内容级）与提交操作数；
- `key_columns`：本次业务行键列；
- `precheck`：上周事实/交付的精确计数（`fact_row_count`、`intersection_row_count`、
  `missing_row_count`、`spurious_row_count`、`sets_equal`）与逐表投影核对；
- `changes`：`complete_applied`（是否完整应用）、`submitted_operation_count`、
  `accepted_operation_count`、`by_operation`、受影响的业务键清单
  （`affected_business_keys` 与计数）；
- `tables`：各交付表 `before/after` 行数、`added_distinct_row_count`、
  `removed_distinct_row_count`、`unchanged_row_count`；
- `postcheck`：新包按同一验收协议复核的精确计数；
- `outputs`：本周交付/事实内容哈希、逐表行数；
- `warnings`：结构性无损失败等提示。

相同输入重复运行得到相同的集合结果与**逐字节一致**的报告（报告不含时间戳或
绝对路径）。整批失败时以 `status=REJECTED`、`stage`、`reasons` 说明原因，
不写任何输出。

---

# 三、批量主键查询（`key_query.py`，不需要原事实表）

客服只有供应商交付的 `delivery.json`（窄表、连接树、列目录并集）和一批业务主键时，
可用本入口导出这些主键对应的完整事实行，并得到逐键查询报告。既有
`accept` / `verify-report` / `ledger` / `apply` 命令均保持不变。

## 1. 查询语义

- 至少指定一个主键列，可重复 `--key-col`；列名必须在交付包列目录中且不得重复；
- 主键值只允许整数、字符串、显式 `null`，并沿用空值字面相等协议；
- 同一主键重复提交只按一个请求键处理；
- 一条完整行只有在连接树的**每张**窄表中都存在投影相容的实际行时才返回；
- 相邻表按全部共享列对齐。共享接口不相容时，不会把不同分支的局部值拼成新行；
- 整行按集合去重；同一事实行即使由多条支持路径得到也只输出一份；
- 返回行的主键列元组必须等于某个去重后的请求键；
- 空命中是合法结果，事实文件为 `{"columns": [...], "rows": []}`，退出码仍为 0。

执行时给每个请求键分配一个位掩码，在树上做自底向上和自顶向下两次接口约束传播：
先剔除不能被请求键/子树/祖先支持的窄表行，再只枚举最终结果规模的行组合。
因此不会生成未请求键的整棵连接结果，也不会先物化可能爆炸的中间连接集合；
无共享列分支的组合只在它确实属于待写出的完整行时才逐行展开。

## 2. 主键文件

单列主键可以直接给标量数组：

```json
{"key_columns": ["id"], "keys": [1001, 1002, null, 1001]}
```

复合键可以给按 `--key-col` 顺序的数组，或列名对象：

```json
{
  "keys": [
    ["BU-1", "TEAM-A", 7],
    {"bu": "BU-2", "team": "TEAM-B", "version": 3}
  ]
}
```

也支持顶层直接为数组；`key_columns` 可写在命令行或主键文件中。布尔值、浮点数、
对象/数组（作为标量值）等违反协议的批次会整批拒收。

## 3. 用法与输出

```bash
python3 -I tool/key_query.py query \
    --delivery cases/q01/delivery.json \
    --key-col id \
    --keys keys.json \
    --out-dir query_out
# 默认成组写出 query_out/fact.json 与 query_out/query_report.json

# 也可分别指定路径：
python3 -I tool/key_query.py query \
    --delivery delivery.json --key-col bu --key-col team \
    --keys keys.json \
    --fact-out fact_rows.json \
    --report-out query_report.json
```

`query` 子命令可省略；`batch_key_query.py`、`key_lookup.py` 等路径保留为兼容入口，
`lossless_accept.py query ...` 也转发到同一实现。

事实行文件就是现有验收工具接受的原事实表格式：

```json
{
  "columns": ["id", "bu", "team", "amount"],
  "rows": [[1001, "BU-1", "TEAM-A", 1200]]
}
```

结果行按完整行的确定性值序排序（`null` 最前，整数按数值，字符串按 Unicode 码点）。
报告的核心字段为：

```json
{
  "status": "OK",
  "key_columns": ["id"],
  "requested_key_count": 3,
  "submitted_key_count": 4,
  "duplicate_request_count": 1,
  "total_row_count": 2,
  "hit_key_count": 1,
  "missing_key_count": 2,
  "results": [
    {"key": {"id": 1001}, "hit": true, "row_count": 2},
    {"key": {"id": 1002}, "hit": false, "row_count": 0}
  ],
  "missing_keys": [{"id": 1002}],
  "warnings": []
}
```

`results` 按规范键序排序；每个请求键都有一条 `hit` / `row_count` 记录。
报告同时提供 `key_results`、`missing_key_values` 等常见别名字段。
事实文件与报告通过同组临时文件和回滚逻辑原子发布；输入、键批次或查询校验失败时
退出码为 `2`，不写新的成功结果组，若目标路径已有上一版完整结果也会保留/回滚。
相同输入重复执行得到相同集合及逐字节一致的报告（不含时间戳或绝对路径）。

# 四、测试

```bash
python3 -I run_tests.py        # 验收：38 项端到端断言（13 个场景 + 证据篡改驳回）
python3 -I random_diff_test.py # 验收：120 个随机实例对朴素全枚举的差分测试 + 亿级隐式连接压力
python3 -I ledger_tests.py     # 台账：44 项端到端断言（手算分摊/待分摊/非法拒收/压力）
python3 -I ledger_diff_test.py # 台账：150 个随机实例对朴素全枚举的差分测试 + 3.4 亿隐式组合压力
python3 -I rollforward_tests.py    # 滚动发布：83 项端到端断言（预检/变更/投影/原子/幂等/压力）
python3 -I rollforward_diff_test.py # 滚动发布：150 个随机实例对朴素基准的差分测试
python3 -I key_query_tests.py      # 主键查询：26 项端到端断言（复合键/null/接口剪枝/原子/幂等/兼容入口）
```

台账压力场景：单据仅 1 张、4 张窄表共约 2100 行输入，三个无共享列分支各 700 行，
朴素完整拼接含 `700³ = 343,000,000` 个组合，工具在约 0.06 秒内出具台账，
且不产生任何拼接落地文件。

滚动发布压力场景：三张无共享列分支各 400 行的隐式连接（`400³ = 64,000,000`
组合），工具在约 0.06 秒内完成精确预检，不枚举、不落地连接结果。
