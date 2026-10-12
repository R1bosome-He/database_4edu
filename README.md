# EduDBMS 数据库原理及安全大作业

EduDBMS 是一个面向“数据库原理及安全”课程设计的教学型迷你数据库管理系统。项目不是普通 CRUD 应用，而是实现了一个简化 DBMS 内核：页式存储、缓冲池、SQL 解析、查询优化、执行引擎、B+ 树索引、事务雏形、安全权限、审计日志，以及可视化 Web 演示界面。

项目目标是用于课程提交、课堂演示和答辩展示。推荐答辩时优先使用 Web 控制台，因为它可以直观看到执行计划树、优化前后性能柱状图、数据目录和审计日志。

## 1. 环境要求

只需要 Python，不需要安装第三方依赖。

```text
Python 3.10+
Windows / macOS / Linux 均可
```

本仓库当前在 Windows PowerShell 下开发和验证。

## 2. 快速启动 Web 演示界面

先进入项目目录：

```powershell
cd "D:\unversity homework\database_4edu"
```

启动 Web 控制台并重置演示数据：

```powershell
python -m edu_dbms.web --reset-demo
```

看到类似输出说明启动成功：

```text
EduDBMS Web Console running at http://127.0.0.1:8765
```

浏览器打开：

```text
http://127.0.0.1:8765
```

如果 `python` 命令不可用，可以试：

```powershell
py -m edu_dbms.web --reset-demo
```

如果 8765 端口被占用，换一个端口：

```powershell
python -m edu_dbms.web --reset-demo --port 8766
```

然后打开：

```text
http://127.0.0.1:8766
```

## 3. 推荐答辩演示流程

打开 Web 页面后，建议按这个顺序演示：

```text
1. 点击“索引查询对比”
   展示优化关闭时 SeqScan，优化开启时 IndexScan。

2. 点击“Join 优化对比”
   展示优化关闭时 NestedLoopJoin，优化开启时 HashJoin。

3. 点击“ORDER BY 排序”
   展示 Sort 物理算子和排序结果。

4. 点击 BEGIN
   开启事务。

5. 点击 UPDATE
   修改一条学生记录。

6. 点击 ROLLBACK
   回滚事务，再查询可证明数据恢复。

7. 点击“导出测试报告”
   自动下载 Markdown 测试报告。
```

Web 控制台上可以重点讲这几个区域：

- SQL 工作台：输入或选择 SQL。
- 结果与指标：展示查询结果、磁盘读页、耗时、返回行数。
- 柱状图：展示优化关闭/开启的性能差异。
- 执行计划可视化：展示物理算子树。
- 数据目录：展示表、字段、行数、页数、索引。
- 安全审计：展示用户执行 SQL 的成功/失败记录。

## 4. 登录账号

默认管理员账号：

```text
用户名：admin
密码：admin
```

演示数据会自动创建只读用户：

```text
用户名：reader
密码：reader123
```

`reader` 默认只有 `students` 表的 `SELECT` 权限。可以用它演示权限控制：普通用户不能执行未授权表或未授权操作。

## 5. 命令行运行方式

除了 Web 界面，也可以使用 CLI。

一键命令行 demo：

```powershell
python -m edu_dbms --demo
```

启动交互式命令行：

```powershell
python -m edu_dbms --db .edudb
```

执行单条 SQL：

```powershell
python -m edu_dbms --execute "CREATE TABLE students (id INT, name TEXT, PRIMARY KEY (id));"
```

运行测试：

```powershell
python -m unittest discover -s tests
```

## 6. 支持的 SQL

当前支持的 SQL 子集如下。

建表：

```sql
CREATE TABLE students (
  id INT,
  name TEXT,
  major TEXT,
  grade INT,
  PRIMARY KEY (id)
);
```

插入：

```sql
INSERT INTO students (id, name, major, grade)
VALUES (1, 'Ada', 'database', 2024);
```

创建索引：

```sql
CREATE INDEX idx_students_id ON students (id);
```

查询：

```sql
SELECT id, name, major FROM students WHERE id = 1;
```

Join：

```sql
SELECT students.name, enrollments.course, enrollments.score
FROM students JOIN enrollments
ON students.id = enrollments.student_id
WHERE students.id = 1;
```

排序：

```sql
SELECT id, name, grade
FROM students
WHERE major = 'database'
ORDER BY grade DESC, id ASC;
```

查看执行计划：

