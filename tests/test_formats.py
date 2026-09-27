"""
Format tests for the filtering logic of the "Definition Queries" plugin.

For every layer format and every test case, the plugin is compared against a TRUTH
computed in plain Python from the values QGIS reads from the layer (independent of the
plugin and of the SQL engine behind each format). Each case checks:
  1. the actual filter (setSubsetString with the generated SQL)
  2. "Verify" (feature count)
  3. "Select"
  4. the round trip SQL -> builder -> SQL
Then 150 random combinations of the clauses that passed on their own are tested with
AND/OR, groups and subgroups.

Run it from the plugin folder with the Python that comes with QGIS, after generating the
test data (on Linux without a display, prefix the commands with QT_QPA_PLATFORM=offscreen;
DEFINITION_QUERIES_LANG=en shows the plugin's messages in English):

    python tests/make_data.py
    python tests/test_formats.py [GeoPackage Shapefile ...]

Without arguments every format is tested: GeoPackage, Shapefile, GeoJSON, FlatGeobuf,
FileGDB, Excel, SpatiaLite, CSV, "Temporary layer", "Virtual layer" and PostGIS. PostGIS is only tested
when the environment variable DQ_PG_URI holds a QGIS postgres data source URI for a table
with the same data (e.g. loaded from tests/data/tricky.gpkg with ogr2ogr), such as

    DQ_PG_URI="host=localhost dbname=test user=test password=test key='gid' srid=32719 type=Point table=\"public\".\"tricky\" (geom)"

otherwise it is skipped.

A summary per format is printed at the end and the failing cases are written to
tests/results.json. Exit status: 1 if any case fails or a requested layer cannot be
loaded (missing data or GDAL driver), 0 otherwise.
"""
import collections
import datetime
import json
import os
import pathlib
import random
import re
import sys

from qgis.core import QgsApplication, QgsDataSourceUri, QgsFeatureRequest, QgsProject, QgsVectorLayer

app = QgsApplication([], False)
app.initQgis()
HERE = os.path.dirname(os.path.abspath(__file__))
# the plugin is imported as the package "definition_queries" from the folder that contains it
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from definition_queries import sql_builder as sb, query_store as st, sql_parser as sp  # noqa: E402

DATA = os.path.join(HERE, "data")
RESULTS = os.path.join(HERE, "results.json")
PG_URI = os.environ.get("DQ_PG_URI", "").strip()
FORMATS = ("GeoPackage", "Shapefile", "GeoJSON", "FlatGeobuf", "FileGDB", "Excel", "SpatiaLite", "CSV",
           "Temporary layer", "Virtual layer", "PostGIS")
ONLY = sys.argv[1:]  # optional: names of the formats to test
# The field names of the test data are in Spanish: TEXTO = text, ENTERO = integer,
# REAL = real, FECHA = date, FECHAHORA = date-time, BOOL = boolean (see make_data.py).


