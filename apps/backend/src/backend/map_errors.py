"""Errors raised while reading persisted map and replay data."""


class MapError(Exception):
    """Base for map-domain errors translated by the HTTP routes."""


class MapNotFoundError(MapError):
    """A requested persisted map entity or run does not exist."""


class MapDataError(MapError):
    """Persisted map data cannot satisfy a request consistently."""


class MapDatabaseNotFoundError(MapNotFoundError):
    def __init__(self, requested_database: str, current_database: str) -> None:
        self.requested_database = requested_database
        self.current_database = current_database
        super().__init__(
            f"所选图层数据库 {requested_database!r} 在后端 PostgreSQL 中不存在。"
            f"后端当前数据库为 {current_database!r}，请确认 QGIS 图层的 database 是否对应。"
        )


class MapSchemaMismatchError(MapDataError):
    def __init__(self, detail: str | dict[str, object]) -> None:
        self.detail = detail
        message = detail if isinstance(detail, str) else str(detail.get("message", detail))
        super().__init__(message)
