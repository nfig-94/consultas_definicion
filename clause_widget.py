# -*- coding: utf-8 -*-
"""Visual builder row:  [AND/OR] [Field ▼] [Operator ▼] [Value ▼] [×]"""

from qgis.PyQt.QtCore import QEvent, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)
from qgis.gui import QgsFieldComboBox

from . import sql_builder
from .flow_layout import FlowLayout
from .i18n import tr

CHIP_STYLE = """
QFrame#valueChip { background: palette(alternate-base); border: 1px solid palette(mid); border-radius: 10px; }
QFrame#valueChip QToolButton { border: none; padding: 0px; }
"""
CHIP_MAX_CHARS = 60


def _value_combo(parent):
    cb = QComboBox(parent)
    cb.setEditable(True)
    cb.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    cb.setMinimumWidth(140)
    cb.setMaxVisibleItems(20)
    cb.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    comp = QCompleter(cb.model(), cb)
    comp.setFilterMode(Qt.MatchFlag.MatchContains)
    comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    comp.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
    cb.setCompleter(comp)
    cb.lineEdit().setPlaceholderText(tr("Type or pick a value…"))
    return cb


class ValueChecklistMenu(QMenu):
    """Dropdown with a search box and checkboxes for «is one of (list)».
    Opens below the clause and stays open while you tick values."""

    valuesChanged = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._extra = []  # values typed by hand that are not in the layer
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(4)

        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search…  (Enter adds a value that is not listed)"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(self._enter)
        v.addWidget(self.search)

        self.list = QListWidget()
        self.list.setMinimumHeight(220)
        self.list.setUniformItemSizes(True)
        self.list.itemClicked.connect(self._toggle)
        v.addWidget(self.list)

        row = QHBoxLayout()
        b_all = QToolButton()
        b_all.setText(tr("Check visible"))
        b_all.clicked.connect(lambda: self._set_visible(True))
        b_none = QToolButton()
        b_none.setText(tr("Uncheck all"))
        b_none.clicked.connect(self._clear_all)
        row.addWidget(b_all)
        row.addWidget(b_none)
        row.addStretch()
        self.count_lbl = QLabel()
        row.addWidget(self.count_lbl)
        v.addLayout(row)

        wa = QWidgetAction(self)
        wa.setDefaultWidget(box)
        self.addAction(wa)
        self.box = box
        self.aboutToShow.connect(self._on_show)

    # ------------------------------------------------------------
    def set_values(self, values, checked):
        checked = list(checked)
        self._extra = [c for c in checked if c not in set(values)]
        self.list.clear()
        chk = set(checked)
        for val in list(values) + self._extra:
            self._add_item(val, val in chk)
        self.search.clear()
        self._update_count()

    def _add_item(self, value, checked):
        it = QListWidgetItem(sql_builder.display_value(value))
        it.setData(Qt.ItemDataRole.UserRole, value)
        # no ItemIsUserCheckable: a click anywhere on the row toggles it
        it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        it.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.list.addItem(it)
        return it

    def checked_values(self):
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]

    def _emit(self):
        self._update_count()
        self.valuesChanged.emit(self.checked_values())

    def _toggle(self, it):
        it.setCheckState(Qt.CheckState.Unchecked if it.checkState() == Qt.CheckState.Checked
                         else Qt.CheckState.Checked)
        self._emit()

    def _filter(self, text):
        t = text.lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(t not in it.text().lower())

    def _set_visible(self, state):
        for i in range(self.list.count()):
            it = self.list.item(i)
            if not it.isHidden():
                it.setCheckState(Qt.CheckState.Checked if state else Qt.CheckState.Unchecked)
        self._emit()

    def _clear_all(self):
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.CheckState.Unchecked)
        self._emit()

    def _enter(self):
        text = self.search.text().strip()
        if not text:
            return
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.text().lower() == text.lower():
                it.setCheckState(Qt.CheckState.Checked)
                break
        else:
            self._add_item(text, True)
        self.search.clear()
        self._emit()

    def _update_count(self):
        n = len(self.checked_values())
        self.count_lbl.setText((tr("{} checked") if n == 1 else tr("{} checked ")).format(n).strip())

    def _on_show(self):
        QTimer.singleShot(0, self.search.setFocus)


