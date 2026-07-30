"""可视化 replay 溯源链路：空间分布 + 溯源 DAG。
Visualize a replay trace: spatial scatter + provenance flow diagram.

Usage: python -m track.v2.manual_thin_benchmark.visualize_replay
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D

# ── Config ────────────────────────────────────────────────────────────────
TRACE_FILE = Path("generated/replay_trace_i1.json")
OUT_DIR = Path("generated/replay_viz")
DPI = 150


# ── Color map per node kind ───────────────────────────────────────────────
KIND_COLORS = {
    "intersection":                    "#e41a1c",  # red
    "candidate_intersection":          "#ff7f00",  # orange
    "entry_exit_point":                "#377eb8",  # blue
    "candidate_entry_exit_point":      "#a6cee3",  # light blue
    "candidate_intersection_event":    "#984ea3",  # purple
    "turn_event":                      "#4daf4a",  # green
    "trajectory_point":                "#b3b3b3",  # grey
}

KIND_MARKERS = {
    "intersection":                    "s",
    "candidate_intersection":          "D",
    "entry_exit_point":                "^",
    "candidate_entry_exit_point":      "v",
    "candidate_intersection_event":    "X",
    "turn_event":                      "o",
    "trajectory_point":                ".",
}

KIND_SIZES = {
    "intersection":                    130,
    "candidate_intersection":          110,
    "entry_exit_point":                65,
    "candidate_entry_exit_point":      50,
    "candidate_intersection_event":    38,
    "turn_event":                      58,
    "trajectory_point":                5,
}

KIND_ZORDER = {
    "intersection":                    10,
    "candidate_intersection":          9,
    "entry_exit_point":                8,
    "candidate_entry_exit_point":      7,
    "candidate_intersection_event":    5,
    "turn_event":                      6,
    "trajectory_point":                1,
}

KIND_ORDER = [
    "intersection", "candidate_intersection",
    "entry_exit_point", "candidate_entry_exit_point",
    "candidate_intersection_event", "turn_event",
    "trajectory_point",
]


# ── Helpers ────────────────────────────────────────────────────────────────

def _node_center(n: dict) -> tuple[float, float] | None:
    d = n.get("data", {})
    geom = d.get("geom")
    if isinstance(geom, dict) and geom.get("type") == "Polygon":
        ring = geom["coordinates"][0]
        xs = [pt[0] for pt in ring]
        ys = [pt[1] for pt in ring]
        return (sum(xs) / len(xs), sum(ys) / len(ys))
    if isinstance(geom, dict) and geom.get("type") == "Point":
        c = geom["coordinates"]
        return (c[0], c[1])
    if "lon" in d and "lat" in d:
        return (d["lon"], d["lat"])
    if "crossing_lon" in d and "crossing_lat" in d:
        return (d["crossing_lon"], d["crossing_lat"])
    if "centroid_lon" in d and "centroid_lat" in d:
        return (d["centroid_lon"], d["centroid_lat"])
    return None


def _polygon_patch(coords: list, **kwargs) -> mpatches.Polygon:
    xy = [(pt[0], pt[1]) for pt in coords[0]]
    return mpatches.Polygon(xy, fill=False, **kwargs)


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(TRACE_FILE, "r", encoding="utf-8") as f:
        trace = json.load(f)

    nodes = trace["nodes"]
    edges = trace.get("edges", [])
    target = trace["target"]

    # Index
    node_index: dict[tuple[str, int], dict] = {}
    for n in nodes:
        node_index[(n["kind"], n["id"])] = n

    # ── Figure 1: Spatial scatter ────────────────────────────────────
    print("Building spatial view...")
    fig1, ax1 = plt.subplots(figsize=(16, 14))
    fig1.suptitle(
        f"Replay Trace — Spatial View\n"
        f"intersection #{target['id']}  |  "
        f"run {target['run_id'][:8]}…  |  "
        f"status={trace['status']}  |  "
        f"{len(nodes)} nodes",
        fontsize=11, fontfamily="monospace",
    )

    plotted: dict[str, list[tuple[float, float]]] = {k: [] for k in KIND_COLORS}
    polygons: list[tuple[list, str]] = []

    for n in nodes:
        kind = n["kind"]
        geom = n.get("data", {}).get("geom")
        if isinstance(geom, dict) and geom.get("type") == "Polygon":
            polygons.append((geom["coordinates"], kind))
        center = _node_center(n)
        if center is not None:
            plotted[kind].append(center)

    # Polygons (underneath)
    for coords, kind in polygons:
        ax1.add_patch(_polygon_patch(
            coords, edgecolor=KIND_COLORS[kind], linewidth=1.8,
            linestyle="--", alpha=0.65, zorder=KIND_ZORDER[kind],
        ))

    # Scatter
    legend_h = []
    for kind in KIND_ORDER:
        pts = plotted.get(kind, [])
        if not pts:
            continue
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        ax1.scatter(
            xs, ys, c=KIND_COLORS[kind], marker=KIND_MARKERS[kind],
            s=KIND_SIZES[kind], zorder=KIND_ZORDER[kind],
            alpha=0.75 if kind != "trajectory_point" else 0.35,
            edgecolors="none" if kind == "trajectory_point" else "face",
            linewidths=0.3,
        )
        legend_h.append(
            Line2D([0], [0], marker=KIND_MARKERS[kind], color="w",
                    markerfacecolor=KIND_COLORS[kind],
                    markersize=max(math.sqrt(KIND_SIZES[kind]) * 0.48, 4),
                    label=f"{kind} ({len(pts)})")
        )

    ax1.legend(handles=legend_h, loc="upper left", fontsize=8,
               framealpha=0.92, ncols=2)
    ax1.set_xlabel("Longitude")
    ax1.set_ylabel("Latitude")
    ax1.grid(True, alpha=0.25, linestyle=":")
    ax1.set_aspect(1.0 / math.cos(math.radians(34.88)))
    ax1.autoscale_view()

    out1 = OUT_DIR / "replay_spatial.png"
    fig1.savefig(out1, dpi=DPI, bbox_inches="tight")
    plt.close(fig1)
    print(f"  [OK] {out1}")

    # ── Figure 2: Provenance flow (Sankey-style layered diagram) ────
    print("Building provenance flow...")
    fig2, ax2 = plt.subplots(figsize=(20, 12))
    fig2.suptitle(
        f"Replay Trace — Provenance Flow  (depth 0 → leaf)\n"
        f"intersection #{target['id']}  |  run {target['run_id'][:8]}…  |  "
        f"{len(nodes)} nodes, {len(edges)} edges  |  status={trace['status']}",
        fontsize=11, fontfamily="monospace",
    )

    # Build adjacency
    adj: dict[tuple[str, int], list[tuple[str, int, str]]] = defaultdict(list)
    for e in edges:
        c = (e["child"]["kind"], e["child"]["id"])
        p = (e["parent"]["kind"], e["parent"]["id"])
        adj[c].append((*p, e["relation"]))
        # ensure non-leaf parents also appear as keys
        adj.setdefault((e["parent"]["kind"], e["parent"]["id"]), [])

    # Group nodes by depth
    depth_groups: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for n in nodes:
        depth_groups[n["depth"]].append((n["kind"], n["id"]))
    max_depth = max(depth_groups)

    # Within each depth, sort by kind then id
    for d in depth_groups:
        depth_groups[d].sort(key=lambda x: (KIND_ORDER.index(x[0]), x[1]))

    # Pre-compute y-positions per depth (stacked by kind for compactness)
    kind_height = 0.06
    depth_gap = 0.12  # vertical gap between kinds within same depth
    y_pos: dict[tuple[str, int], float] = {}
    next_y = 0.0
    for d in range(max_depth + 1):
        d_nodes = depth_groups[d]
        # Group by kind
        by_kind: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for nk in d_nodes:
            by_kind[nk[0]].append(nk)
        y_offset = next_y
        for kind in KIND_ORDER:
            knodes = by_kind.get(kind, [])
            if not knodes:
                continue
            n_count = len(knodes)
            for i, nk in enumerate(knodes):
                y_pos[nk] = y_offset + (n_count - 1 - i) * kind_height
            y_offset += n_count * kind_height + depth_gap
        next_y = y_offset

    total_height = next_y

    # Draw edges as bezier curves
    from matplotlib.patches import FancyArrowPatch
    import matplotlib.patches as mpatches_pkg

    REL_COLORS = {
        "candidate":        "#e41a1c",
        "member_event":     "#984ea3",
        "turn_event":       "#4daf4a",
        "entry":            "#377eb8",
        "exit":             "#ff7f00",
    }
    default_edge_colors = ["#cccccc", "#dddddd", "#eeeeee"]

    # Batch edges by relation for efficiency
    edge_batches: dict[str, list] = defaultdict(list)
    for e in edges:
        c = (e["child"]["kind"], e["child"]["id"])
        p = (e["parent"]["kind"], e["parent"]["id"])
        rel = e["relation"]
        if c in y_pos and p in y_pos:
            c_depth = node_index[c]["depth"]
            p_depth = node_index[p]["depth"]
            edge_batches[rel].append((c, p, c_depth, p_depth))

    # Draw
    for rel, batch in sorted(edge_batches.items()):
        color = REL_COLORS.get(rel, "#cccccc")
        alpha = 0.25 if "input" in rel else 0.45
        lw = 0.2 if "input" in rel else 0.6
        for c, p, cd, pd in batch:
            ax2.plot(
                [-cd, -pd], [y_pos[c], y_pos[p]],
                color=color, linewidth=lw, alpha=alpha, zorder=0,
            )

    # Draw nodes
    for kind in KIND_ORDER:
        xs, ys, szs = [], [], []
        for nk, y in y_pos.items():
            if nk[0] != kind:
                continue
            d = node_index[nk]["depth"]
            xs.append(-d)
            ys.append(y)
            # size proportional to importance
            szs.append(max(KIND_SIZES.get(kind, 30) * 0.2, 3))
        if not xs:
            continue
        ax2.scatter(xs, ys, c=KIND_COLORS[kind], s=szs,
                     marker=KIND_MARKERS[kind], zorder=5,
                     alpha=0.7, edgecolors="none")

    # Depth labels at top
    for d in range(max_depth + 1):
        count = len(depth_groups[d])
        ax2.text(-d, total_height + 0.15, f"depth {d}\n({count} nodes)",
                  fontsize=7, ha="center", va="bottom", color="#555555",
                  fontfamily="monospace")

    # Kind labels on right side
    # Compute average y per kind per depth for label placement
    for kind in KIND_ORDER:
        ys_kind = [y for nk, y in y_pos.items() if nk[0] == kind]
        if not ys_kind:
            continue
        mid = (min(ys_kind) + max(ys_kind)) / 2
        cnt = sum(1 for nk in y_pos if nk[0] == kind)
        ax2.text(1.2, mid, f"{kind}", fontsize=7, va="center",
                  color=KIND_COLORS[kind], fontfamily="monospace", fontweight="bold")
        ax2.text(1.2, mid - 0.08, f"({cnt})", fontsize=6, va="top",
                  color=KIND_COLORS[kind], fontfamily="monospace")

    # Relation legend
    rel_legend = []
    for rel, color in REL_COLORS.items():
        if rel in edge_batches:
            cnt = len(edge_batches[rel])
            rel_legend.append(Line2D([0], [0], color=color, linewidth=2,
                                      label=f"{rel} ({cnt})"))
    # "input" edges
    input_cnt = sum(len(v) for k, v in edge_batches.items() if "input" in k)
    if input_cnt:
        rel_legend.append(Line2D([0], [0], color="#cccccc", linewidth=1,
                                  alpha=0.4, label=f"input_from_N ({input_cnt})"))

    ax2.legend(handles=rel_legend, loc="lower left", fontsize=7,
               framealpha=0.9, title="Edge relations", title_fontsize=8)

    ax2.set_xlim(-max_depth - 0.8, 3.5)
    ax2.set_ylim(-0.5, total_height + 0.8)
    ax2.axis("off")

    out2 = OUT_DIR / "replay_flow.png"
    fig2.savefig(out2, dpi=DPI, bbox_inches="tight")
    plt.close(fig2)
    print(f"  [OK] {out2}")

    # ── Figure 3: Kind/depth summary bar chart ───────────────────────
    print("Building summary chart...")
    fig3, ax3 = plt.subplots(figsize=(14, 6))
    fig3.suptitle(
        f"Replay Trace — Node Distribution by Depth & Kind\n"
        f"intersection #{target['id']}  |  run {target['run_id'][:8]}…  |  "
        f"status={trace['status']}",
        fontsize=10, fontfamily="monospace",
    )

    # Build matrix: depth × kind
    depths = list(range(max_depth + 1))
    matrix = {kind: [0] * len(depths) for kind in KIND_ORDER}
    for n in nodes:
        matrix[n["kind"]][n["depth"]] += 1

    bottom = [0.0] * len(depths)
    x = list(range(len(depths)))
    bar_width = 0.65
    for kind in KIND_ORDER:
        counts = matrix[kind]
        if sum(counts) == 0:
            continue
        bars = ax3.bar(x, counts, bar_width, bottom=bottom,
                        color=KIND_COLORS[kind], label=f"{kind} ({sum(counts)})",
                        edgecolor="white", linewidth=0.5)
        # Annotate non-zero bars
        for i, (c, b) in enumerate(zip(counts, bars)):
            if c > 0:
                ax3.text(b.get_x() + b.get_width() / 2,
                          b.get_y() + b.get_height() / 2,
                          str(c), ha="center", va="center", fontsize=7,
                          color="white" if c > 20 else "black")
        for i in range(len(depths)):
            bottom[i] += counts[i]

    ax3.set_xticks(x)
    ax3.set_xticklabels([f"depth {d}" for d in depths])
    ax3.set_ylabel("Node Count")
    ax3.legend(loc="upper right", fontsize=8, framealpha=0.9, ncols=2)
    ax3.grid(axis="y", alpha=0.3)

    out3 = OUT_DIR / "replay_summary.png"
    fig3.savefig(out3, dpi=DPI, bbox_inches="tight")
    plt.close(fig3)
    print(f"  [OK] {out3}")

    # ── Figure 4: Turn angle distribution ────────────────────────────
    print("Building turn angle distribution...")
    turn_degs = []
    eep_degs = []
    for n in nodes:
        d = n.get("data", {})
        if n["kind"] == "turn_event" and "turn_deg" in d:
            turn_degs.append(d["turn_deg"])
        elif n["kind"] == "entry_exit_point" and "turn_deg" in d:
            eep_degs.append(d["turn_deg"])

    if turn_degs or eep_degs:
        fig4, (ax4a, ax4b) = plt.subplots(1, 2, figsize=(14, 5))
        fig4.suptitle(
            f"Replay Trace — Turn Angle Distributions\n"
            f"intersection #{target['id']}  |  run {target['run_id'][:8]}…",
            fontsize=10, fontfamily="monospace",
        )

        if turn_degs:
            ax4a.hist(turn_degs, bins=36, color=KIND_COLORS["turn_event"],
                       edgecolor="white", alpha=0.85)
            ax4a.set_title(f"turn_event turn_deg (n={len(turn_degs)})", fontsize=9)
            ax4a.set_xlabel("Turn Angle (°)")
            ax4a.set_ylabel("Count")
            ax4a.grid(axis="y", alpha=0.3)
            ax4a.axvline(x=90, color="red", linestyle="--", alpha=0.4, linewidth=1)
            ax4a.axvline(x=180, color="red", linestyle="--", alpha=0.4, linewidth=1)

        if eep_degs:
            ax4b.hist(eep_degs, bins=36, color=KIND_COLORS["entry_exit_point"],
                       edgecolor="white", alpha=0.85)
            ax4b.set_title(f"entry_exit_point turn_deg (n={len(eep_degs)})", fontsize=9)
            ax4b.set_xlabel("Turn Angle (°)")
            ax4b.set_ylabel("Count")
            ax4b.grid(axis="y", alpha=0.3)
            ax4b.axvline(x=90, color="red", linestyle="--", alpha=0.4, linewidth=1)
            ax4b.axvline(x=180, color="red", linestyle="--", alpha=0.4, linewidth=1)

        out4 = OUT_DIR / "replay_turn_angles.png"
        fig4.savefig(out4, dpi=DPI, bbox_inches="tight")
        plt.close(fig4)
        print(f"  [OK] {out4}")

    # ── Console summary ──────────────────────────────────────────────
    print(f"\nDone. {len(nodes)} nodes, {len(edges)} edges → {OUT_DIR.resolve()}/")
    for kind in KIND_ORDER:
        cnt = sum(1 for n in nodes if n["kind"] == kind)
        if cnt:
            depths = sorted(set(n["depth"] for n in nodes if n["kind"] == kind))
            print(f"  {kind:35s} {cnt:5d}  depths={depths}")


if __name__ == "__main__":
    main()
