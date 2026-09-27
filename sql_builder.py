# -*- coding: utf-8 -*-
"""
Translates the visual builder's clauses into SQL.

Each clause is a dict:
    {"connector": "AND"|"OR", "field": str, "op": str,
     "value": str, "value2": str, "values": [str, ...], "groups": [ids]}

The SQL is written for the ENGINE that evaluates the layer's filter, because each one
behaves differently (all of this was verified with tests against the data):

  sqlite   GeoPackage, SpatiaLite      «=» exact · LIKE is case-insensitive only for
                                        unaccented letters · datetimes stored as text
  ogrsql   Shapefile, GeoJSON,         «=» and IN IGNORE case (so LIKE is used, which is
           FlatGeobuf, FileGDB, Excel…  exact) · ILIKE only for unaccented letters · escape is «!»
  postgres PostgreSQL/PostGIS           «=» exact · ILIKE · escape «\\»
  qgis     memory layers, CSV           QGIS expression engine · dates via
           (delimited text)             to_date()/to_datetime() · «\\» in strings

Semantics guaranteed in ALL formats:
  * «is equal to», «is one of»: exact match (case- and accent-sensitive).
  * «contains», «starts with», «ends with»: case-insensitive (also for
    Á/á, Ñ/ñ…), and the text is matched literally: «_» and «%» are not wildcards.
  * Negations («is not equal to», «is none of», «does not contain») exclude nulls.
"""

import datetime as _dt
import re
from .i18n import tr

try:
    from qgis.core import NULL
except ImportError:  # pragma: no cover
    NULL = None

# key, label, value type ("none" | "one" | "two" | "list")
OPERATORS = [
    ("eq", tr("is equal to"), "one"),
    ("ne", tr("is not equal to"), "one"),
    ("in", tr("is one of (list)"), "list"),
    ("not_in", tr("is none of (list)"), "list"),
    ("contains", tr("contains the text"), "one"),
    ("not_contains", tr("does not contain the text"), "one"),
    ("starts", tr("starts with"), "one"),
    ("ends", tr("ends with"), "one"),
    ("gt", tr("is greater than"), "one"),
    ("ge", tr("is greater than or equal to"), "one"),
    ("lt", tr("is less than"), "one"),
    ("le", tr("is less than or equal to"), "one"),
    ("between", tr("is between"), "two"),
    ("blank", tr("is blank (empty or null)"), "none"),
    ("not_blank", tr("is not blank"), "none"),
    ("null", tr("is null (NULL)"), "none"),
    ("not_null", tr("is not null"), "none"),
]
OP_BY_KEY = {o[0]: o for o in OPERATORS}

_OPS_TEXT = ["eq", "ne", "in", "not_in", "contains", "not_contains", "starts", "ends",
             "blank", "not_blank", "null", "not_null"]
_OPS_ORDERED = ["eq", "ne", "in", "not_in", "gt", "ge", "lt", "le", "between", "null", "not_null"]
_OPS_BOOL = ["eq", "ne", "null", "not_null"]
_OPS_OTHER = ["null", "not_null"]
_LIKE_OPS = ("contains", "not_contains", "starts", "ends")

# OGR formats whose filter is evaluated by the database itself (native SQL)
_OGR_SQLITE = {"GPKG", "SQLITE"}
_OGR_POSTGRES = {"POSTGRESQL"}


class ClauseError(ValueError):
    pass


class Incomplete(ClauseError):
    """The clause still has something to pick: not an error, it is just unfinished."""


# ---------------------------------------------------------------- filter engine
def dialect_of(layer):
    """'sqlite' | 'ogrsql' | 'postgres' | 'qgis', depending on what evaluates the layer's filter."""
    if layer is None:
        return "qgis"
    prov = layer.providerType()
    if prov in ("memory", "delimitedtext", "virtual_memory"):
        return "qgis"
    if prov == "spatialite":
        return "sqlite"
    if prov == "postgres":
        return "postgres"
    if prov == "ogr":
        try:
            storage = (layer.storageType() or "").upper()
        except Exception:
            storage = ""
        if storage in _OGR_SQLITE:
            return "sqlite"
        if storage in _OGR_POSTGRES:
            return "postgres"
        return "ogrsql"
    return "postgres"  # other SQL engines (MSSQL, Oracle…): standard SQL


