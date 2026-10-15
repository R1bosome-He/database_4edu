# A 模块：新版接口交接与验收

日期：2026-10-16。依据此前提供并读取的《数据库模块接口约定.md》实现 A 职责。共享定义为 `edudb/contracts.py`，入口为 `edudb.module_a.StorageService`。附件原微信临时路径在本次收尾时已不存在；本文是实现交接说明，不是原附件全文。

## 1. 完成范围

| 功能 | 实现方法 | 验收重点 |
| --- | --- | --- |
| 目录和统计 | `get_table/get_view/list_objects/get_statistics` | ID/名称分离、快照、owner、版本 |
| 元数据变更 | `apply_catalog_command` | 表/视图/单列索引创建删除；依赖检查；独立 DDL 日志事务 |
| 页固定释放 | `fetch_page/release_page` | READ 共享、WRITE 独占、固定页不可淘汰、释放后不可用 |
| 页分配 | `allocate_page` | 页与分配清单都记录日志，可撤销 |
| 记录读取 | `list_record_ids/read_record` | 列表值、稳定 RID，迭代不跨 yield 固定页 |
| 准备修改 | `prepare_record_change` | INSERT/UPDATE/DELETE；未应用不可见；跨日志追加保持固定 |
| 发布与丢弃 | `apply_changes/discard_changes` | 校验 LSN/事务/映像，整批验证后发布并释放句柄 |
| 缓冲写回 | `flush_page/flush_all` | LRU、脏页、先持久化日志再写页；失败保留脏页 |
| 冷缓存 | `reset_cache` | 独占实验、无活动事务、无固定页 |
| 恢复应用 | `apply_recovery_change` | 启动门禁，UPDATE/CLR 重放，page_lsn 幂等 |
| 文件同步 | `sync_files` | 对已写入页文件 fsync，不隐含提交 |

## 2. 共享对象与指标

统一从 `edudb.contracts` 导入类型。不要使用旧 `edudb.models` 的字典记录。对象 ID 与名称分离，退役对象 ID 不能复用；列 ID 应由调用方生成不重复标识。`Record.values` 按 `TableSchema.columns` 顺序排列。A 检查结构和可存储性；SQL 转换由 B/C 完成，约束由 C 检查。

`normalize_name` 默认原样比较；B 确定策略后注入，重开必须采用相同策略。每查询独立创建 `ExecutionContext.metrics`。缓存命中不算文件读，DATA/INDEX 分别计数；已读出但校验失败也计一次读取。这是应用文件读取，不是绕过系统缓存的物理磁盘 I/O。

目录页排除在 data/index 读页和写页计数之外；后台操作归入 `diagnostics().system_metrics`，不自动归到查询。统计为即时扫描，目前没有列直方图。

## 3. C 必须注入的服务

构造：`StorageService(root, log, transactions, capacity=16, normalize_name=None, supported_constraints=frozenset())`。

- 日志：`append_log(record) -> lsn`、`ensure_log_durable(lsn)`、`get_log_record(lsn) -> LogRecord`。
- 事务：`begin(session_id)`、`get_transaction(tx_id, session_id)`、`acquire_lock(tx_id, resource, mode, timeout_ms)`、`commit(tx_id)`、`rollback(tx_id)`、`has_active_transactions()`。
- C 维护单调且重启不重置的 LSN、事务 `last_lsn` 和日志链，提交先持久化日志，锁保持到正确事务边界。

**显式接口补充，需 A/C 共同确认：** `get_log_record` 用于验证传入 LSN 确实属于相应事务和页映像；`has_active_transactions` 用于 DDL、冷缓存和关闭门禁。二者已写入 Protocol，不可省略。日志不能在仍需验证和恢复时提前截断。

`examples/memory_services.py` 是内存测试替身，不写持久 WAL，记录锁请求但不实现锁隔离，不能用于正式数据库运行。

## 4. 启动、回滚与恢复

1. D 初始化 C 的服务，再创建 A，A 初始为 `RECOVERING`，不开放普通 API。
2. C 分析日志并调度 `apply_recovery_change(change, lsn, direction)`。UPDATE 的 REDO 使用 after_image。
3. UNDO 前必须追加 CLR，传 CLR 的 LSN。此实现 CLR 的 change 保存原 UPDATE 前后映像，UNDO 或 REDO CLR 都恢复 before_image；C 不能再次反转映像。
4. `undo_next_lsn` 指向下一撤销位置，不是已撤销记录编号。在线回滚调用补充接口 `apply_rollback_change(change, clr_lsn, tx)`。
5. C 解决事务与恢复进度后调用 `finish_recovery()`，A 检查目录、已分配页、活动事务及固定页，再开放请求。

