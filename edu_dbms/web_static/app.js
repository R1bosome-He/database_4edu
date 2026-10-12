const state = {
  user: "admin",
  lastResult: null,
};

const $ = (id) => document.getElementById(id);

const sqlInput = $("sqlInput");
const runBtn = $("runBtn");
const loadDemoBtn = $("loadDemoBtn");
const exportBtn = $("exportBtn");
const loginBtn = $("loginBtn");
const readerBtn = $("readerBtn");

sqlInput.value = "COMPARE SELECT id, name, major FROM students WHERE id = 480;";

runBtn.addEventListener("click", () => executeSql(sqlInput.value));
loadDemoBtn.addEventListener("click", resetDemo);
exportBtn.addEventListener("click", exportReport);
loginBtn.addEventListener("click", () => login($("loginUser").value, $("loginPassword").value));
readerBtn.addEventListener("click", () => login("reader", "reader123"));

document.querySelectorAll("[data-sql]").forEach((button) => {
  button.addEventListener("click", () => {
    sqlInput.value = button.dataset.sql;
    executeSql(button.dataset.sql);
  });
});

sqlInput.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    executeSql(sqlInput.value);
  }
});

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...options,
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || "请求失败");
  }
  return data;
}

async function refresh() {
  try {
    const data = await api("/api/status");
    applySnapshot(data);
  } catch (error) {
    toast(error.message);
  }
}

async function resetDemo() {
  setBusy(loadDemoBtn, true);
  try {
    const data = await api("/api/reset-demo", { method: "POST", body: "{}" });
    state.lastResult = null;
    applySnapshot(data);
    renderEmpty();
    toast("演示数据已重置");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(loadDemoBtn, false);
  }
}

async function login(username, password) {
  try {
    const data = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    state.user = data.user;
    $("userBadge").textContent = data.user;
    toast(`已登录：${data.user}`);
    refresh();
  } catch (error) {
    toast(error.message);
  }
}

async function executeSql(sql) {
  setBusy(runBtn, true);
  try {
    const data = await api("/api/execute", {
      method: "POST",
      body: JSON.stringify({ sql }),
    });
    state.lastResult = data.result;
    applySnapshot(data);
    renderResult(data.result);
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(runBtn, false);
  }
}

async function exportReport() {
  setBusy(exportBtn, true);
  try {
    const response = await fetch("/api/export-report", { credentials: "same-origin" });
    if (!response.ok) throw new Error("导出失败");
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = getFilename(response.headers.get("Content-Disposition")) || "edudbms_report.md";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast("测试报告已导出");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(exportBtn, false);
  }
}

function applySnapshot(data) {
  const tables = data.tables || [];
  state.user = data.user || state.user;
  $("userBadge").textContent = state.user;
  $("optimizerBadge").textContent = data.optimizer ? "Optimizer ON" : "Optimizer OFF";
  $("optimizerBadge").className = data.optimizer ? "badge success" : "badge warn";
  $("statTables").textContent = tables.length;
  $("statRows").textContent = tables.reduce((sum, table) => sum + table.row_count, 0);
  $("statIndexes").textContent = tables.reduce((sum, table) => sum + table.indexes.length, 0);
  $("statTx").textContent = data.transaction_active ? "Active" : "Ready";
  renderSchema(tables);
  renderAudit(data.audit || []);
}

function renderResult(result) {
  $("resultHint").textContent = result.message || result.kind;
  $("compareView").innerHTML = "";
  $("chartView").innerHTML = "";
  $("metricsView").innerHTML = "";
  $("resultTable").innerHTML = "";

  if (result.kind === "compare") {
    renderCompare(result.comparisons || []);
    renderCompareChart(result.comparisons || []);
    renderCompareTrees(result.comparisons || []);
    const [off, on] = result.comparisons || [];
    $("planView").textContent = [
      "Optimizer OFF",
      off?.plan || "",
      "",
      "Optimizer ON",
      on?.plan || "",
    ].join("\n");
    renderTable(on?.columns || [], on?.rows || []);
    return;
  }

  $("planView").textContent = result.plan || result.message || "No execution plan.";
  renderPlanTrees([{ title: "当前计划", tree: result.plan_tree || [] }]);
  renderMetrics(result.metrics || {});
  renderTable(result.columns || [], result.rows || []);
}

