"""SQL tokenizer for module B.

The lexer keeps string literals opaque: keywords, commas and parentheses inside
a quoted TEXT literal are never treated as syntax. Single quotes are escaped by
doubling them (SQL standard), e.g. 'it''s' -> it's.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..c_api import DBError

# Reserved words. Aggregate names (COUNT/SUM/...) are deliberately NOT reserved so
# they can also be used as ordinary identifiers; the parser recognises a call by
# the following "(".
KEYWORDS = {
    "SELECT", "DISTINCT", "FROM", "WHERE", "GROUP", "BY", "HAVING", "ORDER",
    "ASC", "DESC", "NULLS", "FIRST", "LAST", "LIMIT", "OFFSET", "AS",
    "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER", "CROSS", "ON",
    "AND", "OR", "NOT", "IS", "NULL", "IN", "EXISTS", "BETWEEN",
    "INSERT", "INTO", "VALUES", "UPDATE", "SET", "DELETE",
    "CREATE", "TABLE", "INDEX", "UNIQUE", "PRIMARY", "KEY", "FOREIGN",
    "REFERENCES", "CHECK", "CONSTRAINT",
    "INT", "INTEGER", "FLOAT", "REAL", "DOUBLE", "TEXT", "VARCHAR", "CHAR",
    "BOOL", "BOOLEAN", "TRUE", "FALSE",
    "BEGIN", "START", "TRANSACTION", "COMMIT", "ROLLBACK", "WORK", "EXPLAIN",
}

# Multi-character operators must be tried before their single-character prefixes.
_OPERATORS = ["<=", ">=", "<>", "!=", "=", "<", ">", "+", "-", "*", "/", "%"]
_PUNCT = set("(),.;")


@dataclass
class Token:
    type: str   # KEYWORD | IDENT | INT | FLOAT | STRING | OP | PUNCT | EOF
    value: object
    text: str
    pos: int

    def is_keyword(self, *words: str) -> bool:
        return self.type == "KEYWORD" and self.value in words


def _syntax(message: str, pos: int) -> DBError:
    return DBError("SYNTAX_ERROR", message, position=pos)


def tokenize(sql: str) -> list[Token]:
    if not isinstance(sql, str):
        raise DBError("SYNTAX_ERROR", "SQL text must be a string")
    tokens: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":  # line comment
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":  # block comment
            end = sql.find("*/", i + 2)
            if end == -1:
                raise _syntax("Unterminated block comment", i)
            i = end + 2
            continue
        if ch == "'":
            start = i
            i += 1
            chars: list[str] = []
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":  # escaped quote
                        chars.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                chars.append(sql[i])
                i += 1
            else:
                raise _syntax("Unterminated string literal", start)
            tokens.append(Token("STRING", "".join(chars), sql[start:i], start))
            continue
        if ch.isdigit():
            start = i
            while i < n and sql[i].isdigit():
                i += 1
            is_float = False
            if i < n and sql[i] == ".":
                is_float = True
                i += 1
                while i < n and sql[i].isdigit():
                    i += 1
            if i < n and sql[i] in "eE":
                is_float = True
                i += 1
                if i < n and sql[i] in "+-":
                    i += 1
                if i >= n or not sql[i].isdigit():
                    raise _syntax("Malformed number", start)
                while i < n and sql[i].isdigit():
                    i += 1
            text = sql[start:i]
            if i < n and (sql[i].isalpha() or sql[i] == "_"):
                raise _syntax("Malformed number", start)
            tokens.append(Token("FLOAT" if is_float else "INT",
                                float(text) if is_float else int(text), text, start))
            continue
        if ch.isalpha() or ch == "_":
            start = i
            while i < n and (sql[i].isalnum() or sql[i] == "_"):
                i += 1
            text = sql[start:i]
            upper = text.upper()
            if upper in KEYWORDS:
                tokens.append(Token("KEYWORD", upper, text, start))
            else:
                tokens.append(Token("IDENT", text.lower(), text, start))
            continue
        for op in _OPERATORS:
            if sql.startswith(op, i):
                tokens.append(Token("OP", op, op, i))
                i += len(op)
                break
        else:
            if ch in _PUNCT:
                tokens.append(Token("PUNCT", ch, ch, i))
                i += 1
            else:
                raise _syntax(f"Unexpected character {ch!r}", i)
    tokens.append(Token("EOF", None, "", n))
    return tokens
