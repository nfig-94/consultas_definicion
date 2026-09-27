# -*- coding: utf-8 -*-
"""
Storage and execution of definition queries.

Queries are stored as a custom property of each layer, so they travel
inside the project (.qgz / .qgs) with no extra files.

Stored structure (JSON):
    {"version": 1, "active": "<id>" | null,
     "queries": [{"id": str, "name": str, "mode": "builder"|"sql",
                  "clauses": [...], "sql": str, "expression": str}]}
"""

import collections
import datetime
import html
import json
import re
import uuid
import zlib

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsCsException,
    QgsDataSourceUri,
    QgsExpressionContextUtils,
    QgsGeometry,
    QgsLayerNotesUtils,
    QgsPointXY,
    QgsExpression,
    QgsFeatureRequest,
    QgsProject,
    QgsProviderRegistry,
    QgsRenderContext,
    QgsVectorLayer,
    QgsVectorLayerTemporalContext,
)

from . import lint
from . import sql_builder
from . import sql_parser
from .i18n import tr

PROP_KEY = "definition_queries/data"
OLD_PROP_KEYS = ("consultas_definicion/data",)  # projects saved before the plugin was renamed


# ---------------------------------------------------------------- compat
def _selection_behavior(name):
    """name: 'set' | 'add' | 'remove' | 'intersect'"""
    try:
        enum = Qgis.SelectBehavior
        return {"set": enum.SetSelection, "add": enum.AddToSelection,
                "remove": enum.RemoveFromSelection, "intersect": enum.IntersectSelection}[name]
    except AttributeError:
        enum = QgsVectorLayer.SelectBehavior
        return {"set": enum.SetSelection, "add": enum.AddToSelection,
                "remove": enum.RemoveFromSelection, "intersect": enum.IntersectSelection}[name]


def _no_geometry_flag():
    try:
        return Qgis.FeatureRequestFlag.NoGeometry
    except AttributeError:
        return QgsFeatureRequest.Flag.NoGeometry


def feature_count(layer):
    """Number of features (with the current filter). QGIS sometimes returns -1 ("unknown"),
    e.g. with several layers from the same GeoPackage: then they are counted one by one."""
    n = layer.featureCount()
    if n is None or n < 0:
        req = QgsFeatureRequest().setNoAttributes().setFlags(_no_geometry_flag())
        n = sum(1 for _ in layer.getFeatures(req))
    return n


# ---------------------------------------------------------------- read / write
def empty_data():
    return {"version": 1, "active": None, "queries": []}


def _text(v):
    if v is None or isinstance(v, (dict, list)):
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return v if isinstance(v, str) else str(v)


def _clean_clause(c):
    """A clause with the expected types (projects edited by hand, JSON made elsewhere…)."""
    if not isinstance(c, dict):
        return None
    out = dict(c)
    out["connector"] = "OR" if str(c.get("connector", "AND")).upper() == "OR" else "AND"
    out["field"] = _text(c.get("field"))
    op = c.get("op")
    out["op"] = op if isinstance(op, str) and op in sql_builder.OP_BY_KEY else "eq"
    out["value"] = _text(c.get("value"))
    out["value2"] = _text(c.get("value2"))
    values = c.get("values")
    out["values"] = [_text(v) for v in values] if isinstance(values, list) else []
    groups = sql_builder.clause_path(c) if isinstance(c.get("groups", []), list) else []
    out["groups"] = [g for g in groups if isinstance(g, int) and not isinstance(g, bool)]
    out.pop("group", None)
    return out


