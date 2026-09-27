# -*- coding: utf-8 -*-
"""
Converts a SQL filter (e.g. one made with QGIS «Filter…») into
visual builder clauses, with groups and subgroups.

The SQL is read according to the layer's ENGINE (see sql_builder): e.g. in a
GeoPackage «LIKE» is case-insensitive, but in a Shapefile it is not. Only what the
builder can represent WITHOUT CHANGING the results is converted; otherwise the
reason is returned and the query stays in SQL mode.
"""

import re

from qgis.core import (
    QgsExpression,
    QgsExpressionNodeBinaryOperator as B,
    QgsExpressionNodeColumnRef,
    QgsExpressionNodeInOperator,
    QgsExpressionNodeLiteral,
    QgsExpressionNodeUnaryOperator as U,
)

from . import sql_builder
from .i18n import tr

# internal markers for escaped wildcards (matched literally)
_LIT_PCT = ""
_LIT_UND = ""


class _Unsupported(Exception):
    """Something in the SQL the builder cannot show (the message says what)."""

    def __init__(self, reason=tr("uses something the builder cannot show")):
        super().__init__(reason)
        self.reason = reason


def _fn_name(node):
    try:
        return QgsExpression.Functions()[node.fnIndex()].name().lower()
    except Exception:
        return ""


def _describe(node):
    """Short, plain explanation of why a node does not fit in the builder."""
    name = type(node).__name__
    if name == "QgsExpressionNodeFunction":
        fn = _fn_name(node)
        return tr("uses the function {}()").format(fn) if fn else tr("uses a function")
    if isinstance(node, B):
        if _is_col(node.opLeft()) and _is_col(node.opRight()):
            return tr("uses a comparison between two fields")
        return tr("uses a calculation or operator")
    if name == "QgsExpressionNodeCondition":
        return tr("uses CASE WHEN")
    return tr("uses an expression")


_BO = B.BinaryOperator
_UO = U.UnaryOperator
_CMP = {_BO.boEQ: "eq", _BO.boNE: "ne", _BO.boGT: "gt", _BO.boGE: "ge", _BO.boLT: "lt", _BO.boLE: "le"}
_FLIP = {"eq": "eq", "ne": "ne", "gt": "lt", "ge": "le", "lt": "gt", "le": "ge"}
# operator -> (is_LIKE (not ILIKE), negated)
_LIKE = {_BO.boLike: (True, False), _BO.boNotLike: (True, True),
         _BO.boILike: (False, False), _BO.boNotILike: (False, True)}
_NEGATIONS = {"ne", "not_in", "not_contains", "not_starts", "not_ends", "not_between", "not_blank"}


# ---------------------------------------------------------------- provider SQL -> QGIS syntax
def _read_literal(sql, i, d):
    """Reads a '...' literal starting at sql[i] == "'". Returns (value, end_index)."""
    j = i + 1
    out = []
    while j < len(sql):
        ch = sql[j]
        if ch == "\\" and d == "ogrsql" and j + 1 < len(sql) and sql[j + 1] == "'":
            out.append("'")          # GDAL accepts \' as a quote
            j += 2
            continue
        if ch == "'":
            if j + 1 < len(sql) and sql[j + 1] == "'":
                out.append("'")
                j += 2
                continue
            return "".join(out), j + 1
        out.append(ch)
        j += 1
    raise ValueError(tr("unclosed quote"))


def _decode_like(value, esc):
    """Removes the escapes from a LIKE pattern; escaped wildcards become markers."""
    out, k = [], 0
    while k < len(value):
        ch = value[k]
        if ch == esc and k + 1 < len(value):
            nxt = value[k + 1]
            out.append(_LIT_PCT if nxt == "%" else _LIT_UND if nxt == "_" else nxt)
            k += 2
            continue
        out.append(ch)
        k += 1
    return "".join(out)


_ESCAPE_RX = re.compile(r"\s*ESCAPE\s*'", re.I)


