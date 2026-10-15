# EduDB Modular：逐模块数据库课程项目

这是一个从头实现、独立运行的 Python 项目，供 4 人小组按模块开发“数据库原理及安全”大作业。代码不依赖上一级的 `edu_dbms`，可以单独复制本目录开发；目前与旧版共用 Git 仓库，方便团队查看进度。

**A 的新版存储、目录和缓冲接口已实现，106 项模块及兼容测试通过；C 的真实 WAL、锁和事务服务尚待接入联调。新项目还不能执行 SQL，没有 Web 服务。** 访问旧版的 `http://127.0.0.1:8765/` 不会显示这个新项目。

**队友接入请先看 [新版 A 接口交接与验收](docs/module_a_contract.md)**，统一使用 `edudb.contracts` 与 `StorageService`。下文 M1/M2 介绍保留作为早期教学材料，不能替代新版约定。新版演示为 `python -m edudb contract-demo`，使用内存 C 测试替身，不是正式持久化 WAL。

## 1. 队友先看什么

| 文档 | 解决的问题 |
| --- | --- |
| [需求与模块验收](docs/requirements.md) | 老师要求什么、本组扩展什么、怎样证明做到了 |
| [4 人分工与阶段计划](docs/team_plan.md) | 每人改哪些模块、先做什么、交什么、由谁复核 |
| [模块接口与存储设计](docs/architecture.md) | 模块如何连接、哪些接口已经实现、哪些尚待实现 |
| [A 模块接入与讲解](docs/module_a.md) | 缓冲池怎样使用、指标怎么算、如何演示和升级 M1 数据 |
| [M1/M2 测试记录](docs/test_report.md) | 本轮实际运行了什么、通过什么、没有验证什么 |

姓名和日期尚未提供，分工用 A/B/C/D 表示拟定责任，不代表已经完成的个人贡献。

## 2. 运行 A 的演示

需要 Python 3.10+，运行和测试不需要安装第三方库。以下命令从本仓库根目录开始：

```powershell
cd modular_dbms
python -m edudb contract-demo
python -m edudb buffer-demo
python -m edudb storage-demo
python -m unittest discover -s tests -v
```

Windows 上如果 `python` 不可用，可以将其替换为 `py`。当前验证平台为 Windows；macOS/Linux 尚未实测。

演示默认创建临时数据库，最后只清理本次创建的临时目录。`buffer-demo` 演示 LRU、热缓冲、延迟写回和重开：

```text
LRU: disk_reads=3, hits=1, evictions=1
Warm reads: disk_reads=0, hits=2
Dirty pages before flush: 2
Dirty pages after flush: 0
Rows after reopen: 4; updated value: updated in buffer
```

`storage-demo` 单独演示 M1 的建表、插入、修改、重新打开和删除：

```text
Rows after reopen: 180
Pages: 2; page size: 4096 bytes
Rows after deleting RID 0:0: 179
```

要保留数据文件供讲解，可指定一个新目录：

```powershell
python -m edudb buffer-demo --db .edudb_m2_demo --buffer-pages 2
```

再次对同一目录运行演示会提示表已存在，不会重置数据。日常测试使用临时目录，不需要删除旧版数据库。

之前保存的 M1 数据缺少新增的 `storage.json` 页分配清单，需要显式升级；页面内容不会改写：

```powershell
python -m edudb migrate-storage --db .edudb_m1_demo
```

仅对确认完整的 M1 目录执行升级。它不能恢复已经丢失的尾页；不要用它给已损坏数据库重建清单。没有保存过 M1 数据时不需要执行。

## 3. 模块进度

