"""轨迹可视化工具 / Trajectory visualization utility.

plot_traj(points, *, path=False, arrow=False) — 将轨迹点绘制在 lon/lat
坐标平面上，可选绘制连线路径和方向箭头。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import matplotlib.pyplot as plt
import numpy as np

from .models import (
    EntryExitObservation,
    HotspotCluster,
    IntersectionTopology,
    TrajectoryPoint,
)


def plot_traj(
    points: Sequence[TrajectoryPoint],
    *,
    path: bool = False,
    arrow: bool = False,
    show: bool = False,
    ax: plt.Axes | None = None,
    color: str = "steelblue",
    marker_size: float = 12,
    title: str | None = None,
) -> plt.Axes:
    """Plot trajectory points on a lon/lat scatter plot.

    Args:
        points: TrajectoryPoint 序列（至少 1 个点）。
        path: True 则按时间顺序连线。
        arrow: True 则在每个线段中点绘制方向箭头。
        show: True 则调用 plt.show() 并等待 Enter 后自动 close。
        ax: 可选，在已有 Axes 上绘制而非创建新 Figure。
        color: 点和线的颜色。
        marker_size: 散点大小。
        title: 图表标题。

    Returns:
        matplotlib Axes 对象。
    """
    if ax is None:
        _, ax = plt.subplots(figsize=(10, 8))

    pts = sorted(points, key=lambda p: p.timestamp)
    lons = np.array([p.lon for p in pts], dtype=float)
    lats = np.array([p.lat for p in pts], dtype=float)

    # ── 散点 ────────────────────────────────────────────────────
    ax.scatter(lons, lats, c=color, s=marker_size, zorder=3, alpha=0.85)

    # ── 连线 ────────────────────────────────────────────────────
    if path and len(pts) >= 2:
        ax.plot(lons, lats, color=color, linewidth=0.8, alpha=0.5, zorder=1)

    # ── 箭头 ────────────────────────────────────────────────────
    if arrow and len(pts) >= 2:
        arrow_spacing = max(1, len(pts) // 30)  # 最多约 30 个箭头
        for i in range(0, len(pts) - 1, arrow_spacing):
            mid_lon = (lons[i] + lons[i + 1]) / 2.0
            mid_lat = (lats[i] + lats[i + 1]) / 2.0
            dlon = lons[i + 1] - lons[i]
            dlat = lats[i + 1] - lats[i]
            norm = math.hypot(dlon, dlat)
            if norm < 1e-12:
                continue
            dlon /= norm
            dlat /= norm
            # 等比例坐标下的箭头长度
            span_lon = lons.max() - lons.min() or 1.0
            span_lat = lats.max() - lats.min() or 1.0
            arrow_len = min(span_lon, span_lat) * 0.015
            ax.arrow(
                mid_lon - dlon * arrow_len / 2,
                mid_lat - dlat * arrow_len / 2,
                dlon * arrow_len,
                dlat * arrow_len,
                head_width=arrow_len * 0.6,
                head_length=arrow_len * 0.4,
                fc=color,
                ec=color,
                alpha=0.6,
                zorder=2,
                length_includes_head=True,
            )

    # ── 标注首尾 ────────────────────────────────────────────────
    if len(pts) >= 1:
        ax.annotate(
            "S",
            xy=(lons[0], lats[0]),
            fontsize=10,
            fontweight="bold",
            color="green",
            ha="right",
            va="bottom",
        )
    if len(pts) >= 2:
        ax.annotate(
            "E",
            xy=(lons[-1], lats[-1]),
            fontsize=10,
            fontweight="bold",
            color="red",
            ha="left",
            va="top",
        )

    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_aspect("equal")
    if title:
        ax.set_title(title)
    elif len(pts) >= 1:
        vid = pts[0].vehicle_id
        ax.set_title(f"Trajectory — {vid}  ({len(pts)} pts)")

    if show:
        plt.show(block=False)
        input("Press Enter to close...")
        plt.close(ax.figure)

    return ax


def plot_multi_traj(
    tracks: Sequence[Sequence[TrajectoryPoint]],
    *,
    path: bool = True,
    arrow: bool = False,
    show: bool = False,
    title: str | None = None,
) -> plt.Axes:
    """Plot multiple trajectories on the same axes with distinct colors.

    Args:
        tracks: 多个轨迹的列表，每个轨迹是 TrajectoryPoint 序列。
        path: True 则按时间顺序连线。
        arrow: True 则绘制方向箭头。
        show: True 则调用 plt.show() 并等待 Enter 后自动 close。
        title: 图表标题。

    Returns:
        matplotlib Axes 对象。
    """
    _, ax = plt.subplots(figsize=(12, 10))
    colors = plt.cm.tab20(np.linspace(0, 1, max(1, len(tracks))))

    for idx, points in enumerate(tracks):
        color = colors[idx % len(colors)]
        plot_traj(
            points,
            path=path,
            arrow=arrow,
            ax=ax,
            color=(
                f"C{idx % 10}"
                if len(tracks) <= 10
                else color
            ),
            marker_size=6,
        )

    if title:
        ax.set_title(title)
    else:
        ax.set_title(f"{len(tracks)} trajectories")

    if show:
        plt.show(block=False)
        input("Press Enter to close...")
        plt.close(ax.figure)

    return ax


# ═══════════════════════════════════════════════════════════════════════
# Intersection visualization
# ═══════════════════════════════════════════════════════════════════════

_SOURCE_COLORS: dict[str, str] = {
    "turn":                    "steelblue",
    "orthogonal_cross_track":  "darkorange",
    "merged":                  "mediumpurple",
}


def _circle_lonlat(
    center_lon: float,
    center_lat: float,
    radius_m: float,
    n: int = 64,
) -> tuple[list[float], list[float]]:
    """Approximate a metre-radius circle on a lon/lat plane.

    Returns ``(lons, lats)`` vertex lists suitable for ``ax.plot()``.
    """
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * math.cos(math.radians(center_lat)))
    angles = np.linspace(0, 2 * math.pi, n)
    lons = center_lon + dlon * np.cos(angles)
    lats = center_lat + dlat * np.sin(angles)
    return lons.tolist(), lats.tolist()


def _branch_point(
    center_lon: float,
    center_lat: float,
    radius_m: float,
    angle_deg: float,
) -> tuple[float, float]:
    """Point on the circle boundary at *angle_deg* (compass bearing from centre)."""
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * math.cos(math.radians(center_lat)))
    rad = math.radians(angle_deg)
    lon = center_lon + dlon * math.sin(rad)
    lat = center_lat + dlat * math.cos(rad)
    return lon, lat


def plot_intersection(
    cluster: HotspotCluster,
    *,
    topology: IntersectionTopology | None = None,
    observations: Sequence[EntryExitObservation] = (),
    branch: bool = False,
    is_complete: bool | None = None,
    show: bool = False,
    ax: plt.Axes | None = None,
    title: str | None = None,
) -> plt.Axes:
    """Draw one intersection candidate.

    Args:
        cluster: HotspotCluster to draw (centroid + radius circle).
        topology: Optional IntersectionTopology for branch-centre arcs.
        observations: EntryExitObservations for this cluster (entry/exit dots).
        branch: If True and *topology* is given, draw branch-direction arrows
                and entry (green) / exit (red) crossing points.
        is_complete: Override completeness for alpha blending.
                     ``None`` reads from ``topology.is_complete``.
        show: If True, call ``plt.show()`` and wait for Enter.
        ax: Optional existing Axes.
        title: Optional chart title.

    Returns:
        matplotlib Axes.
    """
    if ax is None:
        _, ax = plt.subplots(figsize=(10, 8))

    color = _SOURCE_COLORS.get(cluster.source, "gray")

    # Completeness → alpha
    if is_complete is None:
        is_complete = topology.is_complete if topology else True
    alpha = 0.85 if is_complete else 0.45

    # ── radius circle ──────────────────────────────────────────
    clons, clats = _circle_lonlat(cluster.centroid_lon, cluster.centroid_lat, cluster.radius_m)
    ax.plot(clons, clats, color=color, linestyle="--", linewidth=0.8, alpha=alpha, zorder=1)

    # ── centroid ───────────────────────────────────────────────
    ax.scatter(
        cluster.centroid_lon, cluster.centroid_lat,
        c=color, s=60, marker="o", zorder=4, alpha=alpha,
        edgecolors="white", linewidths=0.5,
    )
    ax.annotate(
        f"#{cluster.cluster_id}",
        xy=(cluster.centroid_lon, cluster.centroid_lat),
        fontsize=7, color=color, alpha=alpha,
        ha="center", va="bottom", xytext=(0, 4), textcoords="offset points",
    )

    # ── branch arrows + entry/exit dots ────────────────────────
    if branch and topology is not None:
        arrow_len_deg = cluster.radius_m / 111_320.0 * 0.6

        # entry (green) / exit (red) crossing points
        for obs in observations:
            if obs.cluster_id != cluster.cluster_id:
                continue
            elon, elat = _branch_point(cluster.centroid_lon, cluster.centroid_lat,
                                       cluster.radius_m, obs.entry_branch_angle)
            ax.scatter(elon, elat, c="green", s=18, marker="o", zorder=5, alpha=0.9,
                       edgecolors="white", linewidths=0.3)
            xlon, xlat = _branch_point(cluster.centroid_lon, cluster.centroid_lat,
                                       cluster.radius_m, obs.exit_branch_angle)
            ax.scatter(xlon, xlat, c="red", s=18, marker="o", zorder=5, alpha=0.9,
                       edgecolors="white", linewidths=0.3)

        # branch-centre arrows (centre → boundary)
        for angle in topology.branch_centers_deg:
            blon, blat = _branch_point(
                cluster.centroid_lon, cluster.centroid_lat,
                cluster.radius_m * 0.35, angle,
            )
            elon, elat = _branch_point(
                cluster.centroid_lon, cluster.centroid_lat,
                cluster.radius_m, angle,
            )
            dlon = elon - cluster.centroid_lon
            dlat = elat - cluster.centroid_lat
            ax.arrow(
                blon, blat, dlon * 0.55, dlat * 0.55,
                head_width=arrow_len_deg * 0.5,
                head_length=arrow_len_deg,
                fc=color, ec=color, alpha=alpha * 0.8,
                zorder=3, length_includes_head=True,
            )

    # ── labels ─────────────────────────────────────────────────
    source_label = cluster.source if cluster.source != "merged" else "turn+ortho"
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_aspect("equal")
    if title:
        ax.set_title(title)
    else:
        status = "✓" if is_complete else "✗"
        ax.set_title(
            f"Cluster #{cluster.cluster_id}  [{source_label}]  {status}  "
            f"r={cluster.radius_m:.0f}m  events={len(cluster.events)}"
        )

    if show:
        plt.show(block=False)
        input("Press Enter to close...")
        plt.close(ax.figure)

    return ax


def plot_intersections(
    clusters: Sequence[HotspotCluster],
    *,
    topologies: Sequence[IntersectionTopology] = (),
    observations_by_cluster: dict[int, list[EntryExitObservation]] | None = None,
    branch: bool = False,
    all_clusters: bool = True,
    show: bool = False,
    ax: plt.Axes | None = None,
    title: str | None = None,
) -> plt.Axes:
    """Draw multiple intersection candidates, colour-coded by source.

    Args:
        clusters: HotspotCluster list.
        topologies: IntersectionTopology list (matched by ``cluster_id``).
        observations_by_cluster: ``{cluster_id: [EntryExitObservation, …]}``.
        branch: If True, draw branch arrows and entry/exit dots.
        all_clusters: If True, draw all topologies. If False, only draw
                      those with ``is_complete == True``.
        show: If True, call ``plt.show()`` and wait for Enter.
        ax: Optional existing Axes.
        title: Optional chart title.

    Returns:
        matplotlib Axes.
    """
    if ax is None:
        _, ax = plt.subplots(figsize=(12, 10))

    # Index topologies by cluster_id
    topo_by_id: dict[int, IntersectionTopology] = {t.cluster_id: t for t in topologies}
    obs_by_id = observations_by_cluster or {}

    # Filter
    if all_clusters:
        draw_clusters = list(clusters)
    else:
        complete_ids = {t.cluster_id for t in topologies if t.is_complete}
        draw_clusters = [c for c in clusters if c.cluster_id in complete_ids]

    for c in draw_clusters:
        topo = topo_by_id.get(c.cluster_id)
        obs = obs_by_id.get(c.cluster_id, [])
        plot_intersection(
            c,
            topology=topo,
            observations=obs,
            branch=branch,
            is_complete=topo.is_complete if topo else None,
            ax=ax,
        )

    # Legend
    from matplotlib.lines import Line2D

    legend_handles = [
        Line2D([0], [0], marker="o", color="w", markerfacecolor="steelblue",
               markersize=8, label="turn events"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="darkorange",
               markersize=8, label="orthogonal cross-track"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="mediumpurple",
               markersize=8, label="merged"),
    ]
    ax.legend(handles=legend_handles, loc="upper right", fontsize=8)

    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_aspect("equal")
    n_complete = sum(1 for t in topologies if t.is_complete)
    if title:
        ax.set_title(title)
    else:
        ax.set_title(
            f"{len(draw_clusters)} intersections "
            f"({n_complete} complete, {len(draw_clusters) - n_complete} partial)"
        )

    if show:
        plt.show(block=False)
        input("Press Enter to close...")
        plt.close(ax.figure)

    return ax