class ClauseWidget(QFrame):
    changed = pyqtSignal()
    removeRequested = pyqtSignal(object)
    checkToggled = pyqtSignal()

    def __init__(self, layer, values_provider, first=False, parent=None):
        """values_provider(field_name) -> list of strings (unique values)."""
        super().__init__(parent)
        self.layer = layer
        self.values_provider = values_provider
        self._list_values = []
        self._loaded_field = None
        self._missing = None  # SQL field missing from the layer (another one must be picked)
        self.groups = []  # path of groups it belongs to (outermost first)

        self.setObjectName("clauseRow")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 4, 4, 4)
        outer.setSpacing(4)
        lay = QHBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        outer.addLayout(lay)

        self.check = QCheckBox()
        self.check.setToolTip(tr("Tick several consecutive clauses to group them"))
        self.check.toggled.connect(lambda _on: self.checkToggled.emit())
        lay.addWidget(self.check)

        self.where_lbl = QLabel(tr("<b>Where</b>"))
        self.where_lbl.setMinimumWidth(60)
        self.connector = QComboBox()
        self.connector.addItem(tr("AND"), "AND")
        self.connector.addItem(tr("OR"), "OR")
        self.connector.setToolTip(tr("AND: both conditions must be met.\nOR: one of them is enough."))
        self.connector.setMinimumWidth(52)
        lay.addWidget(self.where_lbl)
        lay.addWidget(self.connector)

        self.field = QgsFieldComboBox()
        # only the source's own fields: joined/virtual ones cannot be used in a layer filter
        self.fields = sql_builder.provider_fields(layer) if layer is not None else None
        if self.fields is not None and hasattr(self.field, "setFields"):
            self.field.setFields(self.fields)
        else:
            self.field.setLayer(layer)
        self.field.setMinimumWidth(150)
        lay.addWidget(self.field)
        # red text inside the box when the SQL field does not exist in the layer
        self.missing_lbl = QLabel(self.field)
        self.missing_lbl.setStyleSheet("color: #c62828; font-weight: 600; background: transparent;")
        self.missing_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.missing_lbl.hide()
        self.field.installEventFilter(self)

        self.op = QComboBox()
        self.op.setMinimumWidth(150)
        lay.addWidget(self.op)

        # --- value area
        self.value_stack = QStackedWidget()
        self.value_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # 0: no value
        self.value_stack.addWidget(QLabel(""))
        # 1: one value
        self.value1 = _value_combo(self)
        self.value_stack.addWidget(self.value1)
        # 2: between
        w2 = QWidget()
        h2 = QHBoxLayout(w2)
        h2.setContentsMargins(0, 0, 0, 0)
        self.value_a = _value_combo(self)
        self.value_b = _value_combo(self)
        h2.addWidget(self.value_a)
        h2.addWidget(QLabel(tr("and")))
        h2.addWidget(self.value_b)
        self.value_stack.addWidget(w2)
        # 3: list
        self.list_btn = QPushButton(tr("Choose values…"))
        self.list_btn.setStyleSheet("text-align:left; padding:3px 8px;")
        self.list_menu = ValueChecklistMenu(self.list_btn)
        self.list_menu.aboutToShow.connect(self._fill_list_menu)
        self.list_menu.valuesChanged.connect(self._on_list_values)
        self.list_btn.setMenu(self.list_menu)
        self.value_stack.addWidget(self.list_btn)
        lay.addWidget(self.value_stack, 1)

        self.del_btn = QToolButton()
        self.del_btn.setText("✕")
        self.del_btn.setToolTip(tr("Remove this clause"))
        self.del_btn.setAutoRaise(True)
        self.del_btn.clicked.connect(lambda: self.removeRequested.emit(self))
        lay.addWidget(self.del_btn)

        # --- values picked from the list: chips that wrap to a new line when they do not fit
        self.chips_box = QWidget()
        self.chips_box.setStyleSheet(CHIP_STYLE)
        self.chips = FlowLayout(self.chips_box, spacing=4)
        self.chips_box.setContentsMargins(26, 0, 30, 0)
        sp = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        sp.setHeightForWidth(True)
        self.chips_box.setSizePolicy(sp)
        self.chips_box.setVisible(False)
        outer.addWidget(self.chips_box)

        self.set_first(first)
        self._pick_default_field()

        self.field.fieldChanged.connect(self._on_field_changed)
        self.op.currentIndexChanged.connect(self._on_op_changed)
        self.connector.currentIndexChanged.connect(self.changed)
        for cb in (self.value1, self.value_a, self.value_b):
            cb.editTextChanged.connect(self.changed)

        self._on_field_changed(self.field.currentField())

    # ------------------------------------------------------------ helpers
    def _pick_default_field(self):
        """First useful field for a new clause (not the internal identifier)."""
        if self.layer is None or self.fields is None or self.fields.count() == 0:
            return
        pk_names = {self.layer.fields().at(i).name().lower() for i in self.layer.primaryKeyAttributes()
                    if 0 <= i < self.layer.fields().count()}
        for fld in self.fields:
            n = fld.name()
            if n.lower() in pk_names or n.lower() in ("fid", "objectid", "ogc_fid", "gid"):
                continue
            self.field.setField(n)
            return

    def set_first(self, first):
        self.set_role("where" if first else "connector")

    def set_role(self, role):
        """'where': first clause ("Where") · 'connector': shows AND/OR ·
        'open': first clause inside a group (the connector goes in the header)."""
        self.where_lbl.setText(tr("<b>Where</b>") if role == "where" else "")
        self.where_lbl.setVisible(role in ("where", "open"))
        self.connector.setVisible(role == "connector")

    def _kind(self):
        if self._missing and not self.field.currentField():
            return "any"
        idx = self.layer.fields().indexOf(self.field.currentField())
        return sql_builder.field_kind(self.layer.fields().at(idx)) if idx >= 0 else "text"

    def eventFilter(self, obj, event):
        if obj is self.field and event.type() == QEvent.Type.Resize:
            self._place_missing_lbl()
        return False

    def _place_missing_lbl(self):
        r = self.field.rect()
        self.missing_lbl.setGeometry(8, 0, max(10, r.width() - 30), r.height())

    def _set_missing(self, name):
        """Marks in red a field that does not exist in the layer, so the user picks another."""
        self._missing = name or None
        self.missing_lbl.setText("⚠ {}".format(name) if self._missing else "")
        self.missing_lbl.setVisible(bool(self._missing))
        self._place_missing_lbl()
        if self._missing:
            self.field.setCurrentIndex(-1)
            self.field.setStyleSheet("QComboBox { border: 2px solid #c62828; border-radius: 3px; }")
            self.field.setToolTip(tr("Field «{}» does not exist in this layer: choose the right field (values and operator are kept).").format(name))
        else:
            self.field.setStyleSheet("")
            self.field.setToolTip("")

    def is_missing(self):
        return bool(self._missing) and not self.field.currentField()

    def _on_field_changed(self, name):
        from_missing = bool(self._missing) and bool(name)
        if from_missing:
            self._set_missing(None)
        prev = self.op.currentData()
        self.op.blockSignals(True)
        self.op.clear()
        for key, label in sql_builder.operators_for_kind(self._kind()):
            self.op.addItem(label, key)
        i = self.op.findData(prev)
        self.op.setCurrentIndex(i if i >= 0 else 0)
        self.op.blockSignals(False)
        self._loaded_field = None
        if not from_missing:  # values are kept when fixing a missing field
            self._list_values = []
        self._on_op_changed()

    def _ensure_values(self):
        name = self.field.currentField()
        if not name or self._loaded_field == name:
            return
        self._loaded_field = name
        vals = self.values_provider(name)
        for cb in (self.value1, self.value_a, self.value_b):
            txt = cb.currentText()
            cb.blockSignals(True)
            cb.clear()
            cb.addItems([sql_builder.display_value(v) for v in vals])
            cb.setEditText(txt)
            cb.blockSignals(False)

    def _on_op_changed(self, *_):
        key = self.op.currentData()
        vtype = sql_builder.OP_BY_KEY.get(key, (None, None, "one"))[2]
        page = {"none": 0, "one": 1, "two": 2, "list": 3}[vtype]
        self.value_stack.setCurrentIndex(page)
        if page in (1, 2) and key not in ("contains", "not_contains", "starts", "ends"):
            self._ensure_values()
        self._refresh_list_btn()
        self.changed.emit()

    def _fill_list_menu(self):
        name = self.field.currentField()
        values = self.values_provider(name) if name else []
        self.list_menu.set_values(values, self._list_values)
        self.list_menu.box.setMinimumWidth(max(self.list_btn.width(), 340))

    def _on_list_values(self, values):
        self._list_values = list(values)
        self._refresh_list_btn()
        self.changed.emit()

    def _refresh_list_btn(self):
        n = len(self._list_values)
        is_list = self.value_stack.currentIndex() == 3
        if n == 0:
            self.list_btn.setText(tr("Choose values…"))
        else:
            self.list_btn.setText((tr("{} value chosen") if n == 1 else tr("{} values chosen")).format(n))
        self.list_btn.setToolTip("\n".join(sql_builder.display_value(v) for v in self._list_values))
        self._rebuild_chips(is_list)

    def _rebuild_chips(self, visible):
        while self.chips.count():
            item = self.chips.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.hide()          # no setParent(None): avoid creating stray windows
                w.deleteLater()
        if visible:
            for v in self._list_values:
                self.chips.addWidget(self._make_chip(v))
        self.chips_box.setVisible(visible and bool(self._list_values))
        self.chips_box.updateGeometry()

    def _make_chip(self, value):
        chip = QFrame()
        chip.setObjectName("valueChip")
        h = QHBoxLayout(chip)
        h.setContentsMargins(8, 1, 3, 1)
        h.setSpacing(2)
        shown = sql_builder.display_value(value)
        text = shown if len(shown) <= CHIP_MAX_CHARS else shown[:CHIP_MAX_CHARS - 1] + "…"
        lbl = QLabel(text)
        lbl.setToolTip(shown)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        h.addWidget(lbl)
        x = QToolButton()
        x.setText("✕")
        x.setToolTip(tr("Remove this value"))
        x.setAutoRaise(True)
        x.clicked.connect(lambda _=False, v=value: self._remove_value(v))
        h.addWidget(x)
        return chip

    def _remove_value(self, value):
        if value in self._list_values:
            self._list_values.remove(value)
            self._refresh_list_btn()
            self.changed.emit()

    # ------------------------------------------------------------ API
    def clause(self):
        return {
            "connector": self.connector.currentData(),
            "field": self.field.currentField() or (self._missing or ""),
            "op": self.op.currentData(),
            "value": (self.value_a.currentText() if self.op.currentData() == "between"
                      else sql_builder.stored_value(self.value1.currentText())),
            "value2": self.value_b.currentText(),
            "values": list(self._list_values),
            "groups": list(self.groups),
        }

    def set_clause(self, c):
        self.groups = sql_builder.clause_path(c)
        for w in (self.field, self.op, self.connector):
            w.blockSignals(True)
        name = c.get("field", "") or ""
        pool = self.fields if self.fields is not None else (self.layer.fields() if self.layer else None)
        exists = pool is not None and pool.lookupField(name) >= 0
        if exists or not name:
            self._set_missing(None)
            self.field.setField(name)
        else:
            self._set_missing(name)
        self.connector.setCurrentIndex(1 if c.get("connector") == "OR" else 0)
        for w in (self.field, self.connector):
            w.blockSignals(False)
        self.op.blockSignals(False)
        self._on_field_changed(self.field.currentField())
        i = self.op.findData(c.get("op", "eq"))
        if i >= 0:
            self.op.setCurrentIndex(i)
        self._list_values = list(c.get("values") or [])
        if c.get("op") == "between":
            self.value_a.setEditText(c.get("value", "") or "")
        else:
            self.value1.setEditText(sql_builder.display_value(c.get("value", "") or ""))
        self.value_b.setEditText(c.get("value2", "") or "")
        self._refresh_list_btn()
