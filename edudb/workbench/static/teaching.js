"use strict";
// These views consume the authorized query response, never rerun a query.
let teachingTab="optimizer", teachingStep=0, flowStep=0;
const ruleNames={projection_pushdown:"投影前条件下推",filter_merge:"合并相邻筛选",predicate_pushdown:"连接条件下推",index_scan:"使用索引定位",hash_join:"使用哈希连接"};
const operatorNames={SeqScan:"全表扫描",IndexScan:"索引扫描",Filter:"条件筛选",Projection:"选择输出字段",Sort:"排序",Distinct:"去重",Aggregate:"分组与聚合",Limit:"限制返回行数",HashJoin:"哈希连接",NestedLoopJoin:"嵌套循环连接",Values:"常量记录"};
const operatorHelp={SeqScan:"遍历表的数据页，取出记录。",IndexScan:"沿索引定位范围，再按记录位置访问数据页。",Filter:"计算条件，只向上游返回条件为真的记录。",Projection:"选择或计算输出字段；视图边界还负责字段重命名。",Sort:"按指定键排序，随后输出记录。",Distinct:"消除重复输出记录。",Aggregate:"按分组键组织记录并计算聚合值。",Limit:"跳过指定行数并限制返回量；子节点可能只被部分消费。",HashJoin:"构建哈希表并探测匹配记录，同时校验连接条件。",NestedLoopJoin:"遍历两侧记录组合并检查连接条件。",Values:"产生 SQL 中指定的常量记录。"};
function resetTeaching(){teachingTab="optimizer";teachingStep=0;flowStep=0;}
function renderTeaching(){
  const result=state.result,host=$("#result-teaching"),t=result?.teaching;
  if(!t){host.innerHTML=empty("此语句没有查询过程","执行 SELECT，或使用 EXPLAIN 查看计划","route");return;}
  host.innerHTML=`<div class="teaching-tabs" role="tablist" aria-label="查询过程"><button data-teaching="views" role="tab">视图展开${t.views.length?` · ${t.views.length}`:""}</button><button data-teaching="optimizer" role="tab">优化步骤 · ${t.step_count}</button><button data-teaching="flow" role="tab">执行数据流</button></div><div id="teaching-content"></div>`;
  $$('[data-teaching]',host).forEach(button=>{const active=button.dataset.teaching===teachingTab;button.classList.toggle("active",active);button.setAttribute("aria-selected",active);});
  if(teachingTab==="views")renderViewExpansion(t);else if(teachingTab==="flow")renderExecutionFlow(result);else renderOptimization(t);
  icons();
}
function renderViewExpansion(t){
  const host=$("#teaching-content");
  if(!t.views.length){host.innerHTML=empty("本次查询没有引用视图","可先运行示例中的“创建示例视图”，再运行“视图查询”。","layers");return;}
  const views=[...t.views].sort((a,b)=>a.depth-b.depth);
  host.innerHTML=`<div class="teaching-note">普通视图保存查询定义；下方展示本次查询实际展开的定义与字段来源。点击字段查看映射，点击计划中的视图可收起子树。</div><div class="view-expansions">${views.map((v,i)=>`<details class="view-expansion" open><summary><span class="step-number">${i+1}</span><b>${escapeHTML(v.view_name)}</b><span>引用名 ${escapeHTML(v.alias)}${v.parent?` · 上层视图 ${escapeHTML(v.parent)}`:" · 用户查询"}</span></summary><div class="view-expansion-body"><pre class="teaching-sql">${escapeHTML(v.query_sql)}</pre><div class="lineage-list">${v.columns.map(c=>`<button class="lineage-row" data-lineage="${escapeHTML(`${v.alias}.${c.output} → ${c.origins.join('、')||c.source}`)}"><code>${escapeHTML(v.alias+"."+c.output)}</code><span>→</span><code>${escapeHTML(c.origins.join("、")||c.source)}</code></button>`).join("")}</div></div></details>`).join("")}</div><p id="lineage-detail" class="teaching-note" aria-live="polite">选择一个字段，查看其底层来源。</p><h3>展开后的初始计划</h3>${planTree(t.baseline,{},"expanded")}`;
}
function renderOptimization(t){
  const host=$("#teaching-content"),steps=t.steps;
  teachingStep=Math.min(Math.max(teachingStep,0),steps.length);
  const step=steps[teachingStep-1];
  host.innerHTML=`<div class="teaching-controls"><button class="button" data-opt-move="-1" ${teachingStep===0?"disabled":""}>上一步</button><span aria-live="polite">${teachingStep===0?"初始计划":`第 ${teachingStep} 步：${escapeHTML(ruleNames[step.rule]||step.rule)}`}</span><button class="button" data-opt-move="1" ${teachingStep===steps.length?"disabled":""}>下一步</button></div><div class="teaching-note">${step?escapeHTML(step.reason):t.optimizer_enabled?(steps.length?"这是视图展开后、优化前的计划。点击下一步查看真实改写。":"本次没有可应用的安全改写；保留原计划。"):"本次关闭查询优化；仍会正常展开视图，但不应用优化规则。"}</div>${t.trace_truncated?`<p class="teaching-note">实际发生 ${t.step_count} 步改写，仅展示前 ${steps.length} 步；最终执行计划以“执行计划”页为准。</p>`:""}${step?`<div class="teaching-plan-pair"><section><h3>改写前</h3>${planTree(step.before,{},"step-before",step.affected_nodes)}</section><section><h3>改写后</h3>${planTree(step.after,{},"step-after",step.affected_nodes)}</section></div>`:`<div class="teaching-initial">${planTree(t.baseline,{},"initial")}</div>`}<details class="teaching-boundaries"><summary>改写边界</summary><p>只对直接字段投影执行外层条件下推；复杂计算、分组、去重和行数限制不会被任意跨越。外连接只允许满足保留侧规则的下推。这里记录的是实际规则改写，不代表穷举了所有计划或按成本选出了全局最优解。</p></details>`;
}
function flowNodes(plan,metrics){const nodes=[];function visit(node){planChildren(node).forEach(visit);nodes.push({node,metrics:metrics.per_operator?.[node.node_id]});}if(plan)visit(plan);return nodes;}
function renderExecutionFlow(result){
  const host=$("#teaching-content");
  if(result.teaching.execution_kind==="plan_only"){host.innerHTML=empty("EXPLAIN 没有执行查询","可查看视图和优化步骤；运行 SELECT 后才有实际行数。","route");return;}
  const nodes=flowNodes(result.plan,result.metrics);flowStep=Math.min(Math.max(flowStep,0),Math.max(0,nodes.length-1));
  const selected=nodes[flowStep];if(!selected){host.innerHTML=empty("没有可展示的执行节点");return;}
  const maximum=Math.max(1,...nodes.map(n=>n.metrics?.output_rows||0));
  const m=selected.metrics;
  function draw(node,parent){
    const index=nodes.findIndex(entry=>entry.node===node),m=nodes[index].metrics;
    return `<li>${parent?`<div class="flow-edge">↑ ${m?fmt(m.output_rows,0)+" 行":"未记录"} → ${escapeHTML(parent.node_id)}</div>`:""}<button class="flow-node ${index===flowStep?"selected":""}" data-flow-index="${index}"><span class="step-number">${index+1}</span><span class="flow-node-name"><b>${escapeHTML(operatorNames[node.op]||node.op)}</b><small>${escapeHTML(node.properties.view_name||node.properties.table_id||node.node_id)}</small></span><span class="flow-output">${m?fmt(m.output_rows,0)+" 行":"未记录"}</span><span class="flow-bar" style="--flow-width:${Math.max(0,Math.min(100,(m?.output_rows||0)/maximum*100))}%"></span></button>${planChildren(node).length?`<ul>${planChildren(node).map(child=>draw(child,node)).join("")}</ul>`:""}</li>`;
  }
  host.innerHTML=`<div class="teaching-note">本次执行结束后的统计回放。按数据依赖从底向上讲解，不是实际调用时间线；部分节点可能因行数限制或条件分支而未执行。相关子查询的统计是多次调用的累计值。</div><div class="teaching-controls"><button class="button" data-flow-move="-1" ${flowStep===0?"disabled":""}>上一步</button><span>节点 ${flowStep+1} / ${nodes.length}</span><button class="button" data-flow-move="1" ${flowStep===nodes.length-1?"disabled":""}>下一步</button></div><div class="flow-layout"><div class="flow-lane"><div class="flow-tree"><ul>${draw(result.plan,null)}</ul></div></div><aside class="flow-detail"><h3>${escapeHTML(operatorNames[selected.node.op]||selected.node.op)}</h3><p>${escapeHTML(operatorHelp[selected.node.op]||"执行计划中的处理节点。")}</p>${m?tableHTML(["实测指标","数值"],[["输入行数",m.input_rows],["输出行数",m.output_rows],["检查记录数",m.rows_examined],["连接比较次数",m.join_comparisons],["哈希构建行数",m.hash_build_rows],["哈希探测次数",m.hash_probes],["节点耗时（毫秒）",m.elapsed_ms]]):'<p>该节点没有本次统计，不能当作已执行零行。</p>'}<p class="teaching-note">${planChildren(selected.node).length?`依赖输入：${planChildren(selected.node).map(c=>escapeHTML(c.node_id)).join("、")}`:"底层数据来源节点"}。节点耗时可能包含子节点耗时，不应直接累加。</p></aside></div>`;
}
document.addEventListener("click",event=>{
  const tab=event.target.closest("[data-teaching]");if(tab){teachingTab=tab.dataset.teaching;renderTeaching();}
  const opt=event.target.closest("[data-opt-move]");if(opt&&!opt.disabled){teachingStep+=Number(opt.dataset.optMove);renderTeaching();}
  const flow=event.target.closest("[data-flow-move]");if(flow&&!flow.disabled){flowStep+=Number(flow.dataset.flowMove);renderTeaching();}
  const select=event.target.closest("[data-flow-index]");if(select){flowStep=Number(select.dataset.flowIndex);renderTeaching();}
  const lineage=event.target.closest("[data-lineage]");if(lineage){$$('.lineage-row').forEach(el=>el.classList.toggle("selected",el===lineage));$("#lineage-detail").textContent=lineage.dataset.lineage;}
});