def _stable_id(*parts):
    """Same id on every load for a query that came without one (so it can be found again)."""
    return "{:08x}".format(zlib.crc32(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")))


def _clean_query(q, position=0):
    if not isinstance(q, dict):
        return None
    out = dict(q)
    out["id"] = _text(q.get("id"))
    out["name"] = _text(q.get("name")).strip() or tr("Query")
    out["mode"] = "sql" if q.get("mode") == "sql" else "builder"
    clauses = q.get("clauses")
    out["clauses"] = [c for c in map(_clean_clause, clauses if isinstance(clauses, list) else []) if c]
    out["sql"] = _text(q.get("sql"))
    out["expression"] = _text(q.get("expression"))
    return out


def clean_data(data):
    """Saved data with the expected structure; anything unusable is dropped."""
    if not isinstance(data, dict):
        return empty_data()
    out = dict(data)
    queries = data.get("queries")
    out["queries"] = []
    items = [(pos, _clean_query(raw, pos), raw) for pos, raw in enumerate(queries if isinstance(queries, list) else [])]
    given = {q["id"] for _pos, q, _raw in items if q is not None and q["id"]}
    seen = set()
    for position, q, raw in items:
        if q is None:
            continue
        if not q["id"] or q["id"] in seen:
            # missing or repeated id: a new one, the same on every load, taken by nobody else
            n = 0
            while True:
                candidate = _stable_id(raw, position, n)
                if candidate not in given and candidate not in seen:
                    break
                n += 1
            q["id"] = candidate
        seen.add(q["id"])
        out["queries"].append(q)
    active = data.get("active")
    out["active"] = active if isinstance(active, str) and active in seen else None
    return out


def load(layer):
    if layer is None:
        return empty_data()
    raw = layer.customProperty(PROP_KEY, "")
    if not raw:
        for key in OLD_PROP_KEYS:
            raw = layer.customProperty(key, "")
            if raw:
                break
    if not raw:
        return empty_data()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return empty_data()
    return clean_data(data)


def save(layer, data, dirty=True):
    layer.setCustomProperty(PROP_KEY, json.dumps(data, ensure_ascii=False))
    for key in OLD_PROP_KEYS:
        layer.removeCustomProperty(key)
    update_layer_note(layer, data)
    if dirty:
        QgsProject.instance().setDirty(True)


def new_query(name=tr("New query"), mode="builder", clauses=None, sql="", expression=""):
    return {"id": uuid.uuid4().hex[:12], "name": name, "mode": mode,
            "clauses": clauses or [], "sql": sql, "expression": expression}


def unique_name(data, base):
    names = {q["name"] for q in data["queries"]}
    if base not in names:
        return base
    i = 2
    while "{} ({})".format(base, i) in names:
        i += 1
    return "{} ({})".format(base, i)


def default_name(data, base=tr("Query")):
    """First free «Query N», to suggest as a name."""
    names = {q["name"].strip().lower() for q in data["queries"]}
    n = 1
    while "{} {}".format(base, n).lower() in names:
        n += 1
    return "{} {}".format(base, n)


def find(data, qid=None, name=None):
    for q in data["queries"]:
        if qid is not None and q["id"] == qid:
            return q
        if name is not None and q["name"].strip().lower() == name.strip().lower():
            return q
    return None


# ---------------------------------------------------------------- SQL of a query
def query_sql(layer, q):
    """SQL for setSubsetString()."""
    if q.get("mode") == "sql":
        return (q.get("sql") or "").strip()
    return sql_builder.build_sql(q.get("clauses", []), layer, "provider")


def query_expression(layer, q):
    """QGIS expression for selecting, or None if there is no valid one."""
    if q.get("mode") == "sql":
        expr_txt = (q.get("expression") or q.get("sql") or "").strip()
    else:
        expr_txt = sql_builder.build_sql(q.get("clauses", []), layer, "expression")
    if not expr_txt:
        return None
    e = QgsExpression(expr_txt)
    if e.hasParserError():
        return None
    return expr_txt


# ---------------------------------------------------------------- unfiltered layer
def unfiltered_clone(layer):
    """Unfiltered copy of the layer (to count and list values over all the data).
    Returns None for memory layers, which cannot be cloned from their source."""
    if layer is None or layer.providerType() in ("memory",):
        return None
    try:
        clone = QgsVectorLayer(layer.source(), "tmp_definition_queries", layer.providerType())
    except Exception:
        return None
    if not clone.isValid():
        return None
    clone.setSubsetString("")
    return clone


VALUES_LIMIT = 5000


def _exact_unique(src, idx, limit):
    """Distinct values read feature by feature. For decimal fields GDAL's own list (GeoJSON,
    FlatGeobuf…) rounds them to 15 digits (0.3333333333333333 becomes 0.333333333333333),
    and a value picked from such a list would match nothing."""
    req = QgsFeatureRequest().setFlags(_no_geometry_flag()).setSubsetOfAttributes([idx])
    seen, out = set(), []
    for f in src.getFeatures(req):
        v = f.attribute(idx)
        key = None if sql_builder.is_null(v) else v
        if key not in seen:
            seen.add(key)
            out.append(v)
            if len(out) >= limit:
                break
    return out


def unique_values(layer, field_name, clone=None, limit=VALUES_LIMIT):
    src = clone or layer
    idx = src.fields().indexOf(field_name)
    if idx < 0:
        return []
    if sql_builder._type_id(src.fields().at(idx)) == 6 and sql_builder.dialect_of(src) == "ogrsql":
        vals = _exact_unique(src, idx, limit)
    else:
        vals = src.uniqueValues(idx, limit)
    out = set()
    has_null = False
    has_empty = False
    for v in vals:
        t = sql_builder.value_to_text(v)
        if t is None:
            has_null = True
        elif t == "":
            has_empty = True   # shown as <Empty> instead of a blank row
        else:
            out.add(t)
    if not has_null and len(vals) >= limit:  # truncated list: ask the data directly
        req = QgsFeatureRequest().setFilterExpression(
            "{} IS NULL".format(QgsExpression.quotedColumnRef(field_name)))
        req.setFlags(_no_geometry_flag()).setNoAttributes().setLimit(1)
        has_null = any(True for _ in src.getFeatures(req))
    kind = sql_builder.field_kind(src.fields().at(idx))
    if kind == "num":
        def key(s):
            try:
                return (0, float(s))
            except ValueError:
                return (1, s)
        ordered = sorted(out, key=key)
    else:
        ordered = sorted(out, key=lambda s: s.lower())
    return ([sql_builder.NULL_VALUE] if has_null else []) + ([sql_builder.EMPTY_VALUE] if has_empty else []) + ordered


def geometry_column(layer):
    """Name of the geometry column in the data source ('' if it has none or it is unknown)."""
    try:
        name = QgsDataSourceUri(layer.source()).geometryColumn()
        if name or layer.providerType() != "ogr":
            return name or ""
        reg = QgsProviderRegistry.instance()
        parts = reg.decodeUri("ogr", layer.source())
        wanted = parts.get("layerName") or ""
        for d in reg.querySublayers(parts.get("path") or layer.source()):
            if d.providerKey() == "ogr" and (not wanted or d.name() == wanted):
                return d.geometryColumnName() or ""
    except Exception:  # an unreadable source: nothing to add
        return ""
    return ""


def _missing_fields(layer, names):
    """(missing, foreign) for the names a filter uses: see sql_builder.field_problems. The
    geometry column is not a missing field («"geom" IS NOT NULL» is a valid filter)."""
    missing, foreign = sql_builder.field_problems(layer, names)
    if missing:
        geom = geometry_column(layer).lower()
        missing = [m for m in missing if not geom or m.lower() != geom]
    return missing, foreign


def check_fields(layer, sql):
    """Error message if the SQL uses fields that don't exist or are not from the data
    source (joined or virtual): the provider doesn't know them and, in SQLite, an unknown
    quoted field is taken as text and the filter fails SILENTLY."""
    missing, foreign = _missing_fields(layer, sql_parser.referenced_fields(sql, layer))
    if missing:
        return tr("The layer does not have the field(s): {}.").format(", ".join(missing))
    if foreign:
        names = ", ".join("«{}»".format(f) for f in foreign)
        if len(foreign) == 1:
            return tr("{} is a joined or virtual field: layer filters can only use fields of the data source itself.").format(names)
        return tr("{} are joined or virtual fields: layer filters can only use fields of the data source itself.").format(names)
    return None


EMPTY_HINT = tr("No feature matches it: check the values, they may have changed in the data.")


def broken_filter(layer):
    """If the layer's current filter uses fields that no longer exist, the provider
    returns no features without saying why. Returns an explanation, or None."""
    try:
        if not layer.isValid():  # data source not available: QGIS already reports it
            return None
        subset = (layer.subsetString() or "").strip()
    except RuntimeError:
        return None
    if not subset:
        return None
    missing, _foreign = _missing_fields(layer, sql_parser.referenced_fields(subset, layer))
    if missing:
        return tr("the field(s) {} no longer exist").format(", ".join("«{}»".format(m) for m in missing))
    return None


def missing_query_fields(layer, data=None):
    """Fields used by the saved queries that the layer no longer has (sorted)."""
    data = data or load(layer)
    names = set(layer.fields().names())
    in_sql, in_clauses = set(), set()
    for q in data["queries"]:
        if q.get("mode") == "sql":
            in_sql.update(sql_parser.referenced_fields(q.get("sql") or "", layer))
        else:
            in_clauses.update(c.get("field", "") for c in q.get("clauses", []) if c.get("field"))
    missing = {f for f in in_clauses if f not in names}
    if in_sql:
        missing.update(_missing_fields(layer, sorted(in_sql))[0])
    return sorted(missing)


def replace_field(layer, old, new):
    """Replaces a field name in every saved query of the layer (for example after the
    field was renamed). Re-applies the active query if it used that field.
    Returns (number of queries changed, message)."""
    data = load(layer)
    quoted_old = '"{}"'.format(old.replace('"', '""'))
    quoted_new = '"{}"'.format(new.replace('"', '""'))
    changed_ids = []
    for q in data["queries"]:
        hit = False
        for c in q.get("clauses", []):
            if c.get("field") == old:
                c["field"] = new
                hit = True
        for key in ("sql", "expression", "applied"):
            txt = q.get(key) or ""
            if quoted_old in txt:
                q[key] = txt.replace(quoted_old, quoted_new)
                hit = True
        if hit:
            changed_ids.append(q["id"])
    if not changed_ids:
        return 0, tr("No saved query uses «{}».").format(old)
    save(layer, data)
    n = len(changed_ids)
    msg = (tr("«{}» replaced by «{}» in 1 query.").format(old, new) if n == 1 else
           tr("«{}» replaced by «{}» in {} queries.").format(old, new, n))
    if data.get("active") in changed_ids and not layer.isEditable():
        ok, amsg = apply_filter(layer, data["active"])
        msg += " " + amsg
    return len(changed_ids), msg


def count_matching(layer, sql, clone=None):
    """(n_matching, n_total, error). Runs the provider SQL on an unfiltered copy."""
    err = check_fields(layer, sql)
    if err:
        return None, feature_count(layer), err
    clone = clone or unfiltered_clone(layer)
    if clone is not None:
        clone.setSubsetString("")
        total = feature_count(clone)
        if not clone.setSubsetString(sql):
            clone.setSubsetString("")
            return None, total, tr("The data provider rejected the query (check the syntax).")
        n = feature_count(clone)
        clone.setSubsetString("")
        return n, total, None
    # Memory layers: the filter is a QGIS expression -> evaluate it on the layer.
    e = QgsExpression(sql)
    if e.hasParserError():
        return None, feature_count(layer), e.parserErrorString()
    req = QgsFeatureRequest(e).setFlags(_no_geometry_flag())
    n = sum(1 for _ in layer.getFeatures(req))
    return n, feature_count(layer), None


# ---------------------------------------------------------------- actions
def active_status(layer, data=None):
    """(active_query, status). Status:
    * "ok":      the layer filter is exactly the SQL the query generates now.
    * "equivalent": the same query written with another syntax (e.g. by hand in
                 Filter…): it may filter slightly differently.
    * "pending": the query changed since it was applied (edited or mode switched): the
                 layer still has the previous filter.
    In the last two cases it should be applied again."""
    data = data or load(layer)
    q = find(data, qid=data.get("active"))
    if q is None:
        return None, None
    subset = (layer.subsetString() or "").strip()
    try:
        current = query_sql(layer, q)
    except sql_builder.ClauseError:
        current = None
    if current == subset:
        return q, "ok"
    if not subset:
        return None, None
    if q.get("applied") == subset:
        return q, "pending"
    if _same_logic(layer, q, subset):   # same query written by hand with another syntax
        return q, "equivalent"
    return None, None


def active_state(layer, data=None):
    """(active_query, outdated): see active_status."""
    q, status = active_status(layer, data)
    return q, status in ("equivalent", "pending")


def active_query(layer, data=None):
    """Active query, if the layer's current filter matches it."""
    return active_state(layer, data)[0]


def _same_logic(layer, q, subset):
    """Is the SQL «subset» the same query as q, written with another equivalent syntax?"""
    if q.get("mode") == "sql":
        return False
    clauses = sql_parser.sql_to_clauses(subset, layer)
    if not clauses:
        return False
    try:
        return sql_builder.build_sql(clauses, layer, "provider") == query_sql(layer, q)
    except sql_builder.ClauseError:
        return False


def apply_filter(layer, qid):
    """Activate the query as a filter (definition query). Returns (ok, message)."""
    data = load(layer)
    q = find(data, qid=qid)
    if q is None:
        return False, tr("Query not found.")
    try:
        sql = query_sql(layer, q)
    except sql_builder.ClauseError as e:
        return False, str(e)
    if layer.isEditable():
        return False, tr("The layer is in edit mode: save or discard changes before filtering.")
    err = check_fields(layer, sql)
    if err:
        return False, err
    if not layer.setSubsetString(sql):
        return False, tr("QGIS rejected the filter. Check the query:\n") + sql
    data["active"] = q["id"]
    q["applied"] = sql
    save(layer, data)
    layer.triggerRepaint()
    return True, tr("Filter «{}» applied: {} features visible.").format(q["name"], feature_count(layer))


def clear_filter(layer):
    if layer.isEditable():
        return False, tr("The layer is in edit mode: save or discard changes before removing the filter.")
    data = load(layer)
    layer.setSubsetString("")
    data["active"] = None
    save(layer, data)
    layer.triggerRepaint()
    return True, tr("Filter removed: showing all {} features.").format(feature_count(layer))


def _stable_ids(layer):
    """Do feature ids in a copy of the layer match those of the layer?"""
    prov = layer.providerType()
    if prov in ("ogr", "spatialite", "delimitedtext"):
        return True
    if prov == "postgres":
        pk = layer.primaryKeyAttributes()
        if len(pk) == 1:
            return sql_builder.field_kind(layer.fields().at(pk[0])) == "num"
    return False


def _by_content(layer):
    """Virtual layers without a key renumber their features when filtered, so rows are
    matched by content instead. A filter only reads the row's values (and geometry), so
    rows with identical content always get the same answer."""
    return layer.providerType() == "virtual" and len(layer.primaryKeyAttributes()) != 1


def _row_key(f, names):
    values = tuple(None if sql_builder.is_null(v) else (type(v).__name__, str(v))
                   for v in (f.attribute(n) for n in names))
    g = f.geometry()
    return values, (bytes(g.asWkb()) if g is not None and not g.isNull() else b"")


def _source_names(clone):
    return [fld.name() for fld in clone.fields()]


def matching_ids(layer, sql, clone=None):
    """Ids of the features matching the SQL, evaluated by the SAME engine as the filter
    (so the selection always matches what the filter would show).
    Returns (ids, error); ids is None if it can't be evaluated this way."""
    if not sql:
        return {f.id() for f in layer.getFeatures(QgsFeatureRequest().setNoAttributes()
                                                  .setFlags(_no_geometry_flag()))}, None
    if layer.providerType() == "memory":
        e = QgsExpression(sql)
        if e.hasParserError():
            return None, e.parserErrorString()
        req = QgsFeatureRequest(e).setFlags(_no_geometry_flag())
        return {f.id() for f in layer.getFeatures(req)}, None
    clone = clone or unfiltered_clone(layer)
    if clone is None:
        return None, None
    if not clone.setSubsetString(sql):
        clone.setSubsetString("")
        return None, tr("The data provider rejected the query (check the syntax).")
    req = QgsFeatureRequest().setFlags(_no_geometry_flag())
    if _stable_ids(layer):
        req.setNoAttributes()
        ids = {f.id() for f in clone.getFeatures(req)}
    elif _by_content(layer):
        names = _source_names(clone)
        keys = {_row_key(f, names) for f in clone.getFeatures(QgsFeatureRequest())}
        ids = {f.id() for f in layer.getFeatures(QgsFeatureRequest()) if _row_key(f, names) in keys}
    else:
        pk = layer.primaryKeyAttributes()
        if len(pk) != 1:
            clone.setSubsetString("")
            return None, None
        keys = {f.attribute(pk[0]) for f in clone.getFeatures(req)}
        ids = {f.id() for f in layer.getFeatures(QgsFeatureRequest().setFlags(_no_geometry_flag()))
               if f.attribute(pk[0]) in keys}
    clone.setSubsetString("")
    return ids, None


COMPARE_LIMIT = 300000


class _TooMany(Exception):
    pass


def compare_results(layer, sql_a, sql_b, clone=None, limit=COMPARE_LIMIT):
    """Do both SQLs keep exactly the same features (using the layer's engine)?
    Returns (equal, n_a, n_b), or None if it can't be told (provider error or
    more than «limit» features, to avoid a long wait)."""
    flag = _no_geometry_flag()

    def collect(features, key):
        out = collections.Counter()
        for i, f in enumerate(features):
            if i >= limit:
                raise _TooMany()
            out[key(f)] += 1
        return out

    try:
        if layer.providerType() == "memory":
            def keys(sql):
                e = QgsExpression(sql)
                if e.hasParserError():
                    return None
                return collect(layer.getFeatures(QgsFeatureRequest(e).setFlags(flag)), lambda f: f.id())
            a, b = keys(sql_a), keys(sql_b)
        else:
            clone = clone or unfiltered_clone(layer)
            if clone is None:
                return None
            # ids are stable within the same copy; on PostgreSQL compare by primary key
            pk = [] if layer.providerType() in ("ogr", "spatialite", "delimitedtext") \
                else list(layer.primaryKeyAttributes())

            by_content = _by_content(layer)
            names = _source_names(clone)

            def keys(sql):
                if not clone.setSubsetString(sql):
                    return None
                if by_content:
                    return collect(clone.getFeatures(QgsFeatureRequest()), lambda f: _row_key(f, names))
                req = QgsFeatureRequest().setFlags(flag)
                if pk:
                    req.setSubsetOfAttributes(pk)
                    return collect(clone.getFeatures(req), lambda f: tuple(f.attribute(i) for i in pk))
                req.setNoAttributes()
                return collect(clone.getFeatures(req), lambda f: f.id())
            try:
                a, b = keys(sql_a), keys(sql_b)
            finally:
                clone.setSubsetString("")
    except _TooMany:
        return None
    if a is None or b is None:
        return None
    return a == b, sum(a.values()), sum(b.values())


def select_with_query(layer, qid, behavior="set"):
    """Select (among the visible features) those matching the query."""
    data = load(layer)
    q = find(data, qid=qid)
    if q is None:
        return False, tr("Query not found.")
    try:
        sql = query_sql(layer, q)
    except sql_builder.ClauseError as e:
        return False, str(e)
    err = check_fields(layer, sql)
    if err:
        return False, err
    ids, err = matching_ids(layer, sql)
    if err:
        return False, err
    if ids is None:
        # last resort (providers without stable ids): QGIS expression
        try:
            expr = query_expression(layer, q)
        except sql_builder.ClauseError as e:
            return False, str(e)
        if expr is None:
            return False, tr("Could not evaluate the query to select.")
        layer.selectByExpression(expr, _selection_behavior(behavior))
        return True, tr("«{}»: {} features selected.").format(q["name"], layer.selectedFeatureCount())
    if (layer.subsetString() or "").strip():
        visible = {f.id() for f in layer.getFeatures(QgsFeatureRequest().setNoAttributes()
                                                     .setFlags(_no_geometry_flag()))}
        ids &= visible
    current = set(layer.selectedFeatureIds())
    result = {"set": ids, "add": current | ids, "remove": current - ids,
              "intersect": current & ids}[behavior]
    layer.selectByIds(sorted(result), _selection_behavior("set"))
    return True, tr("«{}»: {} features selected.").format(q["name"], layer.selectedFeatureCount())


def visible_ids(layer, map_settings):
    """Ids of the features visible on the map: inside the current view (also with a rotated
    map), with the layer's active filter and scale range, and without the categories turned
    off in the legend. Returns (ids, error_message)."""
    if layer is None or not layer.isSpatial():
        return None, tr("The layer has no geometry: nothing of it is drawn on the map.")
    node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
    if node is not None and not node.isVisible():
        return None, tr("«{}» is turned off in the Layers panel.").format(layer.name())
    scale = map_settings.scale()
    if layer.hasScaleBasedVisibility() and not layer.isInScaleRange(scale):
        return None, tr("«{}» is not drawn at the current scale.").format(layer.name())
    pts = [QgsPointXY(pt.x(), pt.y()) for pt in map_settings.visiblePolygon()]
    if len(pts) < 3:
        return None, tr("The map view could not be converted to the layer's coordinate system.")
    if pts[0] != pts[-1]:
        pts.append(pts[0])  # closed polygon (otherwise QGIS treats it as a line)
    view = QgsGeometry.fromPolygonXY([pts])
    if map_settings.destinationCrs() != layer.crs():
        ct = QgsCoordinateTransform(map_settings.destinationCrs(), layer.crs(), map_settings.transformContext())
        view = view.densifyByCount(50)  # straight screen edges become curved in another CRS
        try:
            view.transform(ct)
        except QgsCsException:
            try:
                view = QgsGeometry.fromRect(ct.transformBoundingBox(map_settings.visibleExtent()))
            except QgsCsException:
                return None, tr("The map view could not be converted to the layer's coordinate system.")
    ctx = QgsRenderContext.fromMapSettings(map_settings)
    ctx.expressionContext().appendScope(QgsExpressionContextUtils.layerScope(layer))
    renderer = layer.renderer().clone() if layer.renderer() is not None else None
    request = QgsFeatureRequest().setFilterRect(view.boundingBox())
    temporal = layer.temporalProperties()
    if map_settings.isTemporal() and temporal is not None and temporal.isActive():
        # the Temporal Controller only draws the features of the current time
        if not temporal.isVisibleInTemporalRange(map_settings.temporalRange()):
            return None, tr("«{}» is not drawn at the current time (Temporal Controller).").format(layer.name())
        tctx = QgsVectorLayerTemporalContext()
        tctx.setLayer(layer)
        time_filter = temporal.createFilterString(tctx, map_settings.temporalRange())
        if time_filter:
            request.combineFilterExpression(time_filter)
    engine = QgsGeometry.createGeometryEngine(view.constGet())
    engine.prepareGeometry()
    ids = []
    if renderer is not None:
        renderer.startRender(ctx, layer.fields())
    try:
        for f in layer.getFeatures(request):
            g = f.geometry()
            if g is None or g.isNull() or g.isEmpty() or not engine.intersects(g.constGet()):
                continue
            if renderer is not None:
                ctx.expressionContext().setFeature(f)
                if not renderer.willRenderFeature(f, ctx):
                    continue
            ids.append(f.id())
    finally:
        if renderer is not None:
            renderer.stopRender(ctx)
    return ids, None


def select_visible(layer, map_settings):
    """Select what is visible on the map (replaces the selection). Returns (ok, message)."""
    ids, err = visible_ids(layer, map_settings)
    if err:
        return False, err
    layer.selectByIds(sorted(ids), _selection_behavior("set"))
    if not ids:
        return False, tr("No feature of «{}» is visible in the current map view.").format(layer.name())
    return True, tr("«{}»: {} visible features selected.").format(layer.name(), len(ids))


# formats whose FID doesn't change on edit (a real primary key)
_STABLE_FID_STORAGE = {"GPKG", "SQLITE", "OPENFILEGDB", "FILEGDB", "POSTGRESQL", "MSSQLSPATIAL", "OCI"}
_ID_NAME = re.compile(r"^(id|fid|objectid|gid|cod|codigo|código|code|parcela|punto|id_.+|.+_id|cod_.+|.+_cod)$", re.I)


def _unique_field(layer, clone=None):
    """A field of the layer itself with a distinct, non-null value for every feature (e.g. ID)."""
    src = clone or unfiltered_clone(layer) or layer
    total = feature_count(src)
    if total <= 0:
        return None
    cands = []
    for idx, fld in enumerate(src.fields()):
        if not sql_builder.is_provider_field(src, idx):
            continue
        kind = sql_builder.field_kind(fld)
        if kind not in ("num", "text"):
            continue
        score = (0 if _ID_NAME.match(fld.name()) else 1, 0 if kind == "num" else 1, idx)
        cands.append((score, idx, fld))
    for _score, idx, fld in sorted(cands, key=lambda c: c[0]):
        vals = src.uniqueValues(idx)
        if len(vals) != total or any(sql_builder.is_null(v) for v in vals):
            continue
        return fld.name()
    return None


LARGE_SELECTION = 5000   # above this many listed features the filter gets slow to draw
_MAX_RANGE_CLAUSES = 40  # more ranges than this are kept as SQL instead of builder clauses


def _runs(ints):
    """Sorted integers -> list of (first, last) runs of consecutive values."""
    runs = []
    for v in ints:
        if runs and v == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], v)
        else:
            runs.append((v, v))
    return runs