# datetime("f") (version 1.0) and datetime(substr("f", 1, 19)): the field itself
_SQLITE_DT = re.compile(r'\bdatetime\s*\(\s*(?:substr\s*\(\s*("(?:[^"]|"")*")\s*,\s*1\s*,\s*19\s*\)'
                        r'|("(?:[^"]|"")*"))\s*\)', re.I)


def to_expression_syntax(sql, d):
    """Rewrites a provider's SQL so the QGIS parser can read it:
    strings are re-quoted QGIS-style, the ESCAPE clause is resolved
    (its escaped wildcards become literal markers) and, in SQLite,
    datetime("field") is read as the field itself."""
    if d == "qgis":
        return sql
    segs = []   # (is_literal, text)
    for is_lit, text in _segments(sql, d):
        if not is_lit and d in ("sqlite", "virtual"):
            text = _SQLITE_DT.sub(lambda m: m.group(1) or m.group(2), text)
        segs.append(text)
    return "".join(segs)


def _segments(sql, d):
    """Splits the SQL into (is_literal, text) chunks; literals come back already re-quoted."""
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            ident = sql[i:j + 1]
            if out and not out[-1][0]:
                out[-1] = (False, out[-1][1] + ident)
            else:
                out.append((False, ident))
            i = j + 1
            continue
        if ch == "'":
            value, j = _read_literal(sql, i, d)
            m = _ESCAPE_RX.match(sql, j)
            if m:
                esc, k = _read_literal(sql, m.end() - 1, d)
                if len(esc) == 1:
                    value = _decode_like(value, esc)
                    j = k
            out.append((True, QgsExpression.quotedString(value)))
            i = j
            continue
        if out and not out[-1][0]:
            out[-1] = (False, out[-1][1] + ch)
        else:
            out.append((False, ch))
        i += 1
    return out


def referenced_fields(sql, layer):
    """Fields used by a SQL string (empty if it cannot be parsed)."""
    try:
        e = QgsExpression(to_expression_syntax(sql, sql_builder.dialect_of(layer)))
    except ValueError:
        return []
    if e.hasParserError():
        return []
    return [str(c) for c in e.referencedColumns() if c and c != "#!allattributes!#"]


# ---------------------------------------------------------------- nodes
def _flatten(node, op):
    if isinstance(node, B) and node.op() == op:
        return _flatten(node.opLeft(), op) + _flatten(node.opRight(), op)
    return [node]


def _is_col(node):
    if isinstance(node, QgsExpressionNodeColumnRef):
        return True
    # datetime("field") counts as the field itself (that is how GeoPackage compares datetimes)
    return (type(node).__name__ == "QgsExpressionNodeFunction" and _fn_name(node) == "datetime"
            and len(node.args().list()) == 1 and isinstance(node.args().list()[0], QgsExpressionNodeColumnRef))


def _col_name(node):
    if isinstance(node, QgsExpressionNodeColumnRef):
        return node.name()
    return node.args().list()[0].name()


class _Ctx:
    """Layer fields and engine. With allow_missing=True, a missing field does not block the
    conversion: the clause keeps that name so the user can pick the right one."""

    def __init__(self, layer, allow_missing=False):
        self.fields = layer.fields()
        self.d = sql_builder.dialect_of(layer)
        self.allow_missing = allow_missing

    def resolve(self, name):
        idx = self.fields.lookupField(name)
        if idx >= 0:
            return self.fields.at(idx).name()
        if self.allow_missing:
            return name
        raise _Unsupported(tr("field «{}» does not exist in the layer").format(name))

    def kind(self, name):
        idx = self.fields.lookupField(name)
        return sql_builder.field_kind(self.fields.at(idx)) if idx >= 0 else "any"

    def allows(self, name, op):
        """Does the builder offer this operator for this field? (e.g. no «is one of» for
        true/false fields, no «is between» for texts)."""
        return op in {k for k, _label in sql_builder.operators_for_kind(self.kind(name))}


def _col(node, ctx):
    if not _is_col(node):
        raise _Unsupported(_describe(node))
    return ctx.resolve(_col_name(node))