def _resolve_dialect(layer, dialect):
    if dialect in ("sqlite", "ogrsql", "postgres", "qgis"):
        return dialect
    if dialect == "expression":
        return "qgis"
    return dialect_of(layer)


# ---------------------------------------------------------------- field types
def _type_id(field):
    t = field.type()
    t = getattr(t, "value", t)
    try:
        return int(t)
    except (TypeError, ValueError):
        return -1


def field_kind(field):
    """'text' | 'num' | 'date' | 'datetime' | 'time' | 'bool' | 'other'."""
    if field is None:
        return "text"
    tid = _type_id(field)
    tn = (field.typeName() or "").lower()
    if tid == 1 or tn in ("bool", "boolean", "integer_boolean"):
        return "bool"
    try:
        if field.isNumeric():
            return "num"
    except AttributeError:
        pass
    if tid == 16 or "datetime" in tn or "timestamp" in tn:
        return "datetime"
    if tid == 14 or tn == "date":
        return "date"
    if tid == 15 or tn == "time":
        return "time"
    if tid in (2, 3, 4, 5, 6, 32, 33, 35, 36, 38) or any(
            t in tn for t in ("int", "real", "double", "float", "numeric", "decimal")):
        return "num"
    if tid in (8, 9, 11, 12) or any(t in tn for t in ("list", "map", "blob", "binary", "json", "[]")):
        return "other"
    return "text"


def operators_for_kind(kind):
    """kind: a field_kind() value or 'any' (unknown field: all operators)."""
    if kind == "any":
        keys = [o[0] for o in OPERATORS]
    elif kind == "text":
        keys = _OPS_TEXT
    elif kind in ("num", "date", "datetime", "time"):
        keys = _OPS_ORDERED
    elif kind == "bool":
        keys = _OPS_BOOL
    else:
        keys = _OPS_OTHER
    return [(k, OP_BY_KEY[k][1]) for k in keys]


def _equals_null(v):
    """v == NULL, without failing on types that cannot be compared."""
    try:
        return bool(v == NULL)
    except Exception:
        return False


def is_null(v):
    if v is None:
        return True
    if _equals_null(v):
        return True
    is_null_attr = getattr(v, "isNull", None)
    if callable(is_null_attr):
        try:
            return bool(is_null_attr())
        except Exception:
            return False
    return False


NULL_VALUE = "\u0000null"  # stored in a clause when the user picks <Null> from a value list


def null_label():
    """How <Null> is shown in the value lists (ArcGIS shows it the same way)."""
    return tr("<Null>")


def display_value(value):
    return null_label() if value == NULL_VALUE else value


def stored_value(text):
    return NULL_VALUE if text == null_label() else text


def bool_text(value):
    """True/false word in the UI language (what the dropdowns show)."""
    return tr("true") if value else tr("false")


def value_to_text(v):
    """Converts an attribute value to readable text for the dropdowns."""
    if is_null(v):
        return None
    cls = type(v).__name__
    if cls == "QDate":
        return v.toString("yyyy-MM-dd")
    if cls == "QDateTime":
        return v.toString("yyyy-MM-dd HH:mm:ss")
    if cls == "QTime":
        return v.toString("HH:mm:ss")
    if isinstance(v, bool):
        return bool_text(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


# ---------------------------------------------------------------- values typed by the user
_DATE_PATTERNS = [
    (re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$"), ("y", "m", "d")),
    (re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$"), ("d", "m", "y")),
]
_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2})(?:[.,]\d+)?)?$")
_TRUE = {"true", "verdadero", "verdadeiro", "v", "t", "1", "si", "sí", "sim", "yes", "y"}
_FALSE = {"false", "falso", "f", "0", "no", "n", "não", "nao"}


def _parse_date(text):
    for rx, order in _DATE_PATTERNS:
        m = rx.match(text)
        if m:
            parts = dict(zip(order, (int(g) for g in m.groups())))
            try:
                return _dt.date(parts["y"], parts["m"], parts["d"])
            except ValueError:
                break
    raise ClauseError(tr("«{}» is not a valid date (use YYYY-MM-DD or DD-MM-YYYY).").format(text))