def _split_runs(ints):
    """(singles, ranges): runs of 3 or more consecutive ids become ranges."""
    singles, ranges = [], []
    for a, b in _runs(sorted(set(ints))):
        if b - a >= 2:
            ranges.append((a, b))
        else:
            singles.extend(range(a, b + 1))
    return singles, ranges


def _large_warning(n_listed):
    if n_listed <= LARGE_SELECTION:
        return None
    return tr("The filter lists {} features one by one: with so many, the map may be slow to draw. "
              "If you can, filter by an attribute instead (for example a zone or category field).").format(n_listed)


def _join_warnings(*parts):
    parts = [p for p in parts if p]
    return "\n".join(parts) if parts else None


def selection_to_query(layer):
    """What to save to reproduce the current selection. Returns a dict:
    {"clauses": [...]} (shown in the builder) or {"sql": ..., "expression": ...},
    plus "warning" when the identifier may change or the filter will be slow."""
    request = QgsFeatureRequest().setFlags(_no_geometry_flag())
    feats = list(layer.getSelectedFeatures(request))
    if not feats:
        raise sql_builder.ClauseError(tr("The layer has no selected features."))
    prov = layer.providerType()
    try:
        storage = (layer.storageType() or "").upper()
    except Exception:
        storage = ""
    fids = sorted(f.id() for f in feats)

    def by_field(name):
        idx = layer.fields().lookupField(name)
        fld = layer.fields().at(idx)
        raw = [f.attribute(idx) for f in feats]
        if sql_builder.field_kind(fld) == "num" and all(isinstance(v, int) and not isinstance(v, bool) for v in raw):
            singles, ranges = _split_runs(raw)
        else:
            singles, ranges = raw, []
        clauses = []
        if singles:
            clauses.append({"field": fld.name(), "op": "in", "connector": "OR", "groups": [],
                            "values": [sql_builder.value_to_text(v) for v in singles]})
        for a, b in ranges:
            clauses.append({"field": fld.name(), "op": "between", "value": str(a), "value2": str(b),
                            "connector": "OR", "groups": []})
        warning = _large_warning(len(singles))
        if clauses and clauses[0]["op"] == "in" and sql_builder.field_kind(fld) == "text" \
                and sql_builder.dialect_of(layer) == "ogrsql" \
                and sum(map(sql_builder.has_ascii_letters, clauses[0]["values"])) > sql_builder.MAX_EXACT_LIST:
            src = unfiltered_clone(layer) or layer
            others = [sql_builder.value_to_text(v) for v in src.uniqueValues(src.fields().lookupField(fld.name()))]
            clash = lint.case_clash(layer, "in", clauses[0]["values"], others)
            if clash:
                warning = _join_warnings(warning, tr(
                    "With more than {} values, this format does not tell upper and lower case apart: "
                    "«{}» also counts as chosen.").format(sql_builder.MAX_EXACT_LIST, clash))
        if len(ranges) > _MAX_RANGE_CLAUSES:  # too many clauses for the builder: keep it as SQL
            return {"sql": sql_builder.build_sql(clauses, layer, "provider"),
                    "expression": sql_builder.build_sql(clauses, layer, "expression"), "warning": warning}
        return {"clauses": clauses, "warning": warning}

    pk = [i for i in layer.primaryKeyAttributes() if 0 <= i < layer.fields().count()]
    stable = prov in ("postgres", "spatialite") or (prov == "ogr" and storage in _STABLE_FID_STORAGE)
    if stable and len(pk) == 1:
        return by_field(layer.fields().at(pk[0]).name())
    uf = _unique_field(layer)
    if uf:
        return by_field(uf)
    unstable = tr("Careful: the layer has no unique ID field, so the filter uses each feature's internal number, which may change if features are deleted or the file is edited. Recommended: add an ID field.")
    singles, ranges = _split_runs(fids)

    def id_sql(ref):
        parts = []
        if singles:
            parts.append("{} IN ({})".format(ref, ", ".join(str(i) for i in singles)))
        parts += ["({0} >= {1} AND {0} <= {2})".format(ref, a, b) for a, b in ranges]
        return " OR ".join(parts)

    warning = _join_warnings(unstable, _large_warning(len(singles)))
    if prov in ("memory", "delimitedtext"):
        expr = id_sql("$id")
        return {"sql": expr, "expression": expr, "warning": warning}
    if prov == "ogr":
        return {"sql": id_sql("FID"), "expression": id_sql("$id"), "warning": warning}
    raise sql_builder.ClauseError(
        tr("This layer has no unique identifier; create the query in SQL mode using an identifier field."))


