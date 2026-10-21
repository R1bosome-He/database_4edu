"""Module B: SQL compiler, query planner and rule-based optimizer.

Pipeline: tokenize -> parse (ast) -> bind/plan against a CatalogView (planner)
-> optional optimize (optimizer) -> CompiledStatement (compiler). Output plans
are edudb.c_api dataclasses consumed directly by module C's QueryExecutor.
"""
from __future__ import annotations

from .catalog import (CatalogView, ColumnInfo, IndexDesc, InMemoryCatalog,
                      RuntimeCatalog, TableInfo)
from .compiler import CompileOptions, CompiledStatement, compile_sql, render_plan
from .driver import SqlSession
from .optimizer import optimize
from .parser import parse
from .planner import Planner, Privilege

__all__ = [
    "compile_sql", "CompileOptions", "CompiledStatement", "render_plan",
    "parse", "Planner", "Privilege", "optimize",
    "CatalogView", "TableInfo", "ColumnInfo", "IndexDesc",
    "InMemoryCatalog", "RuntimeCatalog", "SqlSession",
]
