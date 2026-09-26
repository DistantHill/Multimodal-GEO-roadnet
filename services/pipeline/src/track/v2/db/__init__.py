"""Database infrastructure layer for track.v2 pipeline stages.

ZH: 基建层——存放 Pipeline 阶段可复用的数据库查询与 schema 契约。
    Clip、Detect 和 validation.replay_lineage 等上层按职责组织数据访问。
EN: Infrastructure layer — reusable database queries and schema contracts
    for Pipeline stages. Clip, Detect, and validation.replay_lineage organize
    data access at their respective boundaries.
"""