def selection_to_sql(layer):
    """Compatibility: (provider_sql, expression) that reproduce the current selection."""
    r = selection_to_query(layer)
    if "clauses" in r:
        sql = sql_builder.build_sql(r["clauses"], layer, "provider")
        return sql, sql_builder.build_sql(r["clauses"], layer, "expression")
    return r["sql"], r["expression"]


def query_from_selection(layer, name=None, activate=True):
    if layer.isEditable():
        if activate:
            return None, False, tr("The layer is in edit mode: save or discard changes before filtering.")
        if any(fid < 0 for fid in layer.selectedFeatureIds()):
            return None, False, tr("Some selected features are new and not saved yet: their IDs are temporary. Save the layer first.")
    r = selection_to_query(layer)
    data = load(layer)
    qname = unique_name(data, name or default_name(data))
    if "clauses" in r:
        q = new_query(qname, mode="builder", clauses=r["clauses"])
        q["sql"] = sql_builder.build_sql(r["clauses"], layer, "provider")
    else:
        q = new_query(qname, mode="sql", sql=r["sql"], expression=r["expression"])
    data["queries"].append(q)
    save(layer, data)
    warning = r.get("warning")
    if activate:
        ok, msg = apply_filter(layer, q["id"])
        if not ok:
            return q, False, msg
        layer.removeSelection()
        return q, True, msg + ("\n" + warning if warning else "")
    return q, True, tr("Query «{}» created.").format(q["name"]) + ("\n" + warning if warning else "")


