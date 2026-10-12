from __future__ import annotations

import tempfile

from .database import Database
from .formatting import format_result


def run_demo() -> str:
    with tempfile.TemporaryDirectory(prefix="edu_dbms_demo_") as tmp:
        db = Database(tmp, buffer_pages=8)
        db.execute(
            "CREATE TABLE students (id INT, name TEXT, major TEXT, grade INT, PRIMARY KEY (id));"
        )
        db.execute(
            "CREATE TABLE enrollments (student_id INT, course TEXT, score FLOAT);"
        )
        majors = ["database", "security", "ai", "network"]
        student_count = 960
        enrollment_count = 1920
        target_id = 720
        for i in range(1, student_count + 1):
            db.execute(
                "INSERT INTO students (id, name, major, grade) "
                f"VALUES ({i}, 'student_{i}', '{majors[i % len(majors)]}', {2020 + i % 4});"
            )
        for i in range(1, enrollment_count + 1):
            db.execute(
                "INSERT INTO enrollments (student_id, course, score) "
                f"VALUES ({(i % student_count) + 1}, 'DB-{i % 6}', {60 + (i % 41)});"
            )
        db.execute("CREATE INDEX idx_students_id ON students (id);")
        db.execute("CREATE INDEX idx_enrollments_student_id ON enrollments (student_id);")
        query = f"SELECT id, name, major FROM students WHERE id = {target_id};"
        join_query = (
            "SELECT students.name, enrollments.course, enrollments.score "
            "FROM students JOIN enrollments ON students.id = enrollments.student_id "
            f"WHERE students.id = {target_id};"
        )
        parts = [
            "=== 单表索引查询优化对比 ===",
            format_result(db.execute("COMPARE " + query)),
            "",
            "=== JOIN 查询优化对比 ===",
            format_result(db.execute("COMPARE " + join_query)),
            "",
            "=== 安全功能演示 ===",
        ]
        db.execute("CREATE USER reader PASSWORD 'reader123';")
        db.execute("GRANT SELECT ON students TO reader;")
        if db.login("reader", "reader123"):
            parts.append("reader 登录成功；该用户只拥有 students 的 SELECT 权限。")
        parts.append(format_result(db.execute(query, user="reader")))
        return "\n".join(parts)