def _is_lit(node):
    if isinstance(node, QgsExpressionNodeLiteral):
        return True
    if isinstance(node, U) and node.op() == _UO.uoMinus and isinstance(node.operand(), QgsExpressionNodeLiteral):
        return True
    if type(node).__name__ != "QgsExpressionNodeFunction" or _fn_name(node) not in ("to_date", "to_datetime", "to_time"):
        return False
    args = node.args().list()
    # QGIS adds the optional arguments (format, language) by itself, with empty values
    return (len(args) >= 1 and isinstance(args[0], QgsExpressionNodeLiteral)
            and all(isinstance(a, QgsExpressionNodeLiteral) and a.value() in (None, "") for a in args[1:]))


def _lit(node, kind="any"):
    """A literal's value as text (None for NULL)."""
    if isinstance(node, U) and node.op() == _UO.uoMinus:
        v = _lit(node.operand(), kind)
        if v is None:
            raise _Unsupported(tr("has an invalid negative number"))
        return "-" + v
    if type(node).__name__ == "QgsExpressionNodeFunction" and _is_lit(node):
        return _lit(node.args().list()[0], kind)
    if not isinstance(node, QgsExpressionNodeLiteral):
        raise _Unsupported(tr("uses a comparison between two fields") if _is_col(node) else _describe(node))
    v = node.value()
    if kind == "bool":
        if isinstance(v, bool):
            return sql_builder.bool_text(v)
        if v in (0, 1) and not isinstance(v, str):
            return sql_builder.bool_text(v == 1)
    if isinstance(v, bool):
        raise _Unsupported(tr("uses TRUE/FALSE values on a field that is not true/false"))
    txt = sql_builder.value_to_text(v)
    if txt is not None and (_LIT_PCT in txt or _LIT_UND in txt):
        txt = txt.replace(_LIT_PCT, "%").replace(_LIT_UND, "_")
    return txt


def _split_pattern(node, ctx):
    """LIKE pattern -> (leading_wildcard, trailing_wildcard, literal_text)."""
    if not isinstance(node, QgsExpressionNodeLiteral) or not isinstance(node.value(), str):
        raise _Unsupported(_describe(node))
    raw = node.value()
    chars = []  # (char, is_wildcard)
    k = 0
    while k < len(raw):
        ch = raw[k]
        if ctx.d == "qgis" and ch == "\\" and k + 1 < len(raw) and raw[k + 1] in "%_":
            chars.append((raw[k + 1], False))
            k += 2
            continue
        if ch == _LIT_PCT:
            chars.append(("%", False))
        elif ch == _LIT_UND:
            chars.append(("_", False))
        else:
            chars.append((ch, ch in "%_"))
        k += 1
    lead = len(chars) >= 1 and chars[0] == ("%", True)
    rest = chars[1:] if lead else chars
    trail = len(rest) >= 1 and rest[-1] == ("%", True)
    inner = rest[:-1] if trail else rest
    if any(w for _c, w in inner):
        raise _Unsupported(tr("uses a LIKE pattern with wildcards in the middle"))
    text = "".join(c for c, _w in inner)
    return lead, trail, text


def _like(node, ctx, is_like, negated):
    """is_like: LIKE operator (True) or ILIKE (False). What it means depends on the engine:
    in SQLite LIKE already ignores case; elsewhere LIKE is case-sensitive and ILIKE is not."""
    field = _col(node.opLeft(), ctx)
    lead, trail, text = _split_pattern(node.opRight(), ctx)
    wild = lead or trail
    if ctx.d in ("sqlite", "virtual"):
        if not is_like:
            raise _Unsupported(tr("uses ILIKE, which this format does not recognize"))
        if not wild:
            raise _Unsupported(tr("uses LIKE without wildcards (case-insensitive in this format)"))
    else:
        if is_like and wild:
            raise _Unsupported(tr("uses LIKE, which is case-sensitive (the builder searches case-insensitively)"))
        if not is_like and not wild:
            raise _Unsupported(tr("uses ILIKE without wildcards (case-insensitive equality)"))
    if not wild:  # exact LIKE = «is equal to»
        return {"field": field, "op": "ne" if negated else "eq", "value": text}
    if text == "":
        raise _Unsupported(tr("has a LIKE without text"))
    if lead and trail:
        op = "not_contains" if negated else "contains"
    elif negated:
        op = "not_starts" if trail else "not_ends"
    else:
        op = "starts" if trail else "ends"
    return {"field": field, "op": op, "value": text}


