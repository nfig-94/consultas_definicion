# -*- coding: utf-8 -*-
"""Tools for the Processing Toolbox (runnable like the ArcGIS ones)."""

import os

from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    Qgis,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingOutputNumber,
    QgsProcessingParameterEnum,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorLayer,
    QgsProcessingProvider,
    QgsProject,
)

from . import query_store as store
from . import sql_builder
from .i18n import tr

ICON = os.path.join(os.path.dirname(__file__), "icons", "icon.svg")

try:
    NO_THREADING = Qgis.ProcessingAlgorithmFlag.NoThreading
except AttributeError:
    NO_THREADING = QgsProcessingAlgorithm.Flag.FlagNoThreading

try:  # QGIS >= 3.36 / QGIS 4
    VECTOR_ANY = Qgis.ProcessingSourceType.Vector
except AttributeError:  # QGIS 3.22 - 3.34
    VECTOR_ANY = QgsProcessing.SourceType.TypeVector

SEL_KEYS = ["set", "add", "remove", "intersect"]
SEL_LABELS = [tr("New selection"), tr("Add to selection"), tr("Remove from selection"),
              tr("Select within selection")]


def _saved_queries_help():
    lines = []
    for layer in QgsProject.instance().mapLayers().values():
        if not hasattr(layer, "subsetString"):
            continue
        qs = store.load(layer)["queries"]
        if qs:
            lines.append("<b>{}</b>: {}".format(layer.name(), ", ".join(q["name"] for q in qs)))
    if not lines:
        return tr("<p><i>There are no saved queries in the current project.</i></p>")
    return tr("<p><b>Queries saved in this project:</b><br>") + "<br>".join(lines) + "</p>"


class _Base(QgsProcessingAlgorithm):
    def group(self):
        return tr("Definition Queries")

    def groupId(self):
        return "queries"

    def icon(self):
        return QIcon(ICON)

    def flags(self):
        return super().flags() | NO_THREADING

    def createInstance(self):
        return self.__class__()

    def _layer(self, parameters, context):
        layer = self.parameterAsVectorLayer(parameters, "INPUT", context)
        if layer is None:
            raise QgsProcessingException(tr("Invalid layer."))
        return layer


class ApplySavedQuery(_Base):
    def name(self):
        return "applysavedquery"

    def displayName(self):
        return tr("Apply saved query")

    def shortHelpString(self):
        return (tr("<p>Activates a saved query of the layer, either as a <b>filter</b> (definition query: the layer only shows matching features) or as a <b>selection</b> (select by attributes).</p><p>The name is not case-sensitive.</p>") + _saved_queries_help())

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            "INPUT", tr("Layer"), [VECTOR_ANY]))
        self.addParameter(QgsProcessingParameterString("QUERY", tr("Query name")))
        self.addParameter(QgsProcessingParameterEnum(
            "ACTION", tr("Action"), [tr("Filter the layer"), tr("Select features")], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            "SELECTION", tr("Selection type"), SEL_LABELS, defaultValue=0))
        self.addOutput(QgsProcessingOutputNumber("COUNT", tr("Resulting features")))

    def processAlgorithm(self, parameters, context, feedback):
        layer = self._layer(parameters, context)
        name = self.parameterAsString(parameters, "QUERY", context)
        q = store.find(store.load(layer), name=name)
        if q is None:
            raise QgsProcessingException(tr("Layer «{}» has no query named «{}».").format(layer.name(), name))
        if self.parameterAsEnum(parameters, "ACTION", context) == 0:
            ok, msg = store.apply_filter(layer, q["id"])
            count = store.feature_count(layer)
        else:
            ok, msg = store.select_with_query(
                layer, q["id"], SEL_KEYS[self.parameterAsEnum(parameters, "SELECTION", context)])
            count = layer.selectedFeatureCount()
        if not ok:
            raise QgsProcessingException(msg)
        feedback.pushInfo(msg)
        return {"COUNT": count}


