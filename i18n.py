# -*- coding: utf-8 -*-
"""Interface language: English by default, Spanish or Portuguese when QGIS runs in those languages.

tr("English text") returns the text in the QGIS interface language. Placeholders ({}) are kept,
so translate first and format afterwards: tr("{} features").format(n)."""

import os

from .i18n_data import ES, PT

_LANG = None


def _detect():
    forced = os.environ.get("DEFINITION_QUERIES_LANG", "")
    if forced:
        return forced[:2].lower()
    loc = ""
    try:
        from qgis.core import QgsApplication
        loc = QgsApplication.locale() or ""
    except Exception:
        loc = ""
    if not loc:
        try:
            from qgis.PyQt.QtCore import QLocale
            loc = QLocale.system().name()
        except Exception:
            loc = "en"
    return (loc or "en")[:2].lower()


def lang():
    global _LANG
    if _LANG is None:
        _LANG = _detect()
    return _LANG


def set_lang(code):
    """Only for tests / previews."""
    global _LANG
    _LANG = code


def tr(text):
    code = lang()
    if code == "es":
        return ES.get(text, text)
    if code == "pt":
        return PT.get(text, text)
    return text