def _leaf(node, ctx):
    """A simple condition -> clause dict."""
    if isinstance(node, U) and node.op() == _UO.uoNot:
        inner = node.operand()
        if isinstance(inner, B) and inner.op() in _LIKE:
            cs, neg = _LIKE[inner.op()]
            return _like(inner, ctx, cs, not neg)
        if isinstance(inner, QgsExpressionNodeInOperator) and not inner.isNotIn():
            c = _leaf(inner, ctx)
            c["op"] = "not_in"
            return c
        if isinstance(inner, B) and inner.op() == _BO.boEQ:
            c = _leaf(inner, ctx)          # NOT (f = x) -> «is not equal to»
            if c["op"] == "eq":
                c["op"] = "ne"
                return c
        raise _Unsupported(tr("uses NOT before a complex condition"))

    if isinstance(node, QgsExpressionNodeInOperator):
        field = _col(node.node(), ctx)
        kind = ctx.kind(field)
        vals = [_lit(x, kind) for x in node.list().list()]
        if any(v is None for v in vals) or not vals:
            raise _Unsupported(tr("uses an IN list with calculated values"))
        return {"field": field, "op": "not_in" if node.isNotIn() else "in", "values": vals}

    if isinstance(node, B):
        op = node.op()
        if op in (_BO.boIs, _BO.boIsNot):
            if not (isinstance(node.opRight(), QgsExpressionNodeLiteral) and node.opRight().value() is None):
                raise _Unsupported(tr("uses IS with something other than NULL"))
            return {"field": _col(node.opLeft(), ctx), "op": "null" if op == _BO.boIs else "not_null"}
        if op in _LIKE:
            cs, neg = _LIKE[op]
            return _like(node, ctx, cs, neg)
        if op in _CMP:
            left, right, key = node.opLeft(), node.opRight(), _CMP[op]
            if _is_lit(left) and _is_col(right):
                left, right, key = right, left, _FLIP[key]
            field = _col(left, ctx)
            value = _lit(right, ctx.kind(field))
            if value is None:
                # «= NULL» / «<> NULL»: the intent is «is null» / «is not null»
                if key in ("eq", "ne"):
                    return {"field": field, "op": "null" if key == "eq" else "not_null"}
                raise _Unsupported(tr("compares with NULL using < or >"))
            if key in ("ge", "lt") and ctx.kind(field) == "datetime":
                # only the start of the value counts here: «2024-03-04 00:00:00» -> «2024-03-04»
                value = sql_builder.span_text(value) or value
            return {"field": field, "op": key, "value": value}
    raise _Unsupported(_describe(node))


# ---------------------------------------------------------------- re-join what the builder splits apart
def _same_ci(a, b):
    return a.get("value", "").lower() == b.get("value", "").lower()


def _span_clause(field, lo, hi, inside):
    """Date-time range written by the builder -> «is on» / «is between» (inside) or
    «is not on» / «is not between» (outside)."""
    one = sql_builder.span_text(lo, hi)
    if one is not None:
        return {"field": field, "op": "eq" if inside else "ne", "value": one}
    a, b = sql_builder.span_text(lo), sql_builder.span_end_text(hi)
    if a is None or b is None:
        return None
    return {"field": field, "op": "between" if inside else "not_between", "value": a, "value2": b}


