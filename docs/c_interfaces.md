# C 模块接口与计划格式 v1

统一模型见 `edudb/c_api.py`。错误为 `DBError(code, message, position?, transaction_effect)`；公开执行入口发生运行时错误中止事务，I/O 导致状态不确定时禁止继续服务，要求恢复。

## 1. 公共格式

| 对象 | 字段 |
|---|---|
| ResultColumn | `slot_id, name, type, nullable=True`；slot_id 在当前输出内唯一 |
| Row | 按 output_columns 顺序排列的标量列表；NULL 为 None |
| Expr | `kind, value?, slot_id?, operator?, args=[], level=0, plan?, distinct=False` |
| PlanNode | `node_id, op, children=[], output_columns=[], expressions=[], properties={}, estimated_rows?, estimated_cost?, applied_rules=[]` |
| ExecutionContext | `query_id, session_id, user_id, tx_id, metrics, memory_limit_bytes, outer_rows, lock_timeout_ms, cancelled` |
| QueryResult | `columns, rows, affected_rows, plan, metrics, message?` |
| IndexSpec / IndexInfo | `name, table_id, column_ids, unique`；Info 额外有 `id, root_page_id`，根变化时目录同步更新 |
| RangeSpec | `lower=None, upper=None, lower_inclusive=True, upper_inclusive=True`；None 表示无界，不表示 NULL 键 |
| PageKey / PageChange | `{file_id,page_id}` / `{key,before_image,after_image}`；image 为 `{kind,payload}` 或 None（删除） |

当前 A 适配层将标准化表名、字段名作为稳定 ID；不支持重命名。B 绑定后用独立 slot_id 表示别名、连接和子查询，不用名字字典表示执行结果。`plan_from_dict(asdict(plan))` 可还原 JSON 风格对象；未知字段或算子不静默忽略。

## 2. 执行器接口

| 调用 | 行为和结果 |
|---|---|
| `QueryExecutor(records,indexes,transactions)` | 注入依赖，不依赖旧版全局 Database |
| `execute(plan,ctx) -> QueryResult` | 校验计划、按稳定顺序获取表锁、执行、采集指标；不自动提交；正常返回后 D 负责 commit |
| `open_operator(node,ctx) -> Operator` | 返回批量算子；`open()` 开始，`next_batch(n)` 最多 n 行，空批次为结束，`close()` 幂等；调用方负责事务和异常处理 |
| `evaluate(expr,row_map,ctx) -> Value` | row_map 使用 slot_id；布尔条件要求 BOOL/NULL，不接受 Python 数值真值 |
| `create_index(spec,ctx) -> IndexInfo` | 建树、校验唯一性、记录节点和目录修改；错误中止事务，成功由 D 提交 |
| `search_index(index_id,range_spec,ctx) -> Iterator[RecordId]` | 读取当前事务可见索引，返回 RID；表记录由 RecordAccess.fetch 取得 |
| `update_indexes(table_id,rid,before_values,after_values,ctx)` | 更新所有关联索引；插入前值为空，删除后值为空；仅内部执行器调用 |

`Operator` 打开期间不允许相同事务再执行语句或提交，必须先耗尽或关闭。手动批量接口错误后调用方应回滚；HTTP 层建议只用 execute。内部索引/记录接口不是权限边界。

## 3. 算子参数

| op | children | expressions | properties |
|---|---:|---|---|
| Values | 0 | 无 | `rows: list[Row]`；仅限已验证常量输入 |
| SeqScan | 0 | 无 | `table_id, columns?`；columns 为实际字段名列表 |
| IndexScan | 0 | 无 | `table_id,index_id,range: RangeSpec`，columns 同上 |
| Filter | 1 | 一个 BOOL/NULL 条件 | 无；输出定义与子节点一致 |
| Projection | 1 | 每个输出字段对应一个表达式 | 无 |
| Sort | 1 | 无 | `order_by:[{expr,descending=False,nulls_first=False}]`；默认 NULLS LAST，和升降序独立 |
| Distinct | 1 | 无 | 无；按完整行去重，NULL 归为相同值 |
| Limit | 1 | 无 | `limit?,offset=0`；非负整数 |
| NestedLoopJoin | 2 | ON 条件，省略为 TRUE | `join_type:INNER/LEFT/RIGHT/FULL` |
| HashJoin | 2 | 完整 ON 条件，仍检查残余条件 | join_type 同上，`left_key:Expr,right_key:Expr`；哈希键必须是 ON 中的等值连接键 |
| Aggregate | 1 | 每个输出字段的表达式，可包含 Aggregate | `group_by:list[Expr]`；HAVING 放在其后的 Filter |
| Insert | 0 | 无 | `table_id,rows:list[dict[field,Value\|Expr]]` |
| Update | 0 | 可选 WHERE | `table_id,input_columns:list[ResultColumn],columns?,assignments:dict[field,Expr]` |
| Delete | 0 | 可选 WHERE | `table_id,input_columns,columns?` |

JOIN 输出为左侧输出列接右侧输出列，外连接补空侧的 nullable 应由 B 标明。`input_columns` 是 DML 旧行字段绑定，赋值全部针对同一旧行。Insert 首版接收 VALUES，尚无 INSERT SELECT 专用接口，B 可先明确拒绝该语法。

