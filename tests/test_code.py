"""Checks on the plugin code that do not need QGIS:

* every text shown in the interface (tr("...")) has a Spanish and a Portuguese translation,
  and each translation keeps the same {} placeholders (otherwise .format() would fail);
* the code parses as Python 3.9 (the oldest Python used by QGIS 3.34 builds).

    python tests/test_code.py
"""
import ast
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
FAILS = 0


def check(name, cond, extra=""):
    global FAILS
    print(("ok    " if cond else "FAIL  ") + name, extra)
    FAILS += 0 if cond else 1


ns = {}
with open(os.path.join(PKG, "i18n_data.py"), encoding="utf-8") as fh:
    exec(fh.read(), ns)
tables = {"ES": ns["ES"], "PT": ns["PT"]}

used = set()
for path in glob.glob(os.path.join(PKG, "*.py")):
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    try:
        tree = ast.parse(src, feature_version=(3, 9))
        ok = True
    except SyntaxError as e:
        ok, tree = False, ast.parse(src)
        print("   ", e)
    check("Python 3.9 syntax: " + os.path.basename(path), ok)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "tr" and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            used.add(node.args[0].value)

holes = re.compile(r"\{[^{}]*\}")
for lang, table in tables.items():
    missing = sorted(t for t in used if t not in table)
    check("{}: every interface text is translated".format(lang), not missing, missing[:3])
    unused = sorted(k for k in table if k not in used)
    check("{}: no leftover translations".format(lang), not unused, unused[:3])
    bad = [k for k, v in table.items() if sorted(holes.findall(k)) != sorted(holes.findall(v))]
    check("{}: same {{}} placeholders as the English text".format(lang), not bad, bad[:3])

print("\nFAILS:", FAILS)
sys.exit(1 if FAILS else 0)