def _merge_and(factors, ctx):
    """Within an AND chain:
       ("f" >= a AND "f" <= b)                       -> is between
       ("f" >= a AND "f" < b)  (date-time)           -> is on / is between
       ("f" NOT LIKE v1 AND "f" NOT LIKE v2 …)        -> does not contain (Á/á variants)
       ("f" NOT LIKE 'a' AND "f" NOT LIKE 'b' …)      -> is none of (GDAL engine)
       (<negation> AND "f" IS NOT NULL)              -> the negation (already excludes nulls)"""
    out = []
    for kind, c in factors:
        if kind == "leaf" and out and out[-1][0] == "leaf":
            prev = out[-1][1]
            same = prev["field"] == c["field"]
            if same and c["op"] == "not_null" and prev["op"] in _NEGATIONS:
                if prev["op"] == "ne" and prev.get("value") == "":
                    out[-1] = ("leaf", {"field": prev["field"], "op": "not_blank"})
                continue
            if same and prev["op"] == "ge" and c["op"] == "lt" and ctx.kind(c["field"]) == "datetime":
                merged = _span_clause(c["field"], prev["value"], c["value"], True)
                if merged is not None:
                    out[-1] = ("leaf", merged)
                    continue
            if same and prev["op"] == "ge" and c["op"] == "le":
                out[-1] = ("leaf", {"field": c["field"], "op": "between",
                                    "value": prev["value"], "value2": c["value"]})
                continue
            if same and prev["op"] == c["op"] and c["op"] in ("not_contains", "not_starts", "not_ends") \
                    and _same_ci(prev, c):
                continue
            if same and prev["op"] in ("ne", "not_in") and c["op"] == "ne" and c.get("value") != "" \
                    and prev.get("value") != "":
                vals = prev.get("values") or [prev["value"]]
                out[-1] = ("leaf", {"field": c["field"], "op": "not_in", "values": vals + [c["value"]]})
                continue
        out.append((kind, c))
    return out


def _merge_or(parts, ctx):
    """Across OR parts that are single conditions:
       Á/á variants of «contains/starts/ends»        -> a single condition
       ("f" IS NULL OR "f" = '')                      -> is blank
       ("f" LIKE 'a' OR "f" LIKE 'b')  (GDAL engine)  -> is one of"""
    out = []
    for part in parts:
        if len(part) == 1 and part[0][0] == "leaf" and out and len(out[-1]) == 1 and out[-1][0][0] == "leaf":
            prev, c = out[-1][0][1], part[0][1]
            if prev["field"] == c["field"]:
                if prev["op"] == c["op"] and c["op"] in ("contains", "starts", "ends") and _same_ci(prev, c):
                    continue
                if prev["op"] == "null" and c["op"] == "eq" and c.get("value") == "":
                    out[-1] = [("leaf", {"field": c["field"], "op": "blank"})]
                    continue
                if prev["op"] in ("eq", "in") and c["op"] == "null" and ctx.allows(c["field"], "in"):
                    # … OR "f" IS NULL -> <Null> in the list
                    vals = prev.get("values") or [prev["value"]]
                    out[-1] = [("leaf", {"field": c["field"], "op": "in", "values": vals + [sql_builder.NULL_VALUE]})]
                    continue
                if (ctx.d == "ogrsql" or ctx.kind(c["field"]) == "datetime") and ctx.allows(c["field"], "in") \
                        and prev["op"] in ("eq", "in") and c["op"] == "eq":
                    vals = prev.get("values") or [prev["value"]]
                    out[-1] = [("leaf", {"field": c["field"], "op": "in", "values": vals + [c["value"]]})]
                    continue
        out.append(part)
    return out


def _outside(sub, ctx):
    """("f" < a OR "f" > b): what the builder writes for «is not between»; for date-times
    ("f" < a OR "f" >= b): «is not on» / «is not between». Returns the clause or None."""
    if len(sub) != 2 or sub[1][0] != "OR" or sub[0][1][0] != "leaf" or sub[1][1][0] != "leaf":
        return None
    a, b = sub[0][1][1], sub[1][1][1]
    if a["field"] != b["field"] or a["op"] != "lt":
        return None
    if b["op"] == "gt" and ctx.kind(a["field"]) != "datetime" and ctx.allows(a["field"], "not_between"):
        return {"field": a["field"], "op": "not_between", "value": a["value"], "value2": b["value"]}
    if b["op"] == "ge" and ctx.kind(a["field"]) == "datetime":
        return _span_clause(a["field"], a["value"], b["value"], False)
    return None


