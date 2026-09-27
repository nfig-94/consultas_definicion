"""Scenarios a user may run into, checked with the real plugin (the interface runs off-screen).

    python tests/test_scenarios.py

A  <Null> in the value lists: every operator, text that reads «<Null>», long lists,
   saving and reopening, other languages, SQL <-> builder, warnings; changing the
   operator or the field of a clause; AND and OR mixed without a group.
B  Values typed by hand: «1,500», «1.000», ambiguous dates, very large integers.
C  Unusual text: new lines, emoji, tabs; names with «&», quotes and HTML; odd field names.
C2 Long lists of texts in Shapefile (speed, upper/lower case warning).
C3 Decimals and date-times picked from the list (15-digit rounding, milliseconds, time
   zones) and «is on» a whole day.
C4 Values that look alike: spaces at the start or end, upper/lower case in the list.
D  Project life: missing data source, edit mode with new features, deleting the active
   query, removing the layer with the window open, reloading the plugin, saving without
   the plugin, pasting the style on another layer, a field that changes type, virtual
   layers, polar map projections, the Temporal Controller, Processing tools, SQL that
   uses the geometry column, damaged saved data and strange JSON files.

Exits with status 1 if any check fails.
"""
import json
import os
import shutil
import sys
from unittest.mock import MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
os.environ.setdefault("DEFINITION_QUERIES_LANG", "en")

from osgeo import ogr, osr  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsApplication, QgsCoordinateReferenceSystem, QgsFeature, QgsFeatureRequest, QgsGeometry,
    QgsMapLayerStyle, QgsMapSettings, QgsPointXY, QgsProject, QgsRectangle, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QCoreApplication, QEvent, QSize  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

app = QgsApplication([], True)
app.initQgis()
from definition_queries import i18n, lint as lint_mod, query_store as st, sql_builder as sb, sql_parser as sp  # noqa: E402
from definition_queries.clause_widget import ClauseWidget  # noqa: E402

DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)
FAILS = 0
NUL = sb.NULL_VALUE


def check(name, cond, extra=""):
    global FAILS
    print(("ok    " if cond else "FAIL  ") + name, extra)
    FAILS += 0 if cond else 1