class ApplySql(_Base):
    def name(self):
        return "filterbyattributes"

    def displayName(self):
        return tr("Filter layer by attributes (SQL)")

    def shortHelpString(self):
        return (tr("<p>Applies a WHERE clause as the layer filter. Optionally saves it as a named query to reuse later.</p><p>Example: <code>\"LANDUSE\" = 'Forest' AND \"COVER\" &gt;= 25</code></p>"))

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            "INPUT", tr("Layer"), [VECTOR_ANY]))
        self.addParameter(QgsProcessingParameterString("WHERE", tr("WHERE clause (SQL)"), multiLine=True))
        self.addParameter(QgsProcessingParameterString(
            "NAME", tr("Save with name (optional)"), optional=True))
        self.addOutput(QgsProcessingOutputNumber("COUNT", tr("Resulting features")))

    def processAlgorithm(self, parameters, context, feedback):
        layer = self._layer(parameters, context)
        sql = self.parameterAsString(parameters, "WHERE", context).strip()
        name = self.parameterAsString(parameters, "NAME", context).strip()
        data = store.load(layer)
        if name:
            q = store.find(data, name=name)
            if q is None:
                q = store.new_query(name, mode="sql", sql=sql)
                data["queries"].append(q)
            else:
                q.update({"mode": "sql", "sql": sql, "expression": ""})
            store.save(layer, data)
            ok, msg = store.apply_filter(layer, q["id"])
        else:
            if layer.isEditable():
                raise QgsProcessingException(tr("The layer is in edit mode."))
            ok = layer.setSubsetString(sql)
            msg = tr("Filter applied: {} features.").format(store.feature_count(layer)) if ok else \
                tr("QGIS rejected the filter. Check the syntax.")
            if ok:
                data["active"] = None
                store.save(layer, data)
        if not ok:
            raise QgsProcessingException(msg)
        feedback.pushInfo(msg)
        return {"COUNT": store.feature_count(layer)}


class FilterBySelection(_Base):
    def name(self):
        return "filterbyselection"

    def displayName(self):
        return tr("Filter layer by selection")

    def shortHelpString(self):
        return (tr("<p>Creates a query that shows only the selected features and activates it as a filter (like «Make Layer From Selected Features», but on the same layer).</p>"))

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            "INPUT", tr("Layer"), [VECTOR_ANY]))
        self.addParameter(QgsProcessingParameterString(
            "NAME", tr("Query name (optional)"), optional=True))
        self.addOutput(QgsProcessingOutputNumber("COUNT", tr("Resulting features")))

    def processAlgorithm(self, parameters, context, feedback):
        layer = self._layer(parameters, context)
        name = self.parameterAsString(parameters, "NAME", context).strip() or None
        try:
            _q, ok, msg = store.query_from_selection(layer, name)
        except sql_builder.ClauseError as e:
            raise QgsProcessingException(str(e))
        if not ok:
            raise QgsProcessingException(msg)
        feedback.pushInfo(msg)
        return {"COUNT": store.feature_count(layer)}


class ClearFilter(_Base):
    def name(self):
        return "removefilter"

    def displayName(self):
        return tr("Remove filter (show all)")

    def shortHelpString(self):
        return tr("<p>Removes the layer filter. Saved queries are kept.</p>")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            "INPUT", tr("Layer"), [VECTOR_ANY]))
        self.addOutput(QgsProcessingOutputNumber("COUNT", tr("Resulting features")))

    def processAlgorithm(self, parameters, context, feedback):
        layer = self._layer(parameters, context)
        ok, msg = store.clear_filter(layer)
        if not ok:
            raise QgsProcessingException(msg)
        feedback.pushInfo(msg)
        return {"COUNT": store.feature_count(layer)}


class DefinitionQueriesProvider(QgsProcessingProvider):
    def loadAlgorithms(self):
        for alg in (ApplySavedQuery(), ApplySql(), FilterBySelection(), ClearFilter()):
            self.addAlgorithm(alg)

    def id(self):
        return "definitionqueries"

    def name(self):
        return tr("Definition Queries")

    def icon(self):
        return QIcon(ICON)
