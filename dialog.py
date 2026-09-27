# -*- coding: utf-8 -*-
"""Main window: definition query manager (ArcGIS Pro style)."""

import copy

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QCursor, QFontDatabase
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qgis.core import Qgis, QgsApplication, QgsMapLayerProxyModel, QgsProject
from qgis.gui import QgsMapLayerComboBox

from . import query_store as store
from . import sql_builder
from . import sql_parser
from .clause_widget import ClauseWidget
from .lint import lint
from .name_prompt import ask_name, below, blank_icon, check_icon
from .i18n import tr

try:  # QGIS >= 3.34
    VECTOR_FILTER = Qgis.LayerFilter.VectorLayer
except AttributeError:
    VECTOR_FILTER = QgsMapLayerProxyModel.Filter.VectorLayer


def _icon(name):
    return QgsApplication.getThemeIcon(name)


STYLE = """
QFrame#clauseRow { background: palette(base); border: 1px solid palette(mid); border-radius: 4px; }
QFrame#clauseGroup0 { background: rgba(30, 136, 229, 0.07); border: 2px solid #1e88e5; border-radius: 7px; }
QFrame#clauseGroup1 { background: rgba(67, 160, 71, 0.08); border: 2px solid #43a047; border-radius: 7px; }
QFrame#clauseGroup2 { background: rgba(251, 140, 0, 0.08); border: 2px solid #fb8c00; border-radius: 7px; }
QFrame#clauseGroup3 { background: rgba(142, 36, 170, 0.07); border: 2px solid #8e24aa; border-radius: 7px; }
QLabel#groupTitle0 { color: #1565c0; font-weight: 600; }
QLabel#groupTitle1 { color: #2e7d32; font-weight: 600; }
QLabel#groupTitle2 { color: #e65100; font-weight: 600; }
QLabel#groupTitle3 { color: #6a1b9a; font-weight: 600; }
QLabel#sectionTitle { font-weight: 600; font-size: 11pt; }
QLabel#activeBadge { border-radius: 9px; padding: 2px 10px; background: #2e7d32; color: white; font-weight: 600; }
QLabel#manualBadge { border-radius: 9px; padding: 2px 10px; background: #ef6c00; color: white; font-weight: 600; }
QLabel#noneBadge { border-radius: 9px; padding: 2px 10px; background: palette(mid); color: palette(text); }
QToolButton#segment { padding: 4px 14px; border: 1px solid palette(mid); }
QToolButton#segment:checked { background: palette(highlight); color: palette(highlighted-text); }
QPushButton#primary { font-weight: 600; padding: 5px 14px; }
QLabel#verifyOk { color: #2e7d32; }
QLabel#verifyErr { color: #c62828; }
QListWidget#queryList::item { padding: 6px 4px; }
"""


class QueryManagerDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.layer = None
        self.data = store.empty_data()
        self.current_id = None
        self._loading = False
        self._sql_on_enter = None   # SQL shown when switching from the builder to SQL mode
        self._converted = None      # (typed SQL, builder SQL) from the last conversion
        self._values_cache = {}
        self._clone = None
        self._clone_ready = False
        self.clauses = []

        self.setWindowTitle(tr("Definition Queries"))
        self.setStyleSheet(STYLE)
        self._check_icon = check_icon()
        self._blank_icon = blank_icon()
        self.resize(1100, 680)
        # maximize/minimize buttons in the title bar, plus a resize grip
        self.setWindowFlags(self.windowFlags()
                            | Qt.WindowType.WindowMaximizeButtonHint
                            | Qt.WindowType.WindowMinimizeButtonHint)
        self.setSizeGripEnabled(True)
        self._build_ui()

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(250)
        self._save_timer.timeout.connect(lambda: self._save_current())

        self.layer_combo.layerChanged.connect(self.set_layer)
        self.set_layer(self.layer_combo.currentLayer())

    # ================================================================ UI
    def _build_ui(self):
        root = QVBoxLayout(self)

        # --- header
        head = QHBoxLayout()
        head.addWidget(QLabel(tr("<b>Layer:</b>")))
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(VECTOR_FILTER)
        self.layer_combo.setMinimumWidth(260)
        head.addWidget(self.layer_combo)
        head.addStretch()
        self.badge = QLabel()
        head.addWidget(self.badge)
        self.b_max = QToolButton()
        self.b_max.setText("⤢")
        self.b_max.setToolTip(tr("Maximize / restore the window"))
        self.b_max.setAutoRaise(True)
        f = self.b_max.font()
        f.setPointSizeF(f.pointSizeF() * 1.3)
        self.b_max.setFont(f)
        self.b_max.clicked.connect(self.toggle_maximized)
        head.addWidget(self.b_max)
        root.addLayout(head)

        split = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(split, 1)

        # --- left panel: query list
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 6, 0)
        t = QLabel(tr("Saved queries"))
        t.setObjectName("sectionTitle")
        lv.addWidget(t)
        self.list = QListWidget()
        self.list.setObjectName("queryList")
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                  | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.currentItemChanged.connect(self._on_current_changed)
        self.list.itemChanged.connect(self._on_item_renamed)
        self.list.model().rowsMoved.connect(self._on_rows_moved)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_menu)
        lv.addWidget(self.list, 1)

        hint = QLabel(tr("Double-click to rename · drag to reorder"))
        hint.setStyleSheet("color: gray; font-size: 8pt;")
        lv.addWidget(hint)

        btns = QHBoxLayout()
        self.b_new = QPushButton(_icon("/symbologyAdd.svg"), tr("New"))
        self.b_new.clicked.connect(self.new_query)
        self.b_dup = QToolButton()
        self.b_dup.setIcon(_icon("/mActionEditCopy.svg"))
        self.b_dup.setToolTip(tr("Duplicate query"))
        self.b_dup.clicked.connect(self.duplicate_query)
        self.b_del = QToolButton()
        self.b_del.setIcon(_icon("/symbologyRemove.svg"))
        self.b_del.setToolTip(tr("Delete query"))
        self.b_del.clicked.connect(self.delete_query)
        self.b_more = QToolButton()
        self.b_more.setText("⋯")
        self.b_more.setToolTip(tr("More options"))
        self.b_more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more = QMenu(self)
        more.addAction(_icon("/mActionSelectRectangle.svg"),
                       tr("Create query from current selection"), self.from_selection)
        more.addSeparator()
        more.addAction(_icon("/mActionFileSave.svg"), tr("Export as text: this layer…"),
                       lambda: self.export_text(all_layers=False))
        more.addAction(_icon("/mActionFileSave.svg"), tr("Export as text: whole project…"),
                       lambda: self.export_text(all_layers=True))
        more.addSeparator()
        more.addAction(_icon("/mActionFileSave.svg"), tr("Export queries to JSON…"), self.export_queries)
        more.addAction(_icon("/mActionFileOpen.svg"), tr("Import queries from JSON…"), self.import_queries)
        more.addSeparator()
        more.addAction(_icon("/mActionEditTable.svg"), tr("Replace a field in all queries…"), self.replace_field)
        more.addSeparator()
        self.act_notes = more.addAction(tr("Leave a note on layers for people without the plugin"))
        self.act_notes.setCheckable(True)
        self.act_notes.setToolTip(tr("Off by default. When on, each layer with queries gets a note (icon in the Layers panel) with the SQL of each query, so people without the plugin can use them."))
        self.act_notes.toggled.connect(self._toggle_notes)
        more.aboutToShow.connect(lambda: self._sync_notes_action())
        self.b_more.setMenu(more)
        btns.addWidget(self.b_new)
        btns.addWidget(self.b_dup)
        btns.addWidget(self.b_del)
        btns.addStretch()
        btns.addWidget(self.b_more)
        lv.addLayout(btns)

        self.b_vis = QPushButton(_icon("/mActionSelectAll.svg"), tr("Select visible"))
        self.b_vis.setToolTip(tr("Select what you see in the map view (inside the view, with the layer's filter "
                                 "and without the categories turned off in the legend)."))
        self.b_vis.clicked.connect(self.select_visible)
        self.b_from_sel = QPushButton(_icon("/mActionSelectRectangle.svg"), tr("Filter by selection"))
        self.b_from_sel.setToolTip(tr("Creates a query from the selected features and activates it"))
        self.b_from_sel.clicked.connect(self.from_selection)
        sel_row = QHBoxLayout()
        sel_row.setSpacing(4)
        sel_row.addWidget(self.b_vis)
        sel_row.addWidget(self.b_from_sel)
        lv.addLayout(sel_row)
        split.addWidget(left)

        # --- right panel: editor
        self.right_stack = QStackedWidget()
        split.addWidget(self.right_stack)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        split.setSizes([260, 740])

        # empty page
        empty = QWidget()
        ev = QVBoxLayout(empty)
        ev.addStretch()
        el = QLabel(tr("This layer has no queries yet.\n\nCreate one to filter or select features and save it with a name."))
        el.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.setStyleSheet("color: gray; font-size: 11pt;")
        ev.addWidget(el)
        b = QPushButton(_icon("/symbologyAdd.svg"), tr("  New definition query"))
        b.setObjectName("primary")
        b.clicked.connect(self.new_query)
        bl = QHBoxLayout()
        bl.addStretch()
        bl.addWidget(b)
        bl.addStretch()
        ev.addLayout(bl)
        ev.addStretch()
        self.right_stack.addWidget(empty)

        # editor page
        editor = QWidget()
        rv = QVBoxLayout(editor)
        rv.setContentsMargins(6, 0, 0, 0)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel(tr("Name:")))
        self.name_edit = QLineEdit()
        f = self.name_edit.font()
        f.setPointSizeF(f.pointSizeF() * 1.15)
        self.name_edit.setFont(f)
        self.name_edit.setPlaceholderText(tr("E.g.: Native forest, Plots 2025…"))
        self.name_edit.textEdited.connect(self._on_name_edited)
        name_row.addWidget(self.name_edit, 1)

        self.seg_builder = QToolButton()
        self.seg_builder.setObjectName("segment")
        self.seg_builder.setText(tr("Builder"))
        self.seg_builder.setCheckable(True)
        self.seg_sql = QToolButton()
        self.seg_sql.setObjectName("segment")
        self.seg_sql.setText("SQL")
        self.seg_sql.setCheckable(True)
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        grp.addButton(self.seg_builder)
        grp.addButton(self.seg_sql)
        self.seg_builder.clicked.connect(lambda: self._switch_mode("builder"))
        self.seg_sql.clicked.connect(lambda: self._switch_mode("sql"))
        name_row.addSpacing(12)
        name_row.addWidget(self.seg_builder)
        name_row.addWidget(self.seg_sql)
        rv.addLayout(name_row)

        self.mode_stack = QStackedWidget()
        rv.addWidget(self.mode_stack, 1)

        # ---- builder
        bpage = QWidget()
        bv = QVBoxLayout(bpage)
        bv.setContentsMargins(0, 4, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        holder = QWidget()
        self.clause_holder = holder
        self.group_frames = []
        self.clause_layout = QVBoxLayout(holder)
        self.clause_layout.setContentsMargins(0, 0, 0, 0)
        self.clause_layout.setSpacing(4)
        self.clause_layout.addStretch()
        self.scroll.setWidget(holder)
        bv.addWidget(self.scroll, 1)

        add_row = QHBoxLayout()
        self.b_add_clause = QPushButton(_icon("/symbologyAdd.svg"), tr("Add clause"))
        self.b_add_clause.clicked.connect(lambda: self.add_clause())
        add_row.addWidget(self.b_add_clause)
        add_row.addSpacing(12)
        self.b_group = QPushButton(tr("( )  Group"))
        self.b_group.setToolTip(tr("Tick two or more consecutive clauses and group them:\nthey work like parentheses, e.g.  (A or B) and C.\nTicking clauses inside a group creates a subgroup."))
        self.b_group.clicked.connect(self.group_checked)
        add_row.addWidget(self.b_group)
        self.b_ungroup = QPushButton(tr("Ungroup"))
        self.b_ungroup.setToolTip(tr("Removes the innermost group of the ticked clauses"))
        self.b_ungroup.clicked.connect(self.ungroup_checked)
        add_row.addWidget(self.b_ungroup)
        add_row.addStretch()
        bv.addLayout(add_row)

        bv.addWidget(QLabel(tr("SQL preview:")))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(64)
        self.preview.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        bv.addWidget(self.preview)
        self.lint_lbl = QLabel()
        self.lint_lbl.setWordWrap(True)
        self.lint_lbl.setStyleSheet("color: #b45309;")
        self.lint_lbl.hide()
        bv.addWidget(self.lint_lbl)
        self.mode_stack.addWidget(bpage)

        # ---- SQL
        spage = QWidget()
        sg = QGridLayout(spage)
        sg.setContentsMargins(0, 4, 0, 0)
        sg.addWidget(QLabel(tr("Fields (double-click to insert)")), 0, 0)
        sg.addWidget(QLabel(tr("Values")), 0, 1)
        self.sql_fields = QListWidget()
        self.sql_fields.itemDoubleClicked.connect(
            lambda it: self._insert_sql(sql_builder.quote_ident(it.text())))
        self.sql_fields.currentItemChanged.connect(lambda *_: self.sql_values.clear())
        self.sql_values = QListWidget()
        self.sql_values.itemDoubleClicked.connect(self._insert_value)
        sg.addWidget(self.sql_fields, 1, 0)
        sg.addWidget(self.sql_values, 1, 1)
        vb = QPushButton(tr("Show field values"))
        vb.clicked.connect(self._load_sql_values)
        sg.addWidget(vb, 2, 1)

        ops = QHBoxLayout()
        for tok in ("=", "<>", ">", "<", ">=", "<=", "LIKE", "IN ()", "AND", "OR", "NOT",
                    "IS NULL", "IS NOT NULL", "%", "( )"):
            ob = QToolButton()
            ob.setText(tok)
            ob.clicked.connect(lambda _=False, t=tok: self._insert_sql(
                {"IN ()": " IN ()", "( )": "()"}.get(t, " {} ".format(t))))
            ops.addWidget(ob)
        ops.addStretch()
        sg.addLayout(ops, 3, 0, 1, 2)

        self.sql_edit = QPlainTextEdit()
        self.sql_edit.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.sql_edit.setPlaceholderText(tr("E.g.:  \"LANDUSE\" = 'Forest' AND \"COVER\" >= 25"))
        self.sql_edit.textChanged.connect(self._schedule_save)
        sg.addWidget(self.sql_edit, 4, 0, 1, 2)
        sg.setRowStretch(1, 2)
        sg.setRowStretch(4, 1)
        self.mode_stack.addWidget(spage)

        # ---- verify
        ver = QHBoxLayout()
        self.b_verify = QPushButton(tr("✔  Verify"))
        self.b_verify.setToolTip(tr("Validates the query and counts how many features match"))
        self.b_verify.clicked.connect(self.verify)
        ver.addWidget(self.b_verify)
        self.verify_lbl = QLabel("")
        self.verify_lbl.setWordWrap(True)
        ver.addWidget(self.verify_lbl, 1)
        rv.addLayout(ver)
        self.right_stack.addWidget(editor)

        # --- bottom bar
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(line)
        bottom = QHBoxLayout()
        self.b_clear = QPushButton(_icon("/mActionFilter2.svg"), tr("Show all (remove filter)"))
        self.b_clear.clicked.connect(self.clear_filter)
        bottom.addWidget(self.b_clear)
        bottom.addStretch()

        self.b_select = QToolButton()
        self.b_select.setText(tr("Select"))
        self.b_select.setIcon(_icon("/mIconExpressionSelect.svg"))
        self.b_select.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.b_select.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.b_select.setToolTip(tr("Selects the features that match the query (without hiding the rest)"))
        self.b_select.clicked.connect(lambda: self.select("set"))
        sm = QMenu(self)
        sm.addAction(tr("New selection"), lambda: self.select("set"))
        sm.addAction(tr("Add to current selection"), lambda: self.select("add"))
        sm.addAction(tr("Remove from current selection"), lambda: self.select("remove"))
        sm.addAction(tr("Select within current selection"), lambda: self.select("intersect"))
        self.b_select.setMenu(sm)
        bottom.addWidget(self.b_select)

        self.b_apply = QPushButton(_icon("/mActionFilter2.svg"), tr("Apply filter"))
        self.b_apply.setObjectName("primary")
        self.b_apply.setToolTip(tr("Activates this query as a filter: the layer will only show matching features"))
        self.b_apply.clicked.connect(self.apply_filter)
        bottom.addWidget(self.b_apply)

        b_close = QPushButton(tr("Close"))
        b_close.clicked.connect(self.close)
        bottom.addWidget(b_close)
        root.addLayout(bottom)

    def toggle_maximized(self):
        if self.isMaximized():
            self.showNormal()
            self.b_max.setText("⤢")
        else:
            self.showMaximized()
            self.b_max.setText("⤡")

    # ================================================================ layer
    def set_layer(self, layer):
        self._save_now()
        if self.layer is not None:
            try:
                self.layer.subsetStringChanged.disconnect(self._refresh_badge_and_list)
            except (TypeError, RuntimeError, AttributeError):
                pass
        self.layer = layer
        if self.layer_combo.currentLayer() is not layer and layer is not None:
            self.layer_combo.blockSignals(True)
            self.layer_combo.setLayer(layer)
            self.layer_combo.blockSignals(False)
        self._values_cache = {}
        self._clone = None
        self._clone_ready = False
        self.current_id = None
        if layer is not None:
            try:
                layer.subsetStringChanged.connect(self._refresh_badge_and_list)
            except AttributeError:
                pass
            store.sync_from_layer(layer)
            self.data = store.load(layer)
            self.setWindowTitle(tr("Definition Queries — {}").format(layer.name()))
        else:
            self.data = store.empty_data()
        self._fill_sql_fields()
        self._reload_list()
        for w in (self.b_new, self.b_clear, self.b_from_sel, self.b_vis):
            w.setEnabled(layer is not None)

    def _get_clone(self):
        if not self._clone_ready:
            self._clone = store.unfiltered_clone(self.layer)
            self._clone_ready = True
        return self._clone

    def _values_complete(self, field_name):
        """Is the value list for this field complete (not cut off at the limit)?"""
        return len(self.values_for(field_name)) < store.VALUES_LIMIT

    def values_for(self, field_name):
        if field_name not in self._values_cache:
            QgsApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
            try:
                self._values_cache[field_name] = store.unique_values(
                    self.layer, field_name, self._get_clone())
            finally:
                QgsApplication.restoreOverrideCursor()
        return self._values_cache[field_name]

    # ================================================================ list
    def _reload_list(self, select_id=None):
        self._loading = True
        self.list.clear()
        active = store.active_query(self.layer, self.data) if self.layer else None
        for q in self.data["queries"]:
            it = QListWidgetItem(q["name"])
            it.setData(Qt.ItemDataRole.UserRole, q["id"])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsEditable)
            it.setIcon(self._blank_icon)
            if active is not None and active["id"] == q["id"]:
                it.setIcon(self._check_icon)
                font = it.font()
                font.setBold(True)
                it.setFont(font)
                it.setToolTip(tr("Active query (filter applied)"))
            self.list.addItem(it)
        self._loading = False
        target = select_id or self.current_id or (active["id"] if active else None)
        row = 0
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == target:
                row = i
        if self.list.count():
            self.list.setCurrentRow(row)
            self._on_current_changed(self.list.currentItem(), None)
        else:
            self.current_id = None
            self.right_stack.setCurrentIndex(0)
        self._update_buttons()
        self._refresh_badge()

    def _refresh_badge_and_list(self, *_):
        if self._loading:
            return
        self._save_now()
        store.sync_from_layer(self.layer)
        self.data = store.load(self.layer)
        self._reload_list(self.current_id)

    def _refresh_badge(self):
        if self.layer is None:
            self.badge.setText("")
            return
        active, status = store.active_status(self.layer, self.data)
        subset = (self.layer.subsetString() or "").strip()
        self.badge.setToolTip("")
        if active and status == "equivalent":
            self.badge.setObjectName("manualBadge")
            self.badge.setText(tr("«{}»: written with another syntax").format(active["name"]))
            self.badge.setToolTip(tr("The layer filter matches this query but was written with another syntax (for example by hand in Filter…), so it may filter slightly differently:\n{}\n\nClick «Apply filter» to rewrite it.").format(subset))
        elif active and status == "pending":
            self.badge.setObjectName("manualBadge")
            self.badge.setText(tr("«{}»: changes not applied").format(active["name"]))
            self.badge.setToolTip(tr("The layer still has the filter from before your last changes to this query:\n{}\n\nClick «Apply filter» to update it.").format(subset))
        elif active:
            self.badge.setObjectName("activeBadge")
            self.badge.setText(tr("Active filter: «{}»").format(active["name"]))
        elif subset:
            self.badge.setObjectName("manualBadge")
            self.badge.setText(tr("Manual filter (not saved)"))
            self.badge.setToolTip(subset)
        else:
            self.badge.setObjectName("noneBadge")
            self.badge.setText(tr("No filter"))
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)

    def _update_buttons(self):
        has = self.current_query() is not None
        for w in (self.b_dup, self.b_del, self.b_apply, self.b_select, self.b_verify):
            w.setEnabled(has)

    def current_query(self):
        return store.find(self.data, qid=self.current_id) if self.current_id else None

    def _on_current_changed(self, cur, _prev):
        if self._loading:
            return
        self._save_now()
        if cur is None:
            self.current_id = None
            self.right_stack.setCurrentIndex(0)
            self._update_buttons()
            return
        self.current_id = cur.data(Qt.ItemDataRole.UserRole)
        self._load_editor(self.current_query())
        self.right_stack.setCurrentIndex(1)
        self._update_buttons()

    def _on_item_renamed(self, item):
        if self._loading:
            return
        q = store.find(self.data, qid=item.data(Qt.ItemDataRole.UserRole))
        if q is None:
            return
        new = item.text().strip() or q["name"]
        others = [o for o in self.data["queries"] if o is not q]
        if any(o["name"].strip().lower() == new.lower() for o in others):
            new = store.unique_name({"queries": others}, new)   # no duplicate names
            self._loading = True
            item.setText(new)
            self._loading = False
        q["name"] = new
        q.pop("origin", None)
        if q["id"] == self.current_id:
            self.name_edit.setText(new)
        store.save(self.layer, self.data)
        self._refresh_badge()

    def _on_rows_moved(self, *_):
        order = [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]
        by_id = {q["id"]: q for q in self.data["queries"]}
        self.data["queries"] = [by_id[i] for i in order if i in by_id]
        store.save(self.layer, self.data)

    def _list_menu(self, pos):
        it = self.list.itemAt(pos)
        m = QMenu(self)
        if it is not None:
            m.addAction(_icon("/mActionFilter2.svg"), tr("Apply as filter"), self.apply_filter)
            m.addAction(_icon("/mIconExpressionSelect.svg"), tr("Select features"), lambda: self.select("set"))
            m.addSeparator()
            m.addAction(tr("Rename"), lambda: self.list.editItem(it))
            m.addAction(_icon("/mActionEditCopy.svg"), tr("Duplicate"), self.duplicate_query)
            m.addAction(_icon("/symbologyRemove.svg"), tr("Delete"), self.delete_query)
        else:
            m.addAction(_icon("/symbologyAdd.svg"), tr("New query"), self.new_query)
        m.exec(self.list.mapToGlobal(pos))

    # ================================================================ editor
    def _load_editor(self, q):
        self._loading = True
        self._sql_on_enter = None
        self._converted = None
        self.name_edit.setText(q["name"])
        self.name_edit.setStyleSheet("")
        self.name_edit.setToolTip("")
        for c in list(self.clauses):
            self._remove_clause_widget(c, relayout=False)
        for c in q.get("clauses", []):
            w = self.add_clause(emit=False, relayout=False)
            w.set_clause(c)
        if not self.clauses and q.get("mode") != "sql":
            self.add_clause(emit=False, relayout=False)
        self._relayout()
        self.sql_edit.setPlainText(q.get("sql", ""))
        self._set_mode_ui(q.get("mode", "builder"))
        self.verify_lbl.setText("")
        self._loading = False
        self._update_preview()

    def _set_clauses(self, clauses):
        was = self._loading
        self._loading = True
        for c in list(self.clauses):
            self._remove_clause_widget(c, relayout=False)
        for c in clauses:
            w = self.add_clause(emit=False, relayout=False)
            w.set_clause(c)
        self._relayout()
        self._loading = was
        self._update_preview()

    def _set_mode_ui(self, mode):
        self.seg_builder.setChecked(mode == "builder")
        self.seg_sql.setChecked(mode == "sql")
        self.mode_stack.setCurrentIndex(0 if mode == "builder" else 1)

    def _switch_mode(self, mode):
        """Switch between Builder and SQL without asking:
        * Builder -> SQL: the SQL is built from the clauses (if the user just converted
          their SQL and has not touched the builder, their SQL comes back unchanged).
        * SQL -> Builder: if the SQL was not touched, the builder stays as it was; if it
          was edited, it is converted to clauses (with a warning if that changes the result);
          if it cannot be, it stays in SQL and explains why. Empty SQL = empty builder."""
        q = self.current_query()
        if q is None or q.get("mode") == mode:
            self._set_mode_ui(mode)
            return
        edited = self._save_timer.isActive()
        try:
            built = sql_builder.build_sql(self._collect_clauses(), self.layer, "provider")
        except sql_builder.ClauseError:
            built = None
        if mode == "sql":
            if built and self._converted and self._converted[1] == built:
                text = self._converted[0]          # the user's original SQL, not rewritten
            else:
                text = built
            if text:
                self._loading = True
                self.sql_edit.setPlainText(text)
                self._loading = False
            elif built is None:
                self._verify_msg(tr("The builder has an incomplete clause; showing the previous SQL."), False)
            self._sql_on_enter = self.sql_edit.toPlainText().strip()
        else:
            sql = self.sql_edit.toPlainText().strip()
            untouched = sql == built or (self._sql_on_enter is not None and sql == self._sql_on_enter)
            note = ""
            if not untouched:
                if not sql:
                    self._set_clauses([])
                    self._converted = None
                else:
                    parsed, reason = sql_parser.parse(sql, self.layer, allow_missing=True)
                    if parsed is None:
                        self._set_mode_ui("sql")
                        self._verify_msg(sql_parser.reason_message(reason), False)
                        self._save_now()
                        return
                    self._set_clauses(parsed)
                    note = self._conversion_note(sql, parsed)
            self._sql_on_enter = None
            if not self.clauses:
                self.add_clause(emit=False)
                self._update_preview()
            self.verify_lbl.setText("")
            missing = sorted({w.clause()["field"] for w in self.clauses if w.is_missing()})
            if missing:
                names = ", ".join("«{}»".format(m) for m in missing)
                if len(missing) > 1:
                    msg = tr("{} do not exist in this layer: choose the right fields in the clauses marked in red.")
                else:
                    msg = tr("{} does not exist in this layer: choose the right field in the clause marked in red.")
                self._verify_msg(msg.format(names), False)
            elif note:
                self._verify_msg(note, False)
        q["mode"] = mode
        self._set_mode_ui(mode)
        # save the mode change right away (the applied filter depends on the mode)
        self._save_timer.stop()
        self._save_current(edited=edited)

    def _conversion_note(self, sql, parsed):
        """Warning if the clauses converted from the user's SQL do not keep exactly the same
        features as that SQL (checked against the data, so it is never a false alarm)."""
        try:
            new = sql_builder.build_sql(self._collect_clauses(), self.layer, "provider")
        except sql_builder.ClauseError:
            self._converted = None
            return ""
        self._converted = (sql, new)
        if not new or new == sql:
            return ""
        QgsApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            res = store.compare_results(self.layer, sql, new, self._get_clone())
        finally:
            QgsApplication.restoreOverrideCursor()
        if res is None or res[0]:
            return ""
        _same, n_sql, n_new = res
        fields = self.layer.fields()

        def is_text(c):
            idx = fields.lookupField(c.get("field", ""))
            return idx >= 0 and sql_builder.field_kind(fields.at(idx)) == "text"
        if sql_builder.dialect_of(self.layer) == "ogrsql" and any(
                c.get("op") in ("eq", "ne", "in", "not_in") and is_text(c) for c in parsed):
            why = (tr("In this format «=» ignores upper/lower case; in the builder «is equal to» does not (it is exact)."))
        else:
            why = tr("The builder reads your SQL slightly differently (case or accents).")
        return (tr("⚠ Careful: your SQL kept {} features and the builder keeps {}. {} Switch back to «SQL» to get your SQL back as it was.").format(n_sql, n_new, why))

    def add_clause(self, emit=True, path=None, relayout=True):
        """Append a clause at the end, or at the end of the group given by 'path'."""
        w = ClauseWidget(self.layer, self.values_for, first=not self.clauses)
        w.changed.connect(self._on_clause_changed)
        w.removeRequested.connect(self._on_remove_clause)
        w.checkToggled.connect(self._update_group_buttons)
        pos = len(self.clauses)
        if path:
            path = list(path)
            w.groups = list(path)
            members = [i for i, c in enumerate(self.clauses) if c.groups[:len(path)] == path]
            if members:
                pos = members[-1] + 1
        self.clauses.insert(pos, w)
        if relayout:
            self._relayout()
        if emit:
            self._on_clause_changed()
        return w

    def _remove_clause_widget(self, w, relayout=True):
        self.clauses.remove(w)
        parent = w.parentWidget()
        if parent is not None and parent.layout() is not None:
            parent.layout().removeWidget(w)
        w.hide()
        w.deleteLater()
        if relayout:
            self._relayout()

    def _on_remove_clause(self, w):
        self._remove_clause_widget(w)
        self._on_clause_changed()

    # ---------------------------------------------------------------- groups
    def _tree(self, normalize=True):
        """Group tree. With normalize=True, reassigns clean paths to the clauses."""
        tree = sql_builder.build_tree(self.clauses, lambda w: w.groups)

        def set_path(w, p):
            if normalize:
                w.groups = p
        return sql_builder.assign_paths(tree, set_path)

    def _relayout(self):
        """Rebuild the clause list, drawing groups and subgroups as boxes."""
        tree = self._tree()
        for w in self.clauses:
            parent = w.parentWidget()
            if parent is not None and parent.layout() is not None:
                parent.layout().removeWidget(w)
            w.setParent(self.clause_holder)
        for fr in self.group_frames:
            self.clause_layout.removeWidget(fr)
            fr.hide()
            fr.deleteLater()
        self.group_frames = []
        self._group_checks = []

        pos = 0
        for k, node in enumerate(tree):
            widget = self._render_node(node, k, depth=0, in_group=False)
            self.clause_layout.insertWidget(pos, widget)
            widget.show()
            if "leaf" not in node:
                self.group_frames.append(widget)
            pos += 1
        self._update_group_buttons()

    def _render_node(self, node, index, depth, in_group):
        """Return the widget for a node: a clause row or a group box."""
        if "leaf" in node:
            w = node["leaf"]
            if index > 0:
                w.set_role("connector")
            else:
                w.set_role("open" if in_group else "where")
            return w

        level = min(depth, 3)
        frame = QFrame()
        frame.setObjectName("clauseGroup{}".format(level))
        v = QVBoxLayout(frame)
        v.setContentsMargins(8, 4, 6, 6)
        v.setSpacing(4)
        head = QHBoxLayout()
        gcheck = QCheckBox()
        gcheck.setTristate(True)
        gcheck.setToolTip(tr("Tick the whole group (e.g. to group it with another clause)"))
        members = sql_builder.leaves(node)
        gcheck.clicked.connect(lambda _=False, m=members: self._toggle_group_check(m))
        self._group_checks.append((gcheck, members))
        head.addWidget(gcheck)
        if index > 0:
            first = sql_builder.first_leaf(node)
            cb = QComboBox()
            cb.addItem(tr("AND"), "AND")
            cb.addItem(tr("OR"), "OR")
            cb.setToolTip(tr("How this group joins what comes before"))
            cb.setCurrentIndex(first.connector.currentIndex())
            cb.currentIndexChanged.connect(lambda i, w=first: w.connector.setCurrentIndex(i))
            head.addWidget(cb)
        elif not in_group:
            head.addWidget(QLabel(tr("<b>Where</b>")))
        title = QLabel(tr("( group )") if depth == 0 else tr("( subgroup )"))
        title.setObjectName("groupTitle{}".format(level))
        title.setToolTip(tr("Clauses in this box are evaluated together, as if in parentheses"))
        head.addWidget(title)
        head.addStretch()
        path = node["path"]
        b_add = QToolButton()
        b_add.setIcon(_icon("/symbologyAdd.svg"))
        b_add.setText(tr("Clause here"))
        b_add.setToolTip(tr("Adds a clause at the end of this group"))
        b_add.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        b_add.setAutoRaise(True)
        b_add.clicked.connect(lambda _=False, p=path: self.add_clause(path=p))
        head.addWidget(b_add)
        b_un = QToolButton()
        b_un.setText(tr("Ungroup"))
        b_un.setToolTip(tr("Removes this box (the clauses are kept)"))
        b_un.setAutoRaise(True)
        b_un.clicked.connect(lambda _=False, p=path: self._ungroup_paths([p]))
        head.addWidget(b_un)
        v.addLayout(head)
        for k, ch in enumerate(node["children"]):
            child = self._render_node(ch, k, depth + 1, in_group=True)
            v.addWidget(child)
            child.show()
        return frame

    def _checked(self):
        return [w for w in self.clauses if w.check.isChecked()]

    def _group_plan(self):
        """Validate grouping the ticked clauses.
        Return (parent_group_path, None) if possible, or (None, message)."""
        sel = self._checked()
        if len(sel) < 2:
            return None, tr("Tick at least two clauses to group them.")
        tree = self._tree(normalize=False)
        sel_ids = {id(w) for w in sel}
        # innermost group that contains all the ticked clauses
        parent_children, parent_path = tree, ()
        while True:
            inner = None
            for ch in parent_children:
                if "leaf" in ch:
                    continue
                if sel_ids <= {id(x) for x in sql_builder.leaves(ch)}:
                    inner = ch
                    break
            if inner is None:
                break
            parent_children, parent_path = inner["children"], inner["path"]
        touched = [k for k, ch in enumerate(parent_children)
                   if {id(x) for x in sql_builder.leaves(ch)} & sel_ids]
        if touched != list(range(touched[0], touched[-1] + 1)):
            return None, tr("Clauses to group must be consecutive.")
        for k in touched:
            if not {id(x) for x in sql_builder.leaves(parent_children[k])} <= sel_ids:
                return None, (tr("The selection splits a group: tick all of its clauses or only some inside it."))
        if len(touched) < 2:
            return None, tr("Tick at least two items of the same level.")
        if len(touched) == len(parent_children):
            return None, tr("Those clauses already form a whole group.")
        return parent_path, None

    def _toggle_group_check(self, members):
        target = not all(w.check.isChecked() for w in members)
        for w in members:
            w.check.blockSignals(True)
            w.check.setChecked(target)
            w.check.blockSignals(False)
        self._update_group_buttons()

    def _update_group_buttons(self):
        for cb, members in getattr(self, "_group_checks", []):
            n = sum(1 for w in members if w.check.isChecked())
            state = (Qt.CheckState.Checked if n == len(members) else
                     Qt.CheckState.PartiallyChecked if n else Qt.CheckState.Unchecked)
            cb.blockSignals(True)
            cb.setCheckState(state)
            cb.blockSignals(False)
        _p, err = self._group_plan()
        self.b_group.setEnabled(err is None)
        self.b_group.setToolTip(err or tr("Groups the ticked items (like parentheses)"))
        self.b_ungroup.setEnabled(any(w.groups for w in self._checked()))

    def group_checked(self):
        parent_path, err = self._group_plan()
        if err:
            self._verify_msg(err, False)
            return
        depth = len(parent_path)
        new_id = max([g for w in self.clauses for g in w.groups] + [0]) + 1
        sel = self._checked()
        for w in sel:
            w.groups = w.groups[:depth] + [new_id] + w.groups[depth:]
        self._uncheck_all()
        self._relayout()
        self._on_clause_changed()

    def ungroup_checked(self):
        """Whole group ticked -> remove that group; single clause ticked -> remove its innermost group."""
        tree = self._tree()
        checked = {id(w) for w in self._checked()}
        paths, covered = set(), set()

        def walk(children):
            for ch in children:
                if "leaf" in ch:
                    continue
                ids = {id(x) for x in sql_builder.leaves(ch)}
                if ids <= checked:
                    paths.add(ch["path"])
                    covered.update(ids)
                else:
                    walk(ch["children"])
        walk(tree)
        for w in self._checked():
            if id(w) not in covered and w.groups:
                paths.add(tuple(w.groups))
        self._uncheck_all()
        self._ungroup_paths(paths)

    def _uncheck_all(self):
        for w in self.clauses:
            w.check.blockSignals(True)
            w.check.setChecked(False)
            w.check.blockSignals(False)
        self._update_group_buttons()

    def _ungroup_paths(self, paths):
        # deepest first, so the other paths are not shifted
        for p in sorted({tuple(p) for p in paths}, key=len, reverse=True):
            n = len(p)
            for w in self.clauses:
                if tuple(w.groups[:n]) == p:
                    w.groups = w.groups[:n - 1] + w.groups[n:]
        self._relayout()
        self._on_clause_changed()

    def _collect_clauses(self):
        return [w.clause() for w in self.clauses]

    def _on_clause_changed(self):
        if self._loading:
            return
        self.verify_lbl.setText("")
        self._update_preview()
        self._schedule_save()

    def _update_preview(self):
        if self.layer is None:
            return
        clauses = self._collect_clauses()
        try:
            sql = sql_builder.build_sql(clauses, self.layer, "provider")
            self.preview.setStyleSheet("")
            self.preview.setPlainText(sql)
        except sql_builder.Incomplete as e:
            # something is still unset: show it quietly (Apply and Verify enforce it)
            self.preview.setStyleSheet("color: #6b6b6b; font-style: italic;")
            self.preview.setPlainText(str(e))
        except sql_builder.ClauseError as e:
            self.preview.setStyleSheet("color: #c62828;")
            self.preview.setPlainText("⚠ " + str(e))
        try:
            warns = lint(clauses, self.layer, self.values_for, self._values_complete)
        except Exception:
            warns = []
        source = sql_builder.untested_source(self.layer)
        if source:
            warns.insert(0, tr("This type of data source ({}) has not been tested with the plugin: its database "
                               "applies its own rules (e.g. upper/lower case). Check the result with Verify.")
                         .format(source))
        self.lint_lbl.setText("\n".join("⚠ " + w for w in warns))
        self.lint_lbl.setVisible(bool(warns))

    def _name_taken(self, text):
        q = self.current_query()
        t = text.strip().lower()
        return bool(t) and any(o is not q and o["name"].strip().lower() == t for o in self.data["queries"])

    def _on_name_edited(self, text):
        taken = self._name_taken(text)
        self.name_edit.setStyleSheet("QLineEdit { border: 2px solid #c62828; }" if taken else "")
        self.name_edit.setToolTip(tr("A query with that name already exists.") if taken else "")
        it = self.list.currentItem()
        if it is not None and not taken and text.strip():
            self._loading = True
            it.setText(text)
            self._loading = False
        self._schedule_save()

    def _schedule_save(self):
        if not self._loading:
            self._save_timer.start()

    def _save_now(self):
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save_current()

    def _save_current(self, edited=True):
        q = self.current_query()
        if q is None or self.layer is None:
            return
        if edited:
            q.pop("origin", None)  # edited in the plugin: no longer the temporary «QGIS filter»
        typed = self.name_edit.text().strip()
        if typed and not self._name_taken(typed):
            q["name"] = typed
        q["clauses"] = self._collect_clauses()
        if q.get("mode") == "sql":
            new_sql = self.sql_edit.toPlainText().strip()
            if new_sql != q.get("sql"):
                q["expression"] = ""  # the alternative expression no longer applies
            q["sql"] = new_sql
        else:
            q["sql"] = self.sql_edit.toPlainText().strip()
        store.save(self.layer, self.data)
        self._refresh_badge()

    # ================================================================ SQL helpers
    def _fill_sql_fields(self):
        self.sql_fields.clear()
        self.sql_values.clear()
        if self.layer is None:
            return
        for fld in sql_builder.provider_fields(self.layer):
            it = QListWidgetItem(self.layer.fields().iconForField(
                self.layer.fields().indexOf(fld.name())), fld.name())
            it.setToolTip(fld.typeName())
            self.sql_fields.addItem(it)

    def _insert_sql(self, txt):
        self.sql_edit.insertPlainText(txt)
        self.sql_edit.setFocus()

    def _load_sql_values(self):
        it = self.sql_fields.currentItem()
        if it is None:
            return
        self.sql_values.clear()
        self.sql_values.addItems([v for v in self.values_for(it.text()) if v not in sql_builder.SPECIAL_VALUES])

    def _insert_value(self, item):
        fld = self.sql_fields.currentItem()
        kind = "text"
        if fld is not None:
            idx = self.layer.fields().indexOf(fld.text())
            kind = sql_builder.field_kind(self.layer.fields().at(idx))
        try:
            self._insert_sql(sql_builder.literal(item.text(), kind))
        except sql_builder.ClauseError:
            self._insert_sql(sql_builder.quote_text(item.text()))

    # ================================================================ actions
    def new_query(self):
        if self.layer is None:
            return
        self._save_now()
        q = store.new_query(store.default_name(self.data))
        self.data["queries"].append(q)
        store.save(self.layer, self.data)
        self.current_id = q["id"]
        self._reload_list(q["id"])
        self.name_edit.setFocus()
        self.name_edit.selectAll()

    def duplicate_query(self):
        q = self.current_query()
        if q is None:
            return
        self._save_now()
        nq = copy.deepcopy(q)
        nq.update(store.new_query(store.unique_name(self.data, q["name"] + tr(" (copy)"))))
        nq["mode"], nq["clauses"], nq["sql"], nq["expression"] = (
            q.get("mode"), copy.deepcopy(q.get("clauses", [])), q.get("sql", ""), q.get("expression", ""))
        self.data["queries"].insert(self.data["queries"].index(q) + 1, nq)
        store.save(self.layer, self.data)
        self._reload_list(nq["id"])

    def delete_query(self):
        q = self.current_query()
        if q is None:
            return
        r = QMessageBox.question(self, tr("Delete query"), tr("Delete query «{}»?").format(q["name"]))
        if r != QMessageBox.StandardButton.Yes:
            return
        was_active = store.active_query(self.layer, self.data) is q
        self.data["queries"].remove(q)
        if self.data.get("active") == q["id"]:
            self.data["active"] = None
        store.save(self.layer, self.data)
        if was_active:
            ok, msg = store.clear_filter(self.layer)
            if not ok:
                self._bar(False, msg)
        self.current_id = None
        self._reload_list()

    def verify(self):
        q = self.current_query()
        if q is None:
            return
        self._save_now()
        try:
            sql = store.query_sql(self.layer, q)
        except sql_builder.ClauseError as e:
            self._verify_msg(str(e), False)
            return
        if not sql:
            self._verify_msg(tr("The query is empty: it would show all features."), True)
            return
        QgsApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            n, total, err = store.count_matching(self.layer, sql, self._get_clone())
        finally:
            QgsApplication.restoreOverrideCursor()
        if err:
            self._verify_msg(err, False)
        else:
            note = ""
            if self.layer.providerType() == "memory" and (self.layer.subsetString() or "").strip():
                note = tr(" (temporary layer: counted within its current filter)")
            if n == 0:
                self._verify_msg(tr("⚠ Valid query, but no feature matches it{}.").format(note), False)
            else:
                self._verify_msg(tr("✔ Valid query: {} of {} features match{}.").format(n, total, note), True)

    def _verify_msg(self, text, ok):
        self.verify_lbl.setObjectName("verifyOk" if ok else "verifyErr")
        self.verify_lbl.setText(text)
        self.verify_lbl.style().unpolish(self.verify_lbl)
        self.verify_lbl.style().polish(self.verify_lbl)

    def _bar(self, ok, msg):
        level = Qgis.MessageLevel.Success if ok else Qgis.MessageLevel.Warning
        self.iface.messageBar().pushMessage(tr("Definition Queries"), msg, level, 5)

    def apply_filter(self):
        q = self.current_query()
        if q is None:
            return
        self._save_now()
        self._loading = True
        ok, msg = store.apply_filter(self.layer, q["id"])
        self._loading = False
        self.data = store.load(self.layer)
        self._reload_list(q["id"])
        if ok and store.feature_count(self.layer) == 0:
            ok, msg = False, msg + " " + store.EMPTY_HINT
        self._bar(ok, msg)
        if not ok:
            self._verify_msg(msg, False)

    def clear_filter(self):
        if self.layer is None:
            return
        self._save_now()
        self._loading = True
        ok, msg = store.clear_filter(self.layer)
        self._loading = False
        self.data = store.load(self.layer)
        self._reload_list(self.current_id)
        self._bar(ok, msg)

    def select(self, behavior):
        q = self.current_query()
        if q is None:
            return
        self._save_now()
        ok, msg = store.select_with_query(self.layer, q["id"], behavior)
        self._bar(ok, msg)
        if not ok:
            self._verify_msg(msg, False)

    def replace_field(self):
        """Replace a field name in all the saved queries of the layer (e.g. after renaming it)."""
        if self.layer is None:
            return
        self._save_now()
        missing = store.missing_query_fields(self.layer, self.data)
        used = sorted({c.get("field") for q in self.data["queries"] for c in q.get("clauses", []) if c.get("field")}
                      | set(missing))
        if not used:
            self._bar(False, tr("No saved query uses fields yet."))
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("Replace a field in all queries"))
        grid = QGridLayout(dlg)
        grid.addWidget(QLabel(tr("Field used in the queries:")), 0, 0)
        old_box = QComboBox()
        for name in used:
            old_box.addItem(tr("{} (no longer in the layer)").format(name) if name in missing else name, name)
        grid.addWidget(old_box, 0, 1)
        grid.addWidget(QLabel(tr("Replace with the layer field:")), 1, 0)
        new_box = QComboBox()
        new_box.addItems(self.layer.fields().names())
        grid.addWidget(new_box, 1, 1)
        row = QHBoxLayout()
        row.addStretch()
        b_cancel = QPushButton(tr("Cancel"))
        b_ok = QPushButton(tr("Replace"))
        b_ok.setDefault(True)
        b_cancel.clicked.connect(dlg.reject)
        b_ok.clicked.connect(dlg.accept)
        row.addWidget(b_cancel)
        row.addWidget(b_ok)
        grid.addLayout(row, 2, 0, 1, 2)
        if not dlg.exec():
            return
        old, new = old_box.currentData(), new_box.currentText()
        if not new or old == new:
            return
        self._loading = True
        try:
            n, msg = store.replace_field(self.layer, old, new)
        finally:
            self._loading = False
        self.data = store.load(self.layer)
        self._reload_list(self.current_id)
        self._bar(n > 0, msg)

    def select_visible(self):
        if self.layer is None:
            return
        ok, msg = store.select_visible(self.layer, self.iface.mapCanvas().mapSettings())
        self._bar(ok, msg)

    def from_selection(self):
        if self.layer is None:
            return
        self._save_now()
        n = self.layer.selectedFeatureCount()
        if n == 0:
            self._bar(False, tr("The layer has no selected features."))
            return
        name = ask_name(self, store.default_name(self.data), below(self.b_from_sel),
                        hint=tr("Filter by the {} selected features").format(n), ok_text=tr("Filter"))
        if name is None:
            return
        self._loading = True
        try:
            q, ok, msg = store.query_from_selection(self.layer, name)
        except sql_builder.ClauseError as e:
            self._loading = False
            self._bar(False, str(e))
            return
        self._loading = False
        if q is None:
            self._bar(False, msg)
            return
        self.data = store.load(self.layer)
        self._reload_list(q["id"])
        self._bar(ok, msg)

    def _sync_notes_action(self):
        self.act_notes.blockSignals(True)
        self.act_notes.setChecked(store.notes_enabled())
        self.act_notes.blockSignals(False)

    def _toggle_notes(self, on):
        store.set_notes_enabled(on)
        QgsProject.instance().setDirty(True)
        self._bar(True, tr("Notes for people without the plugin: {}.").format(
            "activadas" if on else tr("removed from all layers")))

    def export_text(self, all_layers=False):
        self._save_now()
        if all_layers:
            layers = [lyr for lyr in QgsProject.instance().mapLayers().values()
                      if hasattr(lyr, "subsetString")]
            base = QgsProject.instance().baseName() or "proyecto"
        else:
            if self.layer is None:
                return
            layers, base = [self.layer], self.layer.name()
        path, _ = QFileDialog.getSaveFileName(self, tr("Export queries as text"),
                                              tr("{}_queries.txt").format(base), tr("Text (*.txt)"))
        if path:
            n = store.export_text(layers, path)
            self._bar(True, tr("{} queries exported as text.").format(n))

    def export_queries(self):
        if self.layer is None or not self.data["queries"]:
            return
        self._save_now()
        path, _ = QFileDialog.getSaveFileName(self, tr("Export queries"),
                                              tr("{}_queries.json").format(self.layer.name()), "JSON (*.json)")
        if path:
            n = store.export_json(self.layer, path)
            self._bar(True, tr("{} queries exported.").format(n))

    def import_queries(self):
        if self.layer is None:
            return
        self._save_now()
        path, _ = QFileDialog.getOpenFileName(self, tr("Import queries"), "", "JSON (*.json)")
        if not path:
            return
        try:
            n = store.import_json(self.layer, path)
        except (OSError, ValueError) as e:
            self._bar(False, tr("Could not read the file: {}").format(e))
            return
        self.data = store.load(self.layer)
        self._reload_list()
        self._bar(True, tr("{} queries imported.").format(n))

    # ================================================================ lifecycle
    def open_for(self, layer, new=False):
        if layer is not None:
            self.layer_combo.setLayer(layer)
            if layer is not self.layer:
                self.set_layer(layer)
        self.show()
        self.raise_()
        self.activateWindow()
        if new:
            self.new_query()

    def closeEvent(self, e):
        self._save_now()
        super().closeEvent(e)
