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
  virtual  virtual layers               SQLite, but a plain «col LIKE 'x'» is handed to
                                        QGIS, which makes it case-sensitive: every LIKE
                                        gets an ESCAPE clause so SQLite evaluates it itself

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
    ("not_starts", tr("does not start with"), "one"),
    ("ends", tr("ends with"), "one"),
    ("not_ends", tr("does not end with"), "one"),
    ("gt", tr("is greater than"), "one"),
    ("ge", tr("is greater than or equal to"), "one"),
    ("lt", tr("is less than"), "one"),
    ("le", tr("is less than or equal to"), "one"),
    ("between", tr("is between"), "two"),
    ("not_between", tr("is not between"), "two"),
    ("blank", tr("is blank (empty or null)"), "none"),
    ("not_blank", tr("is not blank"), "none"),
    ("null", tr("is null (NULL)"), "none"),
    ("not_null", tr("is not null"), "none"),
]
OP_BY_KEY = {o[0]: o for o in OPERATORS}

_OPS_TEXT = ["eq", "ne", "in", "not_in", "contains", "not_contains", "starts", "not_starts", "ends", "not_ends",
             "blank", "not_blank", "null", "not_null"]
_OPS_ORDERED = ["eq", "ne", "in", "not_in", "gt", "ge", "lt", "le", "between", "not_between", "null", "not_null"]
# dates read better with ArcGIS-style words
_DATE_LABELS = {"eq": tr("is on"), "ne": tr("is not on"), "gt": tr("is after"), "ge": tr("is on or after"),
                "lt": tr("is before"), "le": tr("is on or before")}
_OPS_BOOL = ["eq", "ne", "null", "not_null"]
_OPS_OTHER = ["null", "not_null"]
_LIKE_OPS = ("contains", "not_contains", "starts", "not_starts", "ends", "not_ends")

# OGR formats whose filter is evaluated by the database itself (native SQL)
_OGR_SQLITE = {"GPKG", "SQLITE"}
_OGR_POSTGRES = {"POSTGRESQL"}


class ClauseError(ValueError):
    pass


class Incomplete(ClauseError):
    """The clause still has something to pick: not an error, it is just unfinished."""


# ---------------------------------------------------------------- filter engine
def dialect_of(layer):
    """'sqlite' | 'ogrsql' | 'postgres' | 'qgis' | 'virtual', depending on what evaluates
    the layer's filter."""
    if layer is None:
        return "qgis"
    prov = layer.providerType()
    if prov in ("memory", "delimitedtext", "virtual_memory"):
        return "qgis"
    if prov == "spatialite":
        return "sqlite"
    if prov == "virtual":
        return "virtual"
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


# Data sources covered by the tests in tests/. Any other database applies its own rules
# (e.g. SQL Server usually ignores upper/lower case in «=»), so the dialog says so.
_TESTED_PROVIDERS = {"ogr", "spatialite", "postgres", "delimitedtext", "memory", "virtual"}
_OGR_DATABASES = {"MSSQLSPATIAL", "OCI", "MYSQL", "ODBC", "PGEO", "HANA", "WFS", "OAPIF", "ESRIJSON",
                  "ELASTICSEARCH", "MONGODBV3", "CARTO", "NGW"}


def untested_source(layer):
    """Name of the layer's data source type if the filters were not tested on it, else ''."""
    if layer is None:
        return ""
    prov = layer.providerType()
    if prov not in _TESTED_PROVIDERS:
        return prov
    if prov == "ogr":
        try:
            storage = layer.storageType() or ""
        except Exception:
            storage = ""
        if storage.upper() in _OGR_DATABASES:
            return storage
    return ""


def _resolve_dialect(layer, dialect):
    if dialect in ("sqlite", "ogrsql", "postgres", "qgis", "virtual"):
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
    if kind in ("date", "datetime"):
        return [(k, _DATE_LABELS.get(k, OP_BY_KEY[k][1])) for k in keys]
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


NULL_VALUE = "\u0000null"    # stored in a clause when the user picks <Null> from a value list
EMPTY_VALUE = "\u0000empty"  # ... and when the user picks <Empty> (a text with nothing written: '')
SPECIAL_VALUES = (NULL_VALUE, EMPTY_VALUE)


def null_label():
    """How <Null> is shown in the value lists (ArcGIS shows it the same way)."""
    return tr("<Null>")


def empty_label():
    """How an empty text ('') is shown in the value lists."""
    return tr("<Empty>")


EDGE_SPACE = "\u2423"   # «␣»: a space at the start or end of a value, made visible


