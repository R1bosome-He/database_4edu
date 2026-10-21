# EduDB：模块化数据库课程项目

新版项目及 C 模块现位于仓库根目录，旧版完整演示系统保存在 `legacy/`。保留原有 Git 提交历史。

## 项目结构

```text
edudb/                 新版存储基础及 C 执行、索引、事务恢复
tests/                 新版测试
docs/                  新版设计、接口、测试及协作说明
README_C.md            C 模块使用、实现范围和限制
pyproject.toml         新版项目配置
legacy/                旧版 edu_dbms、测试、示例及文档
```

## 运行新版

### A+B+C 集成模式（2026-10-21）

现在可使用 A 的正式 `StorageService` 承载 B 的 SQL 与 C 的执行器、索引、持久 WAL，不再由参考存储替代 A。先运行自动演示：

```powershell
python -m edudb.sql_demo --integrated
```

自己输入 SQL，数据会保存在指定目录，退出后可用同一命令重开：

```powershell
python -m edudb.integrated --db .edudb_integrated
```

在 `sql>` 提示符逐条输入（每条以分号结束）：

```sql
CREATE TABLE students (id INT PRIMARY KEY, name TEXT, score INT);
INSERT INTO students VALUES (1,'Ada',90),(2,'Bob',80);
CREATE INDEX ix_students_id ON students(id);
SELECT name, score FROM students WHERE id=1;
BEGIN;
UPDATE students SET score=100 WHERE id=1;
ROLLBACK;
SELECT * FROM students ORDER BY score DESC;
```

输入 `.quit` 退出；未提交事务在正常退出时回滚。第二次打开已有库不要再次 CREATE 同名表。可用 `--sql "SELECT * FROM students"` 执行单条语句，或重复指定 `--sql`。详细接口、限制及恢复测试见 [ABC 集成说明](docs/abc_integration.md)。这是本地开发入口，没有 Web 或认证授权，不应作为对外服务。

需要 Python 3.10+，运行仅使用标准库。在仓库根目录执行：

```powershell
python -m unittest discover -s tests -v
python -m edudb.c_demo
python -m edudb.sql_demo
```

B 的 SQL 编译器产出 C 消费的 c_api 计划。新增 ContractRuntime 通过明确适配把 A 的正式目录、记录、缓冲及恢复接口接入，B/C 内部计划类型暂不改写成 contracts.Expr。正式 Web/权限接入仍由 D 完成。未指定 `--integrated` 的 sql_demo 和原 c_demo 保留参考运行时，用于队友原有回归，不代表正式 A 链路。

- [A 模块接入说明](docs/module_a_contract.md)
- [B 模块说明（SQL 编译/计划/优化）](docs/module_b.md)
- [C 模块说明](README_C.md)
- [C 接口与计划格式](docs/c_interfaces.md)
- [C 实现设计](docs/c_design.md)
- [C 测试记录](docs/c_test_report.md)
- [GitHub 登录与推送步骤](docs/github_push.md)

## 运行旧版

```powershell
cd legacy
python -m unittest discover -s tests -v
python -m edu_dbms.web --reset-demo
```

旧版是提交 `eee4896` 的完整归档，保持独立运行。新版的数据页带 page_lsn，不能直接读取旧版 JSON 数据或原始 A v1 数据，不执行隐式迁移。

## 版本与接入状态

已保留远端 d7c7dee 的完整 A 模块实现和本地 C 初版。A 文档见 [A 接口交接](docs/module_a_contract.md)，C 文档见 [C 模块说明](README_C.md)。

历史目录整合与接口差异见 [整合状态](docs/integration_status.md)。2026-10-21 新增真实 A+B+C 集成入口、端到端与进程退出恢复测试，见 [ABC 集成说明](docs/abc_integration.md)。旧参考数据目录不自动迁移，请为集成入口使用新的数据目录。

本地改动需提交并推送后，队友才能从 GitHub 拉取。