function renderEmpty() {
  $("resultHint").textContent = "等待执行 SQL。";
  $("compareView").innerHTML = "";
  $("chartView").innerHTML = "";
  $("metricsView").innerHTML = "";
  $("resultTable").innerHTML = "";
  $("planTreeView").innerHTML = "";
  $("planView").textContent = "Projection\n  IndexScan / SeqScan\n  Filter / Join";
}

function renderCompare(comparisons) {
  const labels = ["优化关闭", "优化开启"];
  $("compareView").innerHTML = comparisons
    .map((item, index) => {
      const m = item.metrics || {};
      const good = index === 1 ? " good" : "";
      return `
        <article class="compare-card${good}">
          <h3>${labels[index] || item.kind}</h3>
          <dl>
            <dt>执行时间</dt><dd>${formatNumber(m.elapsed_ms)} ms</dd>
            <dt>磁盘读页</dt><dd>${m.disk_reads ?? "-"}</dd>
            <dt>访问页数</dt><dd>${m.pages_touched ?? "-"}</dd>
            <dt>返回行数</dt><dd>${m.rows ?? item.row_count ?? "-"}</dd>
          </dl>
        </article>
      `;
    })
    .join("");

  const [off, on] = comparisons;
  if (off && on) {
    const savedReads = (off.metrics.disk_reads ?? 0) - (on.metrics.disk_reads ?? 0);
    const savedTime = (off.metrics.elapsed_ms ?? 0) - (on.metrics.elapsed_ms ?? 0);
    $("metricsView").innerHTML = `
      <div class="metric-card"><span>节省磁盘读页</span><strong>${savedReads}</strong></div>
      <div class="metric-card"><span>节省执行时间</span><strong>${formatNumber(savedTime)} ms</strong></div>
      <div class="metric-card"><span>优化后计划</span><strong>${firstOperator(on.plan_tree) || "-"}</strong></div>
      <div class="metric-card"><span>结果行数</span><strong>${on.metrics.rows ?? on.row_count ?? 0}</strong></div>
    `;
  }
}

function renderCompareChart(comparisons) {
  if (comparisons.length < 2) return;
  const labels = ["关闭优化", "开启优化"];
  const readMax = Math.max(...comparisons.map((item) => Number(item.metrics?.disk_reads || 0)), 1);
  const timeMax = Math.max(...comparisons.map((item) => Number(item.metrics?.elapsed_ms || 0)), 1);
  $("chartView").innerHTML = `
    ${renderBarGroup("磁盘读页", comparisons, labels, "disk_reads", readMax, "")}
    ${renderBarGroup("执行耗时", comparisons, labels, "elapsed_ms", timeMax, " ms")}
  `;
}

function renderBarGroup(title, comparisons, labels, key, max, suffix) {
  const rows = comparisons
    .map((item, index) => {
      const value = Number(item.metrics?.[key] || 0);
      const width = Math.max(4, (value / max) * 100);
      return `
        <div class="bar-row">
          <span>${labels[index]}</span>
          <div class="bar-track"><div class="bar-fill ${index === 1 ? "accent" : ""}" style="width:${width}%"></div></div>
          <strong>${formatNumber(value)}${suffix}</strong>
        </div>
      `;
    })
    .join("");
  return `<article class="chart-card"><h3>${title}</h3>${rows}</article>`;
}

function renderCompareTrees(comparisons) {
  renderPlanTrees([
    { title: "关闭优化", tree: comparisons[0]?.plan_tree || [] },
    { title: "开启优化", tree: comparisons[1]?.plan_tree || [] },
  ]);
}

function renderPlanTrees(groups) {
  $("planTreeView").innerHTML = groups
    .map(
      (group) => `
        <article class="tree-card">
          <h3>${group.title}</h3>
          <div class="tree-root">${renderTreeNodes(group.tree)}</div>
        </article>
      `,
    )
    .join("");
}

function renderTreeNodes(nodes) {
  if (!nodes.length) return `<div class="empty-tree">无计划树</div>`;
  return `<ul>${nodes.map(renderTreeNode).join("")}</ul>`;
}

