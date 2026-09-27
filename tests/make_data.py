"""
Generate the "tricky" test layers used by test_formats.py.

The same 117 point features (39 tricky texts x 3) are written to tests/data/ in eight
formats. The attributes cover text, integer, real, date, date-time and boolean fields,
with NULLs, accents, "Ñ", quotes, backslashes, LIKE wildcards (% and _), leading and
trailing spaces and empty strings:

    tricky.gpkg (GeoPackage)    tricky.shp (Shapefile)     tricky.geojson (GeoJSON)
    tricky.fgb (FlatGeobuf)     tricky.gdb (FileGDB)       tricky.sqlite (SpatiaLite)
    tricky.xlsx (Excel)         tricky.csv (delimited text)

plus rows.json, a copy of the source rows for reference. The folder tests/data/ is
deleted and recreated on every run.

Run it from the plugin folder with the Python that comes with QGIS (on Linux without a
display, prefix the commands with QT_QPA_PLATFORM=offscreen):

    python tests/make_data.py
    python tests/test_formats.py [GeoPackage Shapefile ...]
"""
import csv
import datetime
import json
import os
import shutil

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsField,
                       QgsGeometry, QgsPointXY, QgsProject, QgsVectorFileWriter, QgsVectorLayer)
from qgis.PyQt.QtCore import QVariant, QDate, QDateTime, QTime

app = QgsApplication([], False)
app.initQgis()
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
shutil.rmtree(DATA, ignore_errors=True)
os.makedirs(DATA)

# The texts are deliberate test data (case, accents, "Ñ", quotes, backslashes, % and _
# wildcards, spaces, empty string and NULL): do not "fix" them.
TEXTS = ["Vega", "VEGA", "vega", "Vega altoandina", " Vega", "Vega ",
         "Árbol nativo", "árbol nativo", "ÁRBOL NATIVO", "Arbol nativo",
         "Ñandú", "ñandú", "ÑANDÚ", "Güiña", "GÜIÑA",
         "COD_1", "CODX1", "COD_10", "cod_1",
         "50%", "500", "5000", "100% cubierto",
         "O'Higgins", "o'higgins",
         "Matorral xerofítico", "Matorral xerofitico", "MATORRAL XEROFÍTICO",
         "a\\b", "C:\\ruta\\capa", "x_y", "xay",
         "Bosque esclerófilo", "bosque", "Pajonal", "¡Ojo! 5%_x", "fin!", "", None]
# The field names are part of the test data and stay in Spanish: TEXTO = text,
# ENTERO = integer, REAL = real, FECHA = date, FECHAHORA = date-time, BOOL = boolean.
rows = []
i = 0
for rep in range(3):
    for t in TEXTS:
        i += 1
        rows.append({
            "ID": i,
            "TEXTO": t,
            "ENTERO": None if i % 17 == 0 else (i % 13) - 4,
            "REAL": None if i % 19 == 0 else round(((i * 7.3) % 50) - 10, 2),
            "FECHA": None if i % 23 == 0 else datetime.date(2023 + i % 3, 1 + i % 12, 1 + i % 28),
            "FECHAHORA": None if i % 29 == 0 else datetime.datetime(2024, 1 + i % 6, 1 + i % 3, (i * 5) % 24, (i * 7) % 60, 0),
            "BOOL": None if i % 11 == 0 else (i % 2 == 0),
        })


def qv(v):
    """Python value -> the Qt value QGIS expects in a feature attribute."""
    if isinstance(v, datetime.datetime):
        return QDateTime(QDate(v.year, v.month, v.day), QTime(v.hour, v.minute, v.second))
    if isinstance(v, datetime.date):
        return QDate(v.year, v.month, v.day)
    return v


mem = QgsVectorLayer("Point?crs=EPSG:32719", "tricky", "memory")
pr = mem.dataProvider()
pr.addAttributes([QgsField("ID", QVariant.Int), QgsField("TEXTO", QVariant.String, len=80),
                  QgsField("ENTERO", QVariant.Int), QgsField("REAL", QVariant.Double, len=12, prec=3),
                  QgsField("FECHA", QVariant.Date), QgsField("FECHAHORA", QVariant.DateTime),
                  QgsField("BOOL", QVariant.Bool)])
mem.updateFields()
feats = []
for r in rows:
    f = QgsFeature(mem.fields())
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(350000 + r["ID"] * 10, 7400000 + r["ID"] * 3)))
    f.setAttributes([qv(r[k]) for k in ("ID", "TEXTO", "ENTERO", "REAL", "FECHA", "FECHAHORA", "BOOL")])
    feats.append(f)
pr.addFeatures(feats)


def write(driver, path, layer_name=None, opts=None, dsopts=None):
    """Write the in-memory layer with a GDAL/OGR driver and report the result."""
    o = QgsVectorFileWriter.SaveVectorOptions()
    o.driverName = driver
    if layer_name:
        o.layerName = layer_name
    if opts:
        o.layerOptions = opts
    if dsopts:
        o.datasourceOptions = dsopts
    err = QgsVectorFileWriter.writeAsVectorFormatV3(mem, path, QgsCoordinateTransformContext(), o)
    print(f"{driver:15s} -> {os.path.basename(path)}: {'OK' if err[0] == 0 else err}")


write("GPKG", os.path.join(DATA, "tricky.gpkg"), "tricky")
write("ESRI Shapefile", os.path.join(DATA, "tricky.shp"))
write("GeoJSON", os.path.join(DATA, "tricky.geojson"))
write("FlatGeobuf", os.path.join(DATA, "tricky.fgb"))
write("OpenFileGDB", os.path.join(DATA, "tricky.gdb"), "tricky")
write("SQLite", os.path.join(DATA, "tricky.sqlite"), "tricky", dsopts=["SPATIALITE=YES"])
write("XLSX", os.path.join(DATA, "tricky.xlsx"), "tricky")
# CSV for the "delimitedtext" (delimited text) provider
with open(os.path.join(DATA, "tricky.csv"), "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["ID", "TEXTO", "ENTERO", "REAL", "FECHA", "FECHAHORA", "BOOL", "X", "Y"])
    for r in rows:
        w.writerow([r["ID"], "" if r["TEXTO"] is None else r["TEXTO"],
                    "" if r["ENTERO"] is None else r["ENTERO"], "" if r["REAL"] is None else r["REAL"],
                    "" if r["FECHA"] is None else r["FECHA"].isoformat(),
                    "" if r["FECHAHORA"] is None else r["FECHAHORA"].isoformat(sep=" "),
                    "" if r["BOOL"] is None else ("true" if r["BOOL"] else "false"),
                    350000 + r["ID"] * 10, 7400000 + r["ID"] * 3])
print("CSV             -> tricky.csv: OK")
QgsProject.instance().addMapLayer(mem)
# also keep a copy of the source rows for reference (test_formats.py does not need it)
with open(os.path.join(DATA, "rows.json"), "w", encoding="utf-8") as fh:
    json.dump([{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()} for r in rows],
              fh, ensure_ascii=False, indent=0)
print("rows:", len(rows))
