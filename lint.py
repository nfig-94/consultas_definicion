# -*- coding: utf-8 -*-
"""
Non-blocking warnings for the builder: catch common mistakes BEFORE filtering,
e.g. a misspelled value that would silently leave the filter empty.
"""

import re
import unicodedata

from . import sql_builder
from .i18n import tr

_THOUSANDS = re.compile(r"^[+-]?\d{1,3}\.\d{3}$")  # «1.000» (with more dots it is no longer a number)


def _fold(text):
    """Lowercase, without accents (for «did you mean…?» suggestions)."""
    t = unicodedata.normalize("NFD", str(text).strip().lower())
    return "".join(ch for ch in t if unicodedata.category(ch) != "Mn")


def lint(clauses, layer, values_for, is_complete):
    """List of warnings. values_for(field) -> the layer's values (as text);
    is_complete(field) -> True if that list holds ALL the distinct values."""
    out = []
    fields = layer.fields()
    for c in clauses:
        name, op = c.get("field", ""), c.get("op")
        idx = fields.lookupField(name)
        if idx < 0:
            continue
        kind = sql_builder.field_kind(fields.at(idx))
        if op in ("eq", "ne"):
            given = [c.get("value")]
        elif op in ("in", "not_in"):
            given = list(c.get("values") or [])
        elif op in ("gt", "ge", "lt", "le"):
            given = [c.get("value")]
        elif op == "between":
            given = [c.get("value"), c.get("value2")]
        else:
            continue
        given = [g for g in given if g is not None and str(g) != ""]
        if not given:
            continue
        if kind == "num":
            for g in given:
                if _THOUSANDS.match(str(g).strip()):
                    num = str(g).strip()
                    read_as = num.replace(".", ",", 1).rstrip("0").rstrip(",")
                    out.append(tr("«{}» is read as {} (the dot is the decimal separator). If you meant {}, type it without the dot.").format(
                        num, read_as, num.replace(".", "")))
            continue
        if kind != "text" or op not in ("eq", "ne", "in", "not_in"):
            continue
        known = values_for(name) or []
        known_set = set(known)
        for g in given:
            g = str(g)
            if g in known_set or g == sql_builder.NULL_VALUE:
                continue
            if g.strip() != g and g.strip() in known_set:
                out.append(tr("«{}» has spaces at the start or end; «{}» contains «{}».").format(
                    g, name, g.strip()))
                continue
            if not is_complete(name):
                continue
            similar = [k for k in known if _fold(k) == _fold(g)]
            if similar:
                out.append(tr("«{}» does not appear in «{}». Did you mean «{}»? («is equal to» is case- and accent-sensitive; «contains» is not).").format(g, name, similar[0]))
            else:
                out.append(tr("«{}» does not appear in the data of «{}».").format(g, name))
    seen, uniq = set(), []
    for w in out:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return uniq