def layers():
    out = {}
    gpkg = os.path.join(DATA, "tricky.gpkg") + "|layername=tricky"
    out["GeoPackage"] = QgsVectorLayer(gpkg, "gpkg", "ogr")
    out["Shapefile"] = QgsVectorLayer(os.path.join(DATA, "tricky.shp"), "shp", "ogr")
    out["GeoJSON"] = QgsVectorLayer(os.path.join(DATA, "tricky.geojson"), "geojson", "ogr")
    out["FlatGeobuf"] = QgsVectorLayer(os.path.join(DATA, "tricky.fgb"), "fgb", "ogr")
    out["FileGDB"] = QgsVectorLayer(os.path.join(DATA, "tricky.gdb") + "|layername=tricky", "gdb", "ogr")
    out["Excel"] = QgsVectorLayer(os.path.join(DATA, "tricky.xlsx") + "|layername=tricky", "xlsx", "ogr")
    u = QgsDataSourceUri()
    u.setDatabase(os.path.join(DATA, "tricky.sqlite"))
    u.setDataSource("", "tricky", "GEOMETRY")
    out["SpatiaLite"] = QgsVectorLayer(u.uri(), "spatialite", "spatialite")
    csv_url = pathlib.Path(DATA, "tricky.csv").as_uri()
    out["CSV"] = QgsVectorLayer(f"{csv_url}?delimiter=,&xField=X&yField=Y&crs=EPSG:32719&detectTypes=yes",
                                "csv", "delimitedtext")
    # temporary (memory) layer: an in-memory copy of the GeoPackage layer
    g = QgsVectorLayer(gpkg, "g", "ogr")
    out["Temporary layer"] = g.materialize(QgsFeatureRequest()) if g.isValid() else g
    # virtual layer (SQL query over the GeoPackage layer)
    out["Virtual layer"] = QgsVectorLayer("?layer=ogr:{}:tricky:UTF-8&query=SELECT * FROM tricky".format(
        os.path.join(DATA, "tricky.gpkg")), "virtual", "virtual")
    if not ONLY or "PostGIS" in ONLY:
        if PG_URI:
            out["PostGIS"] = QgsVectorLayer(PG_URI, "pg", "postgres")
        else:
            print("Note: PostGIS skipped (set the environment variable DQ_PG_URI to test it).")
    if ONLY:
        out = {k: v for k, v in out.items() if k in ONLY}
    return out


def py(v):
    """Attribute value read by QGIS -> plain Python value (None, date, datetime...)."""
    if sb.is_null(v):
        return None
    n = type(v).__name__
    if n == "QDate":
        return datetime.date(v.year(), v.month(), v.day())
    if n == "QDateTime":
        d, t = v.date(), v.time()
        return datetime.datetime(d.year(), d.month(), d.day(), t.hour(), t.minute(), t.second())
    return v


def num(x):
    x = str(x).strip()
    if x.count(",") == 1 and "." not in x:
        x = x.replace(",", ".")
    return float(x)


def to_date(x):
    x = str(x).strip()
    m = re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$", x)
    if m:
        return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return datetime.date.fromisoformat(x[:10])


def to_dt(x):
    x = str(x).strip()
    parts = x.split(" ", 1)
    d = to_date(parts[0])
    t = datetime.time.fromisoformat(parts[1]) if len(parts) > 1 else datetime.time(0, 0)
    return datetime.datetime.combine(d, t)


def dt_span(x):
    """What a date-time value means: a date alone = the whole day, HH:MM = that minute,
    HH:MM:SS = that second. Returns [start, end)."""
    parts = re.split(r"[ T]+", str(x).strip(), maxsplit=1)
    d = to_date(parts[0])
    start = datetime.datetime(d.year, d.month, d.day)
    if len(parts) == 1 or not parts[1]:
        return start, start + datetime.timedelta(days=1)
    hms = parts[1].split(":")
    start = start.replace(hour=int(hms[0]), minute=int(hms[1]), second=int(hms[2][:2]) if len(hms) > 2 else 0)
    return start, start + (datetime.timedelta(seconds=1) if len(hms) > 2 else datetime.timedelta(minutes=1))