def _parse_time(text):
    m = _TIME.match(text)
    if not m:
        raise ClauseError(tr("«{}» is not a valid time (use HH:MM or HH:MM:SS).").format(text))
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if h > 23 or mi > 59 or s > 59:
        raise ClauseError(tr("«{}» is not a valid time.").format(text))
    return _dt.time(h, mi, s)


def normalize_value(value, kind):
    """Validates what the user typed and returns it in canonical form (text)."""
    if kind == "text" or kind == "other" or kind == "any":
        return "" if value is None else str(value)
    text = "" if value is None else str(value).strip()
    if text == "":
        raise Incomplete(tr("A value is missing."))
    if kind == "num":
        v = text.replace(" ", "")
        if v.count(",") == 1 and "." not in v:
            v = v.replace(",", ".")
        try:
            f = float(v)
        except ValueError:
            raise ClauseError(tr("«{}» is not a valid number.").format(text))
        if f != f or f in (float("inf"), float("-inf")):
            raise ClauseError(tr("«{}» is not a valid number.").format(text))
        return v
    if kind == "date":
        return _parse_date(text.split("T")[0].split(" ")[0]).isoformat()
    if kind == "datetime":
        parts = re.split(r"[ T]+", text, maxsplit=1)
        d = _parse_date(parts[0])
        t = _parse_time(parts[1]) if len(parts) > 1 and parts[1] else _dt.time(0, 0, 0)
        return "{} {}".format(d.isoformat(), t.strftime("%H:%M:%S"))
    if kind == "time":
        return _parse_time(text).strftime("%H:%M:%S")
    if kind == "bool":
        low = text.lower()
        if low in _TRUE:
            return "true"
        if low in _FALSE:
            return "false"
        raise ClauseError(tr("«{}» is not true/false.").format(text))
    return text


def literal(value, kind):
    """Compatibility: standard SQL literal for a value (text in single quotes)."""
    canon = normalize_value(value, kind)
    if kind == "num":
        return canon
    return quote_text(canon)


# ---------------------------------------------------------------- quoting and wildcards
def quote_ident(name):
    return '"' + str(name).replace('"', '""') + '"'


def quote_text(value):
    return "'" + str(value).replace("'", "''") + "'"


def _quote(value, d):
    value = str(value)
    if d == "qgis":
        from qgis.core import QgsExpression
        return QgsExpression.quotedString(value)
    if d == "ogrsql" and ("\\'" in value or value.endswith("\\")):
        raise ClauseError(tr("This format cannot search for a text with «\\» right before a quote or at the end (GDAL limitation)."))
    return quote_text(value)


_ESC = {"sqlite": "\\", "postgres": "\\", "ogrsql": "!", "qgis": "\\"}


def _like_pattern(text, d, lead, trail):
    """LIKE pattern that matches «text» literally. Returns (pattern, needs_escape)."""
    esc = _ESC[d]
    out, needs = [], False
    for ch in text:
        if ch in ("%", "_") or (ch == esc and d != "qgis"):
            out.append(esc + ch)
            needs = True
        else:
            out.append(ch)
    if d == "qgis" and (re.search(r"\\[%_]", text) or (trail and text.endswith("\\"))):
        # the QGIS engine cannot express a «\» right before a wildcard
        raise ClauseError(tr("In this layer type (temporary or delimited text) you cannot search for «\\» right before «%», «_» or at the end of the text."))
    return ("%" if lead else "") + "".join(out) + ("%" if trail else ""), needs


def _escape_clause(needs, d):
    if not needs or d == "qgis":
        return ""
    return " ESCAPE " + quote_text(_ESC[d])


