"""SQL canonicalization and dependency analysis (owner: Kevin). See docs/interfaces.md.

Canonical text comes from the SQLGlot AST: whitespace, keyword case, comments and
operator spacing are normalized, and table aliases are renamed positionally
(`users u` / `users AS x` -> `users AS _t0`) when that is provably unambiguous.
Table keys are lowercased; merging two keys only over-invalidates, never under.

Statements SQLGlot would rewrite rather than reformat (casts and column types,
GREATEST/LEAST, CREATE TABLE AS) keep their original text as the canonical form.

Assumptions (otherwise a fuzzy hit may be stale), see docs/taxonomy.md:
  - The initial database has no views, triggers or cascading foreign keys.
    Creating a view or trigger in-rollout is rejected as unknown.
  - The tool output does not echo query text inside error messages. Column
    headers are handled: SQLite labels an unaliased result expression with its
    text as typed, so that text is kept in the canonical form (`_labels`).
  - Stored values are never the string 'now' when passed to a date function.
"""

from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.dialects import Dialect
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from fuzzytv.interfaces import CallAnalysis, CallContext, sql_table, unknown

DML = (exp.Insert, exp.Update, exp.Delete)
DDL = (exp.Create, exp.Drop, exp.Alter)
DDL_KINDS = {exp.Create: {"TABLE", "INDEX"}, exp.Drop: {"TABLE"}, exp.Alter: {"TABLE"}}
VOLATILE = (exp.CurrentDate, exp.CurrentTime, exp.CurrentTimestamp, exp.Rand, exp.Uuid)
VOLATILE_NAMES = {"now", "random", "randomblob", "changes", "total_changes", "last_insert_rowid"}
NOW_CALLS = {"date()", "time()", "datetime()", "julianday()", "unixepoch()"}  # SQLite: no arg = now
WRITE_FUNCS = {"nextval", "setval"}
CATALOGS = {"sqlite_master", "sqlite_schema", "sqlite_temp_master", "sqlite_sequence"}
# SQLGlot's generator rewrites these (type names, CAST AS DATE -> DATE(), GREATEST -> MAX),
# which can change SQLite semantics. Statements containing them keep their original text.
REWRITTEN = (exp.DataType, exp.Greatest, exp.Least)
# Read-only PRAGMAs whose output depends only on one table's schema.
TABLE_PRAGMAS = {"table_info", "table_xinfo", "index_list", "foreign_key_list"}
# Tokens that end a top-level select list.
SELECT_LIST_END = {
    "FROM", "WHERE", "GROUP_BY", "HAVING", "WINDOW", "ORDER_BY", "LIMIT",
    "UNION", "EXCEPT", "INTERSECT", "SEMICOLON",
}


class SqlCanonicalizer:
    """Canonicalizer for single SQL statements in `call["sql"]` (or `call["query"]`)."""

    def __init__(self, dialect: str = "sqlite") -> None:
        self.dialect = dialect

    def analyze(self, call: dict[str, Any], ctx: CallContext) -> CallAnalysis:
        sql = call.get("sql", call.get("query"))
        if not isinstance(sql, str):
            return unknown("no sql text")
        try:
            statements = [s for s in sqlglot.parse(sql, read=self.dialect) if s]
        except Exception:  # SQLGlot raises several error types on bad input
            return unknown("sql parse failed")
        if len(statements) != 1:
            return unknown("expected exactly one sql statement")
        try:
            return self._analyze(statements[0], sql, ctx)
        except Exception as error:  # never raise on odd ASTs; fail closed
            return unknown(f"sql analysis failed: {error}")

    def _analyze(self, stmt: exp.Expression, sql: str, ctx: CallContext) -> CallAnalysis:
        if isinstance(stmt, exp.Pragma):
            return _pragma(stmt, ctx)
        if not isinstance(stmt, (exp.Query, *DML, *DDL)):
            return unknown(f"unsupported statement: {stmt.key}")
        if isinstance(stmt, DDL) and stmt.args.get("kind") not in DDL_KINDS[type(stmt)]:
            return unknown(f"unsupported ddl: {stmt.key} {stmt.args.get('kind')}")
        if stmt.find(exp.TemporaryProperty, exp.VirtualProperty):
            return unknown("temporary or virtual table")
        if stmt.find(exp.Into):
            return unknown("SELECT INTO")
        if any(isinstance(n, exp.Anonymous) and n.name.lower() in WRITE_FUNCS for n in stmt.walk()):
            return unknown("sequence function")
        nested = [] if isinstance(stmt, exp.Alter) else list(stmt.walk())[1:]  # DROP COLUMN is fine
        if any(isinstance(n, (*DML, *DDL)) for n in nested):
            return unknown("nested write")

        if isinstance(stmt, DDL):  # CREATE TABLE ... AS SELECT reads its query
            source = stmt.args.get("expression")
            read_nodes = _tables(source) if source else []
            write_nodes = [t for t in _tables(stmt) if all(t is not r for r in read_nodes)]
        else:
            read_nodes = _tables(stmt)
            write_nodes = _tables(stmt.this) if isinstance(stmt, DML) else []

        reads_unknown, reason = False, None
        if any(_volatile(n, self.dialect) for n in stmt.walk()):
            reads_unknown, reason = True, "volatile"
        try:
            reads = frozenset(_key(t, ctx) for t in read_nodes)
            writes = frozenset(_key(t, ctx) for t in write_nodes)
        except LookupError as error:
            if write_nodes:
                return unknown(str(error))
            return CallAnalysis(None, reads_unknown=True, reason=str(error))
        # CREATE TABLE AS also keeps its text: column names come from expression spelling.
        verbatim = stmt.find(*REWRITTEN) or (isinstance(stmt, exp.Create) and stmt.expression)
        labels = None if verbatim else _labels(stmt, sql, self.dialect)
        if verbatim or labels is None:
            canonical = sql.strip()
        else:
            canonical = _canonical(stmt, self.dialect) + labels
        return CallAnalysis(canonical, reads, writes, reads_unknown=reads_unknown, reason=reason)


