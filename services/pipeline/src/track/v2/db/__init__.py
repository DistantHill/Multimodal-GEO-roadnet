"""Database infrastructure layer for track.v2 pipeline stages.

ZH: 基建层——存放各 pipeline stage 的数据库查询，与业务逻辑分离。
    上层 (clip.py, intersect2.py, replay.py) 只做组装，不写 SQL。
EN: Infrastructure layer — database queries for each pipeline stage,
    separated from business logic. Upper layers (clip.py, intersect2.py,
    replay.py) only orchestrate; they never write SQL.
"""
