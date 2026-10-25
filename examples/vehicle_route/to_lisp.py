"""Turn a planned route (plan_route.py output) into one AutoLISP expression for execute_lisp.

Creates the route layer if missing and one LWPOLYLINE (with bulges) per gear; returns the handles.
"""
import json, sys

route = json.load(open(sys.argv[1], encoding="utf-8"))
layer = sys.argv[2] if len(sys.argv) > 2 else "Cayenne route"
color = {"forward": 3, "reverse": 1}

def num(v):
    return repr(float(v))

parts = []
for s in route["segments"]:
    verts = " ".join(f"(10 {num(x)} {num(y)}) (42 . {num(b)})" for x, y, b in s["vertices"])
    parts.append(
        f'(entmake (list (cons 0 "LWPOLYLINE") (cons 100 "AcDbEntity") (cons 8 lay) (cons 62 {color[s["gear"]]}) '
        f'(cons 100 "AcDbPolyline") (cons 90 {len(s["vertices"])}) (cons 70 0) '
        + " ".join(f"(list 10 {num(x)} {num(y)}) (cons 42 {num(b)})" for x, y, b in s["vertices"])
        + f')) (setq out (cons (list (cons "gear" "{s["gear"]}") (cons "handle" (cdr (assoc 5 (entget (entlast)))))) out))'
    )
code = (f'(progn (setq lay "{layer}" out nil) '
        f'(if (not (tblsearch "LAYER" lay)) (entmake (list (cons 0 "LAYER") (cons 100 "AcDbSymbolTableRecord") '
        f'(cons 100 "AcDbLayerTableRecord") (cons 2 lay) (cons 70 0) (cons 62 4) (cons 6 "Continuous")))) '
        + " ".join(parts) + " (reverse out))")
print(code)
