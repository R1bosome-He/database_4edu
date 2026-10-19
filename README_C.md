# C：执行引擎、B+ 树、事务与恢复

## 运行

在仓库根目录（包含 `edudb/` 的目录）下执行：

```powershell
python -m unittest discover -s tests -v
python -m edudb.c_demo
```

演示默认使用临时目录；若要保留数据，可运行 `python -m edudb.c_demo --db .c_demo_data`。指定目录必须新建或为空，不自动删除、重置已有数据。演示直接构造计划，不假装已经实现 SQL 解析器或优化器。

Windows 沙箱若不能使用系统临时目录，可以先设置项目内的临时目录：

```powershell
New-Item -ItemType Directory -Force .test_tmp | Out-Null
$env:TEMP=(Resolve-Path .test_tmp).Path
$env:TMP=$env:TEMP
```

## 已实现

| 部分 | 内容 |
|---|---|
| 执行 | Values、SeqScan、IndexScan、Filter、Projection、Sort、Distinct、Limit、Aggregate |
| 连接 | NestedLoopJoin、HashJoin；INNER/LEFT/RIGHT/FULL；保留重复行；NULL 连接键不相等 |
| 表达式 | 比较、算术、布尔、括号对应的树结构、IS NULL/IS NOT NULL、三值逻辑、短路求值 |
| 聚合 | COUNT(*)、COUNT(expr)、SUM/AVG/MIN/MAX、聚合 DISTINCT、空输入和 NULL 分组；HAVING 使用聚合后的 Filter |
| 子查询 | 标量、EXISTS/NOT EXISTS、IN/NOT IN、相关外层列；标量多行明确报错 |
| 修改 | 插入、更新、删除；赋值表达式基于旧行同时求值；先确定修改目标；失败中止整个事务 |
| 约束 | A 的类型、主键、NOT NULL；参考目录扩展 UNIQUE/CHECK/外键；外键引用单列主键，立即 RESTRICT，无级联 |
| 索引 | 单列 B+ 树；重复键、等值/范围、插入分裂、删除借位/合并、根收缩、叶链；节点持久化及修改后增量维护 |
| 事务 | BEGIN/COMMIT/ROLLBACK、会话归属、严格两阶段表级 S/X 锁、锁升级、等待和超时中止；每个事务同时只执行一条语句 |
| 恢复 | 带长度及 CRC 的 WAL、事务前向/回溯链、物理页前后镜像、日志先落盘、page_lsn、REDO/UNDO、CLR 可续撤销、静止检查点 |
| 指标 | 执行耗时、返回行数、真实数据/索引页读取、写页、缓存命中/缺失/淘汰、逐算子输入输出/扫描/连接/哈希次数 |
| 运行边界 | 单写进程保护、线程内多会话、取消检查、物化内存预算、批量算子、JSON 计划转换 |

## 代码位置

```text
edudb/
  c_api.py                   C 公共模型、协议、错误、JSON 转换
  execution/
    expressions.py           SQL 表达式和 NULL 语义
    executor.py              算子、数据修改、约束、指标与内存预算
  index/
    bplus.py                 独立 B+ 树算法及结构验证
    manager.py               节点页持久化、索引目录、记录索引维护
  transactions/
    wal.py                   帧式日志、校验、落盘、残尾处理
    manager.py               事务、表锁、提交回滚、恢复和检查点
  integration/
    store.py                 A/C 参考页后端和 RecordStore 适配
    runtime.py               独立组装、日志化目录、启动恢复与关闭
  c_demo.py                  C 专用演示入口
tests/
  test_c_execution.py
  test_c_index.py
  test_c_transactions.py
```

## 与 A/B/D 的接入

- A：提供 `c_api.PageStore` 语义及 `RecordAccess`。当前原始 PageIO 没有 page_lsn 和索引页，不能直接声明满足事务接口。`integration/` 是可替换的参考实现，正式 A 缓冲池接入后运行同一套 C 测试。
- B：生成 `PlanNode/Expr`，完成名称、类型和分组合法性检查，展开视图。C 不重新解析 SQL，也不决定优化开关；ON/OFF 体现为 B 提供的不同物理计划。
- D：认证授权后创建上下文、串行处理同一会话请求、管理自动提交，收到结果后提交成功才向用户确认。C 低层接口仅限内部调用，不能作为公开 HTTP 接口。

## 明确限制

1. 原始 A 的 v1 页及目录保持原样。参考运行时使用 `c_pages_v1/` 的 4KB EDC1 页，增加 page_lsn、数据/索引/目录载荷及删除墓碑。两者不可混读；正式迁移或替换需 A 评审。
2. 参考缓冲为写穿 LRU，不冒充 A 已完成脏页缓冲池。所有 C 数据/索引读写经过同一页后端。元数据访问与 WAL I/O 不计入数据页查询指标，目录枚举的文件系统成本不计作页读取。
3. B+ 树查询按根到叶路径读取，范围查询从下界沿叶链扫描。v1 的修改操作为校验和易懂性仍加载整棵树，并重算分隔键，只写发生变化的节点；后续应改为局部修改路径，不能宣称所有范围查询总开销更小。
4. 索引目录、单表结构目录、约束目录及索引超级块暂为各一个 4KB 页，过大会明确失败并回滚。重复键每个 RID 独立占一个叶条目；NULL 不入普通索引，IS NULL 应由 B 选择顺序扫描。
5. 排序、聚合、嵌套连接右侧和索引工作集采用内存算法；无外部排序/哈希溢写。预算为保守估算，并非进程 RSS 硬限制。WAL 和事务历史保留内存及磁盘历史，尚未实现归档、裁剪或长期服务回收。
6. 锁粒度为表级，索引目录变更采用目录锁，会限制不同表的写入并发。超时用于打破死锁，不实现等待图检测、公平调度或多进程并发。
7. 参考建表与建索引有 WAL 保护；不实现 SQL DDL 调度、视图定义/展开、DROP、权限及视图写入。完整视图功能属于 A/B/D 集成；C 可执行展开后的计划。
8. CHECK 仅支持行内标量表达式；外键暂支持单列主键引用、MATCH SIMPLE 和立即 RESTRICT。自引用删除保守拒绝，不实现延迟约束、级联或复杂批量键置换。
9. 检查点要求入口暂停接收事务，当前有活动事务时报错，不自动等待。保留全 WAL，启动从头分析；恢复统计的 redone 是重放记录数，不保证等于实际重写页数。
10. 已验证普通进程异常退出和恢复中断后的重启语义；未模拟真实断电、损坏磁盘、目录同步失败或 WAL 丢失。完整 WAL 帧校验失败会拒绝启动，不静默丢弃。提交 fsync 失败属于结果不确定，需要恢复后确认。

不把这些限制写成已完成，也不把独立 C 测试当作整套数据库联调验收。
