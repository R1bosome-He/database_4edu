"""Pre-execution teaching estimates, not observed timings or a cost optimizer."""
from __future__ import annotations

import math


def estimate_plan(plan,statistics):
    nodes=[]
    def walk(node):
        children=[walk(child) for child in node.children]
        inputs=sum(c["rows"] for c in children)
        rows=inputs
        operations=sum(c["operations"] for c in children)
        data_pages=sum(c["data_pages"] for c in children)
        index_pages=sum(c["index_pages"] for c in children)
        memory_rows=max([c["memory_rows"] for c in children]+[0])
        own=0
        complexity="O(n)"
        op=node.op
        if op in {"SeqScan","IndexScan"}:
            stat=statistics.get(node.properties["table_id"],{"rows":1000,"pages":16})
            n,p=stat["rows"],stat["pages"]
            if op=="SeqScan":
                rows=n;own=n;data_pages=p;index_pages=0
            else:
                spec=node.properties.get("range")
                equal=spec and spec.lower is not None and spec.lower==spec.upper
                unique=node.properties["index_id"] in stat.get("unique_indexes",[])
                rows=min(n,1 if unique and equal else math.ceil(n*(.1 if equal else .25)))
                if spec and spec.lower is not None and spec.upper is not None and (spec.lower>spec.upper or (equal and not(spec.lower_inclusive and spec.upper_inclusive))):
                    rows=0
                height=max(1,math.ceil(math.log(max(n,2),8)))
                own=height+rows;data_pages=min(p,rows);index_pages=2+height+math.ceil(rows/8)
                complexity="O(log₈ n + k)"
        elif op=="Values":
            rows=len(node.properties.get("rows",[]));own=rows
        elif op=="Filter":
            rows=math.ceil(inputs/3);own=inputs
            expression=node.expressions[0] if node.expressions else None
            child=node.children[0]
            if child.op=="SeqScan" and expression and expression.kind=="Binary" and expression.operator=="=":
                for ref,value in (expression.args,expression.args[::-1]):
                    if ref.kind=="ColumnRef" and value.kind=="Literal" and value.value is not None:
                        name=next((c.name for c in child.output_columns if c.slot_id==ref.slot_id),None)
                        if name in statistics.get(child.properties["table_id"],{}).get("unique_columns",[]):
                            rows=min(inputs,1)
        elif op=="Projection":
            own=inputs*max(1,len(node.expressions))
        elif op=="Sort":
            own=math.ceil(inputs*math.log2(max(inputs,2)));memory_rows=max(memory_rows,inputs)
            complexity="O(n log n)"
        elif op=="Distinct":
            own=inputs;memory_rows=max(memory_rows,inputs)
        elif op=="Aggregate":
            own=inputs*max(1,len(node.expressions));rows=min(inputs,max(1,math.ceil(math.sqrt(inputs)))) if node.properties.get("group_by") else 1
            memory_rows=max(memory_rows,inputs)
        elif op in {"NestedLoopJoin","HashJoin"}:
            left,right=children
            rows=math.ceil(left["rows"]*right["rows"]*.1)
            kind=node.properties.get("join_type","INNER")
            if kind in {"LEFT","FULL"}: rows=max(rows,left["rows"])
            if kind in {"RIGHT","FULL"}: rows=max(rows,right["rows"])
            own=left["rows"]*right["rows"] if op=="NestedLoopJoin" else left["rows"]+right["rows"]+rows
            complexity="O(n × m + k)" if op=="NestedLoopJoin" else "平均 O(n + m + k)"
            memory_rows=max(memory_rows,right["rows"])
        elif op=="Limit":
            rows=max(0,inputs-node.properties.get("offset",0))
            if node.properties.get("limit") is not None: rows=min(rows,node.properties["limit"])
            own=rows
        operations+=own
        item=dict(node_id=node.node_id,operator=op,estimated_rows=rows,own_operations=own,complexity=complexity)
        nodes.append(item)
        return dict(rows=rows,operations=operations,data_pages=data_pages,index_pages=index_pages,memory_rows=max(memory_rows,rows))
    result=walk(plan)
    dominant=[]
    for item in nodes:
        if item["operator"] in {"NestedLoopJoin","HashJoin","Sort","IndexScan"} and item["complexity"] not in dominant:
            dominant.append(item["complexity"])
    result.update(total_pages=result["data_pages"]+result["index_pages"],
                  complexity=" + ".join(dominant) if dominant else "O(n)",nodes=nodes)
    return result