# ---------------------------------------------------------------- import / export
def export_json(layer, path):
    data = load(layer)
    payload = {"plugin": "definition_queries", "layer": layer.name(), "queries": data["queries"]}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return len(data["queries"])


def import_json(layer, path):
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    incoming = payload.get("queries", []) if isinstance(payload, dict) else payload
    data = load(layer)
    n = 0
    for q in incoming if isinstance(incoming, list) else []:
        if not isinstance(q, dict) or "name" not in q:
            continue
        q = _clean_query(q)
        nq = new_query(unique_name(data, q["name"]), q["mode"], q["clauses"], q["sql"], q["expression"])
        data["queries"].append(nq)
        n += 1
    save(layer, data)
    return n


# ---------------------------------------------------------------- sync with QGIS
IMPORTED_NAME = tr("QGIS filter")


def fill_from_sql(layer, q, sql):
    """Load SQL into the query; when possible, also as builder clauses."""
    sql = (sql or "").strip()
    clauses = sql_parser.sql_to_clauses(sql, layer)
    q["sql"] = sql
    q["expression"] = ""
    q["clauses"] = clauses or []
    exact = False
    if clauses:
        try:
            exact = sql_builder.build_sql(clauses, layer, "provider") == sql
        except sql_builder.ClauseError:
            exact = False
    # If the builder reproduces the SQL exactly, show it in the builder;
    # otherwise keep the exact original SQL (the builder is one click away).
    q["mode"] = "builder" if exact else "sql"
    return q


