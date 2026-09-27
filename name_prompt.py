# -*- coding: utf-8 -*-
"""Small popup for naming a query (with a suggested name filled in)."""

from qgis.PyQt.QtCore import QPoint, Qt
from qgis.PyQt.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from qgis.PyQt.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)
from .i18n import tr


def ask_name(parent, proposed, pos=None, title=tr("Name of the new query"),
             hint="", ok_text=tr("OK")):
    """Show the popup and return the chosen name, or None if cancelled.
    Enter accepts; Esc or a click outside cancels."""
    dlg = QDialog(parent, Qt.WindowType.Popup)
    frame = QFrame(dlg)
    frame.setObjectName("namePrompt")
    frame.setStyleSheet("QFrame#namePrompt { border: 1px solid palette(mid); background: palette(window); }")
    outer = QVBoxLayout(dlg)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.addWidget(frame)
    v = QVBoxLayout(frame)
    v.setContentsMargins(10, 8, 10, 8)
    v.setSpacing(6)

    lbl = QLabel("<b>{}</b>".format(title))
    v.addWidget(lbl)
    if hint:
        h = QLabel(hint)
        h.setStyleSheet("color: gray;")
        v.addWidget(h)
    edit = QLineEdit(proposed)
    edit.setMinimumWidth(280)
    edit.selectAll()
    v.addWidget(edit)

    row = QHBoxLayout()
    row.addStretch()
    b_cancel = QPushButton(tr("Cancel"))
    b_ok = QPushButton(ok_text)
    b_ok.setDefault(True)
    b_ok.setAutoDefault(True)
    b_cancel.clicked.connect(dlg.reject)
    b_ok.clicked.connect(dlg.accept)
    edit.returnPressed.connect(dlg.accept)
    row.addWidget(b_cancel)
    row.addWidget(b_ok)
    v.addLayout(row)

    dlg.adjustSize()
    if pos is not None:
        dlg.move(pos)
    edit.setFocus()
    if not dlg.exec():
        return None
    return edit.text().strip() or proposed


def check_icon(size=16, color="#2e7d32"):
    """Simple check mark icon (✓) to mark the active query."""
    pm = QPixmap(size, size)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(size * 0.14)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    path = QPainterPath()
    path.moveTo(size * 0.18, size * 0.55)
    path.lineTo(size * 0.42, size * 0.78)
    path.lineTo(size * 0.84, size * 0.24)
    p.drawPath(path)
    p.end()
    return QIcon(pm)


def blank_icon(size=16):
    pm = QPixmap(size, size)
    pm.fill(QColor(0, 0, 0, 0))
    return QIcon(pm)


def below(widget):
    """Global point just below a widget (to open the popup there)."""
    return widget.mapToGlobal(QPoint(0, widget.height()))
