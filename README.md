# EduDB：模块化数据库课程项目

## 新版 Web 工作台（D）

```powershell
python -m edudb.web --demo --port 8766
```

首次安装依赖：`python -m pip install -e .`。新版工作台需要 Windows，使用加密库和当前 Windows 账号保护数据密钥。默认数据目录为项目下的 `.edudb_workbench_v2`，旧目录不自动覆盖。演示数据为 20 名学生、3 门课程、30 条选课记录；只在空库初始化。

打开终端显示的 `http://127.0.0.1:8766/`。首次启动会生成管理员 `admin` 和随机密码，只在启动终端显示一次；密码以加盐 PBKDF2 摘要保存。后续启动沿用账号，不重置数据。`--demo` 仅在空库导入课程样例，不会删除已有表。

新版界面直接调用 ABC 集成运行时，包含 SQL 工作台、分页结果与 CSV、可点击执行计划树、真实优化对比、表结构与索引、用户和表级授权、脱敏审计，以及一键 HTML 验收报告。**不是 legacy 的旧 Web，也不是模拟返回数据。**

导出报告前，可先生成真实测试证据，再启动服务加载：

```powershell
python -m edudb.workbench.evidence
python -m edudb.web --demo --port 8766 --test-evidence .test_tmp/workbench/test-evidence.json
```

证据与源文件指纹绑定，代码改变后自动标为过期；没有证据则标记未运行，不把历史测试数当作本次测试通过。报告包含执行前的模型预估、实验逐次原始测量、指标中位数、环境及脱敏查询记录。

详细启动、API、安全边界、浏览器验收见 [D 工作台交接说明](docs/module_d.md)。初次密码也可通过 `EDUDB_ADMIN_PASSWORD` 环境变量指定（至少 12 字符），不要写入源码或提交到 Git。

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
INSERT INTO students VALUES (1,'张三',90),(2,'李四',80);
CREATE INDEX ix_students_id ON students(id);
SELECT name, score FROM students WHERE id=1;
BEGIN;
UPDATE students SET score=100 WHERE id=1;
ROLLBACK;
SELECT * FROM students ORDER BY score DESC;
```

输入 `.quit` 退出；未提交事务在正常退出时回滚。第二次打开已有库不要再次 CREATE 同名表。可用 `--sql "SELECT * FROM students"` 执行单条语句，或重复指定 `--sql`。详细接口、限制及恢复测试见 [ABC 集成说明](docs/abc_integration.md)。这是本地开发入口，没有 Web 或认证授权，不应作为对外服务。

需要 Python 3.10+；新版加密工作台另需 `cryptography>=41`，可用 `python -m pip install -e .` 安装项目依赖。在仓库根目录执行：

```powershell
python -m unittest discover -s tests -v
python -m edudb.c_demo
python -m edudb.sql_demo
```

B 的 SQL 编译器产出 C 消费的 c_api 计划。新增 ContractRuntime 通过明确适配把 A 的正式目录、记录、缓冲及恢复接口接入，B/C 内部计划类型暂不改写成 contracts.Expr。正式 Web 已接入身份认证、表和视图权限以及磁盘加密。命令行集成入口仍供开发测试，不提供这些保护。未指定 `--integrated` 的 sql_demo 和原 c_demo 保留参考运行时，用于队友原有回归，不代表正式 A 链路。

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

## 2026-10-28 更新

执行前优化效果预估、普通用户注册、账号/数据页/日志加密、视图端到端支持，以及最高待修复级别的结果正确性修复，见 [本轮修改说明](docs/2026-10-28_changes.md)。旧明文工作台须先关闭，再使用 `python -m edudb.migrate_workbench 旧目录 新目录` 转换副本；转换不会覆盖或删除原目录。旧副本仍然是明文，需要另行妥善处置。

视图展开、逐步优化解释、执行数据流和示例操作见 [教学可视化说明](docs/teaching_visualization.md)。在查询结果区点击“过程演示”使用。
