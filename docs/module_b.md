# B 模块：SQL 编译、查询计划与优化

> 2026-10-21：`SqlSession` 现在也可注入 `ContractRuntime`，运行真实 A 存储的 ABC 链路。计划仍使用 c_api，存储边界由显式适配处理。运行 `python -m edudb.sql_demo --integrated`，详见 [ABC 集成说明](abc_integration.md)。DDL 不允许夹在业务事务中。

B 把 SQL 文本编译成 **C 执行器可直接消费的 `edudb.c_api` 计划**。B 不执行查询、不访问磁盘、不做鉴权；它只负责词法/语法分析、字段绑定、类型检查、逻辑计划生成和基于规则的优化。

## 1. 职责与边界

| 做 | 不做 |
|---|---|
| 分词、语法树、字段绑定（解析为稳定 `slot_id`） | 不执行算子、不读写页、不碰 WAL（C 负责） |
| 用 A 的目录校验表/字段/类型 | 不做认证、授权、自动提交、会话串行（D 负责） |
| 生成 `PlanNode/Expr`（c_api 格式）、外连接补空、分组/聚合合法性、子查询绑定 | 不猜 SQL 语义；不定义页格式或目录（A 负责） |
| 优化开关 ON/OFF 产生不同**物理**计划、EXPLAIN | 不做基于代价的优化器、不做分布式/MVCC |

**接口格式决定（B/C 对齐）：** 项目里有两套 `Expr/PlanNode` —— `contracts.py`（A 的共享合同，`Expr(kind, properties, children)`）与 `c_api.py`（C 执行器消费，`Expr(kind, value, slot_id, operator, args, level, plan, distinct)`）。**C 的执行器实际消费 `c_api` 那套**（见 `edudb/c_demo.py`、`tests/test_c_execution.py`），因此 **B 直接产出 `c_api` 格式**，不再引入第二套转换。此决定已落实在全部代码与端到端测试里。

## 2. 代码位置

```text
edudb/sql/
  tokens.py      词法分析；字符串字面量对关键字/逗号/转义引号保持不透明
  ast.py         未绑定语法树节点
  parser.py      递归下降 + 优先级爬升表达式解析
  catalog.py     CatalogView 协议、内存目录（测试用）、RuntimeCatalog（接 C 运行时）
  planner.py     绑定 + 逻辑计划：名称→slot_id、类型检查、聚合/分组重写、关联子查询作用域栈
  optimizer.py   谓词下推、索引扫描选择、哈希连接
  compiler.py    顶层编排 compile_sql -> CompiledStatement；EXPLAIN 渲染
  driver.py      参考驱动 SqlSession（开发/演示用，非 D 的鉴权入口）
edudb/sql_demo.py  SQL 驱动的演示入口
tests/
  test_sql_parser.py      纯解析/词法（不依赖目录和运行时）
  test_sql_planner.py     绑定/计划/优化（内存目录）
  test_sql_end_to_end.py  经 C 真实执行器/WAL 的端到端，含与 SQLite 对拍
```

## 3. 处理流水线

```text
SQL 文本 → tokenize → parse(AST) → Planner(绑定+逻辑计划) → [optimizer] → CompiledStatement
                                        ↑ CatalogView（表/列/索引）
```

`compile_sql(sql, catalog, CompileOptions(optimizer_enabled))` 返回 `CompiledStatement`：

| 字段 | 说明 |
|---|---|
| `kind` | SELECT/INSERT/UPDATE/DELETE/CREATE_TABLE/CREATE_INDEX/BEGIN/COMMIT/ROLLBACK/EXPLAIN |
| `plan` | c_api `PlanNode`（查询与 DML），交给 `executor.execute(plan, ctx)` |
| `output_columns` | 结果列（`ResultColumn`，带稳定 slot_id） |
| `table_schema`/`constraints` | CREATE TABLE 的 `models.TableSchema` 与 `c_api.Constraint` |
| `index_spec` | CREATE INDEX 的 `c_api.IndexSpec` |
| `required_privileges` | `(object_type, object_id, action)` 列表，供 D 做授权 |
| `optimized` / `explain_text` | 是否启用优化；EXPLAIN 的计划树文本 |

B 只“描述要做什么”，执行由 D 的请求处理器（或参考驱动 `SqlSession`）分派。

## 4. 支持的 SQL 子集

| 范围 | 内容 |
|---|---|
| DDL | `CREATE TABLE`（列类型 INT/FLOAT/TEXT/BOOL，`NOT NULL`、列级/表级 `PRIMARY KEY`、`UNIQUE`、`CHECK(expr)`、单列 `FOREIGN KEY ... REFERENCES`）、`CREATE [UNIQUE] INDEX ... ON t(col)` |
| 查询 | `SELECT [DISTINCT]`，`*` / `表.*` / 表达式 `AS` 别名；`FROM` 单表或 `INNER/LEFT/RIGHT/FULL/CROSS JOIN ... ON`；`WHERE`；`GROUP BY`/`HAVING`；`ORDER BY`（多键、ASC/DESC、NULLS FIRST/LAST、序号）；`LIMIT/OFFSET` |
| 表达式 | 字面量、列引用（限定/非限定）、算术、比较、`AND/OR/NOT`、括号、`IS [NOT] NULL`、`BETWEEN`、`IN(列表)`、`IN(子查询)`、`EXISTS`、标量子查询（支持关联，用作用域层级 `level`） |
| 聚合 | `COUNT(*)`、`COUNT/SUM/AVG/MIN/MAX(expr)`、`COUNT(DISTINCT expr)`；空输入/NULL 语义与 SQLite 对拍一致 |
| DML | `INSERT ... VALUES`（多行，常量表达式）、`UPDATE ... SET ... [WHERE]`、`DELETE FROM ... [WHERE]` |
| 事务/工具 | `BEGIN`/`START TRANSACTION`、`COMMIT`、`ROLLBACK`、`EXPLAIN` |

