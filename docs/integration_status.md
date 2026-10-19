# A/C 目录整合状态（2026-10-20）

## 已完成的版本合并

- 上游：`d7c7dee`，A 存储、元数据、缓冲池、共享合同与测试。
- 本地：`006863e`，目录重组、C 执行器、索引、事务恢复及独立参考运行时。
- 新版统一放在根目录 `edudb/`、`tests/`、`docs/`；旧演示保存在 `legacy/`。
- A 的实现和测试按新路径保留；C 的源码和测试不覆盖 A。
- 原 C 提交另有本地备份分支 `backup-c-before-rebase`，本次不 push。

## 不等于已完成运行接口联调

本次复验：根目录 `python -m unittest discover -s tests -q` 共 149 tests，OK，4.639 秒；包含远端 A 的 106 项及 C 的 43 项。`legacy/` 另有 5 tests，OK，1.719 秒。A contract-demo 与 C c_demo 均通过。首次受限运行出现一处临时页替换 WinError 5；正常权限下完整复验通过，没有据此修改 A 或 C 的存储算法。

| 接口 | A 当前实现 | C 初版 | 后续需要 |
|---|---|---|---|
| 共享模型 | `edudb.contracts` | `edudb.c_api` | 以 contracts 为共同边界，增加显式适配，不能仅替换 import |
| 表与记录 | ID/名称分离，Record.values 列表 | 参考桥使用旧模型及字段字典 | 按列 ID/列序转换，保留稳定 RID |
| 表达式 | Expr(kind,properties,children) | Expr(kind,args,operator,...) | B/C 明确同一表达式格式或提供转换函数 |
| 上下文 | contracts.ExecutionContext | C 增加取消、锁超时、物化预算等状态 | 明确扩展字段或独立运行状态，避免丢失控制信息 |
| 页修改 | prepare/apply_changes，Page 前后镜像 | read_image/write_change，字典镜像 | 适配 A 的句柄生命周期、准备对象与结构化日志验证 |
| 日志读取 | 要求 get_log_record(lsn) | WAL.get(lsn) 返回字典 | 转为 contracts.LogRecord 并同步事务日志链 |
| CLR 编码 | 保留原 UPDATE 映像，应用时恢复 before_image | CLR 保存反转后映像，应用 after_image | 必须明确转换，禁止直接互换导致二次反转 |
| 恢复生命周期 | RECOVERING、apply_recovery_change、finish_recovery | C ReferencePageStore 重放 | 接入 A 的启动门禁及在线回滚接口 |
| 活动事务 | 要求 has_active_transactions() | C 管理器内部维护活动事务 | 提供受同步保护的查询接口用于 DDL、缓存和关闭门禁 |
| 锁资源 | A 使用自身表/目录资源标识 | C 使用 table: / catalog: 前缀 | 统一资源命名和锁顺序，不能让两个名字保护同一对象 |
| 数据格式 | A v3 页/目录 | C c_pages_v1 / EDC1 | 不混读、不自动迁移；正式切换后重跑恢复测试 |

目前的测试证明 A 和 C 各自代码在重组后仍可运行，不证明真实 StorageService 已接入 C 的 WAL 或执行器。后续需要新增 A+C 贯穿测试，覆盖事务建表、增删改、索引一致性、提交重开、锁等待和异常恢复。

## 目录使用

从根目录运行：

```powershell
python -m unittest discover -s tests -v
python -m edudb contract-demo
python -m edudb buffer-demo
python -m edudb.c_demo
```

`contract-demo` 仍明确使用 A 的内存日志/事务测试替身；`c_demo` 使用 C 的持久 WAL 和参考存储。二者不可混为同一真实运行链路。旧版测试在 `legacy/` 单独运行。
