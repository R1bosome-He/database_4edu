# 测试报告

## 测试环境

- 语言：Python 3.10+
- 依赖：仅 Python 标准库
- 启动方式：`python -m edu_dbms`

## 自动化测试

运行命令：

```powershell
python -m unittest discover -s tests
```

覆盖用例：

1. 创建表、批量插入、创建索引。
2. 对同一条 SQL 执行优化关闭和开启对比。
3. 验证优化开启计划包含 `IndexScan`，优化关闭计划包含 `SeqScan`。
4. 验证优化开启后的磁盘读页数低于优化关闭。
5. 验证未授权用户无法查询，授权后可以查询。
6. 验证 Join 查询在 `NestedLoopJoin` 和 `HashJoin` 下结果一致。

## 手工演示测试

运行：

```powershell
python -m edu_dbms --demo
```

演示内容：

- 创建学生表和选课表。
- 插入大批量测试数据。
- 创建 B+ 树索引。
- 对单表查询进行优化开关对比。
- 对 Join 查询进行优化开关对比。
- 创建普通用户并授权查询。

## Web 演示界面

运行命令：

```powershell
python -m edu_dbms.web --reset-demo
```

浏览器访问：

```text
http://127.0.0.1:8765
```

界面验证点：

- 点击“索引查询对比”，应看到优化关闭使用 `SeqScan`，优化开启使用 `IndexScan`。
- 点击“Join 优化对比”，应看到优化关闭使用 `NestedLoopJoin`，优化开启使用 `HashJoin`。
- 右侧指标卡片应显示磁盘读页和执行时间差异。
- 数据目录应显示 `students`、`enrollments` 两张表及索引。
- 安全审计区应展示最近 SQL 记录。

## 关键指标

`COMPARE` 输出包括：

- `elapsed_ms`
- `disk_reads`
- `disk_writes`
- `cache_hits`
- `cache_misses`
- `pages_touched`
- `rows`

这些指标用于证明优化器对查询执行计划和 I/O 成本的影响。

## 新增功能测试点

- `ORDER BY`：执行 `SELECT id, name, grade FROM students WHERE major = 'database' ORDER BY grade DESC, id ASC;`，结果应按年级降序、编号升序排列。
- `UPDATE`：执行 `UPDATE students SET major = 'security' WHERE id = 12;` 后再次查询，应看到字段变化。
- `DELETE`：执行 `DELETE FROM enrollments WHERE score < 62;` 后，选课表行数应减少。
- 事务回滚：执行 `BEGIN; UPDATE ...; ROLLBACK;` 后再次查询，应恢复修改前状态。
- 事务提交：执行 `BEGIN; DELETE ...; COMMIT;` 后再次查询，修改应保留。
- Web 导出：点击“导出测试报告”，应下载 Markdown 报告文件。