```sql
EXPLAIN OPTIMIZER ON SELECT id, name FROM students WHERE id = 1;
EXPLAIN OPTIMIZER OFF SELECT id, name FROM students WHERE id = 1;
```

优化对比：

```sql
COMPARE SELECT id, name FROM students WHERE id = 1;
```

数据修改：

```sql
UPDATE students SET major = 'security' WHERE id = 1;
DELETE FROM enrollments WHERE score < 62;
```

事务雏形：

```sql
BEGIN;
UPDATE students SET major = 'security' WHERE id = 1;
ROLLBACK;
```

也可以提交：

```sql
BEGIN;
DELETE FROM enrollments WHERE score < 62;
COMMIT;
```

安全权限：

```sql
CREATE USER reader PASSWORD 'reader123';
GRANT SELECT ON students TO reader;
GRANT SELECT|INSERT|UPDATE|DELETE|ALL ON students TO reader;
```

## 7. 已实现功能

- 存储管理器：按表目录和页文件持久化记录。
- 缓冲管理器：LRU 缓冲池、脏页写回、命中/缺失/磁盘读写统计。
- SQL 解析器：解析课程设计所需 SQL 子集。
- 查询优化器：索引选择、谓词下推、Hash Join、Sort。
- 执行引擎：`SeqScan`、`IndexScan`、`Filter`、`Projection`、`NestedLoopJoin`、`HashJoin`、`Sort`。
- B+ 树索引：支持等值和范围查询，索引文件持久化。
- 优化对比：`COMPARE` 同时运行优化关闭和优化开启两套计划。
- 事务雏形：`BEGIN`、`COMMIT`、`ROLLBACK`，用快照方式演示原子性。
- 数据修改：`UPDATE`、`DELETE`，修改后会重建相关索引。
- 安全：用户登录、表级权限、管理员权限。
- 审计：记录 SQL、用户、成功状态、耗时、指标。
- Web/GUI：执行计划树、柱状图、数据目录、审计日志、报告导出。

## 8. 系统架构

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

核心模块：

```text
edu_dbms/
  database.py      系统门面，串联各模块
  storage.py       磁盘页式存储
  buffer.py        LRU 缓冲池
  index.py         B+ 树索引
  sqlparser.py     SQL 子集解析器
  planner.py       查询计划与优化
  executor.py      查询执行引擎
  security.py      用户与权限
  audit.py         审计日志
  cli.py           命令行界面
  web.py           本地 Web 控制台后端
  web_static/      Web 控制台前端
examples/demo.sql  演示 SQL
tests/             自动化测试
docs/              设计、分工和测试报告
```

## 9. 提交物说明

课程提交时建议包含：

```text
源代码：edu_dbms/
可执行程序：使用 python -m edu_dbms.web 启动
使用说明：README.md
测试报告：docs/test_report.md，或 Web 页面导出的 Markdown 报告
设计说明：docs/design.md
分工说明：docs/team_plan.md
演示 SQL：examples/demo.sql
```

## 10. 常见问题

### 端口被占用怎么办？

换端口：

```powershell
python -m edu_dbms.web --reset-demo --port 8766
```

### 怎么关闭正在运行的 Web 服务？

如果是在当前 PowerShell 前台运行，按：

```text
Ctrl + C
```

如果知道进程号，例如 `43608`：

```powershell
Stop-Process -Id 43608
```

### 页面没有更新怎么办？

先强制刷新浏览器：

```text
Ctrl + F5
```

如果还是不行，关闭旧服务后重新启动：

```powershell
python -m edu_dbms.web --reset-demo
```

### 运行后生成的 `.edudb_web` 是什么？

这是本地演示数据库目录，里面有数据页、索引、审计日志等运行时文件。它已经写进 `.gitignore`，不需要提交。

### 为什么事务说是“雏形”？

当前事务用数据库目录快照实现：`BEGIN` 保存快照，`ROLLBACK` 恢复快照，`COMMIT` 丢弃快照。它适合课程演示事务原子性，但没有实现并发隔离和锁管理。

## 11. 给队友的最短运行指南

只想快速看效果的话，执行：

```powershell
cd "D:\unversity homework\database_4edu"
python -m edu_dbms.web --reset-demo
```

然后打开：

```text
http://127.0.0.1:8765
```

页面里依次点：

```text
索引查询对比 -> Join 优化对比 -> ORDER BY 排序 -> BEGIN -> UPDATE -> ROLLBACK -> 导出测试报告
```