def case_variants(text, d):
    """Upper/lowercase variants of the accented letters (Á/á, Ñ/ñ…).
    SQLite and GDAL only ignore case for unaccented letters; with these variants
    «contains árbol» also finds «Árbol» and «ÁRBOL». QGIS does not need them."""
    if d == "qgis":
        return [text]
    pos = [i for i, ch in enumerate(text)
           if not ch.isascii() and ch.lower() != ch.upper()
           and len(ch.lower()) == 1 and len(ch.upper()) == 1]
    if len(pos) > 6:
        raise ClauseError(tr("The text has too many accented letters to search it case-insensitively; shorten it."))
    base = list(text)
    out = []
    for mask in range(2 ** len(pos)):
        t = base[:]
        for b, p in enumerate(pos):
            t[p] = base[p].upper() if (mask >> b) & 1 else base[p].lower()
        s = "".join(t)
        if s not in out:
            out.append(s)
    return out


# ---------------------------------------------------------------- literals per engine
def _lit(canon, kind, d):
    if kind == "num":
        return canon
    if kind == "bool":
        val = canon == "true"
        if d in ("sqlite", "ogrsql"):
            return "1" if val else "0"
        return "TRUE" if val else "FALSE"
    if d == "qgis":
        fn = {"date": "to_date", "datetime": "to_datetime", "time": "to_time"}.get(kind)
        if fn:
            return "{}({})".format(fn, _quote(canon, d))
    return _quote(canon, d)


def _fexpr(name, kind, d):
    f = quote_ident(name)
    if d == "sqlite" and kind == "datetime":
        # GeoPackage stores «2024-03-03T10:14:00.000»: normalize it so comparisons work
        return "datetime({})".format(f)
    return f


def _exact(f, value, d, negate=False):
    """Exact text equality (case-sensitive) in engine d."""
    if d == "ogrsql":
        pat, needs = _like_pattern(value, d, False, False)
        return "{} {}LIKE {}{}".format(f, "NOT " if negate else "", _quote(pat, d), _escape_clause(needs, d))
    return "{} {} {}".format(f, "<>" if negate else "=", _quote(value, d))