## 5. 绑定与类型规则

- 标识符按 A 的 `identifier()` 规则小写化；字符串字面量保持原样。
- 字段绑定用 A 的 `CatalogView.get_table`；**空表上引用不存在的列也必定报错**（绑定基于结构，与行数无关）。不存在表 → `UNKNOWN_TABLE`；不存在列 → `UNKNOWN_COLUMN`；跨表同名列未限定 → `AMBIGUOUS_COLUMN`。
- 列引用绑定成稳定 `slot_id`（`别名.列名`）；别名/连接/子查询用独立 slot，不用名字字典表示结果。JOIN 输出严格等于左右子列拼接（slot_id 与 type 一致），别名必须唯一。
- 类型：`WHERE/ON/HAVING` 必须是 BOOL/NULL；算术要求数值；比较要求同基类型或数值互比；`SET`/`INSERT` 赋值做类型相容检查（NULL 放行，最终仍由 A 校验）。
- 分组查询里 `SELECT/HAVING/ORDER BY` 的非聚合子表达式必须能由 GROUP BY 键组成，否则 `GROUPING_ERROR`；`WHERE` 不得含聚合。

## 6. 优化器（ON/OFF 产生不同物理计划）

关闭优化（基线）：`SeqScan` + `NestedLoopJoin` + `Filter`。开启优化按规则改写，语义不变（语义由 C 执行器定义）：

| 规则 | 行为 | 正确性保证 |
|---|---|---|
| 谓词下推 | 把 `WHERE` 的 AND 合取项下推到相关子树 | 仅下推到外连接的**保留侧**（LEFT 推左、RIGHT 推右、INNER 两侧）；含子查询的合取项保留在连接之上 |
| 索引扫描 | `Filter(SeqScan)` 中对已建索引列的等值/范围谓词改写为 `IndexScan`，剩余谓词留作残余 `Filter` | 等值用 `[v,v]`，`</<=/>/>=` 建单边界；NULL 不入索引，谓词也不匹配 NULL 行，结果一致 |
| 哈希连接 | `NestedLoopJoin` 的 ON 中含等值连接键 `左列=右列` 时改写为 `HashJoin`，并保留完整 ON 作残余校验 | 哈希键取自 ON 等值项；完整条件仍由执行器复核 |

`applied_rules` 记录命中的规则，`EXPLAIN` 展示物理形态。优化对比协议（同数据集/SQL/缓冲容量、冷热缓存分离、结果先等价再比性能）见 [需求](requirements.md) §4。

## 7. 运行与演示

从仓库根目录：

```powershell
python -m unittest discover -s tests -v
python -m edudb.sql_demo
```

演示默认用临时目录；`--db <新空目录>` 可保留数据（不自动重置已有数据）。

## 8. 测试证据

`tests/test_sql_parser.py`（13）、`tests/test_sql_planner.py`（17）、`tests/test_sql_end_to_end.py`（10），共 **40 项**；并入全量回归 **189 项通过**（Windows / Python 3.13，约 8 秒）。覆盖：

- 字符串里关键字/逗号/转义引号不被当作语法分隔符；注释；运算符优先级；错误带位置；不支持语法（DROP、`INSERT ... SELECT`、多语句）被明确拒绝。
- 未知表/列（含空表）、歧义列、类型错误、分组合法性、索引选择、外连接保留侧下推、哈希连接、ORDER BY 序号/别名、DISTINCT 规则、权限上报。
- 经 C 真实执行器：建表→插入→查询→排序→改→删→重开持久化；四种 JOIN 与聚合（含 NULL）与 SQLite 对拍；优化 ON/OFF 结果等价且冷缓存下 `IndexScan` 实测减少数据页读取；`BEGIN/ROLLBACK` 恢复；`CHECK/UNIQUE/FOREIGN KEY` 违反报错且中止事务。

`test_sql_parser`/`test_sql_planner` 不依赖运行时（纯逻辑可独立讲解），`test_sql_end_to_end` 证明与 C 的真实联调。

## 9. 明确限制

1. 仅产出 `c_api` 计划；未提供到 `contracts.Expr` 的转换层（如正式统一到 contracts，需 B/C 评审后新增显式转换，不得直接互换字段）。
2. 不支持：派生表（FROM 子查询）、`UNION`/集合运算、`DROP`/`ALTER`、`CREATE VIEW`、`INSERT ... SELECT`、窗口函数、多列索引/多列主键、`LIKE`/排序规则、一次多条语句。遇到即报 `SYNTAX_ERROR` 或明确错误码，不静默误解析。
3. 优化器是**规则式**，非代价式：无统计/直方图、无连接重排、无子查询去关联；索引仅用于单列等值/范围且谓词形如 `列 比较 常量`。不预设“优化一定更快”。
4. `INSERT` 值限常量表达式（字面量、一元正负、常量算术），不接受列引用/子查询。
5. 参考驱动 `SqlSession` 仅供开发与演示：不做认证授权，`BEGIN/COMMIT/ROLLBACK` 的会话归属与自动提交由 D 的正式请求入口实现。
6. 绑定的 `nullable` 为保守估计，仅用于展示；执行语义以 C 为准。
