# Tests

These scripts check the plugin's logic outside the QGIS interface. They are not part of the
plugin package (the ZIP you install does not include this folder).

Run them with the Python that comes with QGIS, from the plugin folder. The folder must be named
`definition_queries` (the name used when you clone the repository with
`git clone https://github.com/nfig-94/definition_queries`). On Linux without a display, add
`QT_QPA_PLATFORM=offscreen` before each command.

| Script | What it checks |
|---|---|
| `make_data.py` | Writes the test layers to `tests/data`: the same tricky values (accents, upper/lower case, `_` and `%` inside texts, quotes, `\`, leading/trailing spaces, empty and null values, dates, date-times, true/false) in GeoPackage, Shapefile, GeoJSON, FlatGeobuf, FileGDB, Excel, SpatiaLite and CSV. |
| `test_formats.py` | For every format and operator, compares the features kept by the plugin's filter, its count ("Verify"), its selection and the SQL → builder → SQL round trip against a result computed in plain Python. Then 150 random combinations with AND/OR, groups and subgroups. Besides the files above it tests a temporary layer, a virtual layer and, if configured, PostGIS. |
| `test_scenarios.py` | Situations a user may run into, with the real interface running off-screen: `<Null>` and `<Empty>` in the lists, changing the operator or field of a clause, AND/OR mixed without a group, values typed by hand, unusual texts, long lists in Shapefile, decimals and date-times picked from the list (milliseconds, time zones, «is on» a whole day), values that look alike, a missing data source, edit mode, reloading the plugin, the Temporal Controller, the Processing tools, damaged saved data. |
| `test_real_world.py` | Renamed fields, changed values, duplicated layers, large and scattered selections, "Select visible features" (layer filter, categories turned off, rotated map), opt-in layer notes, projects saved with older keys. |
| `test_code.py` | Every text shown in the interface has a Spanish and a Portuguese translation with the same `{}` placeholders, and the code parses as Python 3.9. Does not need QGIS. |

```
python tests/make_data.py
python tests/test_formats.py            # all formats; or e.g. GeoPackage Shapefile
python tests/test_scenarios.py
python tests/test_real_world.py
python tests/test_code.py
```

PostGIS is tested only when the environment variable `DQ_PG_URI` points to a table with the same
data (see the docstring of `test_formats.py`). Each script exits with status 1 if a check fails.

Last run (QGIS 3.34, GDAL 3.8, PostgreSQL 16), once with the computer's time zone set to UTC and
once to America/Santiago: all 11 formats passed, with 150/150 grouped combinations each.
Single-operator cases: 183 in GeoPackage, GeoJSON, FlatGeobuf, Excel, SpatiaLite and PostGIS;
182 in CSV and temporary layers (plus one case the plugin refuses on purpose: a lone `\` at the
end of a search text, which the QGIS expression engine cannot express); 178 in FileGDB and 156 in
Shapefile, which cannot store some of the data types; 150 in virtual layers, which expose dates
as text. `test_scenarios.py`, `test_real_world.py` and `test_code.py`: no failures.