def clause_to_sql(clause, kind, d):
    name = clause.get("field", "")
    if not name:
        raise Incomplete(tr("A clause has no field."))
    op = clause.get("op", "eq")
    f = quote_ident(name)
    fx = _fexpr(name, kind, d)
    not_null = " AND {} IS NOT NULL".format(f)

    # <Null> picked from a value list: IS NULL / IS NOT NULL
    if op in ("eq", "ne") and clause.get("value") == NULL_VALUE:
        return "{} IS {}NULL".format(f, "" if op == "eq" else "NOT ")
    if op in ("in", "not_in") and NULL_VALUE in (clause.get("values") or []):
        rest = [v for v in clause["values"] if v != NULL_VALUE]
        if not rest:
            return "{} IS {}NULL".format(f, "" if op == "in" else "NOT ")
        main = clause_to_sql(dict(clause, values=rest), kind, d)
        # «is none of» already leaves empty values out
        return "({} OR {} IS NULL)".format(main, f) if op == "in" else main

    if op == "null":
        return "{} IS NULL".format(f)
    if op == "not_null":
        return "{} IS NOT NULL".format(f)
    if op == "blank":
        return "({} IS NULL OR {})".format(f, _exact(f, "", d))
    if op == "not_blank":
        return "({}{})".format(_exact(f, "", d, negate=True), not_null)

    if op in _LIKE_OPS:
        if kind not in ("text", "any"):
            raise ClauseError(tr("«{}» only works with text fields.").format(OP_BY_KEY[op][1]))
        txt = "" if clause.get("value") is None else str(clause.get("value"))
        if txt == "":
            raise Incomplete(tr("Type the text to search for in '{}'.").format(name))
        lead, trail = {"contains": (True, True), "not_contains": (True, True),
                       "starts": (False, True), "ends": (True, False)}[op]
        like = "LIKE" if d == "sqlite" else "ILIKE"
        parts = []
        for var in case_variants(txt, d):
            pat, needs = _like_pattern(var, d, lead, trail)
            parts.append((_quote(pat, d), _escape_clause(needs, d)))
        if op == "not_contains":
            body = " AND ".join("{} NOT {} {}{}".format(f, like, p, e) for p, e in parts)
            return "({}{})".format(body, not_null)
        if len(parts) == 1:
            return "{} {} {}{}".format(f, like, parts[0][0], parts[0][1])
        return "(" + " OR ".join("{} {} {}{}".format(f, like, p, e) for p, e in parts) + ")"

    if op in ("in", "not_in"):
        vals = clause.get("values") or []
        if not vals:
            raise Incomplete(tr("Choose at least one value for '{}'.").format(name))
        canon = [normalize_value(v, kind) for v in vals]
        if kind in ("text", "any", "other") and d == "ogrsql":
            if op == "in":
                items = [_exact(f, v, d) for v in canon]
                return items[0] if len(items) == 1 else "(" + " OR ".join(items) + ")"
            items = [_exact(f, v, d, negate=True) for v in canon]
            return "(" + " AND ".join(items) + not_null + ")"
        lits = ", ".join(_lit(v, kind, d) for v in canon)
        if op == "in":
            return "{} IN ({})".format(fx, lits)
        return "({} NOT IN ({}){})".format(fx, lits, not_null)

    if op == "between":
        a = _lit(normalize_value(clause.get("value"), kind), kind, d)
        b = _lit(normalize_value(clause.get("value2"), kind), kind, d)
        return "({fx} >= {a} AND {fx} <= {b})".format(fx=fx, a=a, b=b)

    if op in ("gt", "ge", "lt", "le") and kind in ("text", "bool", "other"):
        raise ClauseError(tr("«{}» does not work with this field type.").format(OP_BY_KEY[op][1]))

    raw = clause.get("value")
    if kind not in ("text", "any", "other") and (raw is None or str(raw).strip() == ""):
        raise Incomplete(tr("Value missing for '{}'.").format(name))
    if raw is None or str(raw) == "":
        # an empty value is almost always an oversight (and would match only empty texts)
        raise Incomplete(tr("Value missing for '{}'. To find empty texts use «is blank».").format(name))
    canon = normalize_value(raw, kind)
    if kind in ("text", "any", "other") and op in ("eq", "ne"):
        if op == "eq":
            return _exact(f, canon, d)
        return "({}{})".format(_exact(f, canon, d, negate=True), not_null)
    sym = {"eq": "=", "ne": "<>", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}[op]
    if op == "ne":
        if d == "ogrsql" and kind in ("date", "datetime", "time"):
            # GDAL gets «<>» wrong with dates (it includes equal ones); NOT (=) works
            return "(NOT ({} = {}){})".format(fx, _lit(canon, kind, d), not_null)
        return "({} <> {}{})".format(fx, _lit(canon, kind, d), not_null)
    return "{} {} {}".format(fx, sym, _lit(canon, kind, d))


def build_sql(clauses, layer, dialect="provider"):
    """Builds the full WHERE clause. Raises ClauseError if something is missing.
    dialect: 'provider' (the layer's engine), 'expression' (QGIS) or a specific engine."""
    if not clauses:
        return ""
    d = _resolve_dialect(layer, dialect)
    fields = layer.fields() if layer is not None else None

    def term(c):
        idx = fields.lookupField(c.get("field", "")) if fields is not None else -1
        kind = field_kind(fields.at(idx)) if idx >= 0 else "any"
        return clause_to_sql(c, kind, d)

    def render(children):
        """(sql, compound) for a list of children (leaves or groups)."""
        terms = []
        for ch in children:
            if "leaf" in ch:
                terms.append((connector_of(ch["leaf"]), term(ch["leaf"]), False))
            else:
                sql, compound = render(ch["children"])
                terms.append((connector_of(first_leaf(ch)), sql, compound))
        return _combine(terms)

    return render(build_tree(clauses, clause_path))[0]


# ---------------------------------------------------------------- nested groups
# Each clause stores the path of groups it belongs to, from outermost to
# innermost:  "groups": [3, 7]  = inside group 3 and, within it, subgroup 7.
# Consecutive clauses that share an id at the same level form that group.
# A group's connector (AND/OR) is the one of its first clause.

def clause_path(c):
    """Group path of a saved clause (compatible with version 1.1: 'group')."""
    g = c.get("groups")
    if g is None:
        old = c.get("group")
        g = [] if old is None else [old]
    return list(g)


def connector_of(c):
    conn = c.get("connector", "AND") if isinstance(c, dict) else c.connector.currentData()
    return "OR" if str(conn).upper() == "OR" else "AND"