## 4. 表达式参数

| kind | 参数与语义 |
|---|---|
| Literal | value 为 INT64/有限 FLOAT/TEXT/BOOL/None |
| ColumnRef | slot_id；level=0 当前行，1 最近一层外行，2 再外一层 |
| Binary | operator 为 `= != <> < <= > >= + - * / % AND OR`；args 两个 |
| Unary | operator 为 `NOT + -`；args 一个 |
| IsNull | args 一个；operator 可为 IS NULL 或 IS NOT NULL |
| Aggregate | operator 为 COUNT/SUM/AVG/MIN/MAX；COUNT(*) 的 args 为空，其余一个；distinct 可选 |
| Subquery | plan 为绑定计划；operator 为 SCALAR/EXISTS/NOT EXISTS/IN/NOT IN；IN 类 args 包含左值 |

COUNT 不计 NULL，COUNT(*) 计行；空的无分组聚合仍返回一行。WHERE/ON/HAVING 只接受 TRUE；CHECK 接受 TRUE 或 UNKNOWN。普通比较遇到 NULL 为 UNKNOWN；标量子查询零行返回 NULL、多行报错。TEXT 使用 Python Unicode 码点比较，首版无自定义排序规则。

## 5. 事务与恢复接口

| 调用 | 行为和效果 |
|---|---|
| `begin(session_id) -> Transaction` | 一会话一个活动事务，重复 BEGIN 拒绝 |
| `get_transaction(tx_id,session_id?) -> Transaction` | 返回状态副本，检查活动及可选会话归属 |
| `acquire_lock(tx_id,resource,mode,timeout_ms)` | S/X 表或目录锁，支持升级，超时中止事务并释放其锁 |
| `write_change(tx_id,PageChange) -> lsn` | 校验前镜像，追加 UPDATE、fsync WAL，再应用新镜像 |
| `commit(tx_id)` | 提交日志持久化后标记 COMMITTED 并释放锁 |
| `rollback(tx_id)` | 根据 prev_lsn 撤销修改，先持久化 CLR 再应用撤销，结束记录 ABORT |
| `WAL.append_log(record_without_lsn) -> lsn` | 追加长度/校验帧，分配连续 LSN；自身不承诺持久化 |
| `WAL.ensure_log_durable(lsn)` | fsync 至指定 LSN，失败停止相关操作 |
| `recover() -> {redone,undone,duration_ms}` | 启动分析/重做/撤销，清理不完整末帧，恢复完成才能服务 |
| `checkpoint()` | 要求无活动事务，刷页和同步后持久化检查点；v1 不裁剪 WAL |

## 6. A 依赖与原约定的适配

| 原接口要求 | 此版接入方式 |
|---|---|
| fetch/release_page、prepare/apply_changes | C 核心依赖更小的 PageStore 协议：read_image、validate_image、apply_image、flush_all、sync_files；参考后端只返回独立副本，不向 C 暴露可变缓存页 |
| 页面分配、删除、目录更新 | 通过 PageChange(None→image / image→None) 一起记录 WAL；墓碑保留 page_lsn，页分配的回滚也可重复恢复 |
| A RecordStore 原接口 | RecordAdapter 复用 A 类型、表结构、RID 和记录读取/更新/删除；插入按新页外壳容量选择页。TransactionalPageIO 拦截所有写入并记录页前后镜像 |
| 索引元数据 | C 首版独立 ci_catalog 索引页管理；正式接入 A Catalog 时需要统一，不能维护两套相互矛盾目录 |
| page_lsn 和索引页 | 原始 A 格式不具备；ReferencePageStore 提供测试用升级后端，放在 integration，未覆盖 A 文件 |

这是明确的适配边界，并非声称 A 已实现原约定的所有函数。正式接入时可保留 C 的依赖注入接口，让 A 实现对应语义；任何适配后都必须重新运行事务和故障测试。

## 7. 跨模块约束

1. B 提供已绑定且类型合法的计划，正确安排外连接、分组、子查询及视图展开；C 不猜测 SQL 含义。
2. C 的数据/索引/目录变化全部走页接口及 WAL；禁止直接调用旧版文件修改替代事务写入。
3. A 应在 apply/flush 前确保 page_lsn 的日志已持久化，且页发布顺序与 WAL 一致；C 的参考后端已验证此顺序。
4. 读持有 S 锁，写持有 X 锁直到事务结束；缓冲互斥不能代替事务锁。表锁、目录锁的次序要在集成时保持一致，超时不能被忽略。
5. D 负责认证、授权、自动提交和会话串行；不得将未提交 QueryResult 当作已提交成功。取消或断开必须关闭算子，再回滚活动事务。
6. I/O 失败导致恢复标志后停止新事务；不能声称失败提交必然已经回滚，需重启恢复后确认。
7. 查询指标按上下文收集；元数据和 WAL 成本单独说明；ON/OFF 结果一致后才谈性能收益。

### 工作台附加元信息（2026-10-28）

`QueryResult.teaching` 为可选字典，默认 `None`。由 B 编译和工作台填充视图展开及优化记录，C 的算子和事务执行不依赖此字段。执行数据流复用 `metrics.per_operator`，未执行节点与零输出节点不得混淆。字段格式和展示边界见 [教学可视化说明](teaching_visualization.md)。
