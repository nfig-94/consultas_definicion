# -*- coding: utf-8 -*-
import os

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QCursor, QIcon
from qgis.PyQt.QtWidgets import QMenu

try:  # Qt6 / QGIS 4
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3
    from qgis.PyQt.QtWidgets import QAction
from qgis.core import Qgis, QgsApplication, QgsProject, QgsVectorLayer

from . import query_store as store
from . import sql_builder
from .name_prompt import ask_name, blank_icon, check_icon
from .processing_provider import DefinitionQueriesProvider
from .i18n import tr

PLUGIN_DIR = os.path.dirname(__file__)
MENU = tr("&Definition Queries")

try:
    VECTOR_TYPE = Qgis.LayerType.Vector
except AttributeError:
    from qgis.core import QgsMapLayerType
    VECTOR_TYPE = QgsMapLayerType.VectorLayer  # QGIS < 3.30


class DefinitionQueriesPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.dialog = None
        self.provider = None
        self.actions = []
        self.layer_action = None
        self._watched = {}  # layer id -> (layer, slot)
        self._outdated = set()
        self._fields_warned = set()

    # ------------------------------------------------------------ init / unload
    def initGui(self):
        icon = QIcon(os.path.join(PLUGIN_DIR, "icons", "icon.svg"))

        self.main_action = QAction(icon, tr("Definition Queries…"), self.iface.mainWindow())
        self.main_action.setToolTip(tr("Manage definition queries of the active layer"))
        self.main_action.triggered.connect(lambda: self.open_dialog())
        self.iface.addToolBarIcon(self.main_action)
        self.iface.addPluginToVectorMenu(MENU, self.main_action)
        self.actions.append(self.main_action)

        vis_action = QAction(QgsApplication.getThemeIcon("/mActionSelectAll.svg"),
                             tr("Select visible features"), self.iface.mainWindow())
        vis_action.setToolTip(tr("Select the features of the active layer that are visible in the map view"))
        vis_action.triggered.connect(lambda: self.select_visible())
        self.iface.addPluginToVectorMenu(MENU, vis_action)
        self.actions.append(vis_action)

        sel_action = QAction(QgsApplication.getThemeIcon("/mActionSelectRectangle.svg"),
                             tr("Filter layer by selection"), self.iface.mainWindow())
        sel_action.triggered.connect(self.filter_by_selection)
        self.iface.addPluginToVectorMenu(MENU, sel_action)
        self.actions.append(sel_action)

        # --- context menu (right-click on the layer in the Layers panel)
        self.layer_action = QAction(icon, tr("Definition Queries"), self.iface.mainWindow())
        self.layer_menu = QMenu(self.iface.mainWindow())
        self.layer_menu.aboutToShow.connect(self._populate_layer_menu)
        self.layer_action.setMenu(self.layer_menu)
        self.iface.addCustomActionForLayerType(self.layer_action, "", VECTOR_TYPE, True)

        # --- Processing
        self.provider = DefinitionQueriesProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

        # --- sync with filters set through QGIS's own «Filter…»
        prj = QgsProject.instance()
        prj.layersAdded.connect(self._watch_layers)
        prj.layersWillBeRemoved.connect(self._unwatch_ids)
        self._watch_layers(list(prj.mapLayers().values()))

    def unload(self):
        prj = QgsProject.instance()
        for sig, slot in ((prj.layersAdded, self._watch_layers),
                          (prj.layersWillBeRemoved, self._unwatch_ids)):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._unwatch_ids(list(self._watched))
        for a in self.actions:
            self.iface.removePluginVectorMenu(MENU, a)
            self.iface.removeToolBarIcon(a)
        if self.layer_action is not None:
            self.iface.removeCustomActionForLayerType(self.layer_action)
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None

    # ------------------------------------------------------------ sync
    def _watch_layers(self, layers):
        for layer in layers:
            if not isinstance(layer, QgsVectorLayer) or layer.id() in self._watched:
                continue
            slot = (lambda *_a, lyr=layer: store.sync_from_layer(lyr))
            try:
                layer.subsetStringChanged.connect(slot)
            except AttributeError:
                continue
            fields_slot = (lambda *_a, lyr=layer: self._fields_changed(lyr))
            layer.updatedFields.connect(fields_slot)
            self._watched[layer.id()] = (layer, slot, fields_slot)
            # filters the layer already had (e.g. when opening an old project):
            # register them without marking the project as modified
            store.sync_from_layer(layer, dirty=False)
            store.update_layer_note(layer)  # projects saved before layer notes existed
            q, status = store.active_status(layer)
            if store.broken_filter(layer):
                status = "broken"
            if status in ("pending", "broken"):
                self._outdated.add((status, layer.name()))
                QTimer.singleShot(1500, self._report_outdated)

    def _report_outdated(self):
        """Single warning when opening a project whose filters are out of date."""
        if not self._outdated:
            return
        pending = sorted(n for s, n in self._outdated if s == "pending")
        broken = sorted(n for s, n in self._outdated if s == "broken")
        self._outdated.clear()
        parts = []
        if broken:
            parts.append(tr("The active filter uses fields that no longer exist in: {} (the layer shows no features). "
                            "Fix it in Manage queries… → ⋯ → Replace a field in all queries.").format(", ".join(broken)))
        if pending:
            parts.append(tr("Active queries with changes not applied in: {} (the layer still has the previous filter).").format(", ".join(pending)))
            parts.append(tr("Right-click the layer → Definition Queries → pick the query marked with ✓ to apply it again."))
        self.iface.messageBar().pushMessage(
            tr("Definition Queries"), " ".join(parts), Qgis.MessageLevel.Warning, 20)

    def _unwatch_ids(self, ids):
        for lid in list(ids):
            if not isinstance(lid, str):
                lid = lid.id()
            layer, slot, fields_slot = self._watched.pop(lid, (None, None, None))
            if layer is None:
                continue
            for signal, handler in ((layer.subsetStringChanged, slot), (layer.updatedFields, fields_slot)):
                try:
                    signal.disconnect(handler)
                except (TypeError, RuntimeError):
                    pass

    def _fields_changed(self, layer):
        """A field was renamed or deleted while the project is open: if the active filter
        used it, the layer suddenly shows nothing. Say so once, right away."""
        problem = store.broken_filter(layer)
        key = (layer.id(), problem)
        if not problem or key in self._fields_warned:
            return
        self._fields_warned.add(key)
        self.iface.messageBar().pushMessage(
            tr("Definition Queries"),
            tr("«{}»: the active filter stopped working because {} (the layer shows no features). "
               "Fix it in Manage queries… → ⋯ → Replace a field in all queries.").format(layer.name(), problem),
            Qgis.MessageLevel.Warning, 20)

    # ------------------------------------------------------------ helpers
    def _layer(self):
        layer = self.iface.activeLayer()
        return layer if isinstance(layer, QgsVectorLayer) else None

    def _msg(self, ok, text):
        level = Qgis.MessageLevel.Success if ok else Qgis.MessageLevel.Warning
        self.iface.messageBar().pushMessage(tr("Definition Queries"), text, level, 5)

    def _after_change(self, layer):
        if self.dialog is not None and self.dialog.isVisible() and self.dialog.layer is layer:
            self.dialog._refresh_badge_and_list()

    def open_dialog(self, layer=None, new=False):
        from .dialog import QueryManagerDialog
        if self.dialog is None:
            self.dialog = QueryManagerDialog(self.iface)
        self.dialog.open_for(layer or self._layer(), new=new)

    # ------------------------------------------------------------ quick actions
    def activate(self, layer, qid):
        ok, msg = store.apply_filter(layer, qid) if qid else store.clear_filter(layer)
        if ok and qid and store.feature_count(layer) == 0:
            ok, msg = False, msg + " " + store.EMPTY_HINT
        self._msg(ok, msg)
        self._after_change(layer)

    def select(self, layer, qid):
        ok, msg = store.select_with_query(layer, qid, "set")
        self._msg(ok, msg)

    def select_visible(self, layer=None):
        layer = layer or self._layer()
        if layer is None:
            self._msg(False, tr("Choose a vector layer."))
            return
        ok, msg = store.select_visible(layer, self.iface.mapCanvas().mapSettings())
        self._msg(ok, msg)

    def filter_by_selection(self, layer=None):
        layer = layer or self._layer()
        if layer is None:
            self._msg(False, tr("Choose a vector layer."))
            return
        n = layer.selectedFeatureCount()
        if n == 0:
            self._msg(False, tr("The layer has no selected features."))
            return
        name = ask_name(self.iface.mainWindow(), store.default_name(store.load(layer)), QCursor.pos(),
                        hint=tr("Filter «{}» by the {} selected features").format(layer.name(), n),
                        ok_text=tr("Filter"))
        if name is None:
            return
        try:
            _q, ok, msg = store.query_from_selection(layer, name)
        except sql_builder.ClauseError as e:
            ok, msg = False, str(e)
        self._msg(ok, msg)
        self._after_change(layer)

    def _populate_layer_menu(self):
        m = self.layer_menu
        m.clear()
        layer = self._layer()
        if layer is None:
            m.addAction(tr("(choose a vector layer)")).setEnabled(False)
            return
        store.sync_from_layer(layer)
        data = store.load(layer)
        active, status = store.active_status(layer, data)
        subset = (layer.subsetString() or "").strip()

        label = active["name"] if active else (tr("manual (not saved)") if subset else tr("none"))
        if status == "equivalent":
            label += tr(" — written with another syntax (pick it to rewrite it)")
        elif status == "pending":
            label += tr(" — changes not applied (pick it to apply them)")
        title = m.addAction(tr("Filter: ") + label)
        title.setEnabled(False)
        m.addSeparator()

        # active query marked with a plain check mark (same on Windows, Mac and Linux)
        check, blank = check_icon(), blank_icon()
        a_all = m.addAction(blank if subset else check, tr("None — show all features"))
        a_all.setProperty("activa", not subset)
        a_all.triggered.connect(lambda _=False, lyr=layer: self.activate(lyr, None))
        for q in data["queries"]:
            is_active = active is not None and active["id"] == q["id"]
            a = m.addAction(check if is_active else blank, q["name"])
            a.setProperty("activa", is_active)
            try:
                a.setToolTip(store.query_sql(layer, q))
            except sql_builder.ClauseError:
                pass
            a.triggered.connect(lambda _=False, lyr=layer, i=q["id"]: self.activate(lyr, i))
        m.setToolTipsVisible(True)

        if data["queries"]:
            sm = m.addMenu(QgsApplication.getThemeIcon("/mIconExpressionSelect.svg"),
                           tr("Select features with…"))
            for q in data["queries"]:
                a = sm.addAction(q["name"])
                a.triggered.connect(lambda _=False, lyr=layer, i=q["id"]: self.select(lyr, i))

        m.addSeparator()
        a = m.addAction(QgsApplication.getThemeIcon("/mActionSelectAll.svg"), tr("Select visible features"))
        a.setToolTip(tr("Select what you see in the map view (inside the view, with the layer's filter "
                        "and without the categories turned off in the legend)."))
        a.triggered.connect(lambda _=False, lyr=layer: self.select_visible(lyr))
        n = layer.selectedFeatureCount()
        a = m.addAction(QgsApplication.getThemeIcon("/mActionSelectRectangle.svg"),
                        tr("Filter by selection ({} features)").format(n))
        a.setEnabled(n > 0)
        a.triggered.connect(lambda _=False, lyr=layer: self.filter_by_selection(lyr))
        m.addSeparator()
        m.addAction(QgsApplication.getThemeIcon("/symbologyAdd.svg"), tr("New query…"),
                    lambda _=False, lyr=layer: self.open_dialog(lyr, new=True))
        m.addAction(QgsApplication.getThemeIcon("/mActionOptions.svg"), tr("Manage queries…"),
                    lambda _=False, lyr=layer: self.open_dialog(lyr))