def build_tree(items, get_path):
    """Group tree built from the flat list.
    Returns the root's list of children: {"leaf": item} or {"children": [...]}.
    Groups with a single child are dropped (they add nothing)."""
    def build(seq, depth):
        children = []
        i = 0
        while i < len(seq):
            p = get_path(seq[i])
            if len(p) <= depth:
                children.append({"leaf": seq[i]})
                i += 1
                continue
            gid = p[depth]
            j = i
            while j < len(seq):
                pj = get_path(seq[j])
                if len(pj) <= depth or pj[depth] != gid:
                    break
                j += 1
            sub = build(seq[i:j], depth + 1)
            if len(sub) == 1:
                children.append(sub[0])
            else:
                children.append({"children": sub})
            i = j
        return children
    return build(list(items), 0)


def first_leaf(node):
    while "leaf" not in node:
        node = node["children"][0]
    return node["leaf"]


def leaves(node):
    if "leaf" in node:
        return [node["leaf"]]
    out = []
    for ch in node["children"]:
        out.extend(leaves(ch))
    return out


def assign_paths(children, set_path, prefix=(), counter=None):
    """Rewrites the leaves' paths with new, consecutive ids.
    Stores each group's 'path' (tuple), which also gives its depth."""
    if counter is None:
        counter = iter(range(1, 1000000))
    for ch in children:
        if "leaf" in ch:
            set_path(ch["leaf"], list(prefix))
        else:
            gid = next(counter)
            ch["path"] = tuple(prefix) + (gid,)
            assign_paths(ch["children"], set_path, ch["path"], counter)
    return children


def _combine(terms):
    """Joins (connector, sql, compound) terms, with AND binding tighter than OR.
    Parentheses are always written so it is clear what goes with what:
        A  OR  B  AND  C   ->   A OR (B AND C)"""
    def wrap(sql, compound):
        return "(" + sql + ")" if compound else sql

    runs = [[]]
    for i, (c, sql, compound) in enumerate(terms):
        if i > 0 and c == "OR":
            runs.append([])
        runs[-1].append((sql, compound))
    if len(runs) == 1:
        run = runs[0]
        if len(run) == 1:
            return run[0][0], run[0][1]
        return " AND ".join(wrap(*t) for t in run), True
    out = []
    for run in runs:
        if len(run) == 1:
            out.append(wrap(*run[0]))
        else:
            out.append("(" + " AND ".join(wrap(*t) for t in run) + ")")
    return " OR ".join(out), True


def field_problems(layer, names):
    """Fields that cannot be used in a layer filter: missing ones and joined/virtual ones
    (QGIS filters at the data source, which does not know those fields). Returns (missing, foreign)."""
    fields = layer.fields()
    missing, foreign = [], []
    for n in names:
        idx = fields.lookupField(n)
        if idx < 0:
            if str(n).upper() != "FID" and not str(n).startswith("$"):
                missing.append(n)
            continue
        if not is_provider_field(layer, idx):
            foreign.append(fields.at(idx).name())
    return sorted(set(missing)), sorted(set(foreign))


def is_provider_field(layer, idx):
    """Does the field come from the data source? (not joined, not virtual/calculated)."""
    try:
        origin = layer.fields().fieldOrigin(idx)
    except Exception:
        return True
    val = getattr(origin, "value", origin)
    try:
        from qgis.core import QgsFields
        prov = getattr(QgsFields, "OriginProvider", None)
        if prov is None:
            from qgis.core import Qgis
            prov = Qgis.FieldOrigin.Provider
        prov = getattr(prov, "value", prov)
        return int(val) == int(prov)
    except Exception:
        return True


def provider_fields(layer):
    """QgsFields with only the data source's fields (no joined or virtual ones)."""
    from qgis.core import QgsFields
    out = QgsFields()
    for idx, fld in enumerate(layer.fields()):
        if is_provider_field(layer, idx):
            out.append(fld)
    return out


def unknown_fields(layer, sql):
    """Compatibility: fields referenced in the SQL that do not exist in the layer."""
    from . import sql_parser
    return field_problems(layer, sql_parser.referenced_fields(sql, layer))[0]