def _pragma(stmt: exp.Pragma, ctx: CallContext) -> CallAnalysis:
    """`PRAGMA table_info(t)` and similar: a read of table t's schema."""
    node = stmt.this
    if not isinstance(node, exp.EQ) or not isinstance(node.expression, exp.Column):
        return unknown("unsupported statement: pragma")
    name, schema = node.this, None
    if isinstance(name, exp.Dot):
        name, schema = name.expression, name.this
    if not isinstance(name, exp.Var) or name.name.lower() not in TABLE_PRAGMAS:
        return unknown("unsupported statement: pragma")
    column = node.expression
    if column.table or not isinstance(column.this, exp.Identifier):
        return unknown("unsupported pragma argument")
    if not ctx.database:
        return unknown("unknown database")
    if schema is not None and schema.name.lower() not in {"main", ctx.database.lower()}:
        return unknown(f"qualified pragma: {schema.name}")
    table = column.name.lower()
    if table in CATALOGS:
        return CallAnalysis(None, reads_unknown=True, reason=f"system table: {table}")
    canonical = f"PRAGMA {name.name.lower()}({table})"
    return CallAnalysis(canonical, frozenset({sql_table(ctx.database, table)}), frozenset())


def _labels(stmt: exp.Expression, sql: str, dialect: str) -> str | None:
    """Suffix recording the as-typed text of unaliased output expressions.

    SQLite names such a result column after its source text (`sum( a )`,
    `COUNT(*)`), so two spellings return different values. CTE and FROM
    subquery labels count too when they reach the output through `*`.
    Returns "" when no column needs it and None when the text cannot be
    recovered reliably (the caller then keeps the whole statement verbatim).
    """
    if isinstance(stmt, (*DML, *DDL)):
        return None if stmt.find(exp.Returning) else ""
    select = stmt
    while isinstance(select, exp.SetOperation):
        select = select.this
    if not isinstance(select, exp.Select):
        return None
    targets = [select]
    if select.find(exp.Star):
        targets += [s for s in stmt.find_all(exp.Select) if s is not select and _is_source(s, stmt)]
    targets = [t for t in targets if any(map(_needs_label, t.expressions))]
    if not targets:
        return ""
    lists = _select_lists(sql, dialect)
    if lists is None:
        return None
    labels = []
    for target in targets:
        wanted = [p.sql(dialect, comments=False) for p in target.expressions]
        found = {tuple(items) for items in lists if _parses_as(items, wanted, dialect)}
        if len(found) != 1:
            return None
        (items,) = found
        labels += [i for i, p in zip(items, target.expressions, strict=True) if _needs_label(p)]
    return " /* labels: " + " | ".join(labels).replace("*/", "* /") + " */"


def _parses_as(items: list[str], wanted: list[str], dialect: str) -> bool:
    if len(items) != len(wanted):
        return False
    try:
        parsed = [sqlglot.parse_one(i, read=dialect).sql(dialect, comments=False) for i in items]
    except Exception:  # noqa: BLE001 - any failure means no match
        return False
    return parsed == wanted


def _needs_label(projection: exp.Expression) -> bool:
    return not isinstance(projection, (exp.Alias, exp.Column, exp.Star))


def _is_source(select: exp.Select, root: exp.Expression) -> bool:
    """True if `select` feeds rows to an outer query (CTE body or FROM subquery)."""
    node = select.parent
    while node is not None and node is not root:
        if isinstance(node, exp.CTE):
            return True
        if isinstance(node, exp.Subquery) and isinstance(node.parent, (exp.From, exp.Join)):
            return True
        node = node.parent
    return False