| 模块 | 职责 | 负责人建议 | 当前状态 |
| --- | --- | --- | --- |
| M1 存储与元数据 | 表结构、4KB 页、记录定位、持久化、页分配清单 | A | 已实现并通过测试；待组内复核 |
| M2 缓冲管理 | LRU、脏页、刷盘、页 I/O 统计、失败重试 | A | 已实现并通过测试；待组内复核 |
| M3 查询处理 | SQL 分词、语法树、字段校验、基础计划 | B | 待开发 |
| M4 执行引擎 | 扫描、过滤、投影、连接、排序、数据修改 | C | 待开发 |
| M5 索引与优化 | B+ 树、索引扫描、谓词下推、连接优化 | C：索引；B：优化 | 待开发 |
| M6 安全与界面 | 登录、授权、审计、CLI/Web、计划树 | D | 待开发 |
| M7 事务与实验交付 | 受限事务、优化对比、图表、测试报告导出 | C：事务；B：实验；D：展示 | 待开发 |

成员 A/B/C/D 都负责自己模块的测试和设计说明。D 负责报告整合，不包办其他人的测试或技术内容。

## 4. 已有代码

```text
modular_dbms/
  edudb/
    models.py            表、列、页、记录 ID
    errors.py            模块公共异常
    fileio.py            单文件完整写入后替换
    catalog.py           表结构元数据持久化
    buffer/
      pool.py            PageIO 兼容缓冲池、LRU、脏页和指标
    storage/
      page.py            4KB 页编解码与校验
      disk.py            页文件读写、分配清单、M1 格式升级
      records.py         记录增删改查；PageIO 接口
    __main__.py          M1/M2 演示和数据升级命令
  tests/
    test_catalog.py      元数据和类型约束测试
    test_storage.py      同组存储用例分别通过磁盘和缓冲池执行
    test_buffer.py       LRU、计数、脏页、失败重试和生命周期
    test_manifest.py     分配清单、缺页检测和兼容升级
    test_cli.py          演示和升级命令
  docs/                  需求、设计、分工、验收记录
  pyproject.toml         独立项目声明
```

M1 的增删改查是 Python 存储 API，并不是 SQL 语句支持。M3 和 M4 完成后才通过 SQL 调用它们。

## 5. 给下一位开发者

B/C/D 应按 [新版 A 接口交接](docs/module_a_contract.md)接入 `StorageService`，C 注入日志与事务服务，D 展示每查询 `ExecutionContext.metrics`。旧 `RecordStore` 和 `BufferPool` 从 `edudb.legacy_api` 导入，仅用于旧演示及兼容回归，不是新版接入入口。

缓冲写入只代表内存接受修改。用 `with BufferPool(...)` 管理生命周期，或显式调用 `flush()` / `close()` 后再从磁盘重开。关闭失败会保留缓冲池供重试，不能丢弃仍有脏页的实例。

## 6. 当前边界

以下为旧 M1/M2 的边界。新版格式为 v3，必须使用新空目录，不能用旧迁移命令升级；新版目录暂限一个 4KB 页，记录不支持溢出页，C 负责约束、事务锁和持久 WAL。新版完整限制与待联调项见 [接入说明](docs/module_a_contract.md)。

- 使用固定 4096 字节文件作为数据页；页内记录用 UTF-8 JSON 编码，不是成熟数据库的二进制槽目录。
- 删除使用空槽标记，其他存活记录的 RID 不改变；暂不复用空槽、不压缩页。
- 更新后单页放不下会明确报错并保留原页，不支持跨页搬迁和超大记录。
- 主键唯一性目前全表扫描检查，索引在 M5 引入。
- 支持单进程顺序调用，一个缓冲池独占同一数据库的写路径；没有并发控制、事务日志或断电恢复保证。
- 单文件替换及 CRC32 用于完整写入和检测部分损坏，不能称为事务 ACID、恢复系统或防篡改机制。
- 存储目录格式为 v2，新增 `storage.json`；页头仍是 v1。清单可发现中间页、尾页或整张表数据目录缺失；清单和数据同时遭到删除或伪造不在保证范围内。
- 新页和清单是两个文件的更新，中途崩溃可能留下未登记页；系统检测并报错，不提供自动崩溃恢复。脏页只在成功写回后标为干净。
- I/O 指标统计经缓冲池完成的数据页读写，排除目录检查、元数据、清单和外部直接调用，不能当作整机物理 I/O。

课程完成情况以逐模块验收记录为准；界面、文件数量和已有测试数量都不能单独证明整个 DBMS 已完成。
