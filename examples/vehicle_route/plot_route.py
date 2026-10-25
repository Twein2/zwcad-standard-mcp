"""Preview a planned route (boundary, drawn paths, car outlines) as PNG."""
import json, math, sys
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import plan_route as pr

inp, route, png = sys.argv[1:4]
d, r = json.load(open(inp, encoding="utf-8")), json.load(open(route, encoding="utf-8"))
car = pr.Car.from_vehicle(pr.CAYENNE_E3, d.get("units", "cm"))
ol = car.outline(10)
fig, ax = plt.subplots(figsize=(7, 11))
P = d["limits"] + [d["limits"][0]]
ax.plot(*zip(*P), "k-", lw=1)
poses = r["poses"]
for q in poses[:: max(1, len(poses) // 40)] + [poses[-1]]:
    ax.plot(*pr.transform(ol, *q).T, ",", color="0.6")
ax.plot([q[0] for q in poses], [q[1] for q in poses], "b-", lw=0.8, label="rear axle (replay)")
for s, c in zip(r["segments"], ("g", "r", "m", "c")):
    v = np.array(s["vertices"])[:, :2]
    ax.plot(*v.T, "o--", color=c, ms=3, lw=0.8, label=f'{s["gear"]}: {s["path"]}')
for k, c in (("start", "g"), ("end", "r")):
    (a, b) = d[k]; ax.annotate("", xy=b, xytext=a, arrowprops=dict(color=c, width=1.5))
ax.set_aspect("equal"); ax.legend(loc="lower left", fontsize=7)
ax.set_title(f'gear changes {r["gear_changes"]}, length {r["total_length"]}, min clearance {r["min_clearance"]}')
fig.savefig(png, dpi=90, bbox_inches="tight")