def sync_from_layer(layer, dirty=True):
    """Mirror the layer's filter in the saved queries, even if it was set with QGIS's
    «Filter…», another plugin, or comes from an old project.

    * Filter equal to a saved query -> that query becomes active.
    * Filter different from all -> saved as «QGIS filter» (the same entry is reused
      as long as it isn't renamed or edited in the plugin).
    * No filter -> none active.
    Returns True if anything changed."""
    if not isinstance(layer, QgsVectorLayer):
        return False
    try:
        if not layer.isValid():  # data source not available: leave everything as it is
            return False
        subset = (layer.subsetString() or "").strip()
    except RuntimeError:  # layer already deleted
        return False
    data = load(layer)
    changed = False
    if not subset:
        if data.get("active"):
            data["active"] = None
            changed = True
    else:
        active = find(data, qid=data.get("active"))
        cands = ([active] if active else []) + data["queries"]
        match = None
        for q in cands:                       # 1) same SQL the query generates now
            try:
                if query_sql(layer, q) == subset:
                    match = q
                    break
            except sql_builder.ClauseError:
                continue
        if match is None:                     # 2) the SQL that was applied (previous version)
            match = next((q for q in cands if q.get("applied") == subset), None)
        if match is None:                     # 3) same logic written with another syntax
            match = next((q for q in cands if _same_logic(layer, q, subset)), None)
        if match is not None:
            if data.get("active") != match["id"]:
                data["active"] = match["id"]
                changed = True
        else:
            target = next((q for q in data["queries"] if q.get("origin") == "qgis"), None)
            if target is None:
                target = new_query(unique_name(data, IMPORTED_NAME))
                target["origin"] = "qgis"
                data["queries"].append(target)
            fill_from_sql(layer, target, subset)
            data["active"] = target["id"]
            changed = True
    if changed:
        save(layer, data, dirty)
    return changed


