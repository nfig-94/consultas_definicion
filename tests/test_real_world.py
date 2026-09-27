"""Real-world situations: renamed fields, changed values, duplicated layers, large
selections, "select visible features", layer notes and projects saved with older keys.

It builds its own small data in tests/data (no downloads). Run with the Python that
comes with QGIS (on Linux without a screen add QT_QPA_PLATFORM=offscreen):

    python tests/test_real_world.py

Exits with status 1 if any check fails.
"""
import json
import os
import random
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
os.environ.setdefault("DEFINITION_QUERIES_LANG", "en")

from osgeo import ogr, osr  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsApplication, QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsFillSymbol, QgsGeometry, QgsLayerNotesUtils, QgsMapSettings, QgsProject,
    QgsRectangle, QgsRendererCategory, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QSize  # noqa: E402

app = QgsApplication([], False)
app.initQgis()
from definition_queries import query_store as st  # noqa: E402

DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)
FAILS = 0
CONTINENTS = ["Africa", "Asia", "Europe", "America", "Oceania"]


def check(name, cond, extra=""):
    global FAILS
    print(("ok    " if cond else "FAIL  ") + name, extra)
    FAILS += 0 if cond else 1


def make_gpkg(path):
    """A 20 x 10 grid of 1-degree squares with id, name, continent and population."""
    if os.path.exists(path):
        os.remove(path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    lyr = ds.CreateLayer("cells", srs, ogr.wkbPolygon, options=["FID=id"])
    for name, kind in (("name", ogr.OFTString), ("continent", ogr.OFTString), ("population", ogr.OFTInteger64)):
        lyr.CreateField(ogr.FieldDefn(name, kind))
    rng = random.Random(7)
    lyr.StartTransaction()
    for row in range(10):
        for col in range(20):
            f = ogr.Feature(lyr.GetLayerDefn())
            f.SetField("name", "Cell {}-{}".format(row, col))
            f.SetField("continent", CONTINENTS[(row * 20 + col) % len(CONTINENTS)])
            f.SetField("population", rng.randint(1000, 200000000))
            x, y = col, row
            f.SetGeometry(ogr.CreateGeometryFromWkt(
                "POLYGON(({0} {1},{2} {1},{2} {3},{0} {3},{0} {1}))".format(x, y, x + 1, y + 1)))
            lyr.CreateFeature(f)
    lyr.CommitTransaction()
    ds = None


BASE = os.path.join(DATA, "cells.gpkg")
make_gpkg(BASE)


def fresh(tag):
    path = os.path.join(DATA, "cells_{}.gpkg".format(tag))
    shutil.copy(BASE, path)
    return path


def layer(path, name="cells"):
    lyr = QgsVectorLayer(path + "|layername=cells", name, "ogr")
    QgsProject.instance().addMapLayer(lyr)
    return lyr


def add_query(lyr, name, clauses):
    data = st.load(lyr)
    q = st.new_query(name, clauses=clauses)
    data["queries"].append(q)
    st.save(lyr, data)
    return q


ASIA = [{"field": "continent", "op": "eq", "value": "Asia"}]
N_ASIA = 40

print("== A field is renamed outside QGIS while the project is closed")
path = fresh("rename")
lyr = layer(path)
q = add_query(lyr, "Asia", ASIA)
st.apply_filter(lyr, q["id"])
project_file = os.path.join(DATA, "rename.qgz")
QgsProject.instance().write(project_file)
QgsProject.instance().clear()
ds = ogr.Open(path, 1)
ds.ExecuteSQL("ALTER TABLE cells RENAME COLUMN continent TO region")
ds = None
QgsProject.instance().read(project_file)
lyr = QgsProject.instance().mapLayersByName("cells")[0]
check("the broken filter is detected", bool(st.broken_filter(lyr)), st.broken_filter(lyr))
check("the missing field is listed", st.missing_query_fields(lyr) == ["continent"])
n, msg = st.replace_field(lyr, "continent", "region")
check("replacing the field fixes and re-applies the filter",
      n == 1 and lyr.featureCount() == N_ASIA and not st.broken_filter(lyr), msg)
QgsProject.instance().clear()

print("== The values inside the field change")
path = fresh("values")
lyr = layer(path)
q = add_query(lyr, "Asia", ASIA)
ds = ogr.Open(path, 1)
ds.ExecuteSQL("UPDATE cells SET continent = 'Asia-Pacific' WHERE continent = 'Asia'")
ds = None
lyr.dataProvider().reloadData()
ok, msg = st.apply_filter(lyr, q["id"])
check("an empty result can be detected (the interface warns with EMPTY_HINT)",
      ok and st.feature_count(lyr) == 0 and bool(st.EMPTY_HINT))
QgsProject.instance().clear()

print("== The layer is duplicated")
lyr = layer(fresh("dup"))
q_asia = add_query(lyr, "Asia", ASIA)
q_eu = add_query(lyr, "Europe", [{"field": "continent", "op": "eq", "value": "Europe"}])
st.apply_filter(lyr, q_asia["id"])
copy = lyr.clone()
copy.setName("cells copy")
QgsProject.instance().addMapLayer(copy)
check("the copy keeps the queries and the active filter",
      [x["name"] for x in st.load(copy)["queries"]] == ["Asia", "Europe"] and (st.active_query(copy) or {}).get("name") == "Asia")
st.apply_filter(copy, q_eu["id"])
check("after that both layers are independent",
      (st.active_query(lyr) or {}).get("name") == "Asia" and (st.active_query(copy) or {}).get("name") == "Europe")
QgsProject.instance().clear()

print("== Filter by selection")
lyr = layer(fresh("sel"))
lyr.selectByExpression('"id" <= 150 OR "id" IN (170, 172, 190)')
want = sorted(lyr.selectedFeatureIds())
q, ok, msg = st.query_from_selection(lyr, "selection")
got = sorted(f.id() for f in lyr.getFeatures())
check("consecutive ids become ranges and give the same features", ok and got == want, lyr.subsetString())
check("the ranges are shown in the builder", any(c["op"] == "between" for c in q.get("clauses", [])))
QgsProject.instance().clear()

lyr = layer(fresh("big"))
limit = st.LARGE_SELECTION
st.LARGE_SELECTION = 30
ids = sorted(random.Random(3).sample([f.id() for f in lyr.getFeatures()], 80))
ids = [i for k, i in enumerate(ids) if k == 0 or i != ids[k - 1] + 1]
lyr.selectByIds(ids)
q, ok, msg = st.query_from_selection(lyr, "scattered")
check("a large scattered selection warns that the map may be slow", ok and "slow" in msg)
st.LARGE_SELECTION = limit
QgsProject.instance().clear()

print("== Select visible features")
lyr = layer(fresh("visible"))
crs = QgsCoordinateReferenceSystem("EPSG:3857")
to_map = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), crs, QgsProject.instance())