def display_value(value):
    """How a value is shown in the lists. Spaces at the start or end are shown as «␣»,
    otherwise «Vega» and «Vega » would look the same."""
    if value == NULL_VALUE:
        return null_label()
    if value == EMPTY_VALUE:
        return empty_label()
    if isinstance(value, str) and value != value.strip(" "):
        core = value.strip(" ")
        if not core:
            return EDGE_SPACE * len(value)
        lead = len(value) - len(value.lstrip(" "))
        trail = len(value) - len(value.rstrip(" "))
        return EDGE_SPACE * lead + core + EDGE_SPACE * trail
    return value


def stored_value(text):
    """Inverse of display_value, for what is typed in a value box."""
    if text == null_label():
        return NULL_VALUE
    if text == empty_label():
        return EMPTY_VALUE
    if isinstance(text, str) and (text.startswith(EDGE_SPACE) or text.endswith(EDGE_SPACE)):
        core = text.strip(EDGE_SPACE)
        lead = len(text) - len(text.lstrip(EDGE_SPACE))
        trail = len(text) - len(text.rstrip(EDGE_SPACE))
        return " " * lead + core + " " * trail if core else " " * len(text)
    return text


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
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    return str(v)   # floats: the shortest text that reads back as the same number


# ---------------------------------------------------------------- values typed by the user
_DATE_PATTERNS = [
    (re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$"), ("y", "m", "d")),
    (re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$"), ("d", "m", "y")),
]
_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2})(?:[.,]\d+)?)?(?:Z|[+-]\d{2}(?::?\d{2})?)?$")
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


def datetime_span(value):
    """What a date-time value typed or picked by the user covers, as [start, end):
    a date alone is the whole day, «HH:MM» the whole minute and «HH:MM:SS» that second.
    So «is on 2024-03-03» finds every time of that day, and a value picked from the list
    also finds the features whose time has milliseconds (the list shows whole seconds).
    end is None when the span reaches the last possible moment (e.g. 9999-12-31)."""
    text = "" if value is None else str(value).strip()
    if text == "":
        raise Incomplete(tr("A value is missing."))
    parts = re.split(r"[ T]+", text, maxsplit=1)
    d = _parse_date(parts[0])
    start = _dt.datetime(d.year, d.month, d.day)
    if len(parts) == 1 or not parts[1]:
        step = _dt.timedelta(days=1)
    else:
        t = _parse_time(parts[1])
        start = start.replace(hour=t.hour, minute=t.minute, second=t.second)
        step = _dt.timedelta(minutes=1) if _TIME.match(parts[1]).group(3) is None else _dt.timedelta(seconds=1)
    try:
        return start, start + step
    except OverflowError:
        return start, None


def datetime_text(x, precision="second"):
    """«YYYY-MM-DD HH:MM:SS» (or shorter), with the year always in 4 digits
    (strftime does not pad years before 1000 on every system)."""
    day = "{:04d}-{:02d}-{:02d}".format(x.year, x.month, x.day)
    if precision == "day":
        return day
    if precision == "minute":
        return "{} {:02d}:{:02d}".format(day, x.hour, x.minute)
    return "{} {:02d}:{:02d}:{:02d}".format(day, x.hour, x.minute, x.second)


def _moment(text):
    """Start of a date-time value in any accepted form, or None."""
    try:
        return datetime_span(text)[0]
    except ClauseError:
        return None


def span_text(start, end=None):
    """Inverse of datetime_span, for reading SQL back into the builder: the shortest value
    whose span starts at «start» (and ends at «end», if given), or None if there is none."""
    a = _moment(start)
    b = _moment(end) if end is not None else None
    if a is None or (end is not None and b is None):
        return None
    if end is None:
        if a.time() == _dt.time(0, 0):
            return datetime_text(a, "day")
        return datetime_text(a, "minute" if a.second == 0 else "second")
    if b - a == _dt.timedelta(days=1) and a.time() == _dt.time(0, 0):
        return datetime_text(a, "day")
    if b - a == _dt.timedelta(minutes=1) and a.second == 0:
        return datetime_text(a, "minute")
    if b - a == _dt.timedelta(seconds=1):
        return datetime_text(a, "second")
    return None


def span_end_text(end):
    """The value whose span ENDS at «end» (for the second value of «is between»)."""
    b = _moment(end)
    if b is None:
        return None
    try:
        if b.time() == _dt.time(0, 0):
            return datetime_text(b - _dt.timedelta(days=1), "day")
        if b.second == 0:
            return datetime_text(b - _dt.timedelta(minutes=1), "minute")
        return datetime_text(b - _dt.timedelta(seconds=1), "second")
    except OverflowError:
        return None


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


