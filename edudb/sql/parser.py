"""Recursive-descent SQL parser producing the ast.py tree.

Grammar covered: CREATE TABLE / CREATE [UNIQUE] INDEX, INSERT ... VALUES,
SELECT (DISTINCT, joins, WHERE, GROUP BY, HAVING, ORDER BY, LIMIT/OFFSET,
aggregates, scalar/EXISTS/IN subqueries), UPDATE, DELETE, BEGIN/COMMIT/ROLLBACK
and EXPLAIN. Unsupported forms are rejected with a positioned SYNTAX_ERROR
rather than mis-parsed.
"""
from __future__ import annotations

from ..c_api import DBError
from . import ast
from .tokens import Token, tokenize

_TYPES = {"INT": "INT", "INTEGER": "INT", "FLOAT": "FLOAT", "REAL": "FLOAT",
          "DOUBLE": "FLOAT", "TEXT": "TEXT", "VARCHAR": "TEXT", "CHAR": "TEXT",
          "BOOL": "BOOL", "BOOLEAN": "BOOL"}
_COMPARISONS = {"=", "<>", "!=", "<", "<=", ">", ">="}


def parse(sql: str) -> ast.Node:
    return Parser(tokenize(sql)).parse_program()


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    # --- token helpers ---------------------------------------------------
    @property
    def cur(self) -> Token:
        return self.tokens[self.pos]

    def _next(self) -> Token:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def _error(self, message: str, token: Token | None = None) -> DBError:
        token = token or self.cur
        where = f" near {token.text!r}" if token.type != "EOF" else " at end of input"
        return DBError("SYNTAX_ERROR", message + where, position=token.pos)

    def _at_kw(self, *words: str) -> bool:
        return self.cur.is_keyword(*words)

    def _at_punct(self, ch: str) -> bool:
        return self.cur.type == "PUNCT" and self.cur.value == ch

    def _at_op(self, *ops: str) -> bool:
        return self.cur.type == "OP" and self.cur.value in ops

    def _expect_kw(self, *words: str) -> Token:
        if not self._at_kw(*words):
            raise self._error(f"Expected {' or '.join(words)}")
        return self._next()

    def _expect_punct(self, ch: str) -> Token:
        if not self._at_punct(ch):
            raise self._error(f"Expected {ch!r}")
        return self._next()

    def _expect_ident(self) -> str:
        if self.cur.type != "IDENT":
            raise self._error("Expected an identifier")
        return self._next().value

    def _eat_kw(self, *words: str) -> bool:
        if self._at_kw(*words):
            self._next()
            return True
        return False

    # --- entry -----------------------------------------------------------
    def parse_program(self) -> ast.Node:
        statement = self.parse_statement()
        self._eat_punct_optional(";")
        if self.cur.type != "EOF":
            raise self._error("Only a single SQL statement is supported")
        return statement

    def _eat_punct_optional(self, ch: str) -> None:
        if self._at_punct(ch):
            self._next()

    def parse_statement(self) -> ast.Node:
        if self._at_kw("EXPLAIN"):
            self._next()
            return ast.ExplainStmt(self.parse_statement())
        if self._at_kw("SELECT"):
            return self.parse_select()
        if self._at_kw("INSERT"):
            return self.parse_insert()
        if self._at_kw("UPDATE"):
            return self.parse_update()
        if self._at_kw("DELETE"):
            return self.parse_delete()
        if self._at_kw("CREATE"):
            return self.parse_create()
        if self._at_kw("BEGIN", "START"):
            self._next()
            self._eat_kw("TRANSACTION", "WORK")
            return ast.TxnStmt("BEGIN")
        if self._at_kw("COMMIT", "ROLLBACK"):
            action = self._next().value
            self._eat_kw("WORK", "TRANSACTION")
            return ast.TxnStmt(action)
        raise self._error("Unsupported or unknown statement")

    # --- SELECT ----------------------------------------------------------
    def parse_select(self) -> ast.SelectStmt:
        self._expect_kw("SELECT")
        distinct = self._eat_kw("DISTINCT")
        select_list = [self.parse_select_item()]
        while self._at_punct(","):
            self._next()
            select_list.append(self.parse_select_item())
        stmt = ast.SelectStmt(select_list, distinct=distinct)
        if self._eat_kw("FROM"):
            stmt.from_ = self.parse_from()
        if self._eat_kw("WHERE"):
            stmt.where = self.parse_expr()
        if self._eat_kw("GROUP"):
            self._expect_kw("BY")
            stmt.group_by = [self.parse_expr()]
            while self._at_punct(","):
                self._next()
                stmt.group_by.append(self.parse_expr())
        if self._eat_kw("HAVING"):
            stmt.having = self.parse_expr()
        if self._eat_kw("ORDER"):
            self._expect_kw("BY")
            stmt.order_by = [self.parse_order_item()]
            while self._at_punct(","):
                self._next()
                stmt.order_by.append(self.parse_order_item())
        if self._eat_kw("LIMIT"):
            stmt.limit = self._parse_non_negative_int("LIMIT")
        if self._eat_kw("OFFSET"):
            stmt.offset = self._parse_non_negative_int("OFFSET")
        return stmt

    def _parse_non_negative_int(self, label: str) -> int:
        token = self.cur
        if token.type != "INT" or token.value < 0:
            raise self._error(f"{label} requires a non-negative integer")
        self._next()
        return token.value

    def parse_select_item(self) -> ast.SelectItem:
        if self._at_op("*"):
            pos = self._next().pos
            return ast.SelectItem(ast.Star(None, pos))
        if self.cur.type == "IDENT" and self.tokens[self.pos + 1].value == "." \
                and self.tokens[self.pos + 2].type == "OP" and self.tokens[self.pos + 2].value == "*":
            table = self._next().value
            self._next()  # dot
            pos = self._next().pos  # star
            return ast.SelectItem(ast.Star(table, pos))
        expr = self.parse_expr()
        alias = None
        if self._eat_kw("AS"):
            alias = self._expect_ident()
        elif self.cur.type == "IDENT":
            alias = self._next().value
        return ast.SelectItem(expr, alias)

    def parse_order_item(self) -> ast.OrderItem:
        expr = self.parse_expr()
        descending = False
        if self._eat_kw("ASC"):
            descending = False
        elif self._eat_kw("DESC"):
            descending = True
        nulls_first = None
        if self._eat_kw("NULLS"):
            if self._eat_kw("FIRST"):
                nulls_first = True
            else:
                self._expect_kw("LAST")
                nulls_first = False
        return ast.OrderItem(expr, descending, nulls_first)

    # --- FROM ------------------------------------------------------------
    def parse_from(self) -> ast.Node:
        node = self.parse_table_primary()
        while True:
            join_type = self._parse_join_type()
            if join_type is None:
                return node
            right = self.parse_table_primary()
            on = None
            if join_type != "CROSS":
                self._expect_kw("ON")
                on = self.parse_expr()
            node = ast.JoinRef(node, right, join_type, on)

    def _parse_join_type(self) -> str | None:
        if self._at_kw("CROSS"):
            self._next()
            self._expect_kw("JOIN")
            return "CROSS"
        if self._at_kw("INNER"):
            self._next()
            self._expect_kw("JOIN")
            return "INNER"
        if self._at_kw("LEFT", "RIGHT", "FULL"):
            kind = self._next().value
            self._eat_kw("OUTER")
            self._expect_kw("JOIN")
            return kind
        if self._at_kw("JOIN"):
            self._next()
            return "INNER"
        return None

    def parse_table_primary(self) -> ast.TableRef:
        if self._at_punct("("):
            raise self._error("Derived tables (subqueries in FROM) are not supported")
        pos = self.cur.pos
        name = self._expect_ident()
        alias = None
        if self._eat_kw("AS"):
            alias = self._expect_ident()
        elif self.cur.type == "IDENT":
            alias = self._next().value
        return ast.TableRef(name, alias, pos)

    # --- expressions (precedence climbing) -------------------------------
    def parse_expr(self) -> ast.Node:
        return self.parse_or()

    def parse_or(self) -> ast.Node:
        node = self.parse_and()
        while self._at_kw("OR"):
            pos = self._next().pos
            node = ast.BinaryOp("OR", node, self.parse_and(), pos)
        return node

    def parse_and(self) -> ast.Node:
        node = self.parse_not()
        while self._at_kw("AND"):
            pos = self._next().pos
            node = ast.BinaryOp("AND", node, self.parse_not(), pos)
        return node

    def parse_not(self) -> ast.Node:
        if self._at_kw("NOT"):
            pos = self._next().pos
            return ast.UnaryOp("NOT", self.parse_not(), pos)
        return self.parse_predicate()

    def parse_predicate(self) -> ast.Node:
        node = self.parse_additive()
        while True:
            if self._at_op(*_COMPARISONS):
                op = self._next()
                node = ast.BinaryOp(op.value, node, self.parse_additive(), op.pos)
            elif self._at_kw("IS"):
                pos = self._next().pos
                negated = self._eat_kw("NOT")
                self._expect_kw("NULL")
                node = ast.IsNull(node, negated, pos)
            elif self._at_kw("IN"):
                node = self._parse_in(node, False)
            elif self._at_kw("BETWEEN"):
                node = self._parse_between(node, False)
            elif self._at_kw("NOT") and self.tokens[self.pos + 1].is_keyword("IN", "BETWEEN"):
                self._next()
                if self._at_kw("IN"):
                    node = self._parse_in(node, True)
                else:
                    node = self._parse_between(node, True)
            else:
                return node

    def _parse_in(self, operand: ast.Node, negated: bool) -> ast.Node:
        pos = self._expect_kw("IN").pos
        self._expect_punct("(")
        if self._at_kw("SELECT"):
            sub = self.parse_select()
            self._expect_punct(")")
            return ast.InSubquery(operand, sub, negated, pos)
        items = [self.parse_expr()]
        while self._at_punct(","):
            self._next()
            items.append(self.parse_expr())
        self._expect_punct(")")
        return ast.InList(operand, items, negated, pos)

    def _parse_between(self, operand: ast.Node, negated: bool) -> ast.Node:
        pos = self._expect_kw("BETWEEN").pos
        low = self.parse_additive()
        self._expect_kw("AND")
        high = self.parse_additive()
        return ast.Between(operand, low, high, negated, pos)

    def parse_additive(self) -> ast.Node:
        node = self.parse_multiplicative()
        while self._at_op("+", "-"):
            op = self._next()
            node = ast.BinaryOp(op.value, node, self.parse_multiplicative(), op.pos)
        return node

    def parse_multiplicative(self) -> ast.Node:
        node = self.parse_unary()
        while self._at_op("*", "/", "%"):
            op = self._next()
            node = ast.BinaryOp(op.value, node, self.parse_unary(), op.pos)
        return node

    def parse_unary(self) -> ast.Node:
        if self._at_op("+", "-"):
            op = self._next()
            return ast.UnaryOp(op.value, self.parse_unary(), op.pos)
        return self.parse_primary()

    def parse_primary(self) -> ast.Node:
        token = self.cur
        if token.type in {"INT", "FLOAT", "STRING"}:
            self._next()
            return ast.Literal(token.value, token.pos)
        if self._at_kw("TRUE"):
            self._next()
            return ast.Literal(True, token.pos)
        if self._at_kw("FALSE"):
            self._next()
            return ast.Literal(False, token.pos)
        if self._at_kw("NULL"):
            self._next()
            return ast.Literal(None, token.pos)
        if self._at_kw("EXISTS"):
            self._next()
            self._expect_punct("(")
            sub = self.parse_select()
            self._expect_punct(")")
            return ast.ExistsExpr(sub, False, token.pos)
        if self._at_punct("("):
            self._next()
            if self._at_kw("SELECT"):
                sub = self.parse_select()
                self._expect_punct(")")
                return ast.ScalarSubquery(sub, token.pos)
            inner = self.parse_expr()
            self._expect_punct(")")
            return inner
        if token.type == "IDENT":
            nxt = self.tokens[self.pos + 1]
            if nxt.type == "PUNCT" and nxt.value == "(":
                return self.parse_func_call()
            if nxt.type == "PUNCT" and nxt.value == ".":
                self._next()
                self._next()  # dot
                name = self._expect_ident()
                return ast.ColumnName(token.value, name, token.pos)
            self._next()
            return ast.ColumnName(None, token.value, token.pos)
        raise self._error("Expected an expression")

    def parse_func_call(self) -> ast.FuncCall:
        token = self._next()
        name = token.value
        self._expect_punct("(")
        distinct = False
        star = False
        args: list[ast.Node] = []
        if self._at_op("*"):
            self._next()
            star = True
        elif not self._at_punct(")"):
            distinct = self._eat_kw("DISTINCT")
            args.append(self.parse_expr())
            while self._at_punct(","):
                self._next()
                args.append(self.parse_expr())
        self._expect_punct(")")
        return ast.FuncCall(name, args, distinct, star, token.pos)

    # --- INSERT / UPDATE / DELETE ---------------------------------------
    def parse_insert(self) -> ast.InsertStmt:
        self._expect_kw("INSERT")
        self._expect_kw("INTO")
        table = self._expect_ident()
        columns = None
        if self._at_punct("("):
            self._next()
            columns = [self._expect_ident()]
            while self._at_punct(","):
                self._next()
                columns.append(self._expect_ident())
            self._expect_punct(")")
        if self._at_kw("SELECT"):
            raise self._error("INSERT ... SELECT is not supported; use VALUES")
        self._expect_kw("VALUES")
        rows = [self._parse_value_row()]
        while self._at_punct(","):
            self._next()
            rows.append(self._parse_value_row())
        return ast.InsertStmt(table, columns, rows)

    def _parse_value_row(self) -> list[ast.Node]:
        self._expect_punct("(")
        values = [self.parse_expr()]
        while self._at_punct(","):
            self._next()
            values.append(self.parse_expr())
        self._expect_punct(")")
        return values

    def parse_update(self) -> ast.UpdateStmt:
        self._expect_kw("UPDATE")
        table = self._expect_ident()
        self._expect_kw("SET")
        assignments = [self._parse_assignment()]
        while self._at_punct(","):
            self._next()
            assignments.append(self._parse_assignment())
        where = self.parse_expr() if self._eat_kw("WHERE") else None
        return ast.UpdateStmt(table, assignments, where)

    def _parse_assignment(self) -> tuple[str, ast.Node]:
        column = self._expect_ident()
        if not self._at_op("="):
            raise self._error("Expected '=' in assignment")
        self._next()
        return column, self.parse_expr()

    def parse_delete(self) -> ast.DeleteStmt:
        self._expect_kw("DELETE")
        self._expect_kw("FROM")
        table = self._expect_ident()
        where = self.parse_expr() if self._eat_kw("WHERE") else None
        return ast.DeleteStmt(table, where)

    # --- CREATE ----------------------------------------------------------
    def parse_create(self) -> ast.Node:
        self._expect_kw("CREATE")
        if self._eat_kw("TABLE"):
            return self.parse_create_table()
        unique = self._eat_kw("UNIQUE")
        self._expect_kw("INDEX")
        return self.parse_create_index(unique)

    def parse_create_index(self, unique: bool) -> ast.CreateIndexStmt:
        name = self._expect_ident()
        self._expect_kw("ON")
        table = self._expect_ident()
        self._expect_punct("(")
        column = self._expect_ident()
        self._expect_punct(")")
        return ast.CreateIndexStmt(name, table, column, unique)

    def parse_create_table(self) -> ast.CreateTableStmt:
        name = self._expect_ident()
        self._expect_punct("(")
        columns: list[ast.ColumnDef] = []
        constraints: list[ast.TableConstraint] = []
        while True:
            if self._at_kw("CONSTRAINT"):
                self._next()
                self._expect_ident()  # named constraint; name is not persisted in v1
                constraints.append(self._parse_table_constraint())
            elif self._at_kw("PRIMARY", "UNIQUE", "CHECK", "FOREIGN"):
                constraints.append(self._parse_table_constraint())
            else:
                columns.append(self._parse_column_def(constraints))
            if self._at_punct(","):
                self._next()
                continue
            break
        self._expect_punct(")")
        if not columns:
            raise self._error("A table needs at least one column")
        return ast.CreateTableStmt(name, columns, constraints)

    def _parse_type(self) -> str:
        token = self.cur
        if token.type != "KEYWORD" or token.value not in _TYPES:
            raise self._error("Expected a column type (INT/FLOAT/TEXT/BOOL)")
        self._next()
        if self._at_punct("("):  # VARCHAR(255) etc. - length is ignored in v1
            self._next()
            if self.cur.type != "INT":
                raise self._error("Expected a length")
            self._next()
            self._expect_punct(")")
        return _TYPES[token.value]

    def _parse_column_def(self, constraints: list[ast.TableConstraint]) -> ast.ColumnDef:
        name = self._expect_ident()
        col_type = self._parse_type()
        column = ast.ColumnDef(name, col_type)
        while True:
            if self._at_kw("NOT"):
                self._next()
                self._expect_kw("NULL")
                column.not_null = True
            elif self._at_kw("PRIMARY"):
                self._next()
                self._expect_kw("KEY")
                column.primary_key = True
                column.not_null = True
            elif self._at_kw("UNIQUE"):
                self._next()
                constraints.append(ast.TableConstraint("UNIQUE", [name]))
            elif self._at_kw("CHECK"):
                self._next()
                self._expect_punct("(")
                check = self.parse_expr()
                self._expect_punct(")")
                constraints.append(ast.TableConstraint("CHECK", [], check=check))
            elif self._at_kw("REFERENCES"):
                self._next()
                ref_table = self._expect_ident()
                ref_columns = None
                if self._at_punct("("):
                    self._next()
                    ref_columns = [self._expect_ident()]
                    self._expect_punct(")")
                constraints.append(ast.TableConstraint("FOREIGN_KEY", [name], ref_table, ref_columns))
            else:
                break
        return column

    def _parse_table_constraint(self) -> ast.TableConstraint:
        if self._eat_kw("PRIMARY"):
            self._expect_kw("KEY")
            return ast.TableConstraint("PRIMARY_KEY", self._parse_paren_columns())
        if self._eat_kw("UNIQUE"):
            return ast.TableConstraint("UNIQUE", self._parse_paren_columns())
        if self._eat_kw("CHECK"):
            self._expect_punct("(")
            check = self.parse_expr()
            self._expect_punct(")")
            return ast.TableConstraint("CHECK", [], check=check)
        self._expect_kw("FOREIGN")
        self._expect_kw("KEY")
        columns = self._parse_paren_columns()
        self._expect_kw("REFERENCES")
        ref_table = self._expect_ident()
        ref_columns = self._parse_paren_columns() if self._at_punct("(") else None
        return ast.TableConstraint("FOREIGN_KEY", columns, ref_table, ref_columns)

    def _parse_paren_columns(self) -> list[str]:
        self._expect_punct("(")
        columns = [self._expect_ident()]
        while self._at_punct(","):
            self._next()
            columns.append(self._expect_ident())
        self._expect_punct(")")
        return columns