# ---------------------------------------------------------------- for users without the plugin
PLUGIN_NAME = "Definition Queries"
NOTE_BEGIN = "<!-- definition_queries:begin -->"
NOTE_END = "<!-- definition_queries:end -->"
_OLD_NOTE = ("<!-- consultas_definicion:inicio -->", "<!-- consultas_definicion:fin -->")
_NOTE_RE = re.compile("(?:" + re.escape(NOTE_BEGIN) + "|" + re.escape(_OLD_NOTE[0]) + ").*?"
                      "(?:" + re.escape(NOTE_END) + "|" + re.escape(_OLD_NOTE[1]) + ")", re.S)


def notes_enabled():
    """Layer notes are opt-in: off unless the user turns them on for this project."""
    return QgsProject.instance().readBoolEntry("definition_queries", "layer_notes", False)[0]


def set_notes_enabled(flag):
    QgsProject.instance().writeEntry("definition_queries", "layer_notes", bool(flag))
    for layer in QgsProject.instance().mapLayers().values():
        if isinstance(layer, QgsVectorLayer):
            update_layer_note(layer)


def _safe_sql(layer, q):
    try:
        return query_sql(layer, q) or tr("(empty: shows all features)")
    except sql_builder.ClauseError as e:
        return tr("(incomplete: {})").format(e)