`finish_recovery()` 不代替 C 日志分析。示例直接调用仅因数据库全新或干净关闭，不得用于真实故障启动。分配撤销保留带 CLR LSN 的墓碑；重复恢复按页 LSN 跳过已应用记录。

## 5. C 修改记录的调用顺序

```python
from edudb.contracts import ExecutionContext, LogRecord

tx = transactions.begin(session_id)
transactions.acquire_lock(tx.id, table_id, "X", 5000)
ctx = ExecutionContext(query_id, session_id, user_id, tx.id)
prepared = storage.prepare_record_change(table_id, "INSERT", values=[1, "Ada"], ctx=ctx)
try:
    previous = transactions.get_transaction(tx.id, session_id).last_lsn
    lsns = []
    for change in prepared.changes:
        previous = log.append_log(LogRecord(tx.id, "UPDATE", prev_lsn=previous, change=change))
        lsns.append(previous)
except Exception:
    storage.discard_changes(prepared)
    transactions.rollback(tx.id)
    raise
try:
    storage.apply_changes(prepared, lsns)
except Exception:
    transactions.rollback(tx.id)
    raise
transactions.commit(tx.id)
```

这是顺序示例，不是完整请求处理器；C 还需处理 begin/锁/prepare 失败及提交结果未知。apply 的验证失败也会释放该准备对象的句柄。UPDATE/DELETE 传已有 RID；INSERT 读 `prepared.rid`。不要改准备映像，不要单独释放其句柄。

普通 `fetch_page` 必须用 `try/finally` 释放。返回页为快照，修改它不会写回，必须走准备/日志/应用。`flush_all` 不提交事务，可写回已记日志的未提交页，C 必须能撤销。

## 6. DDL、索引与实验补充入口

`apply_catalog_command(CatalogCommand(...))` 默认开启独立 DDL 事务；保守策略是有任何活动事务时拒绝开始 DDL。索引算法属 C，为使索引页和元数据同事务发布：

```python
with storage.catalog_transaction() as tx:
    storage.register_index_file(index_id, tx)
    root = storage.allocate_page(index_id, tx)
    # C builds pages via prepare_index_changes + WAL + apply_changes.
    # Publish CatalogCommand("CREATE_INDEX", owner_id, index=info, transaction=tx).
```

`CatalogCommand.transaction` 只接受上述 DDL 范围的事务，拒绝业务事务。索引只支持单列元数据，根页须已分配；B+ 树、唯一性与索引维护由 C 实现。DDL 回滚失败或提交未知会关闭普通访问，交 C 恢复，不猜测提交是否成功。

冷缓存需 `with storage.exclusive_experiment(): storage.reset_cache()`。A 的范围不能替代 D 请求门禁及 C 阻止其他会话开始事务。COMPARE 的数据、缓存条件、等价性与实验调度由 B/C/D 负责。

关闭前解决事务并释放句柄，然后 `close()`；关闭不隐式提交。flush_all 跳过固定页，flush_page 指定固定页时报错。写回失败后保留实例重试，不能丢弃脏缓冲。

## 7. 格式与已知限制

- 新目录格式 v3，4096 字节页、CRC、JSON 载荷、page_lsn；单进程单实例独占目录，不提供跨进程锁。
- 元数据目前限定一个 4KB 页，表列、视图 AST、退役 ID 都占空间；超限报 PAGE_FULL，不是无限系统目录。
- 记录或更新后页过大报错，不做溢出、跨页迁移、空槽压缩；删除不改变其他存活 RID。
- 删除对象逻辑退役文件，保留页用于撤销，没有物理垃圾回收。
- 默认约束能力为空，不应声明 C 尚未实现的主键、外键等能力。
- 旧接口从 `edudb.legacy_api` 导入；两套目录不可混用。旧 migrate-storage 仅迁移 M1 到 v2，不支持 v3。
- 持久 WAL、检查点、恢复分析、日志截断、并发隔离及认证授权需其他成员实现。本轮不提供完整断电恢复保证。

## 8. 验收记录

在 modular_dbms 下运行：

```powershell
python -m unittest discover -s tests -v
python -m edudb contract-demo
```

Windows / Python 3.14：36 项新版 A 测试、1 项新 CLI 测试及 69 项兼容回归，共 106 项通过。覆盖 WAL barrier/页写回失败、错误 LSN/事务/映像、固定页耗尽、目录日志失败、DDL 提交未知、CLR 中断重试和重复重放、缺页和损坏页。使用临时目录与模拟故障，不是真实断电实验；最低 Python 3.10 和其他平台尚未实测。

C 联调还需验证真实日志重启、未提交页刷盘后撤销、目录/索引创建途中故障、事务锁隔离；D 验证恢复未完成不开放请求和 COMPARE 隔离。A 的代码与模块测试完成不等于这些整体集成验收已经完成。
