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

需要 Python 3.10+，运行仅使用标准库。在仓库根目录执行：

```powershell
python -m unittest discover -s tests -v
python -m edudb.c_demo
```

C 接收已绑定的结构化计划，SQL 文本编译器和正式 Web/权限接入由 B/D 完成；参考适配层与 A 的正式缓冲池还需联调。

- [A 模块接入说明](docs/module_a_contract.md)
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

本次解决目录与版本冲突，将新版提升至根目录；A 使用 edudb.contracts / StorageService，C 独立演示使用 c_api / 参考适配层，运行接口还没有统一，不把同目录共存表述为已完成联调。差异见 [整合状态](docs/integration_status.md)。SQL 编译和权限/Web 仍由 B/D 接入。

GitHub 默认目录只有在分支推送并合入 main 后才会变化，本次不 push。