def _note_block(layer, data):
    active = active_query(layer, data)
    items = []
    for q in data["queries"]:
        tag = tr(" <i>(active filter)</i>") if active is not None and active["id"] == q["id"] else ""
        sql = _safe_sql(layer, q)
        if len(sql) > 1000:  # e.g. a «filter by selection» with thousands of features
            sql = sql[:1000] + tr(" … (SQL too long: ask for the file exported as text)")
        items.append("<li><b>{}</b>{}<br><code>{}</code></li>".format(
            html.escape(q["name"]), tag, html.escape(sql)))
    n = len(data["queries"])
    return (
        NOTE_BEGIN
        + tr("<h3>Definition Queries</h3>")
        + (tr("<p>This layer has <b>{}</b> query saved with the <b>«{}»</b> plugin.</p>") if n == 1 else
           tr("<p>This layer has <b>{}</b> queries saved with the <b>«{}»</b> plugin.</p>")).format(n, PLUGIN_NAME)
        + tr("<p><b>To use them from a menu</b>, install the plugin: <i>Plugins → Manage and Install Plugins</i> → search «{}» (or <i>Install from ZIP</i> if you were sent the file).</p>").format(PLUGIN_NAME)
        + tr("<p><b>Without the plugin</b> you can use any of them by copying its SQL into <i>right-click the layer → Filter…</i></p>")
        + "<ul>" + "".join(items) + "</ul>"
        + NOTE_END
    )


def update_layer_note(layer, data=None):
    """Keep a notice with the saved queries and their SQL in the layer notes (icon in the
    Layers panel), visible even if QGIS doesn't have the plugin. Leaves the rest of the
    user's own notes untouched."""
    if not isinstance(layer, QgsVectorLayer):
        return
    try:
        current = QgsLayerNotesUtils.layerNotes(layer) or ""
    except RuntimeError:
        return
    data = data or load(layer)
    own = _NOTE_RE.sub("", current).strip()
    own = re.sub(r"(\s*<br\s*/?>\s*)+$", "", own).strip()  # separator left by the previous block
    block = _note_block(layer, data) if (notes_enabled() and data["queries"]) else ""
    new = (own + ("<br>" if own and block else "") + block).strip()
    if new == current.strip():
        return
    if new:
        QgsLayerNotesUtils.setLayerNotes(layer, new)
    else:
        QgsLayerNotesUtils.removeNotes(layer)


def queries_as_text(layers):
    """Plain «name → SQL» text of the queries of one or more layers."""
    prj = QgsProject.instance()
    title = prj.title() or prj.baseName() or tr("unnamed project")
    lines = [
        tr("DEFINITION QUERIES — {}").format(title),
        tr("Exported on {} with the «{}» plugin.").format(
            datetime.date.today().strftime("%d-%m-%Y"), PLUGIN_NAME),
        "",
        tr("To use a query without the plugin: right-click the layer → Filter…,"),
        tr("paste the SQL and accept. (✓ = active filter when exported)"),
        "",
    ]
    total = 0
    for layer in layers:
        data = load(layer)
        if not data["queries"]:
            continue
        active = active_query(layer, data)
        head = tr("Layer: {}").format(layer.name())
        lines += [head, "-" * max(len(head), 40)]
        for q in data["queries"]:
            mark = "✓" if active is not None and active["id"] == q["id"] else " "
            lines.append("{} {}".format(mark, q["name"]))
            lines.append("    {}".format(_safe_sql(layer, q)))
            lines.append("")
            total += 1
        lines.append("")
    if total == 0:
        lines.append(tr("(No saved queries.)"))
    return "\n".join(lines).rstrip() + "\n", total


def export_text(layers, path):
    text, total = queries_as_text(layers)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return total