_ESC = {"sqlite": "\\", "postgres": "\\", "ogrsql": "!", "qgis": "\\", "virtual": "\\"}
_SQLITE = ("sqlite", "virtual")


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
    if d == "virtual":
        # without ESCAPE, QGIS evaluates «col LIKE 'x'» itself and it becomes case-sensitive
        return " ESCAPE " + quote_text(_ESC[d])
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
        if d in ("sqlite", "ogrsql", "virtual"):
            return "1" if val else "0"
        return "TRUE" if val else "FALSE"
    if d == "qgis":
        fn = {"date": "to_date", "datetime": "to_datetime", "time": "to_time"}.get(kind)
        if fn:
            return "{}({})".format(fn, _quote(canon, d))
    return _quote(canon, d)


def _fexpr(name, kind, d):
    f = quote_ident(name)
    if d in _SQLITE and kind == "datetime":
        # GeoPackage stores «2024-03-03T10:14:00.000» (sometimes with «Z» or «-03:00»):
        # normalize it to «2024-03-03 10:14:00», the time QGIS shows, so comparisons work
        return "datetime(substr({}, 1, 19))".format(f)
    return f


def _datetime_sql(op, clause, f, fx, d):
    """Date-time conditions as ranges [start, end) (see datetime_span). A span that reaches
    the last possible moment (9999-12-31) has no end: nothing comes after it."""
    not_null = " AND {} IS NOT NULL".format(f)

    def lit(x):
        return _lit(datetime_text(x), "datetime", d)

    def inside(a, b):
        if b is None:
            return "{} >= {}".format(fx, lit(a))
        return "({fx} >= {a} AND {fx} < {b})".format(fx=fx, a=lit(a), b=lit(b))

    def outside(a, b):
        if b is None:
            return "{} < {}".format(fx, lit(a))
        return "({fx} < {a} OR {fx} >= {b})".format(fx=fx, a=lit(a), b=lit(b))

    if op in ("in", "not_in"):
        spans = [datetime_span(v) for v in clause.get("values") or []]
        if op == "in":
            items = [inside(a, b) for a, b in spans]
            return items[0] if len(items) == 1 else "(" + " OR ".join(items) + ")"
        return "(" + " AND ".join(outside(a, b) for a, b in spans) + not_null + ")"
    if op in ("between", "not_between"):
        a, b = datetime_span(clause.get("value"))[0], datetime_span(clause.get("value2"))[1]
        if op == "between":
            return inside(a, b)
        return "({}{})".format(outside(a, b), not_null)
    a, b = datetime_span(clause.get("value"))
    if op == "eq":
        return inside(a, b)
    if op == "ne":
        return "({}{})".format(outside(a, b), not_null)
    if op == "gt":
        return "{} >= {}".format(fx, lit(b)) if b is not None else "({} IS NULL AND {} IS NOT NULL)".format(f, f)
    if op == "le":
        return "{} < {}".format(fx, lit(b)) if b is not None else "{} IS NOT NULL".format(f)
    return "{} {} {}".format(fx, ">=" if op == "ge" else "<", lit(a))


# GDAL (Shapefile, GeoJSON…) compares texts with «=» and IN ignoring upper/lower case, but only
# for the letters a-z: a text without them is matched exactly by IN, which is fast. Texts with
# a-z need LIKE (exact), which GDAL evaluates slowly: one LIKE per value for every feature.
_ASCII_LETTER = re.compile(r"[A-Za-z]")
MAX_EXACT_LIST = 32   # above this many texts with a-z, «is one of» uses IN (lint warns if that matters)


def has_ascii_letters(text):
    return bool(_ASCII_LETTER.search(str(text)))


