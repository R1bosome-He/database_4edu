# A 模块交接：存储、元数据和缓冲池

> 以下保留早期 M1/M2 教学接口，旧对象从 `edudb.legacy_api` 导入。新版团队接入请使用 [新版 A 接入说明](module_a_contract.md)，不要把下面的 RecordStore/BufferPool 例子用于新版事务流程。

M1/M2 的代码、自动化测试和演示已完成，组内验收待 A/C 实际复核。索引由 C 的 M5 负责；SQL、执行器、权限、事务和 Web 不在本次交付范围。

## 1. 运行和讲解

从仓库根目录运行：

```powershell
cd modular_dbms
python -m edudb buffer-demo
python -m edudb storage-demo
python -m unittest discover -s tests -v
```

讲解分为四步：

1. 用 `storage-demo` 说明表结构、固定 4KB 页、RID 和关闭后重开数据。
2. 用 `buffer-demo` 演示容量 2、访问 `0,1,0,2`，解释为什么淘汰页 1。
3. 展示改动先在缓冲池可见，flush 前磁盘仍是旧值；flush 后脏页变为 0，重新打开可读到新值。
4. 展示测试中的磁盘写入失败，解释为什么缓存不移除脏页、为何重试不会重复写已经成功的页。

LRU 实验的中间状态如下，最左侧最久未使用：

| 访问 | 缓存顺序 | 本次行为 |
| --- | --- | --- |
| 0 | [0] | 缺失，读盘 |
| 1 | [0, 1] | 缺失，读盘 |
| 0 | [1, 0] | 命中，更新顺序 |
| 2 | [0, 2] | 缺失，读盘，淘汰页 1 |

实测合计为 3 次数据页读取、1 次命中、1 次淘汰。随后重复读常驻页 2，两次均命中且没有新增数据页读取。

## 2. 给 B/C 的接入示例

从 `modular_dbms/` 使用以下 Python API；示例临时目录会自动清理：

```python
from tempfile import TemporaryDirectory

from edudb import BufferPool, Catalog, Column, DiskManager, RecordStore, TableSchema

with TemporaryDirectory() as root:
    catalog = Catalog(root)
    disk = DiskManager(root)
    with BufferPool(disk, capacity=2) as pool:
        store = RecordStore(catalog, pool)
        store.create_table(TableSchema("t", (Column("id", "INT"), Column("name", "TEXT")), "id"))
        rid = store.insert("t", {"id": 1, "name": "Ada"})
        store.update("t", rid, {"name": "Grace"})
        print(store.fetch("t", rid))
        print(pool.snapshot_metrics())
    reopened = RecordStore(Catalog(root), DiskManager(root))
    assert reopened.fetch("t", rid).values["name"] == "Grace"
```

同一数据库由一个缓冲池负责写入。创建缓存后不要绕过它直接修改同一页，或创建第二个写缓存；没有并发一致性协议。演示中的直接磁盘读取只用于观察写回效果，初始化写入发生在缓存访问之前。

接口责任：

- Catalog 保存表结构，不缓存数据页。
- RecordStore 检查字段、类型和主键，返回表内 RID；读写通过 PageIO。
- BufferPool 返回页副本，只有 write_page 接受修改；命中顺序、脏标记由内部维护。
- DiskManager 验证页格式及分配清单，完成页文件写入。
- C 的执行层需要协调未来的多记录语句和索引更新；当前单页写入与缓冲 flush 都不是事务。

## 3. 给 B/D 的指标口径

`snapshot_metrics()` 返回新的字典，不泄露内部可变状态；计数自创建或上次 reset_metrics 开始。

| 字段 | 定义 |
| --- | --- |
| logical_reads | 地址有效的 read_page 调用次数，包含后续失败；等于 hits + misses |
| cache_hits / cache_misses | 读取时页是否已经驻留；写请求不计入命中率 |
| logical_writes | 成功被缓冲池接受的 write_page 次数；不等于已持久化次数 |
| disk_reads | 经缓冲池成功完成读取和解码的数据页次数 |
| disk_writes | 经缓冲池成功完成写回的数据页次数，包含新页必要的前置分配 |
| read_errors / write_errors | 调用磁盘层读/写时抛出的异常次数；参数校验不计入 |
| evictions | 为新页腾位置而成功移除的帧数；clear/close 不计入 |
| hit_ratio | hits / logical_reads，取值 0..1；没有读取时为 0 |
| cached_pages / dirty_pages | 当前缓存状态，不是累计次数 |

目录扫描、Catalog、storage.json、临时文件操作、失败尝试内部已发生的 I/O、外部直接使用 DiskManager 都不计入 disk_reads/disk_writes。它们是成功数据页操作计数，不是磁盘设备实际读取次数。读取成功但随后淘汰脏页失败时，该次 disk_reads 仍然计数。

`reset_metrics()` 不改变缓存、脏数据和 LRU；`clear()` 先刷脏页再清缓存。为了测程序冷缓冲，可以 clear 后 reset；这并没有清空操作系统缓存。flush 或 close 报错时保留同一个实例并重试，不能把实例丢弃后声称数据已保存。

## 4. 页清单与升级

新数据库目录包含：

```text
catalog.json             表结构，格式版本 1
storage.json             已持久化的每表页数，目录格式版本 2
data/<table>/00000000.page  固定 4KB 页，页头版本 1
```

DiskManager 对照清单检查页文件，尾页或整张表目录丢失会报 CorruptionError。缓冲中的新页在 page_ids 中可见，成功落盘后才增加清单页数。

已有、且确认完整的 M1 目录可升级：

```powershell
python -m edudb migrate-storage --db .edudb_m1_demo
```

升级会验证现存页并只增加 storage.json，不改原页字节；重复运行不会覆盖已有清单。升级以前已经丢失的尾页无法凭现存文件判断。清单损坏时不自动重建，以免把数据缺失当作正常状态。

新页文件和清单不是跨文件原子事务。正常异常路径会移除本次未登记的新页；进程崩溃或补偿删除再次失败时可能留下不一致，系统报告错误，不自动恢复。完整事务与恢复由 M7 后续设计。

## 5. 交接验收

- 自动化证据见 [测试报告](test_report.md)：69 个测试通过，含同组存储用例经磁盘和缓冲池两种适配器运行。
- 失败覆盖包含淘汰、flush、close、前置新页写回、清单写入、旧格式升级等路径。
- 组员需理解并现场演示；真实姓名、复核结论和个人修改记录填写到 [分工文档](team_plan.md)。