function renderTreeNode(node) {
  const detail = node.detail ? `<small>${escapeHtml(node.detail)}</small>` : "";
  const children = node.children?.length ? renderTreeNodes(node.children) : "";
  return `
    <li>
      <div class="tree-node ${operatorClass(node.label)}">
        <strong>${escapeHtml(node.label)}</strong>
        ${detail}
      </div>
      ${children}
    </li>
  `;
}

function renderMetrics(metrics) {
  const items = [
    ["执行时间", `${formatNumber(metrics.elapsed_ms)} ms`],
    ["返回行数", metrics.rows],
    ["磁盘读页", metrics.disk_reads],
    ["缓冲命中", metrics.cache_hits],
  ];
  $("metricsView").innerHTML = items
    .map(([label, value]) => `<div class="metric-card"><span>${label}</span><strong>${value ?? "-"}</strong></div>`)
    .join("");
}

function renderTable(columns, rows) {
  if (!rows.length) {
    $("resultTable").innerHTML = "<tbody><tr><td>没有结果行</td></tr></tbody>";
    return;
  }
  const visibleColumns = columns.length ? columns : Object.keys(rows[0]);
  const head = `<thead><tr>${visibleColumns.map((c) => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead>`;
  const body = rows
    .slice(0, 80)
    .map((row) => `<tr>${visibleColumns.map((c) => `<td>${escapeHtml(row[c])}</td>`).join("")}</tr>`)
    .join("");
  $("resultTable").innerHTML = `${head}<tbody>${body}</tbody>`;
}

function renderSchema(tables) {
  if (!tables.length) {
    $("schemaView").innerHTML = `<div class="schema-item">还没有表，点击“重置演示数据”初始化。</div>`;
    return;
  }
  $("schemaView").innerHTML = tables
    .map((table) => {
      const indexes = table.indexes.length
        ? table.indexes.map((idx) => `<span class="pill index">${idx.name}(${idx.column})</span>`).join("")
        : `<span class="pill muted">无索引</span>`;
      const columns = table.columns.map((col) => `<span class="pill">${col.name}:${col.type}</span>`).join("");
      return `
        <article class="schema-item">
          <h3>${table.name}<span>${table.row_count} rows / ${table.page_count} pages</span></h3>
          <div class="pill-row">${columns}</div>
          <div class="pill-row">${indexes}</div>
        </article>
      `;
    })
    .join("");
}

function renderAudit(entries) {
  if (!entries.length) {
    $("auditView").innerHTML = `<div class="audit-item">暂无审计记录。</div>`;
    return;
  }
  $("auditView").innerHTML = entries
    .slice()
    .reverse()
    .map((entry) => {
      const status = entry.success ? "成功" : "失败";
      const klass = entry.success ? "" : " failed";
      return `
        <article class="audit-item${klass}">
          <h3><span>${escapeHtml(entry.user)} · ${status}</span><span>${formatNumber(entry.elapsed_ms)} ms</span></h3>
          <p>${escapeHtml(entry.sql)}</p>
        </article>
      `;
    })
    .join("");
}

function firstOperator(tree) {
  if (!tree?.length) return "";
  const labels = [];
  const walk = (node) => {
    labels.push(node.label);
    (node.children || []).forEach(walk);
  };
  tree.forEach(walk);
  return labels.find((label) => ["IndexScan", "HashJoin", "Sort"].includes(label)) || labels[0];
}

function operatorClass(label) {
  if (label.includes("Index")) return "index";
  if (label.includes("Join")) return "join";
  if (label.includes("Sort")) return "sort";
  if (label.includes("Filter")) return "filter";
  return "";
}

function setBusy(button, busy) {
  button.disabled = busy;
  button.style.opacity = busy ? "0.62" : "1";
}

function toast(message) {
  const node = $("toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    node.hidden = true;
  }, 3200);
}

function getFilename(disposition) {
  const match = /filename="?([^"]+)"?/i.exec(disposition || "");
  return match?.[1];
}

function formatNumber(value) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return "-";
  return Number(value).toFixed(Number(value) >= 10 ? 1 : 3).replace(/\.?0+$/, "");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

refresh();
