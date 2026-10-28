"""Small classroom dataset. No padding text or hidden performance workload."""
NAMES=["张三","李四","王五","李明","赵六","孙七","周八","吴九","郑十","张伟",
       "王芳","李娜","刘洋","陈晨","杨帆","黄磊","周敏","吴刚","徐静","孙丽"]
SCORES=[90,85,78,92,80,66,88,73,95,82,76,89,61,84,77,93,68,86,74,None]
DEPARTMENTS=["计算机","软件","信息安全"]
STUDENTS=[(i+1,name,DEPARTMENTS[(i*2+i//5)%3],SCORES[i]) for i,name in enumerate(NAMES)]
COURSES=[("数据库",4),("操作系统",3),("计算机网络",3)]
ENROLLMENTS=[(i,*COURSES[(i+offset)%3]) for i in range(1,19) for offset in range(2 if i<=12 else 1)]


def literal(value):
    if value is None: return "NULL"
    if isinstance(value,str): return "'"+value.replace("'","''")+"'"
    return str(value)


def sql_rows(rows):
    return ",".join("("+",".join(literal(v) for v in row)+")" for row in rows)