def ascii_fold(text):
    """Lowercase for a-z only (how GDAL compares texts with «=» and IN)."""
    return re.sub(r"[A-Z]", lambda m: m.group(0).lower(), str(text))


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

    # <Null> / <Empty> picked from a value list
    vtype = OP_BY_KEY.get(op, (None, None, "one"))[2]
    for special in SPECIAL_VALUES:
        if vtype in ("one", "two") and op not in ("eq", "ne") and special in (clause.get("value"), clause.get("value2")):
            raise ClauseError(tr("«{}» can only be used with «is equal to», «is not equal to», «is one of» and «is none of».")
                              .format(display_value(special)))
    picked = [clause.get("value")] if op in ("eq", "ne") else (clause.get("values") or []) if op in ("in", "not_in") else []
    if EMPTY_VALUE in picked and kind not in ("text", "any", "other"):
        raise ClauseError(tr("«{}» only applies to text fields.").format(empty_label()))
    if op in ("eq", "ne") and clause.get("value") == NULL_VALUE:
        return "{} IS {}NULL".format(f, "" if op == "eq" else "NOT ")
    if op in ("eq", "ne") and clause.get("value") == EMPTY_VALUE:
        if op == "eq":
            return _exact(f, "", d)
        return "({}{})".format(_exact(f, "", d, negate=True), not_null)
    if op in ("in", "not_in") and any(v in SPECIAL_VALUES for v in picked):
        with_null = NULL_VALUE in picked
        rest = ["" if v == EMPTY_VALUE else v for v in picked if v != NULL_VALUE]
        if not rest:
            return "{} IS {}NULL".format(f, "" if op == "in" else "NOT ")
        main = clause_to_sql(dict(clause, values=rest), kind, d)
        # «is none of» already leaves empty values out
        return "({} OR {} IS NULL)".format(main, f) if (op == "in" and with_null) else main

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
                       "starts": (False, True), "not_starts": (False, True),
                       "ends": (True, False), "not_ends": (True, False)}[op]
        like = "LIKE" if d in _SQLITE else "ILIKE"
        parts = []
        for var in case_variants(txt, d):
            pat, needs = _like_pattern(var, d, lead, trail)
            parts.append((_quote(pat, d), _escape_clause(needs, d)))
        if op in ("not_contains", "not_starts", "not_ends"):
            body = " AND ".join("{} NOT {} {}{}".format(f, like, p, e) for p, e in parts)
            return "({}{})".format(body, not_null)
        if len(parts) == 1:
            return "{} {} {}{}".format(f, like, parts[0][0], parts[0][1])
        return "(" + " OR ".join("{} {} {}{}".format(f, like, p, e) for p, e in parts) + ")"

    if kind == "datetime" and op in ("eq", "ne", "gt", "ge", "lt", "le", "between", "not_between",
                                     "in", "not_in"):
        if op in ("in", "not_in") and not (clause.get("values") or []):
            raise Incomplete(tr("Choose at least one value for '{}'.").format(name))
        if op not in ("in", "not_in") and str(clause.get("value") or "").strip() == "":
            raise Incomplete(tr("Value missing for '{}'.").format(name))
        return _datetime_sql(op, clause, f, fx, d)

    if op in ("in", "not_in"):
        vals = clause.get("values") or []
        if not vals:
            raise Incomplete(tr("Choose at least one value for '{}'.").format(name))
        canon = [normalize_value(v, kind) for v in vals]
        if kind in ("text", "any", "other") and d == "ogrsql":
            plain = [v for v in canon if not has_ascii_letters(v)]
            lettered = [v for v in canon if has_ascii_letters(v)]
            if len(lettered) > MAX_EXACT_LIST:
                plain, lettered = canon, []
            listed = ", ".join(_quote(v, d) for v in plain)
            if op == "in":
                items = (["{} IN ({})".format(f, listed)] if plain else []) + [_exact(f, v, d) for v in lettered]
                return items[0] if len(items) == 1 else "(" + " OR ".join(items) + ")"
            items = (["{} NOT IN ({})".format(f, listed)] if plain else []) + \
                [_exact(f, v, d, negate=True) for v in lettered]
            return "(" + " AND ".join(items) + not_null + ")"
        lits = ", ".join(_lit(v, kind, d) for v in canon)
        if op == "in":
            return "{} IN ({})".format(fx, lits)
        return "({} NOT IN ({}){})".format(fx, lits, not_null)

    if op in ("between", "not_between"):
        a = _lit(normalize_value(clause.get("value"), kind), kind, d)
        b = _lit(normalize_value(clause.get("value2"), kind), kind, d)
        if op == "between":
            return "({fx} >= {a} AND {fx} <= {b})".format(fx=fx, a=a, b=b)
        return "(({fx} < {a} OR {fx} > {b}){nn})".format(fx=fx, a=a, b=b, nn=not_null)

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


# fields GDAL adds to every layer (e.g. «OGR_GEOM_AREA > 1000» works in a Shapefile)
_OGR_SPECIAL_FIELDS = {"FID", "OGR_GEOMETRY", "OGR_GEOM_WKT", "OGR_GEOM_AREA", "OGR_STYLE"}


def field_problems(layer, names):
    """Fields that cannot be used in a layer filter: missing ones and joined/virtual ones
    (QGIS filters at the data source, which does not know those fields). Returns (missing, foreign)."""
    fields = layer.fields()
    missing, foreign = [], []
    special = _OGR_SPECIAL_FIELDS if dialect_of(layer) == "ogrsql" else {"FID"}
    for n in names:
        idx = fields.lookupField(n)
        if idx < 0:
            if str(n).upper() not in special and not str(n).startswith("$"):
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
