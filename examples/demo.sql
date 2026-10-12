CREATE TABLE students (id INT, name TEXT, major TEXT, grade INT, PRIMARY KEY (id));
CREATE TABLE enrollments (student_id INT, course TEXT, score FLOAT);

INSERT INTO students (id, name, major, grade) VALUES (1, 'Ada', 'database', 2024);
INSERT INTO students (id, name, major, grade) VALUES (2, 'Grace', 'security', 2023);
INSERT INTO students (id, name, major, grade) VALUES (3, 'Linus', 'database', 2022);

INSERT INTO enrollments (student_id, course, score) VALUES (1, 'DBMS', 96.5);
INSERT INTO enrollments (student_id, course, score) VALUES (2, 'Security', 91.0);
INSERT INTO enrollments (student_id, course, score) VALUES (1, 'Systems', 94.0);

CREATE INDEX idx_students_id ON students (id);
CREATE INDEX idx_enrollments_student_id ON enrollments (student_id);

EXPLAIN OPTIMIZER ON SELECT id, name FROM students WHERE id = 1;
COMPARE SELECT id, name FROM students WHERE id = 1;
COMPARE SELECT students.name, enrollments.course, enrollments.score FROM students JOIN enrollments ON students.id = enrollments.student_id WHERE students.id = 1;
SELECT id, name FROM students ORDER BY id DESC;
BEGIN;
UPDATE students SET major = 'security' WHERE id = 1;
ROLLBACK;
DELETE FROM enrollments WHERE score < 62;