def _terms(node, ctx):
    """List of (connector, ("leaf", clause) | ("group", terms))."""
    parts = []
    for or_part in _flatten(node, _BO.boOr):
        factors = []
        for f in _flatten(or_part, _BO.boAnd):
            if isinstance(f, B) and f.op() == _BO.boOr:
                sub = _terms(f, ctx)
                out_clause = _outside(sub, ctx)
                if len(sub) == 1 and sub[0][1][0] == "leaf":
                    factors.append(sub[0][1])      # e.g. the Á/á variants of «contains»
                elif out_clause is not None:
                    factors.append(("leaf", out_clause))
                else:
                    factors.append(("group", sub))
            else:
                factors.append(("leaf", _leaf(f, ctx)))
        parts.append(_merge_and(factors, ctx))
    terms = []
    for i, part in enumerate(_merge_or(parts, ctx)):
        for j, fac in enumerate(part):
            terms.append(("OR" if (j == 0 and i > 0) else "AND", fac))
    return terms


def _emit(terms, prefix, counter, out):
    for conn, (kind, payload) in terms:
        if kind == "leaf":
            c = {"connector": "AND", "field": "", "op": "eq", "value": "", "value2": "", "values": []}
            c.update(payload)
            c["connector"] = conn
            c["groups"] = list(prefix)
            out.append(c)
        else:
            gid = next(counter)
            start = len(out)
            _emit(payload, prefix + [gid], counter, out)
            out[start]["connector"] = conn


def parse(sql, layer, allow_missing=False):
    """(clauses, None) if the SQL fits in the builder, or (None, reason).
    allow_missing=True: fields missing from the layer do not block the conversion."""
    sql = (sql or "").strip()
    if not sql or layer is None:
        return None, tr("there is no SQL")
    ctx = _Ctx(layer, allow_missing)
    try:
        text = to_expression_syntax(sql, ctx.d)
    except ValueError:
        return None, tr("has a typo (check quotes, parentheses and operators)")
    e = QgsExpression(text)
    if e.hasParserError() or e.rootNode() is None:
        return None, tr("has a typo (check quotes, parentheses and operators)")
    try:
        terms = _terms(e.rootNode(), ctx)
    except _Unsupported as err:
        return None, err.reason
    out = []
    _emit(terms, [], iter(range(1, 1000000)), out)
    if not out:
        return None, tr("there are no conditions")
    for c in out:
        if c.get("op") == "eq" and c.get("value") == "":
            c["value"] = sql_builder.EMPTY_VALUE     # «= ''» -> <Empty>
        elif c.get("op") == "ne" and c.get("value") == "":
            # «<> ''» already excludes nulls (a comparison with NULL never holds): = «is not blank»
            c["op"] = "not_blank"
            c.pop("value", None)
        elif c.get("op") in ("in", "not_in"):
            c["values"] = [sql_builder.EMPTY_VALUE if v == "" else v for v in c.get("values") or []]
    for c in out:
        if not ctx.allows(c["field"], c["op"]):
            return None, tr("uses «{}» on the field «{}», which the builder does not offer for its type").format(
                sql_builder.OP_BY_KEY[c["op"]][1], c["field"])
    try:
        sql_builder.build_sql(out, layer, "provider")
    except sql_builder.ClauseError as err:
        return None, tr("has a condition the builder cannot build ({})").format(
            str(err).rstrip("."))
    return out, None


def reason_message(reason):
    """Sentence to show the user, e.g.
    «Uses the function upper(), which the builder does not support.»"""
    if not reason:
        return ""
    msg = reason[0].upper() + reason[1:]
    if reason.startswith((tr("uses "), tr("compares "))) and "," not in reason:  # not twice «, which…»
        msg += tr(", which the builder does not support")
    return msg + "."


def sql_to_clauses(sql, layer):
    """Clauses equivalent to the SQL, or None if it cannot be represented."""
    return parse(sql, layer)[0]