def settings(extent_4326, rotation=0.0):
    ms = QgsMapSettings()
    ms.setDestinationCrs(crs)
    ms.setOutputSize(QSize(800, 600))
    ms.setLayers([lyr])
    ms.setExtent(to_map.transformBoundingBox(QgsRectangle(*extent_4326)))
    ms.setRotation(rotation)
    return ms


def reference(ms, keep=lambda f: True):
    """Features touching the screen corners polygon, computed independently."""
    m2p = ms.mapToPixel()
    w, h = ms.outputSize().width(), ms.outputSize().height()
    ring = [m2p.toMapCoordinates(x, y) for x, y in ((0, 0), (w, 0), (w, h), (0, h), (0, 0))]
    poly = QgsGeometry.fromPolygonXY([ring]).densifyByCount(50)
    poly.transform(QgsCoordinateTransform(crs, lyr.crs(), QgsProject.instance()))
    return sorted(f.id() for f in lyr.getFeatures() if f.geometry().intersects(poly) and keep(f))


ms = settings((2.5, 2.5, 9.5, 6.5))
ids, err = st.visible_ids(lyr, ms)
check("inside the view", err is None and sorted(ids) == reference(ms), "{} features".format(len(ids or [])))
lyr.setSubsetString('"population" > 100000000')
ids, err = st.visible_ids(lyr, ms)
check("respects the layer filter", sorted(ids) == reference(ms) and 0 < len(ids))
lyr.setSubsetString("")
cats = [QgsRendererCategory(v, QgsFillSymbol.createSimple({}), v, v != "Asia") for v in CONTINENTS]
lyr.setRenderer(QgsCategorizedSymbolRenderer("continent", cats))
ids, err = st.visible_ids(lyr, ms)
check("skips categories turned off in the legend", sorted(ids) == reference(ms, lambda f: f["continent"] != "Asia"))
ms_rot = settings((2.5, 2.5, 9.5, 6.5), rotation=30)
ids, err = st.visible_ids(lyr, ms_rot)
check("works with a rotated map", sorted(ids) == reference(ms_rot, lambda f: f["continent"] != "Asia"))
QgsProject.instance().layerTreeRoot().findLayer(lyr.id()).setItemVisibilityChecked(False)
ok, msg = st.select_visible(lyr, ms)
check("a layer turned off selects nothing", not ok and lyr.selectedFeatureCount() == 0, msg)
QgsProject.instance().layerTreeRoot().findLayer(lyr.id()).setItemVisibilityChecked(True)
ok, msg = st.select_visible(lyr, settings((40, 40, 45, 45)))
check("an empty view selects nothing", not ok and lyr.selectedFeatureCount() == 0, msg)
ok, msg = st.select_visible(lyr, ms)
n = lyr.selectedFeatureCount()
q, ok2, msg2 = st.query_from_selection(lyr, "visible")
check("select visible, then filter by selection", ok and ok2 and st.feature_count(lyr) == n, "{} features".format(n))
QgsProject.instance().clear()

print("== Layer notes are opt-in")
lyr = layer(fresh("notes"))
QgsLayerNotesUtils.setLayerNotes(lyr, "My note")
add_query(lyr, "Asia", ASIA)
check("off by default: the user's note is untouched", not st.notes_enabled() and QgsLayerNotesUtils.layerNotes(lyr) == "My note")
st.set_notes_enabled(True)
check("when turned on, the queries are added", "definition_queries:begin" in QgsLayerNotesUtils.layerNotes(lyr))
st.set_notes_enabled(False)
check("when turned off, only the user's note remains", QgsLayerNotesUtils.layerNotes(lyr) == "My note")
QgsProject.instance().clear()

print("== Projects saved before the plugin was renamed")
lyr = layer(fresh("oldkey"))
lyr.setCustomProperty("consultas_definicion/data", json.dumps({"active": None, "queries": [st.new_query("Old", clauses=[])]}))
check("queries saved under the old key are read", [x["name"] for x in st.load(lyr)["queries"]] == ["Old"])
st.save(lyr, st.load(lyr))
check("and moved to the new key", bool(lyr.customProperty(st.PROP_KEY)) and not lyr.customProperty("consultas_definicion/data"))
QgsProject.instance().clear()

print("\nFAILS:", FAILS)
sys.exit(1 if FAILS else 0)
