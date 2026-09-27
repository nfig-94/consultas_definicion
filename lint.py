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
_COMMA_THOUSANDS = re.compile(r"^[+-]?\d{1,3},\d{3}$")  # «1,500»: read as 1.5
_DMY = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})")  # day/month/year typed by hand


def _fold(text):
    """Lowercase, without accents (for «did you mean…?» suggestions)."""
    t = unicodedata.normalize("NFD", str(text).strip().lower())
    return "".join(ch for ch in t if unicodedata.category(ch) != "Mn")


def case_clash(layer, op, chosen, data_values):
    """Long «is one of» lists in GDAL formats ignore upper/lower case (see sql_builder):
    a value of the data that differs from a chosen one only in case, or None."""
    if op not in ("in", "not_in") or sql_builder.dialect_of(layer) != "ogrsql":
        return None
    lettered = [str(c) for c in chosen if sql_builder.has_ascii_letters(c)]
    if len(lettered) <= sql_builder.MAX_EXACT_LIST:
        return None
    chosen_set = {str(c) for c in chosen}
    folded = {sql_builder.ascii_fold(c) for c in lettered}
    for v in data_values or []:
        if v in sql_builder.SPECIAL_VALUES or v is None:
            continue
        v = str(v)
        if v not in chosen_set and sql_builder.ascii_fold(v) in folded:
            return v
    return None


def mixes_and_or(clauses):
    """True if, at some level (outside or inside a group), AND and OR are both used without
    a group telling which goes first. AND is then applied first, as in SQL, which is often
    not what was meant: «Asia OR Europe AND more than 50 M» keeps all of Asia."""
    def walk(children):
        used = set()
        for i, ch in enumerate(children):
            if "children" in ch and walk(ch["children"]):
                return True
            if i > 0:
                used.add(sql_builder.connector_of(ch["leaf"] if "leaf" in ch else sql_builder.first_leaf(ch)))
        return len(used) > 1
    return walk(sql_builder.build_tree(clauses, sql_builder.clause_path))


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
        elif op in ("between", "not_between"):
            given = [c.get("value"), c.get("value2")]
        else:
            continue
        given = [g for g in given if g is not None and str(g) != ""]
        for special in sql_builder.SPECIAL_VALUES:  # <Null> / <Empty> picked from a list
            if special in given:
                if special not in (values_for(name) or []) and is_complete(name):
                    out.append(tr("«{}» does not appear in the data of «{}».").format(
                        sql_builder.display_value(special), name))
                given = [g for g in given if g != special]
        if kind == "text" and any(str(g).strip() in ("''", '""') for g in given):
            out.append(tr("To find empty texts, pick «{}» from the list, or use «is blank» (empty or null).").format(
                sql_builder.empty_label()))
        if not given:
            continue
        if kind in ("date", "datetime"):
            for g in given:
                m = _DMY.match(str(g).strip())
                if m and int(m.group(1)) <= 12 and int(m.group(2)) <= 12 and m.group(1) != m.group(2):
                    iso = "{}-{:02d}-{:02d}".format(m.group(3), int(m.group(2)), int(m.group(1)))
                    out.append(tr("«{}» is read as {} (day/month/year). If you meant month/day/year, type the date as YYYY-MM-DD.").format(str(g).strip(), iso))
            continue
        if kind == "num":
            for g in given:
                if _COMMA_THOUSANDS.match(str(g).strip()):
                    num = str(g).strip()
                    out.append(tr("«{}» is read as {} (the comma is taken as the decimal separator). If you meant {}, type it without the comma.").format(
                        num, num.replace(",", ".").rstrip("0").rstrip("."), num.replace(",", "")))
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
        clash = case_clash(layer, op, given, known)
        if clash:
            out.append(tr("With more than {} values, this format does not tell upper and lower case apart: "
                          "«{}» also counts as chosen.").format(sql_builder.MAX_EXACT_LIST, clash))
        for g in given:
            g = str(g)
            if g in known_set:
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
    if mixes_and_or(clauses):
        out.append(tr("AND and OR are mixed at the same level: AND is applied first (see the parentheses "
                      "in the preview). If that is not what you want, tick the clauses that go together "
                      "and group them."))
    seen, uniq = set(), []
    for w in out:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return uniq