def _select_lists(sql: str, dialect: str) -> list[list[str]] | None:
    """Every select list in `sql`, each item as SQLite labels it: from the
    item's first token up to the next comma or clause keyword (or the closing
    parenthesis of a subquery), trailing whitespace removed; comments kept."""
    try:
        tokens = Dialect.get_or_raise(dialect).tokenize(sql)
    except Exception:  # noqa: BLE001
        return None
    lists = []
    for index, token in enumerate(tokens):
        if token.token_type.name != "SELECT":
            continue
        depth, items, first = 0, [], None
        end = len(sql)
        for token in tokens[index + 1 :]:
            kind = token.token_type.name
            if depth == 0 and (kind in SELECT_LIST_END or kind in {"COMMA", "R_PAREN"}):
                if first is None:
                    break
                items.append(sql[first.start : token.start].rstrip())
                first = None
                if kind == "COMMA":
                    continue
                end = -1
                break
            if first is None and depth == 0 and not items and kind in {"DISTINCT", "ALL"}:
                continue
            if first is None:
                first = token
            depth += kind == "L_PAREN"
            depth -= kind == "R_PAREN"
        if end != -1 and first is not None:
            items.append(sql[first.start :].rstrip())
        if items:
            lists.append(items)
    return lists


def _tables(node: exp.Expression) -> list[exp.Table]:
    """Real tables referenced under `node`, excluding CTE references."""
    return [t for t in node.find_all(exp.Table) if not _is_cte(t)]


def _is_cte(table: exp.Table) -> bool:
    """True if `table` names a CTE in scope. References from inside a WITH clause
    count as real tables: over-reporting a read is safe, missing one is not."""
    if table.db:
        return False
    child, node = table, table.parent
    while node is not None:
        with_ = node.args.get("with_") or node.args.get("with")  # renamed in sqlglot 28
        if with_ and with_ is not child and table.name in {c.alias for c in with_.expressions}:
            return True
        child, node = node, node.parent
    return False


def _key(table: exp.Table, ctx: CallContext) -> str:
    if not isinstance(table.this, exp.Identifier):
        raise LookupError("table-valued function")
    if not ctx.database:
        raise LookupError("unknown database")
    # Only spellings of the default database: `main.t` and `t` must share a key.
    if table.catalog or table.db.lower() not in {"", "main", ctx.database.lower()}:
        raise LookupError(f"qualified table: {table.sql()}")
    if table.name.lower() in CATALOGS:
        raise LookupError(f"system table: {table.name}")
    return sql_table(ctx.database, table.name.lower())


def _volatile(node: exp.Expression, dialect: str) -> bool:
    return (
        isinstance(node, VOLATILE)
        or (isinstance(node, exp.Anonymous) and node.name.lower() in VOLATILE_NAMES)
        or (isinstance(node, exp.Func) and node.sql(dialect).lower() in NOW_CALLS)
        or (isinstance(node, exp.Literal) and node.is_string and node.name.lower() == "now")
    )


def _canonical(stmt: exp.Expression, dialect: str) -> str:
    stmt = stmt.copy()
    for table in stmt.find_all(exp.Table):  # column names are left alone: they reach output
        for part in ("this", "db"):  # keep `main.`: some positions reject it (CREATE INDEX ON)
            if isinstance(table.args.get(part), exp.Identifier):
                table.set(part, _normalize(table.args[part], dialect))
    _rename_aliases(stmt)
    return stmt.sql(dialect=dialect, comments=False)


def _normalize(ident: exp.Identifier, dialect: str) -> exp.Identifier:
    """Fold case per dialect; drop quotes only if the bare name resolves the same."""
    folded = normalize_identifiers(ident.copy(), dialect=dialect)
    bare = normalize_identifiers(exp.to_identifier(folded.name), dialect=dialect)
    return bare if bare.name == folded.name else folded


def _rename_aliases(stmt: exp.Expression) -> None:
    """Rename table aliases to _t0, _t1, ... if every use is unambiguous.

    Skipped unless each alias names exactly one table and every identifier
    spelled like an old or new alias is that definition or a column qualifier.
    """
    aliases = [t.args["alias"].this for t in stmt.find_all(exp.Table) if t.alias]
    mapping = {a.name.lower(): f"_t{i}" for i, a in enumerate(aliases)}
    if len(mapping) != len(aliases):
        return
    touched = [
        ident for ident in stmt.find_all(exp.Identifier)
        if ident.name.lower() in mapping or ident.name.lower() in mapping.values()
    ]
    for ident in touched:
        parent = ident.parent
        is_alias = any(ident is a for a in aliases)
        is_qualifier = isinstance(parent, exp.Column) and parent.args.get("table") is ident
        if not (is_alias or is_qualifier) or ident.name.lower() not in mapping:
            return
    for ident in touched:
        ident.replace(exp.to_identifier(mapping[ident.name.lower()]))