def truth(c, v, kind, ftype):
    """INTENDED semantics (what the user expects from each operator)."""
    op = c["op"]
    NUL, EMP = sb.NULL_VALUE, sb.EMPTY_VALUE  # <Null> / <Empty> picked from a value list
    if op in ("eq", "ne") and c.get("value") == EMP:
        return v is not None and ((v == "") == (op == "eq"))
    if op in ("in", "not_in") and EMP in (c.get("values") or []):
        return truth(dict(c, values=["" if x == EMP else x for x in c["values"]]), v, kind, ftype)
    if op in ("eq", "ne") and c.get("value") == NUL:
        return (v is None) == (op == "eq")
    if op in ("in", "not_in") and NUL in (c.get("values") or []):
        rest = dict(c, values=[x for x in c["values"] if x != NUL])
        if op == "in":
            return v is None or (bool(rest["values"]) and truth(rest, v, kind, ftype))
        return v is not None and (not rest["values"] or truth(rest, v, kind, ftype))
    if op == "null":
        return v is None
    if op == "not_null":
        return v is not None
    if op == "blank":
        return v is None or v == ""
    if op == "not_blank":
        return v is not None and v != ""
    if v is None:
        return False
    if kind == "text":
        x = c.get("value", "")
        if op == "eq":
            return v == x
        if op == "ne":
            return v != x
        if op == "in":
            return v in c["values"]
        if op == "not_in":
            return v not in c["values"]
        lv, lx = v.lower(), x.lower()
        if op == "contains":
            return lx in lv
        if op == "not_contains":
            return lx not in lv
        if op == "starts":
            return lv.startswith(lx)
        if op == "not_starts":
            return not lv.startswith(lx)
        if op == "ends":
            return lv.endswith(lx)
        if op == "not_ends":
            return not lv.endswith(lx)
    if kind == "bool":
        # "verdadero", "sí", "si" = Spanish for "true" (the cases only use "true" and "false")
        want = str(c.get("value")).strip().lower() in ("true", "verdadero", "1", "sí", "si")
        if op == "eq":
            return bool(v) == want
        if op == "ne":
            return bool(v) != want
    if kind == "datetime" and isinstance(v, datetime.datetime):
        def inside(x):
            a, b = dt_span(x)
            return a <= v < b
        if op == "in":
            return any(inside(x) for x in c["values"])
        if op == "not_in":
            return not any(inside(x) for x in c["values"])
        if op in ("between", "not_between"):
            within = dt_span(c["value"])[0] <= v < dt_span(c["value2"])[1]
            return within if op == "between" else not within
        a, b = dt_span(c["value"])
        return {"eq": a <= v < b, "ne": not a <= v < b, "gt": v >= b, "ge": v >= a, "lt": v < a, "le": v < b}[op]
    if kind in ("num", "date", "datetime", "time"):
        if isinstance(v, datetime.datetime):
            conv = to_dt
        elif isinstance(v, datetime.date):
            conv = to_date
        else:
            conv, v = num, float(v)
        if op == "in":
            return v in {conv(x) for x in c["values"]}
        if op == "not_in":
            return v not in {conv(x) for x in c["values"]}
        if op == "between":
            return conv(c["value"]) <= v <= conv(c["value2"])
        if op == "not_between":
            return not (conv(c["value"]) <= v <= conv(c["value2"]))
        x = conv(c["value"])
        return {"eq": v == x, "ne": v != x, "gt": v > x, "ge": v >= x, "lt": v < x, "le": v <= x}[op]
    raise ValueError((op, kind))


def truth_tree(clauses, rows, fmap, kinds):
    """Evaluate the intended logic with groups: AND binds tighter than OR, groups = parentheses."""
    def ids_for(c):
        f = fmap[c["field"]]
        return {rid for rid, r in rows.items() if truth(c, r[f], kinds[f], None)}

    def node_ids(node):
        if "leaf" in node:
            return ids_for(node["leaf"])
        return eval_children(node["children"])

    def conn(node):
        c = node["leaf"] if "leaf" in node else sb.first_leaf(node)
        return "OR" if str(c.get("connector", "AND")).upper() == "OR" else "AND"

    def eval_children(children):
        runs = [[]]
        for i, ch in enumerate(children):
            if i > 0 and conn(ch) == "OR":
                runs.append([])
            runs[-1].append(node_ids(ch))
        out = set()
        for run in runs:
            acc = run[0]
            for s in run[1:]:
                acc = acc & s
            out |= acc
        return out

    return eval_children(sb.build_tree(clauses, sb.clause_path))


