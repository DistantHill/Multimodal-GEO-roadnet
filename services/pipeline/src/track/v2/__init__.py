"""track.v2 — Step-based trajectory pipeline.

Architecture (linear):
    Parser 0  →  Cleaner 1  →  Intersect2  →  Clip  →  Fitting  →  Network

Each stage has a single public entry point and produces frozen intermediate
artifacts. No stage mutates another stage's output.

Intersect2 uses two complementary methods:
  1. Turn events: bearing-change detection → Getis-Ord G* → DBSCAN
  2. Orthogonal candidates: Cleaner 1 injection points → DBSCAN
Both feed into the same topology verification (entry/exit → branch clustering).
"""

from .cleaner1 import clean
from .intersect2 import IntersectConfig, IntersectResult, detect_intersections
from .parser0 import get_database

