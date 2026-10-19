# C 模块初版

代码位于 `modular_dbms/`，运行环境为 Python 3.10+，仅依赖标准库。

```powershell
cd "C:\Users\16651\Desktop\资料夹\复习资料\数据库原理及安全\dbms_project\modular_dbms"
python -m unittest discover -s tests -v
python -m edudb.c_demo
```

- [C 模块使用、能力和限制](modular_dbms/README_C.md)
- [C 接口和计划格式](modular_dbms/docs/c_interfaces.md)
- [C 设计说明](modular_dbms/docs/c_design.md)
- [实际测试记录](modular_dbms/docs/c_test_report.md)

原始 A 模块基础代码来自仓库 `a567eff`；未复制根目录旧版 `edu_dbms`，未修改原始存储实现。原始 `modular_dbms/README.md` 和旧文档描述的是 M1 时点，当前 C 模块以本页及 `README_C.md` 为准。

本交付为本地初版，未提交或推送 GitHub。SQL 文本解析和权限/Web 是 B/D 的工作；C 接收结构化的已绑定计划。独立演示使用 `integration/` 参考适配层，其数据格式与原始 A v1 数据不互通，未执行自动迁移。