def actual_clause(c, fmap):
    """Clause with the canonical field name replaced by the layer's actual field name."""
    c2 = dict(c)
    c2["field"] = fmap[c["field"]]
    return c2


# ---------------------------------------------------------------- single-operator cases
def single_cases(kinds_canon):
    T = "TEXTO"
    cases = []
    for v in ["Vega", "vega", "Árbol nativo", "ÁRBOL NATIVO", "O'Higgins", "COD_1", "50%", "a\\b", " Vega",
              "Vega ", "", "Ñandú", "no existe", "¡Ojo! 5%_x", "fin!"]:
        cases.append([{"field": T, "op": "eq", "value": v}])
    for v in ["Vega", "COD_1", "a\\b", ""]:
        cases.append([{"field": T, "op": "ne", "value": v}])
    for vs in [["Vega", "Pajonal"], ["O'Higgins", "50%", "COD_1"], ["a\\b", "Ñandú", "Güiña"], ["50%", "500"],
               ["500", "Vega", "vega"]]:
        cases.append([{"field": T, "op": "in", "values": vs}])
        cases.append([{"field": T, "op": "not_in", "values": vs}])
    for v in ["vega", "VEGA", "árbol", "ÁRBOL", "Árbol", "arbol", "ñandú", "ÑANDÚ", "güiña", "COD_1", "cod_1", "_",
              "50%", "%", "o'h", "a\\b", "\\", "xerofítico", "XEROFÍTICO", "ó", " ", "bosque esclerófilo", "x_y",
              "!", "5%_", "¡ojo!"]:
        cases.append([{"field": T, "op": "contains", "value": v}])
    for v in ["vega", "árbol", "_", "%", "ñ"]:
        cases.append([{"field": T, "op": "not_contains", "value": v}])
    for v in ["vega", "ÁRBOL", "cod_", "ñ", " ", "50%"]:
        cases.append([{"field": T, "op": "starts", "value": v}])
    for v in ["vega", "ÁRBOL", "cod_", "ñ", " "]:
        cases.append([{"field": T, "op": "not_starts", "value": v}])
    for v in ["nativo", "NATIVO", "_1", "%", "Ú", "\\b"]:
        cases.append([{"field": T, "op": "ends", "value": v}])
    for v in ["nativo", "_1", "%", "Ú"]:
        cases.append([{"field": T, "op": "not_ends", "value": v}])
    # <Empty> (a text with nothing written) picked from the value lists
    E = sb.EMPTY_VALUE
    cases += [[{"field": T, "op": "eq", "value": E}], [{"field": T, "op": "ne", "value": E}],
              [{"field": T, "op": "in", "values": ["Vega", E]}], [{"field": T, "op": "not_in", "values": ["Vega", E]}],
              [{"field": T, "op": "in", "values": [E, sb.NULL_VALUE]}]]
    cases += [[{"field": T, "op": "null"}], [{"field": T, "op": "not_null"}],
              [{"field": T, "op": "blank"}], [{"field": T, "op": "not_blank"}]]
    # <Null> picked from the value lists (text, integer, real and date fields)
    for f, v in ((T, "Vega"), ("ENTERO", "0"), ("REAL", "0"), ("FECHA", "2025-03-03")):
        N = sb.NULL_VALUE
        cases += [[{"field": f, "op": "eq", "value": N}], [{"field": f, "op": "ne", "value": N}],
                  [{"field": f, "op": "in", "values": [v, N]}], [{"field": f, "op": "not_in", "values": [v, N]}],
                  [{"field": f, "op": "in", "values": [N]}]]
    for f, vals in (("ENTERO", ["0", "-3", "5", "8"]), ("REAL", ["10,5", "-3.25", "0", "12.3"])):
        for op in ("eq", "ne", "gt", "ge", "lt", "le"):
            for v in vals[:2]:
                cases.append([{"field": f, "op": op, "value": v}])
        cases.append([{"field": f, "op": "between", "value": vals[1], "value2": vals[3]}])
        cases.append([{"field": f, "op": "not_between", "value": vals[1], "value2": vals[3]}])
        cases.append([{"field": f, "op": "in", "values": vals[:3]}])
        cases.append([{"field": f, "op": "not_in", "values": vals[:3]}])
        cases += [[{"field": f, "op": "null"}], [{"field": f, "op": "not_null"}]]
    if kinds_canon.get("FECHA") in ("date", "datetime"):
        for op, v in (("eq", "2025-03-03"), ("eq", "03-03-2025"), ("ne", "2025-03-03"), ("gt", "2024-06-15"),
                      ("ge", "2024-11-11"), ("lt", "2023-05-06"), ("le", "11/11/2024")):
            cases.append([{"field": "FECHA", "op": op, "value": v}])
        cases.append([{"field": "FECHA", "op": "between", "value": "2023-06-01", "value2": "2024-02-29"}])
        cases.append([{"field": "FECHA", "op": "not_between", "value": "2023-06-01", "value2": "2024-02-29"}])
    else:
        # e.g. virtual layers, which expose dates as text: text rules apply
        for op, v in (("eq", "2025-03-03"), ("ne", "2025-03-03"), ("starts", "2024-")):
            cases.append([{"field": "FECHA", "op": op, "value": v}])
    cases.append([{"field": "FECHA", "op": "in", "values": ["2025-03-03", "2024-11-11"]}])
    cases += [[{"field": "FECHA", "op": "null"}], [{"field": "FECHA", "op": "not_null"}]]
    if kinds_canon.get("FECHAHORA") == "datetime":
        for op, v in (("ge", "2024-03-02 10:00:00"), ("lt", "2024-02-02 00:00:00"), ("gt", "2024-04-03 05:07:00"),
                      ("eq", "2024-03-03 10:14:00"), ("ne", "2024-03-03 10:14:00"), ("le", "2024-03-03 10:14"),
                      ("ge", "2024-03-03")):
            cases.append([{"field": "FECHAHORA", "op": op, "value": v}])
        cases.append([{"field": "FECHAHORA", "op": "between", "value": "2024-02-01 00:00:00", "value2": "2024-03-03 23:59:59"}])
        cases.append([{"field": "FECHAHORA", "op": "not_between", "value": "2024-02-01 00:00:00", "value2": "2024-03-03 23:59:59"}])
        # a date alone means the whole day; HH:MM the whole minute
        for op in ("eq", "ne", "gt", "ge", "lt", "le"):
            cases.append([{"field": "FECHAHORA", "op": op, "value": "2024-03-03"}])
        cases.append([{"field": "FECHAHORA", "op": "eq", "value": "03/03/2024 10:14"}])
        cases.append([{"field": "FECHAHORA", "op": "between", "value": "2024-02-01", "value2": "2024-03-03"}])
        cases.append([{"field": "FECHAHORA", "op": "not_between", "value": "2024-02-01", "value2": "2024-03-03"}])
        cases.append([{"field": "FECHAHORA", "op": "in", "values": ["2024-03-03", "2024-04-01 15:21:00"]}])
        cases.append([{"field": "FECHAHORA", "op": "not_in", "values": ["2024-03-03", "2024-04-01 15:21:00"]}])
        cases.append([{"field": "FECHAHORA", "op": "in", "values": ["2024-03-03", sb.NULL_VALUE]}])
        cases += [[{"field": "FECHAHORA", "op": "null"}]]
    if kinds_canon.get("BOOL") == "bool":
        for v in ("true", "false"):
            cases.append([{"field": "BOOL", "op": "eq", "value": v}])
            cases.append([{"field": "BOOL", "op": "ne", "value": v}])
        cases += [[{"field": "BOOL", "op": "null"}]]
    return cases


