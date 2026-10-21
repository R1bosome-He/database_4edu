# A+B+C 集成与运行

## 运行入口

从仓库根目录运行，无第三方依赖：

```powershell
python -m edudb.sql_demo --integrated
python -m edudb.integrated --db .edudb_integrated
python -m unittest discover -s tests -q
```

自动演示使用自己的临时目录；SQL shell 使用持久目录，`.quit` 退出、同路径重开。可重复传入 `--sql` 执行多条独立语句。SQL shell 只供本地开发，不提供登录、权限或网络服务。不要对旧参考运行时数据直接使用新入口。

## 实际调用链

```text
SqlSession / SQL shell
  -> B compile_sql (c_api plans)
  -> C QueryExecutor / B+ IndexManager
  -> ContractRuntime adapters
  -> A StorageService (catalog / records / pinned buffers / page files)
  -> C framed WAL / TransactionManager (locks / commit / recovery)
```

实现位于 `edudb/integration/contract_runtime.py`。没有创建 ReferencePageStore，不用它保存任何表、索引或目录。原 Runtime、c_demo、sql_demo 默认模式保留作为参考实现与旧回归。

## 接口决定

- B/C 的计划与表达式继续使用 c_api，避免大范围重写队友代码；不是假装两套类型相同。存储边界显式转换 ID、列表记录、上下文、异常及页映像。
- A 保存独立表 ID 与列 ID；C 的名称/字段字典通过 CatalogAdapter 和 Records 转换，RID 原样保持。A 的目录也保存索引描述与真实根页位置。
- A 记录修改走 prepare -> C WAL append -> A apply，保留固定句柄直至发布或丢弃。C 的事务 last_lsn 在适配器追加日志时同步维护。
- C 的索引节点/超级块通过 A INDEX 页保存，先记录分配，再 prepare_index_changes；C 删除节点在已分配页内保存空逻辑映像，不把 A 的分配目录破坏为缺页。
- C 的 ci_catalog 是从 A 的正式目录生成的只读投影，不再另写第二份索引目录。创建索引使用 A 独立 DDL 范围；根分裂/合并通过新增 `update_index_root` 在相同事务更新目录。
- WAL 使用 C 的校验分帧持久实现。C 的 CLR 保存反转映像，LogAdapter 在读入 A 时反转回原 UPDATE 前后映像，A 再执行 before-image 恢复，防止双重反转。
- 启动先 C recover，经 A apply_recovery_change 重放；完成后 A finish_recovery，最后开放普通调用。在线 rollback 使用 A apply_rollback_change。恢复失败时构造失败，不返回可服务实例。
- A 新增 `file_page_count`、`sync_all_files`、`update_index_root` 三个窄接口，避免适配器直接读写 A 私有缓存和目录。原有 A 方法不移除。

## 事务与并发边界

SqlSession 使用运行时请求锁串行化单次编译/执行。不同会话可以保有事务，隔离沿用 C 的表锁；DDL 必须处于无活动事务的独立范围。CREATE INDEX/CREATE TABLE 不能隐式穿过业务 BEGIN。

额外使用 `storage:write` 排他锁直至事务结束，保守地限制为单写事务。原因是 A 的页分配与索引元数据修改包含整页目录映像；并发写不同表仍可能修改同一目录页，不能让一方回滚覆盖另一方已提交分配。需要更高并发时应先改目录日志粒度，不能直接删掉这把锁。

直接操作底层 adapter/executor 绕过 SqlSession 的并发路径不在此入口保证范围。D 应通过统一请求入口控制会话、身份和恢复门禁，不直接调用 pages 或 storage 修改数据。

## 存储与指标

持久目录包含 `a_storage/`（A v3）、`wal/contract.wal`（C 的 WAL）和运行时进程锁文件。A 数据已存在但日志文件缺失时拒绝启动，不静默创建空日志。旧 `c_pages_v1` 目录明确拒绝，不做隐式迁移。进程独占锁在关闭或进程退出时释放。

数据页/索引页读取由 A 的实际缓存未命中文件读取统计。C 原来把逻辑索引更新计为 page_writes；集成索引适配不沿用该计数，脏页不等于磁盘写入。后台目录、分配与写回部分指标仍在 A system_metrics 中，查询指标不代表整个进程总 I/O。

本次 80 行演示结果等价，关闭优化读取 4 数据页，开启读取 1 数据页另加 4 索引页。因此不能声称总读页或耗时一定下降。不要把指标下降写死成演示结论。

## 验证与限制

2026-10-21 本机实测：新版全量 211 项测试通过（原 189 项加 22 项集成测试）；legacy 独立 5 项通过；`python -m edudb.sql_demo --integrated` 成功运行。测试与自动演示只使用其创建的临时数据目录。

`tests/test_abc_integration.py` 在真实 A 后端重跑 B 的 SQL 端到端用例，并增加持久 ID、索引目录、DDL 边界、失败索引无残留、索引分裂/删除回滚、外键自引用、单写锁超时及命令行重开检查。

子进程用 `os._exit` 模拟进程突然退出，验证：未提交表与索引页已刷盘后的撤销、创建索引中途退出、应用 CLR 后退出，以及两次重启幂等。这是进程故障测试，不等于真实断电或硬件故障保证。

仍有限制：A 目录目前一个 4KB 页，表/列/约束/退役 ID 多时会 PAGE_FULL；记录不支持溢出页；数据格式不迁移；B 的 SQL 子集限制不变；默认入口没有鉴权、审计或 Web。原库目录和 WAL 必须一同备份，不能只复制数据页。

本次只做集成与运行验证，不宣称整个课程项目、安全模块或生产级 ACID 验收已完成。