def pump(n=4):
    for _ in range(n):
        app.processEvents()
        # processEvents() alone does not run deleteLater() outside an event loop
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def make(path, rows, fields):
    """GeoPackage 'data' with the given fields [(name, ogr type)] and rows (lists; None = NULL)."""
    if os.path.exists(path):
        os.remove(path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    lyr = ds.CreateLayer("data", srs, ogr.wkbPoint, options=["FID=id"])
    for name, kind in fields:
        lyr.CreateField(ogr.FieldDefn(name, kind))
    for i, row in enumerate(rows):
        f = ogr.Feature(lyr.GetLayerDefn())
        for (name, _k), v in zip(fields, row):
            if v is not None:
                f.SetField(name, v)
        f.SetGeometry(ogr.CreateGeometryFromWkt("POINT ({} {})".format(i % 50, i // 50)))
        lyr.CreateFeature(f)
    ds = None


def layer(path, name="data"):
    lyr = QgsVectorLayer(path + "|layername=data", name, "ogr")
    QgsProject.instance().addMapLayer(lyr)
    return lyr


def count(lyr, clauses):
    sql = sb.build_sql(clauses, lyr, "provider")
    lyr.setSubsetString(sql)
    n = lyr.featureCount()
    lyr.setSubsetString("")
    return n


def add_query(lyr, name, clauses):
    d = st.load(lyr)
    q = st.new_query(name, clauses=clauses)
    d["queries"].append(q)
    st.save(lyr, d)
    return q


# ------------------------------------------------------------------ A. <Null>
print("== A. <Null> in the value lists")
rows = [["Asia", 5], ["Europe", None], [None, 7], ["<Null>", 1], [None, None], ["Asia", 2]]
path = os.path.join(DATA, "nulls.gpkg")
make(path, rows, [("cont", ogr.OFTString), ("pop", ogr.OFTInteger)])
lyr = layer(path)

for op, extra in (("gt", {"value": NUL}), ("between", {"value": NUL, "value2": "3"}), ("contains", {"value": NUL})):
    try:
        sb.build_sql([dict(field="pop" if op != "contains" else "cont", op=op, **extra)], lyr)
        check("<Null> with «{}» is refused".format(op), False)
    except sb.ClauseError as e:
        check("<Null> with «{}» gives a clear message".format(op), "\x00" not in str(e) and "<Null>" in str(e), str(e))

check("real text «<Null>» and empty values are different things",
      count(lyr, [{"field": "cont", "op": "eq", "value": "<Null>"}]) == 1
      and count(lyr, [{"field": "cont", "op": "eq", "value": NUL}]) == 2)
check("«is one of: Asia, <Null>»", count(lyr, [{"field": "cont", "op": "in", "values": ["Asia", NUL]}]) == 4)
check("«is none of: Asia, <Null>» (empty values are always left out)",
      count(lyr, [{"field": "cont", "op": "not_in", "values": ["Asia", NUL]}]) == 2)
check("«is none of: Asia» also leaves empty values out (as in SQL and ArcGIS)",
      count(lyr, [{"field": "cont", "op": "not_in", "values": ["Asia"]}]) == 2)
check("numeric field: «is equal to <Null>»", count(lyr, [{"field": "pop", "op": "eq", "value": NUL}]) == 2)

vals = st.unique_values(lyr, "cont")
check("the list starts with <Null> and keeps the text «<Null>» as a normal value",
      vals[0] == NUL and "<Null>" in vals[1:], vals)
limit = st.VALUES_LIMIT
st.VALUES_LIMIT = 2
many = st.unique_values(lyr, "cont", limit=2)
check("a cut list (too many values) still offers <Null>", NUL in many, many)
st.VALUES_LIMIT = limit

# the value boxes of a clause
w = ClauseWidget(lyr, lambda f: st.unique_values(lyr, f))
w.set_clause({"field": "cont", "op": "eq", "value": NUL})
pump()
check("the drop-down shows <Null> and saves the empty value", w.value1.currentText() == "<Null>" and w.clause()["value"] == NUL)
i = [k for k in range(w.value1.count()) if w.value1.itemData(k) == "<Null>"][0]
w.value1.setCurrentIndex(i)
check("picking the real text «<Null>» saves that text", w.clause()["value"] == "<Null>")
w.set_clause({"field": "pop", "op": "gt", "value": ""})
pump()
check("<Null> is not offered for «is greater than»",
      all(w.value1.itemData(k) != NUL for k in range(w.value1.count())))
w.set_clause({"field": "cont", "op": "in", "values": ["Asia", NUL]})
pump()
check("list chips show <Null>", w.list_btn.toolTip().split("\n") == ["Asia", "<Null>"])


# changing the operator or the field keeps only what still makes sense
def op_to(widget, key):
    widget.op.setCurrentIndex(widget.op.findData(key))
    pump()


w.set_clause({"field": "cont", "op": "eq", "value": NUL})
pump()
op_to(w, "ne")
check("«is equal to <Null>» -> «is not equal to» keeps <Null>", w.clause()["value"] == NUL, w.clause())
op_to(w, "contains")
check("-> «contains» drops <Null> (it only goes with equal / not equal)", w.clause()["value"] == "", w.clause())
w.set_clause({"field": "pop", "op": "eq", "value": NUL})
pump()
op_to(w, "gt")
check("-> «is greater than» drops <Null> and asks for a value",
      w.clause()["value"] == "" and w.value1.currentText() == "", w.clause())
try:
    sb.build_sql([w.clause()], lyr)
    check("…the clause is left unfinished (not an error)", False)
except sb.Incomplete:
    check("…the clause is left unfinished (not an error)", True)
except sb.ClauseError as e:
    check("…the clause is left unfinished (not an error)", False, str(e))
w.set_clause({"field": "cont", "op": "eq", "value": "Asia"})
pump()
op_to(w, "in")
check("«is equal to Asia» -> «is one of» starts with Asia", w.clause()["values"] == ["Asia"], w.clause())
w.set_clause({"field": "cont", "op": "in", "values": [NUL]})
pump()
op_to(w, "eq")
check("«is one of: <Null>» -> «is equal to» shows <Null> as the list item",
      w.clause()["value"] == NUL and w.value1.currentText() == "<Null>", w.clause())
w.set_clause({"field": "cont", "op": "eq", "value": "Asia"})
pump()
w.field.setField("pop")
pump()
check("changing the field clears the value (another field, other values)", w.clause()["value"] == "", w.clause())
w.set_clause({"field": "pop", "op": "between", "value": "1", "value2": "5"})
pump()
op_to(w, "not_between")
check("«is between» -> «is not between» keeps both values",
      (w.clause()["value"], w.clause()["value2"]) == ("1", "5"), w.clause())
w.set_clause({"field": "cont", "op": "contains", "value": "si"})
pump()
op_to(w, "not_contains")
check("«contains» -> «does not contain» keeps the text", w.clause()["value"] == "si", w.clause())
w.deleteLater()
pump()

# saved, reopened, exported: the empty value survives
q = add_query(lyr, "Asia or empty", [{"field": "cont", "op": "in", "values": ["Asia", NUL]}])
prj_file = os.path.join(DATA, "nulls.qgz")
QgsProject.instance().write(prj_file)
QgsProject.instance().clear()
QgsProject.instance().read(prj_file)
lyr = QgsProject.instance().mapLayersByName("data")[0]
q2 = st.load(lyr)["queries"][0]
check("<Null> survives saving and reopening the project", q2["clauses"][0]["values"] == ["Asia", NUL])
exp = os.path.join(DATA, "nulls.json")
st.export_json(lyr, exp)
d = st.load(lyr)
d["queries"] = []
st.save(lyr, d)
st.import_json(lyr, exp)
check("<Null> survives export and import", st.load(lyr)["queries"][0]["clauses"][0]["values"] == ["Asia", NUL])
ok, msg = st.apply_filter(lyr, st.load(lyr)["queries"][0]["id"])
check("the reopened query filters the same", ok and lyr.featureCount() == 4)
st.clear_filter(lyr)

# another language
i18n.set_lang("es")
check("in Spanish the list shows «<Nulo>»", sb.display_value(NUL) == "<Nulo>" and sb.stored_value("<Nulo>") == NUL)
i18n.set_lang("en")
check("and the saved value is the same in every language", sb.stored_value("<Null>") == NUL)

# SQL -> builder -> SQL keeps the meaning
for clauses in ([{"field": "cont", "op": "in", "values": ["Asia", NUL]}],
                [{"field": "cont", "op": "ne", "value": NUL}],
                [{"field": "pop", "op": "not_in", "values": ["5", NUL]}]):
    sql = sb.build_sql(clauses, lyr)
    back = sp.sql_to_clauses(sql, lyr)
    n1 = count(lyr, clauses)
    n2 = count(lyr, back) if back else -1
    check("SQL -> builder keeps the result: " + sql, n1 == n2, "{} vs {}".format(n1, n2))

# warnings
values_for = (lambda f: st.unique_values(lyr, f))
warn = lint_mod.lint([{"field": "cont", "op": "eq", "value": NUL}], lyr, values_for, lambda f: True)
check("no warning when the field has empty values", not warn, warn)
path2 = os.path.join(DATA, "nonulls.gpkg")
make(path2, [["Asia", 1], ["Europe", 2]], [("cont", ogr.OFTString), ("pop", ogr.OFTInteger)])
full = layer(path2, "full")
warn = lint_mod.lint([{"field": "cont", "op": "in", "values": ["Asia", NUL]}], full,
                     lambda f: st.unique_values(full, f), lambda f: True)
check("warning when <Null> is chosen but the field has no empty values", any("<Null>" in x for x in warn), warn)


# AND and OR mixed without a group
def cl(conn, groups=()):
    return {"field": "cont", "op": "eq", "value": "Asia", "connector": conn, "groups": list(groups)}


mix = lint_mod.mixes_and_or
check("«A OR B AND C» without a group is flagged", mix([cl("AND"), cl("OR"), cl("AND")]))
check("«A OR B» is not flagged", not mix([cl("AND"), cl("OR"), cl("OR")]))
check("«(A OR B) AND C» is not flagged", not mix([cl("AND", [1]), cl("OR", [1]), cl("AND")]))
check("«A AND (B OR C)» is not flagged", not mix([cl("AND"), cl("AND", [1]), cl("OR", [1])]))
check("mixing inside a group is flagged", mix([cl("AND"), cl("AND", [1]), cl("OR", [1]), cl("AND", [1])]))
warn = lint_mod.lint([cl("AND"), cl("OR"), cl("AND")], lyr, values_for, lambda f: True)
check("…and the builder shows the warning", any("AND and OR" in w for w in warn), warn)
QgsProject.instance().clear()

# ------------------------------------------------------------------ B. typed values
print("== B. Values typed by hand")
path = os.path.join(DATA, "typed.gpkg")
make(path, [[1500, "2025-04-03", 5000000000], [1.5, "2025-03-04", 7]],
     [("num", ogr.OFTReal), ("day", ogr.OFTDate), ("big", ogr.OFTInteger64)])
lyr = layer(path)
vf = (lambda f: st.unique_values(lyr, f))
warn = lint_mod.lint([{"field": "num", "op": "eq", "value": "1,500"}], lyr, vf, lambda f: True)
check("«1,500» warns that it is read as 1.5", any("1.5" in x for x in warn), warn)
warn = lint_mod.lint([{"field": "num", "op": "eq", "value": "1.000"}], lyr, vf, lambda f: True)
check("«1.000» warns that it is read as 1", bool(warn), warn)
try:
    sb.build_sql([{"field": "num", "op": "eq", "value": "1,500,000"}], lyr)
    check("«1,500,000» is refused", False)
except sb.ClauseError as e:
    check("«1,500,000» is refused with a message", True, str(e))
warn = lint_mod.lint([{"field": "day", "op": "eq", "value": "03/04/2025"}], lyr, vf, lambda f: True)
check("«03/04/2025» warns: day/month/year", any("2025-04-03" in x for x in warn), warn)
warn = lint_mod.lint([{"field": "day", "op": "eq", "value": "13/04/2025"}], lyr, vf, lambda f: True)
check("«13/04/2025» is not ambiguous: no warning", not warn, warn)
check("«03/04/2025» finds 3 April", count(lyr, [{"field": "day", "op": "eq", "value": "03/04/2025"}]) == 1)
check("integers above 2^31", count(lyr, [{"field": "big", "op": "eq", "value": "5000000000"}]) == 1
      and count(lyr, [{"field": "big", "op": "gt", "value": "4294967296"}]) == 1)
QgsProject.instance().clear()

# ------------------------------------------------------------------ C. unusual text
print("== C. Unusual text")
texts = ["line 1\nline 2", "tree \U0001F332", "tab\there", "  ", "It's \"quoted\""]
path = os.path.join(DATA, "texts.gpkg")
make(path, [[t] for t in texts], [('odd "name"', ogr.OFTString)])
lyr = layer(path)
fname = lyr.fields().at(1).name()
ok = all(count(lyr, [{"field": fname, "op": "eq", "value": t}]) == 1 for t in texts)
check("new lines, emoji, tabs, spaces and quotes: «is equal to»", ok)
check("«is one of» with all of them", count(lyr, [{"field": fname, "op": "in", "values": texts}]) == len(texts))
check("«contains» an emoji", count(lyr, [{"field": fname, "op": "contains", "value": "\U0001F332"}]) == 1)
mem = QgsVectorLayer("Point?crs=EPSG:4326&field=t:string(200)", "mem", "memory")
feats = []
for t in texts:
    f = QgsFeature(mem.fields())
    f["t"] = t
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(0, 0)))
    feats.append(f)
mem.dataProvider().addFeatures(feats)
QgsProject.instance().addMapLayer(mem)
ok = all(count(mem, [{"field": "t", "op": "eq", "value": t}]) == 1 for t in texts)
check("the same in a temporary layer", ok)

# names with «&», quotes and HTML: menu text and layer note
iface = MagicMock()
iface.mainWindow.return_value = QMainWindow()
iface.activeLayer.return_value = lyr
import definition_queries  # noqa: E402
plugin = definition_queries.classFactory(iface)
plugin.initGui()
add_query(lyr, "Asia & Europe", [{"field": fname, "op": "eq", "value": "tab\there"}])
add_query(lyr, "<b>bold</b> 'x'", [{"field": fname, "op": "eq", "value": "  "}])
plugin._populate_layer_menu()
texts_in_menu = [a.text() for a in plugin.layer_menu.actions()]
check("«&» in a query name is shown as typed in the menu", "Asia && Europe" in texts_in_menu, texts_in_menu[:6])
st.set_notes_enabled(True)
from qgis.core import QgsLayerNotesUtils  # noqa: E402
note = QgsLayerNotesUtils.layerNotes(lyr)
check("names with HTML are escaped in the layer note", "&lt;b&gt;bold&lt;/b&gt;" in note and "<b>bold</b>" not in note)
st.set_notes_enabled(False)
plugin.unload()
QgsProject.instance().clear()

# ------------------------------------------------------------------ C2. long lists in GDAL formats
print("== C2. Long lists of texts in Shapefile / GeoJSON")
# GDAL compares texts ignoring upper/lower case in = and IN, so short lists use LIKE (exact);
# long lists of texts with a-z use IN (fast) and the plugin warns if case matters in the data
import time  # noqa: E402
shp = os.path.join(DATA, "codes.shp")
for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
    if os.path.exists(shp[:-4] + ext):
        os.remove(shp[:-4] + ext)
srs = osr.SpatialReference()
srs.ImportFromEPSG(4326)
ds = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(shp)
ol = ds.CreateLayer("codes", srs, ogr.wkbPoint)
ol.CreateField(ogr.FieldDefn("code", ogr.OFTString))
ol.CreateField(ogr.FieldDefn("area", ogr.OFTReal))
codes = ["C%05d" % i for i in range(20000)] + ["c00002"] + ["1%04d" % i for i in range(3000)]
for i, code in enumerate(codes):
    f = ogr.Feature(ol.GetLayerDefn())
    f.SetField("code", code)
    f.SetField("area", 1.5)
    f.SetGeometry(ogr.CreateGeometryFromWkt("POINT ({} {})".format(i % 100, i // 100)))
    ol.CreateFeature(f)
ds = None
codes_lyr = QgsVectorLayer(shp, "codes", "ogr")
QgsProject.instance().addMapLayer(codes_lyr)


def timed_count(clauses):
    t = time.time()
    n = count(codes_lyr, clauses)
    return n, time.time() - t


short = ["C%05d" % i for i in range(0, 40, 2)]          # 20 texts with letters: exact
n, _t = timed_count([{"field": "code", "op": "in", "values": short}])
check("short list: exact (C00002 does not bring c00002)", n == 20, n)
many = ["C%05d" % i for i in range(0, 2000, 2)]         # 1000 texts with letters
n, t = timed_count([{"field": "code", "op": "in", "values": many}])
check("1000 codes: fast", t < 5, "{:.2f} s".format(t))
check("1000 codes: GDAL also takes c00002 (upper/lower case ignored)", n == 1001, n)
warn = lint_mod.lint([{"field": "code", "op": "in", "values": many}], codes_lyr,
                     lambda f: st.unique_values(codes_lyr, f, limit=100000), lambda f: True)
check("…and the plugin warns about it, naming the value", any("c00002" in w for w in warn), warn[:2])
warn = lint_mod.lint([{"field": "code", "op": "in", "values": ["C%05d" % i for i in range(1000, 3000, 2)]}], codes_lyr,
                     lambda f: st.unique_values(codes_lyr, f, limit=100000), lambda f: True)
check("no warning when no value differs only in case", not any("case" in w for w in warn), warn[:2])
digits = ["1%04d" % i for i in range(0, 3000, 3)]      # 1000 texts without letters: exact and fast
n, t = timed_count([{"field": "code", "op": "in", "values": digits}])
check("1000 codes without letters: exact and fast", n == 1000 and t < 5, "{} in {:.2f} s".format(n, t))
n, t = timed_count([{"field": "code", "op": "not_in", "values": digits}])
check("…and «is none of» too", n == len(codes) - 1000 and t < 5, "{} in {:.2f} s".format(n, t))
sql = sb.build_sql([{"field": "code", "op": "in", "values": ["10001", "C00002", "10002"]}], codes_lyr)
back = sp.sql_to_clauses(sql, codes_lyr)
check("mixed list: SQL -> builder gives the same list", back and sorted(back[0]["values"]) == ["10001", "10002", "C00002"],
      sql)
codes_lyr.selectByExpression('"code" LIKE \'C0%\' AND "code" < \'C00100\'')
r = st.selection_to_query(codes_lyr)
check("filter by selection with a text key warns if case matters", "c00002" in (r.get("warning") or ""),
      (r.get("warning") or "")[:120])
codes_lyr.removeSelection()
QgsProject.instance().removeMapLayer(codes_lyr.id())

# ------------------------------------------------------------------ C3. decimals and date-times picked from the list
print("== C3. Decimals and date-times picked from the value list")
import pathlib  # noqa: E402
DECIMALS = [0.1 + 0.2, 1 / 3, 1e-7, 123456789.123456789, -0.0, 1e20, 2.5, -7.25]
STAMPS = ["2024-03-03T10:14:00.123Z", "2024-03-03T10:14:00Z", "2024-03-03T23:30:00", "2024-03-04T01:15:00.5",
          "2024-03-03T22:00:00-03:00", None, "2024-03-03T00:00:00"]


def ogr_file(driver, path, rows):
    if os.path.exists(path):
        ogr.GetDriverByName(driver).DeleteDataSource(path)
    ref = osr.SpatialReference()
    ref.ImportFromEPSG(4326)
    out = ogr.GetDriverByName(driver).CreateDataSource(path)
    lay = out.CreateLayer("data", ref, ogr.wkbPoint)
    lay.CreateField(ogr.FieldDefn("x", ogr.OFTReal))
    lay.CreateField(ogr.FieldDefn("dt", ogr.OFTDateTime))
    for i, (x, dt) in enumerate(rows):
        f = ogr.Feature(lay.GetLayerDefn())
        f.SetField("x", x)
        if dt is not None:
            f.SetField("dt", dt)
        f.SetGeometry(ogr.CreateGeometryFromWkt("POINT ({} 0)".format(i)))
        lay.CreateFeature(f)
    out = None


rows = list(zip(DECIMALS[:len(STAMPS)], STAMPS))
built = {}
for name, driver, ext in (("GeoPackage", "GPKG", "gpkg"), ("GeoJSON", "GeoJSON", "geojson"),
                          ("FlatGeobuf", "FlatGeobuf", "fgb")):
    path = os.path.join(DATA, "picked." + ext)
    ogr_file(driver, path, rows)
    built[name] = QgsVectorLayer(path + ("|layername=data" if ext == "gpkg" else ""), name, "ogr")
csv_path = os.path.join(DATA, "picked.csv")
with open(csv_path, "w", encoding="utf-8") as fh:
    fh.write("x,dt,X,Y\n")
    for i, (x, dt) in enumerate(rows):
        fh.write("{},{},{},0\n".format(repr(x), dt or "", i))
built["CSV"] = QgsVectorLayer(pathlib.Path(csv_path).as_uri() + "?delimiter=,&xField=X&yField=Y&crs=EPSG:4326&detectTypes=yes",
                              "CSV", "delimitedtext")
built["Temporary layer"] = built["GeoPackage"].materialize(QgsFeatureRequest())
for name, lyr_p in built.items():
    QgsProject.instance().addMapLayer(lyr_p)
    wrong = []
    for fname in ("x", "dt"):
        for v in st.unique_values(lyr_p, fname):
            if v in sb.SPECIAL_VALUES:
                continue
            n = count(lyr_p, [{"field": fname, "op": "eq", "value": v}])
            if n < 1:
                wrong.append((fname, v))
    check("{}: every value picked from the list finds its features (decimals, milliseconds)".format(name),
          not wrong, wrong)
    day = count(lyr_p, [{"field": "dt", "op": "eq", "value": "2024-03-03"}])
    check("{}: «is on 2024-03-03» finds the whole day".format(name), day == 5, day)
    after = count(lyr_p, [{"field": "dt", "op": "gt", "value": "2024-03-03"}])
    check("{}: «is after 2024-03-03» starts the next day".format(name), after == 1, after)
    QgsProject.instance().removeMapLayer(lyr_p.id())

# ------------------------------------------------------------------ C4. values that look alike
print("== C4. Values that look alike in a list")
path = os.path.join(DATA, "alike.gpkg")
make(path, [["Vega"], ["Vega "], [" Vega"], ["vega"], ["  "], ["Vega"]], [("name", ogr.OFTString)])
alike = layer(path, "alike")
check("spaces at the start or end are shown as ␣",
      [sb.display_value(v) for v in ("Vega ", " Vega", "  ", "Vega")] == ["Vega␣", "␣Vega", "␣␣", "Vega"])
check("…and read back as spaces", [sb.stored_value(t) for t in ("Vega␣", "␣Vega", "␣␣")] == ["Vega ", " Vega", "  "])
w = ClauseWidget(alike, lambda f: st.unique_values(alike, f))
w.set_clause({"field": "name", "op": "eq", "value": "Vega "})
pump()
check("a saved «Vega » shows as «Vega␣» and keeps its space", w.value1.currentText() == "Vega␣" and
      w.clause()["value"] == "Vega ", (w.value1.currentText(), w.clause()["value"]))
items = [w.value1.itemText(k) for k in range(w.value1.count())]
check("the list shows every variant apart", {"Vega", "Vega␣", "␣Vega", "vega", "␣␣"} <= set(items), items)
check("«Vega» finds only «Vega»", count(alike, [{"field": "name", "op": "eq", "value": "Vega"}]) == 2)
w.op.setCurrentIndex(w.op.findData("in"))
pump()
w.list_menu.set_values(st.unique_values(alike, "name"), [])
w.list_menu.search.setText("vega")
w.list_menu._enter()
picked = w.list_menu.checked_values()
check("typing «vega» + Enter in the list checks «vega», not «Vega»", picked == ["vega"], picked)
w.list_menu.search.setText("VEGA")
w.list_menu._enter()
picked = w.list_menu.checked_values()
check("«VEGA» (not in the data, two case-insensitive matches) is added as typed", "VEGA" in picked, picked)
w.deleteLater()
pump()
QgsProject.instance().removeMapLayer(alike.id())

# ------------------------------------------------------------------ D. project life
print("== D. Project life")
rows = [["Asia", i] for i in range(10)] + [["Europe", i] for i in range(10)]
base = os.path.join(DATA, "life.gpkg")
make(base, rows, [("cont", ogr.OFTString), ("n", ogr.OFTInteger)])


def fresh(tag):
    p = os.path.join(DATA, "life_{}.gpkg".format(tag))
    shutil.copy(base, p)
    return p


ASIA = [{"field": "cont", "op": "eq", "value": "Asia"}]

# D1 missing data source
p = fresh("missing")
lyr = layer(p)
q = add_query(lyr, "Asia", ASIA)
st.apply_filter(lyr, q["id"])
prj_file = os.path.join(DATA, "missing.qgz")
QgsProject.instance().write(prj_file)
QgsProject.instance().clear()
os.rename(p, p + ".moved")
QgsProject.instance().read(prj_file)
lyr = QgsProject.instance().mapLayersByName("data")[0]
st.sync_from_layer(lyr)
check("missing file: no false «field no longer exists» warning", not lyr.isValid() and st.broken_filter(lyr) is None)
check("missing file: queries untouched, no extra «QGIS filter» entry",
      [x["name"] for x in st.load(lyr)["queries"]] == ["Asia"])
QgsProject.instance().clear()
os.rename(p + ".moved", p)
QgsProject.instance().read(prj_file)
lyr = QgsProject.instance().mapLayersByName("data")[0]
check("file back in place: the filter works again", lyr.isValid() and lyr.featureCount() == 10
      and (st.active_query(lyr) or {}).get("name") == "Asia")
QgsProject.instance().clear()

# D2 edit mode with new features
lyr = layer(fresh("edit"))
lyr.startEditing()
f = QgsFeature(lyr.fields())
f["cont"] = "Asia"
f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(1, 1)))
lyr.addFeature(f)
lyr.selectAll()
q, ok, msg = st.query_from_selection(lyr, "sel")
check("filter by selection in edit mode: refused, nothing saved", not ok and q is None and not st.load(lyr)["queries"], msg)
q, ok, msg = st.query_from_selection(lyr, "sel", activate=False)
check("…and new features with temporary IDs are never saved in a query", not ok and q is None, msg)
lyr.rollBack()
QgsProject.instance().clear()

# D3 deleting the active query with the window open, D4 removing the layer with the window open
from definition_queries.dialog import QueryManagerDialog  # noqa: E402
lyr = layer(fresh("dialog"))
other = layer(fresh("dialog2"), "other")
q = add_query(lyr, "Asia", ASIA)
st.apply_filter(lyr, q["id"])
iface = MagicMock()
iface.mainWindow.return_value = QMainWindow()
iface.activeLayer.return_value = lyr
dlg = QueryManagerDialog(iface)
dlg.open_for(lyr)
pump()
from qgis.PyQt.QtWidgets import QMessageBox  # noqa: E402
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
dlg.list.setCurrentRow(0)
dlg.delete_query()
pump()
check("deleting the active query removes its filter", lyr.subsetString() == "" and not st.load(lyr)["queries"])
QgsProject.instance().removeMapLayer(lyr.id())
pump(8)
try:
    dlg.verify()
    dlg.new_query()
    pump()
    alive = True
except RuntimeError as e:
    alive = False
    print("   ", e)
check("removing the layer with the window open does not break the window", alive and dlg.layer is not lyr)
dlg.close()
dlg.deleteLater()
pump(4)
QgsProject.instance().clear()

# D5 reloading the plugin
iface = MagicMock()
iface.mainWindow.return_value = QMainWindow()
lyr = layer(fresh("reload"))
errors = []
try:
    for _ in range(2):
        plugin = definition_queries.classFactory(iface)
        plugin.initGui()
        plugin.unload()
except Exception as e:  # noqa: BLE001
    errors.append(repr(e))
reg = QgsApplication.processingRegistry()
check("loading and unloading the plugin twice", not errors and reg.providerById("definitionqueries") is None, errors)
lyr.setSubsetString('"cont" = \'Europe\'')
check("after unloading, the plugin no longer reacts to layer changes", not st.load(lyr)["queries"])
QgsProject.instance().clear()

# D6 a colleague without the plugin saves the project
lyr = layer(fresh("noplugin"))
add_query(lyr, "Asia", ASIA)
add_query(lyr, "Europe", [{"field": "cont", "op": "eq", "value": "Europe"}])
prj_file = os.path.join(DATA, "noplugin.qgz")
QgsProject.instance().write(prj_file)
QgsProject.instance().clear()
QgsProject.instance().read(prj_file)          # plain QGIS: nothing of the plugin runs here
QgsProject.instance().write(prj_file)
QgsProject.instance().clear()
QgsProject.instance().read(prj_file)
lyr = QgsProject.instance().mapLayersByName("data")[0]
check("saved again without the plugin: the queries are still there",
      [x["name"] for x in st.load(lyr)["queries"]] == ["Asia", "Europe"])
QgsProject.instance().clear()

# D7 pasting the style on a layer with other fields
src = layer(fresh("style"))
add_query(src, "Asia", ASIA)
path_other = os.path.join(DATA, "style_other.gpkg")
make(path_other, [["x", 1]], [("region", ogr.OFTString), ("n", ogr.OFTInteger)])
dst = layer(path_other, "dst")
style = QgsMapLayerStyle()
style.readFromLayer(src)
style.writeToLayer(dst)
check("style pasted on another layer: missing fields are detected", st.missing_query_fields(dst) == ["cont"])
n, msg = st.replace_field(dst, "cont", "region")
check("…and can be replaced", n == 1 and not st.missing_query_fields(dst), msg)
QgsProject.instance().clear()

# D8 a field that changes type while the filter stays saved
p = fresh("type")
lyr = layer(p)
q = add_query(lyr, "n = 3", [{"field": "n", "op": "eq", "value": "3"}])
st.apply_filter(lyr, q["id"])
prj_file = os.path.join(DATA, "type.qgz")
QgsProject.instance().write(prj_file)
QgsProject.instance().clear()
tmp = p + ".tmp.gpkg"
os.system('ogr2ogr -q -f GPKG "{}" "{}" -sql "SELECT id, geom, cont, CAST(n AS TEXT) AS n FROM data" -nln data'.format(tmp, p))
os.replace(tmp, p)
QgsProject.instance().read(prj_file)
lyr = QgsProject.instance().mapLayersByName("data")[0]
_q, status = st.active_status(lyr)
check("field type changed: the stale filter is flagged (not silently ok)",
      lyr.fields().field("n").typeName().lower().startswith(("text", "string")) and status == "pending", status)
ok, msg = st.apply_filter(lyr, q["id"])
check("…and applying the query again fixes it", ok and lyr.featureCount() == 2, msg)
QgsProject.instance().clear()

# D9 virtual layer
p = fresh("virtual")
v = QgsVectorLayer("?layer=ogr:{}:data:UTF-8&query=SELECT * FROM data".format(p), "virtual", "virtual")
QgsProject.instance().addMapLayer(v)
if v.isValid():
    q = add_query(v, "Asia", ASIA)
    ok, msg = st.apply_filter(v, q["id"])
    check("virtual layer: the filter works", ok and v.featureCount() == 10, msg)
    v.selectByExpression('"n" < 3')           # within the Asia filter: 3 features
    q, ok, msg = st.query_from_selection(v, "sel")
    check("virtual layer: filter by selection", ok and v.featureCount() == 3, msg)
else:
    print("   (virtual layers not available here)")
QgsProject.instance().clear()

# D10 polar map projection and a view that crosses the antimeridian
lyr = layer(fresh("polar"))
for crs_id, ext in (("EPSG:3031", QgsRectangle(-3e6, -3e6, 3e6, 3e6)), ("EPSG:4326", QgsRectangle(170, -10, 190, 10))):
    ms = QgsMapSettings()
    ms.setDestinationCrs(QgsCoordinateReferenceSystem(crs_id))
    ms.setOutputSize(QSize(400, 400))
    ms.setLayers([lyr])
    ms.setExtent(ext)
    try:
        ids, err = st.visible_ids(lyr, ms)
        check("select visible with {}: no error".format(crs_id), True, "{} features".format(len(ids or [])))
    except Exception as e:  # noqa: BLE001
        check("select visible with {}: no error".format(crs_id), False, repr(e))
QgsProject.instance().clear()

# D12 «Select visible» with the Temporal Controller: only what is drawn at the current time
from qgis.core import QgsDateTimeRange, QgsVectorLayerTemporalProperties  # noqa: E402
from qgis.PyQt.QtCore import QDate, QDateTime, QTime  # noqa: E402
path = os.path.join(DATA, "timed.gpkg")
make(path, [["2024-01-{:02d}".format(1 + i % 28)] for i in range(40)], [("day", ogr.OFTDate)])
timed = layer(path, "timed")
tp = timed.temporalProperties()
tp.setIsActive(True)
tp.setMode(QgsVectorLayerTemporalProperties.TemporalMode.ModeFeatureDateTimeInstantFromField)
tp.setStartField("day")
ms = QgsMapSettings()
ms.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
ms.setOutputSize(QSize(400, 400))
ms.setLayers([timed])
ms.setExtent(QgsRectangle(-1, -1, 51, 2))
ms.setIsTemporal(True)
ms.setTemporalRange(QgsDateTimeRange(QDateTime(QDate(2024, 1, 1), QTime(0, 0)), QDateTime(QDate(2024, 1, 3), QTime(0, 0))))
ids, err = st.visible_ids(timed, ms)
days = sorted({f["day"].toString("yyyy-MM-dd") for f in timed.getFeatures(ids or [])})
check("select visible with the Temporal Controller: only the current days", err is None and days and
      days[-1] <= "2024-01-03" and len(ids) < timed.featureCount(), (len(ids or []), days))
tp.setMode(QgsVectorLayerTemporalProperties.TemporalMode.ModeFixedTemporalRange)
tp.setFixedTemporalRange(QgsDateTimeRange(QDateTime(QDate(2020, 1, 1), QTime(0, 0)), QDateTime(QDate(2020, 12, 31), QTime(0, 0))))
ids, err = st.visible_ids(timed, ms)
check("…and a layer not drawn at the current time is explained", ids is None and "time" in (err or ""), err)
QgsProject.instance().removeMapLayer(timed.id())

# D13 Processing tools
proc_dir = next((d for d in (os.path.join(QgsApplication.pkgDataPath(), "python", "plugins"),
                             "/usr/share/qgis/python/plugins") if os.path.isdir(os.path.join(d, "processing"))), None)
if proc_dir:
    sys.path.append(proc_dir)
    from processing.core.Processing import Processing  # noqa: E402
    import processing  # noqa: E402
    from qgis.core import QgsProcessingException  # noqa: E402
    Processing.initialize()
    lyr = layer(fresh("processing"))
    iface = MagicMock()
    iface.mainWindow.return_value = QMainWindow()
    plugin = definition_queries.classFactory(iface)
    plugin.initGui()
    add_query(lyr, "Asia", ASIA)
    r = processing.run("definitionqueries:applysavedquery", {"INPUT": lyr, "QUERY": "asia", "ACTION": 0})
    check("Processing: apply a saved query (name not case-sensitive)", r["COUNT"] == 10 and lyr.featureCount() == 10, r)
    r = processing.run("definitionqueries:applysavedquery", {"INPUT": lyr, "QUERY": "Asia", "ACTION": 1, "SELECTION": 0})
    check("Processing: select with a saved query", r["COUNT"] == 10, r)
    r = processing.run("definitionqueries:removefilter", {"INPUT": lyr})
    check("Processing: remove the filter", lyr.subsetString() == "" and r["COUNT"] == lyr.featureCount(), r)
    r = processing.run("definitionqueries:filterbyattributes", {"INPUT": lyr, "WHERE": '"n" < 5', "NAME": "small"})
    check("Processing: filter by SQL and save it", r["COUNT"] == 10 and st.find(st.load(lyr), name="small") is not None, r)
    try:
        processing.run("definitionqueries:filterbyattributes", {"INPUT": lyr, "WHERE": '"nope" = 1'})
        check("Processing: a field that does not exist is refused (not a silent empty filter)", False)
    except QgsProcessingException as e:
        check("Processing: a field that does not exist is refused (not a silent empty filter)", "nope" in str(e), str(e))
    st.clear_filter(lyr)
    lyr.selectByExpression('"cont" = \'Europe\' AND "n" >= 7')
    r = processing.run("definitionqueries:filterbyselection", {"INPUT": lyr, "NAME": "last ones"})
    check("Processing: filter by selection", r["COUNT"] == 3 and lyr.featureCount() == 3, r)
    plugin.unload()
    QgsProject.instance().clear()
else:
    print("   (Processing framework not found: skipped)")

# D14 SQL typed by hand that uses the geometry column or GDAL's own fields
lyr = layer(fresh("geomsql"))
check("«\"geom\" IS NOT NULL» is accepted (the geometry column is not a missing field)",
      st.check_fields(lyr, '"{}" IS NOT NULL'.format(st.geometry_column(lyr) or "geom")) is None, st.geometry_column(lyr))
check("a field that does not exist is still refused", st.check_fields(lyr, '"nope" = 1') is not None)
shp_codes = QgsVectorLayer(os.path.join(DATA, "codes.shp"), "codes", "ogr")
check("Shapefile: «OGR_GEOM_AREA» (a GDAL field) is accepted", st.check_fields(shp_codes, "OGR_GEOM_AREA >= 0") is None)
QgsProject.instance().clear()

# D11 damaged or hand-made data: projects edited by hand, JSON made by another tool
print("== D11. Damaged saved data")
lyr = layer(fresh("damaged"))
iface = MagicMock()
iface.mainWindow.return_value = QMainWindow()
plugin = definition_queries.classFactory(iface)
plugin.initGui()
plugin._layer = lambda: lyr
PAYLOADS = [
    "not json at all {",
    json.dumps([1, 2, 3]),
    json.dumps({"queries": "abc", "active": 5}),
    json.dumps({"queries": [None, 7, "x", {"name": 12}]}),
    json.dumps({"queries": [{"name": "a", "clauses": "abc"}, {"name": "b", "clauses": [None, 5, {"op": "zzz"}]}]}),
    json.dumps({"queries": [{"name": "c", "clauses": [{"field": "cont", "op": "in", "values": "Asia"}]},
                            {"name": "d", "clauses": [{"field": ["x"], "op": "eq", "value": {"a": 1}, "groups": "x"}]},
                            {"name": "e", "clauses": [{"field": "n", "op": "between", "value": 1, "value2": None,
                                                       "groups": [1, "two", True]}]},
                            {"id": "same", "name": "f", "mode": "sql", "sql": 5},
                            {"id": "same", "name": "g", "mode": "weird",
                             "clauses": [{"field": "cont", "op": "eq", "value": "Asia", "group": 3}]}],
                "active": "same"}),
]
for k, raw in enumerate(PAYLOADS):
    lyr.setCustomProperty(st.PROP_KEY, raw)
    try:
        d = st.load(lyr)
        st.active_status(lyr, d)
        st.broken_filter(lyr)
        st.missing_query_fields(lyr)
        for q in d["queries"]:
            try:
                st.query_sql(lyr, q)
            except sb.ClauseError:
                pass
        plugin._populate_layer_menu()
        dlg = QueryManagerDialog(iface)
        dlg.open_for(lyr)
        pump()
        for row in range(dlg.list.count()):
            dlg.list.setCurrentRow(row)
            pump()
        dlg.close()
        dlg.deleteLater()
        pump()
        check("damaged data #{}: no error, usable queries kept".format(k + 1), True,
              [q["name"] for q in st.load(lyr)["queries"]])
    except Exception as e:  # noqa: BLE001
        check("damaged data #{}: no error, usable queries kept".format(k + 1), False, repr(e))
bad_json = os.path.join(DATA, "damaged.json")
for content in ('{"queries": [{"name": 5, "clauses": {"a": 1}}]}', '[{"name": "ok", "clauses": [{"field": "cont"}]}]',
                '{"queries": 3}', '"text"'):
    with open(bad_json, "w", encoding="utf-8") as fh:
        fh.write(content)
    lyr.setCustomProperty(st.PROP_KEY, "")
    try:
        n = st.import_json(lyr, bad_json)
        check("importing a strange JSON: no error ({} imported)".format(n), True)
    except Exception as e:  # noqa: BLE001
        check("importing a strange JSON: no error", False, repr(e))
plugin.unload()
QgsProject.instance().clear()

# ------------------------------------------------------------------ E. edge values found in review
print("== E. Edge values")
path = os.path.join(DATA, "edges.gpkg")
make(path, [["9999-12-31T00:00:00", "<Null>", 1], ["2024-03-03T10:00:00", "Asia", 0], [None, None, None],
            ["0001-01-01T12:00:00", "Asia", 1]],
     [("dt", ogr.OFTDateTime), ("cont", ogr.OFTString), ("flag", ogr.OFTInteger)])
edges = layer(path, "edges")
for op, v, expected in (("eq", "9999-12-31", 1), ("le", "9999-12-31", 3), ("gt", "9999-12-31", 0),
                        ("ne", "9999-12-31", 2), ("eq", "0001-01-01", 1), ("lt", "0001-01-02", 1)):
    try:
        n = count(edges, [{"field": "dt", "op": op, "value": v}])
        check("date-time «{} {}»: {} feature(s)".format(op, v, expected), n == expected, n)
    except Exception as e:  # noqa: BLE001
        check("date-time «{} {}»: {} feature(s)".format(op, v, expected), False, repr(e))
try:
    n = count(edges, [{"field": "dt", "op": "between", "value": "2000-01-01", "value2": "9999-12-31"}])
    check("«is between 2000-01-01 and 9999-12-31»", n == 2, n)
except Exception as e:  # noqa: BLE001
    check("«is between 2000-01-01 and 9999-12-31»", False, repr(e))
try:
    edges.setSubsetString('"dt" < \'9999-12-31\'')
    st.sync_from_layer(edges)
    st.active_status(edges)
    edges.setSubsetString("")
    check("a hand-written filter with 9999-12-31 does not break the plugin", True)
except Exception as e:  # noqa: BLE001
    check("a hand-written filter with 9999-12-31 does not break the plugin", False, repr(e))
check("years before 1000 keep 4 digits in the SQL",
      "'0001-01-01 00:00:00'" in sb.build_sql([{"field": "dt", "op": "eq", "value": "0001-01-01"}], edges))
check("GeoPackage: «OGR_GEOM_AREA» is not a field there (GDAL's own fields are only for GDAL formats)",
      st.check_fields(edges, '"OGR_GEOM_AREA" > 1') is not None)
edges.setSubsetString('"{}" IS NOT NULL'.format(st.geometry_column(edges) or "geom"))
check("a filter on the geometry column is not reported as broken", st.broken_filter(edges) is None,
      st.broken_filter(edges))
edges.setSubsetString("")
back = sp.sql_to_clauses('"flag" = 1 OR "flag" IS NULL', edges)
check("SQL -> builder never gives an operator the field does not offer",
      back is None or all(c["op"] in dict(sb.operators_for_kind(sb.field_kind(edges.fields().field(c["field"]))))
                          for c in back), back)
clauses, reason = sp.parse('("cont" < \'a\' OR "cont" > \'m\')', edges)
check("…e.g. «is not between» on a text is not produced", clauses is None and reason, reason)
edges.setCustomProperty(st.PROP_KEY, json.dumps({"queries": [{"name": "no id", "mode": "sql", "sql": '"flag" = 1'}]}))
qid = st.load(edges)["queries"][0]["id"]
ok, msg = st.apply_filter(edges, qid)
check("a saved query without an id can be applied (same id on every load)",
      ok and qid == st.load(edges)["queries"][0]["id"], msg)
st.clear_filter(edges)
w = ClauseWidget(edges, lambda f: st.unique_values(edges, f))
w.set_clause({"field": "cont", "op": "eq", "value": "<Null>"})   # the real text, not the empty value
pump()
check("a saved text «<Null>» reopens as that text", w.clause()["value"] == "<Null>", w.clause())
w.set_clause({"field": "cont", "op": "eq", "value": NUL})
pump()
w.op.setCurrentIndex(w.op.findData("contains"))
pump()
specials = [w.value1.itemData(k) for k in range(w.value1.count()) if w.value1.itemData(k) in sb.SPECIAL_VALUES]
check("«contains» does not offer <Null> / <Empty> in its list", not specials, specials)
w.set_clause({"field": "flag", "op": "eq", "value": "<Empty>"})   # a text that is not in the list
pump()
check("a saved text that reads like <Empty> stays a text even when it is not in the list",
      w.clause()["value"] == "<Empty>", w.clause())
w.deleteLater()
pump()
clauses, reason = sp.parse('"flag" <> \'\'', edges)
check("«<> ''» on a number is not turned into «is not blank» (not offered for numbers)", clauses is None, clauses)
dup = {"id": "abc", "name": "c"}
taken = st._stable_id(dup, 2, 0)
saved = {"queries": [{"id": taken, "name": "a"}, {"id": "abc", "name": "b"}, dup], "active": taken}
edges.setCustomProperty(st.PROP_KEY, json.dumps(saved))
d = st.load(edges)
ids = [q["id"] for q in d["queries"]]
check("repeated or missing ids never take an id that is already used", len(set(ids)) == 3 and ids[0] == taken
      and d["active"] == taken, ids)
check("…and they are the same on every load", ids == [q["id"] for q in st.load(edges)["queries"]])
QgsProject.instance().clear()

print("\nFAILS:", FAILS)
sys.exit(1 if FAILS else 0)