def run_case(layer, clauses_canon, fmap, kinds, rows, clone, idf):
    clauses = [actual_clause(c, fmap) for c in clauses_canon]
    res = {"sql": None, "problems": []}
    try:
        sql = sb.build_sql(clauses, layer)
    except sb.ClauseError as e:
        res["problems"].append("no SQL generated: " + str(e))
        return res
    res["sql"] = sql
    expected = truth_tree(clauses_canon, rows, fmap, kinds)
    # 1) actual filter
    ok = layer.setSubsetString(sql)
    if not ok:
        res["problems"].append("the provider rejected the SQL")
    else:
        got = {f[idf] for f in layer.getFeatures(QgsFeatureRequest().setFlags(st._no_geometry_flag()))}
        if got != expected:
            res["problems"].append(("FILTER", sorted(got - expected), sorted(expected - got)))
    layer.setSubsetString("")
    # 2) verify
    n, total, err = st.count_matching(layer, sql, clone)
    if err or n != len(expected):
        res["problems"].append(("VERIFY", n, len(expected), err))
    # 3) select
    data = st.load(layer)
    q = st.new_query("test", clauses=clauses)
    data["queries"] = [q]
    st.save(layer, data, dirty=False)
    layer.removeSelection()
    _ok, msg = st.select_with_query(layer, q["id"], "set")
    sel = {f[idf] for f in layer.getSelectedFeatures()}
    layer.removeSelection()
    if sel != expected:
        res["problems"].append(("SELECTION", sorted(sel - expected), sorted(expected - sel)))
    # 4) round trip
    back = sp.sql_to_clauses(sql, layer)
    try:
        sql2 = sb.build_sql(back, layer) if back else None
    except sb.ClauseError:
        sql2 = None
    if sql2 != sql:
        # different text: acceptable only if it returns EXACTLY the same features
        same = False
        if sql2 and layer.setSubsetString(sql2):
            got2 = {f[idf] for f in layer.getFeatures(QgsFeatureRequest().setFlags(st._no_geometry_flag()))}
            same = got2 == expected
        layer.setSubsetString("")
        if not same:
            res["problems"].append(("ROUND_TRIP", sql2))
    return res


