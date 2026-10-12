# 系统设计说明

## 总体架构

```text
用户 SQL
  -> CLI / Web GUI
  -> SQLParser
  -> QueryPlanner / Optimizer
  -> QueryExecutor
  -> BufferManager
  -> StorageManager
  -> 磁盘页文件
```

元数据由 `Catalog` 管理，索引由 `IndexManager` 管理，安全权限由 `SecurityManager` 管理，所有 SQL 经过 `AuditLogger` 记录。

## Web/GUI 演示界面

Web 控制台由 `edu_dbms.web` 提供本地 HTTP 服务，前端资源放在 `edu_dbms/web_static`。

界面分为五个区域：

- SQL 工作台：输入 SQL、运行预设演示、登录切换用户。
- 结果与指标：展示结果表、耗时、磁盘读页、访问页数、优化收益和柱状对比图。
- 执行计划：展示 `SeqScan`、`IndexScan`、`NestedLoopJoin`、`HashJoin`、`Sort` 等物理计划，并生成树状算子图。
- 数据目录：展示表结构、行数、页数和索引。
- 安全审计：展示最近执行的 SQL、用户、成功状态和耗时。
- 报告导出：从当前数据目录和审计日志生成 Markdown 测试报告。

## 存储管理器

每张表对应一个目录：

```text
.edudb/
  catalog.json
  data/
    students/
      page_000000.json
      page_000001.json
  indexes/
    students__id.idx.json
  security.json
  audit.log
```

记录按关系模型字段存储，每条记录有内部 `_rid`，格式为 `page_id:slot_id`。索引记录保存 `_rid`，执行 `IndexScan` 时通过 `_rid` 直接定位页和槽位。

## 缓冲管理器

缓冲池采用 LRU 替换策略。每次页访问记录：

- `cache_hits`
- `cache_misses`
- `disk_reads`
- `disk_writes`
- `pages_touched`
- `evictions`

这些指标会进入 `COMPARE` 输出，用于展示优化效果。

## 查询优化

关闭优化时：

- 单表查询使用 `SeqScan`。
- `WHERE` 在扫描后统一过滤。
- Join 使用 `NestedLoopJoin`。

开启优化时：

- 命中索引的条件使用 `IndexScan`。
- 可下推的 `WHERE` 条件移动到扫描阶段。
- 等值 Join 使用 `HashJoin`。
- `ORDER BY` 生成 `Sort` 物理算子。

示例：

```sql
COMPARE SELECT id, name FROM students WHERE id = 1200;
```

预期计划差异：

```text
Optimizer OFF: Projection -> Filter -> SeqScan
Optimizer ON : Projection -> IndexScan
```

## 安全设计

系统内置管理员 `admin/admin`。管理员可创建用户和授权：

```sql
CREATE USER reader PASSWORD 'reader123';
GRANT SELECT ON students TO reader;
```

普通用户只能访问被授权的表和操作。权限失败也会写入审计日志。

## 事务和数据修改

系统支持教学版事务边界：

```sql
BEGIN;
UPDATE students SET major = 'security' WHERE id = 12;
ROLLBACK;
```

`BEGIN` 时系统保存数据库快照，`COMMIT` 丢弃快照，`ROLLBACK` 恢复快照。该实现适合课程演示事务原子性，不追求并发隔离。

数据修改支持：

```sql
UPDATE students SET grade = 2025 WHERE id = 12;
DELETE FROM enrollments WHERE score < 62;
SELECT id, name FROM students ORDER BY id DESC;
```

执行 `UPDATE` 和 `DELETE` 后，系统会重建相关表的 B+ 树索引，保证后续 `IndexScan` 仍然能命中正确记录。
