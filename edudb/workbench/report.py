"""Self-contained, escaped HTML report; no fabricated test or timing claims."""
import html
import json
from datetime import datetime, timezone


def render_report(report):
    esc = lambda value: html.escape(str(value))
    evidence = report["tests"]
    experiments = []
    for item in report["experiments"]:
        cells = "".join(f"<tr><td>{esc(label)}</td><td>{item['summary']['off'][key]:.3f}</td><td>{item['summary']['on'][key]:.3f}</td></tr>"
                        for label, key in [("规划耗时 / ms", "planning_ms"), ("执行耗时 / ms", "execution_ms"),
                                           ("数据页读取", "data_page_reads"), ("索引页读取", "index_page_reads"),
                                           ("总读页", "total_page_reads")])
        prediction=item.get("prediction")
        predicted=""
        if prediction:
            predicted_cells="".join(
                f"<tr><td>{esc(label)}</td><td>{esc(prediction['off'][key])}</td><td>{esc(prediction['on'][key])}</td></tr>"
                for label,key in [("计算量（模型单位）","operations"),("预计访问页数","total_pages"),("时间复杂度","complexity"),("预估保留行数","memory_rows")])
            predicted=f"<h3>执行前预估</h3><table><tr><th>指标</th><th>关闭优化</th><th>开启优化</th></tr>{predicted_cells}</table><p>模型预估不等于实际耗时；下方为执行后的实测数据。</p>"
        experiments.append(f"<section><h2>优化实验 · {esc(item['id'][:8])}</h2><pre>{esc(item['sql_redacted'])}</pre>"
                           f"<p>结果一致：{esc(item['equivalent'])} · {esc(item['cache'])} · 重复 {item['repeats']} 次 · 中位数</p>"
                           f"{predicted}<h3>实验结果</h3><table><thead><tr><th>指标</th><th>关闭优化</th><th>开启优化</th></tr></thead><tbody>{cells}</tbody></table></section>")
    generated = datetime.fromtimestamp(report["generated_at"], timezone.utc).isoformat()
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>EduDB 验收报告</title><style>body{{font:14px/1.7 system-ui,"Microsoft YaHei",sans-serif;color:#24392e;max-width:1000px;margin:50px auto;padding:0 24px}}header{{border-bottom:3px solid #138d75;padding-bottom:25px}}h1{{font-size:32px}}h2{{font-size:20px;margin-top:32px}}small,p{{color:#6a7f70}}table{{border-collapse:collapse;width:100%;text-align:left}}td,th{{padding:12px;border-bottom:1px solid #dce6de}}th{{background:#edf4ee}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f7f4;padding:16px;font:12px/1.7 monospace}}section{{break-inside:avoid}}.status{{color:#138d75;font-weight:bold}}@media print{{body{{margin:0;font-size:11px}}details{{display:block}}pre{{font-size:9px}}}}</style>
<header><h1>数据库实验与验收报告</h1><p>{esc(generated)} · {esc(report['user'])}</p></header>
<section><h2>运行环境</h2><pre>{esc(json.dumps(report['environment'],ensure_ascii=False,indent=2))}</pre></section>
<section><h2>自动化测试证据</h2><p class="status">{esc(evidence.get('status'))} · {esc(evidence.get('tests_run','未运行'))} tests</p>
<p>测试证据仅在源文件指纹匹配时有效；没有执行或指纹过期不标记为通过。</p><pre>{esc(json.dumps(evidence,ensure_ascii=False,indent=2))}</pre></section>
{''.join(experiments) or '<section><h2>优化实验</h2><p>本会话尚未运行优化实验。</p></section>'}
<section><h2>边界与说明</h2><ul>{''.join('<li>'+esc(item)+'</li>' for item in report['limitations'])}</ul><p>审计链末端：{esc(report['audit_tail'])}</p></section>
<details><summary>完整原始 JSON（含逐次实验测量）</summary><pre>{esc(json.dumps(report,ensure_ascii=False,indent=2))}</pre></details></html>'''.encode("utf-8")