def main():
    unknown = [name for name in ONLY if name not in FORMATS]
    if unknown:
        print(f"Unknown format(s): {', '.join(unknown)}. Valid names: {', '.join(FORMATS)}")
        return 2
    random.seed(20260926)
    summary = collections.OrderedDict()
    not_loaded = []
    failures = collections.defaultdict(list)
    nerr_explicit = collections.defaultdict(list)
    for name, layer in layers().items():
        if not layer.isValid():
            print(f"!! {name}: invalid layer (missing data? run tests/make_data.py first; "
                  f"or the GDAL driver is not available)")
            not_loaded.append(name)
            continue
        QgsProject.instance().addMapLayer(layer)
        fmap = {}
        for canon in ("ID", "TEXTO", "ENTERO", "REAL", "FECHA", "FECHAHORA", "BOOL"):
            idx = layer.fields().lookupField(canon)
            if idx >= 0:
                fmap[canon] = layer.fields().at(idx).name()
        idf = fmap["ID"]
        kinds = {fmap[c]: sb.field_kind(layer.fields().field(fmap[c])) for c in fmap}
        kinds_canon = {c: kinds[fmap[c]] for c in fmap}
        rows = {}
        for f in layer.getFeatures():
            rows[f[idf]] = {fmap[c]: py(f[fmap[c]]) for c in fmap}
        clone = st.unfiltered_clone(layer)
        types = {c: layer.fields().field(fmap[c]).typeName() for c in fmap}
        print(f"\n===== {name} ({layer.providerType()}) — {len(rows)} rows — types: {types}")
        cases = [c for c in single_cases(kinds_canon) if all(x["field"] in fmap for x in c)]
        good_pool = []
        nfail = 0
        asked = 0
        for clauses in cases:
            if len(clauses) == 1 and clauses[0]["op"] in ("eq", "ne") and clauses[0].get("value") == "":
                # an empty value is an unfinished clause: the plugin must ask for the value, not filter
                try:
                    sb.build_sql([actual_clause(clauses[0], fmap)], layer)
                    nfail += 1
                    failures[name].append((clauses, {"sql": None, "problems": ["empty value did NOT ask for a value"]}))
                except sb.Incomplete:
                    asked += 1
                continue
            try:
                r = run_case(layer, clauses, fmap, kinds, rows, clone, idf)
            except Exception as e:
                r = {"sql": None, "problems": ["EXCEPTION " + repr(e)]}
            if r["problems"] and all(str(p).startswith("no SQL generated") for p in r["problems"]):
                nerr_explicit[name].append((clauses, r["problems"][0]))
            elif r["problems"]:
                nfail += 1
                failures[name].append((clauses, r))
            else:
                good_pool.append(clauses[0])
        # random combinations with groups and subgroups (only with clauses that pass on their own)
        ncomb, fcomb = 0, 0
        for k in range(150):
            n = random.randint(2, 6)
            if not good_pool:
                break
            cl = [dict(random.choice(good_pool)) for _ in range(n)]
            for i, c in enumerate(cl):
                c["connector"] = random.choice(["AND", "OR"])
            # random but valid group paths (contiguous nested prefixes)
            paths, cur = [], []
            gid = 100
            for i in range(n):
                r = random.random()
                if r < 0.25 and len(cur) < 3:
                    gid += 1
                    cur = cur + [gid]
                elif r < 0.45 and cur:
                    cur = cur[:-1]
                paths.append(list(cur))
            for c, p in zip(cl, paths):
                c["groups"] = p
            ncomb += 1
            try:
                r = run_case(layer, cl, fmap, kinds, rows, clone, idf)
            except Exception as e:
                r = {"sql": None, "problems": ["EXCEPTION " + repr(e)]}
            if r["problems"]:
                fcomb += 1
                failures[name].append((cl, r))
        ne = len(nerr_explicit[name])
        summary[name] = (len(cases) - ne - asked, nfail, ncomb, fcomb, ne, asked)
        print(f"   operators: {len(cases) - ne - asked - nfail}/{len(cases) - ne - asked} correct | combinations with groups: "
              f"{ncomb - fcomb}/{ncomb} correct | explicit refusals: {ne} | empty value prompted: {asked}")
        for cl, why in nerr_explicit[name]:
            print(f"      explicit refusal: {cl[0]['op']} {cl[0].get('value')!r}: {why}")
    print("\n\n================ SUMMARY ================")
    for fmt, (nops, fops, ncomb, fcomb, nrefused, nasked) in summary.items():
        status = "PASS" if fops == 0 and fcomb == 0 else "FAIL"
        print(f"{fmt:16s} operators {nops - fops:3d}/{nops:3d}   groups {ncomb - fcomb:3d}/{ncomb:3d}"
              f"   explicit refusals {nrefused}   empty value prompted {nasked}/2   {status}")
    for fmt in not_loaded:
        print(f"{fmt:16s} NOT TESTED: the layer could not be loaded   FAIL")
    if "PostGIS" not in summary and "PostGIS" not in not_loaded and (not ONLY or "PostGIS" in ONLY):
        print(f"{'PostGIS':16s} skipped (DQ_PG_URI not set)")
    with open(RESULTS, "w", encoding="utf-8") as fh:
        json.dump({k: [(cl, {"sql": r["sql"], "problems": [str(p) for p in r["problems"]]}) for cl, r in v]
                   for k, v in failures.items()}, fh, ensure_ascii=False, indent=1)
    failed = not_loaded or any(fops or fcomb for (_n, fops, _c, fcomb, _r, _a) in summary.values())
    if failed:
        print(f"\nFAILED (details of the failing cases in {RESULTS})")
    else:
        print("\nAll tested formats passed." if summary else "\nNo format was tested.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
