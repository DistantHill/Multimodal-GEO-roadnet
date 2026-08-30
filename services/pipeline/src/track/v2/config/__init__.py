"""Shared PostgreSQL connection config for track.v2 CLI modules.

ZH: 所有 CLI 模块共用的数据库连接配置。默认值从 ``track/v2/.env`` 读取，
    避免在每个 CLI 中硬编码敏感信息。

EN: Shared database connection config used by all CLI modules. Defaults are
    read from ``track/v2/.env`` so sensitive info isn't hardcoded in every CLI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus


# ── .env loading ───────────────────────────────────────────────────────

def _find_env_path() -> Path | None:
    """Locate the .env file relative to this config module.

    ZH: 从 config 模块所在目录（track/v2/config/）的父目录查找 .env。
    EN: Look for .env in the parent directory of this config module.
    """
    # config/__init__.py → track/v2/config/ → parent = track/v2/
    candidate = Path(__file__).resolve().parent.parent / ".env"
    return candidate if candidate.is_file() else None


def load_dotenv(env_path: str | Path | None = None) -> dict[str, str]:
    """Parse a .env file and return a dict of KEY=VALUE pairs.

    ZH: 解析 .env 文件，返回 KEY=VALUE 字典。忽略空行和 ``#`` 注释。
        值中的引号会被自动剥离。如果文件不存在则返回空字典。

    EN: Parse a .env file into a KEY=VALUE dict. Blank lines and ``#``
        comments are ignored. Quotes are stripped from values. Returns
        an empty dict if the file is missing.
    """
    if env_path is None:
        found = _find_env_path()
        if found is None:
            return {}
        env_path = found
    elif not Path(env_path).is_file():
        return {}

    result: dict[str, str] = {}
    with open(env_path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            result[key] = value
    return result


# ── PostgresConfig ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class PostgresConfig:
    """PostgreSQL connection parameters.

    ZH: 默认值从 ``track/v2/.env`` 读取；运行时可通过 CLI 参数覆盖。
    EN: Defaults are read from ``track/v2/.env``; overridable via CLI args.
    """

    host: str = "127.0.0.1"
    port: int = 5432
    database: str = "postgres"
    user: str = "postgres"
    password: str = ""
    connect_timeout: int = 10
    sslmode: str | None = None
    # ZH: TCP keepalive 参数 — 防止防火墙/NAT 断开长时间运行的 named cursor 连接。
    # EN: TCP keepalive params — prevent firewall/NAT from dropping long-running named-cursor connections.
    keepalives: bool = True
    keepalives_idle: int = 60       # 空闲 60s 后开始发 keepalive 探测
    keepalives_interval: int = 10   # 探测间隔 10s
    keepalives_count: int = 5       # 最多 5 次探测失败后断连
    # ZH: TCP user timeout (PG 12+) — 在 TCP 层面定义数据包无 ACK 确认的最长等待时间。
    # EN: TCP user timeout (PG 12+) — max time to wait for unacknowledged data at TCP level.
    tcp_user_timeout: int = 30000   # ms

    def _build_query(self) -> str:
        """Build the query-string portion shared by dsn() and safe_dsn()."""
        parts = [f"connect_timeout={self.connect_timeout}"]
        if self.sslmode:
            parts.append(f"sslmode={quote_plus(self.sslmode)}")
        if self.keepalives:
            parts.append("keepalives=1")
            parts.append(f"keepalives_idle={self.keepalives_idle}")
            parts.append(f"keepalives_interval={self.keepalives_interval}")
            parts.append(f"keepalives_count={self.keepalives_count}")
        if self.tcp_user_timeout:
            parts.append(f"tcp_user_timeout={self.tcp_user_timeout}")
        return "&".join(parts)

    def dsn(self) -> str:
        """Build a psycopg connection DSN."""
        auth = quote_plus(self.user)
        if self.password:
            auth += f":{quote_plus(self.password)}"
        host = quote_plus(self.host)
        database = quote_plus(self.database)
        query = self._build_query()
        return f"postgresql://{auth}@{host}:{self.port}/{database}?{query}"

    def safe_dsn(self) -> str:
        """Build a DSN with password masked for logging."""
        auth = quote_plus(self.user)
        if self.password:
            auth += ":***"
        host = quote_plus(self.host)
        database = quote_plus(self.database)
        query = self._build_query()
        return f"postgresql://{auth}@{host}:{self.port}/{database}?{query}"


def connect(config: PostgresConfig):
    """Open a psycopg connection using the given config.

    ZH: 打开 PostgreSQL 连接。psycopg 不可用时给出安装提示。
    """
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PostgreSQL support requires psycopg. Install with: pip install 'psycopg[binary]'"
        ) from exc
    return psycopg.connect(config.dsn())


# ── Env-based default config ───────────────────────────────────────────

def default_postgres_config(env_path: str | Path | None = None) -> PostgresConfig:
    """Build a PostgresConfig from process environment and legacy .env values.

    ZH: Docker/服务器环境优先读取进程环境变量，并兼容原有
        ``TRACK_PG_*`` 命名及包内 .env 文件。CLI 仍可覆盖这些默认值。

    EN: Process variables take precedence for container deployments. Legacy
        ``TRACK_PG_*`` names and the package-local .env file remain supported.
    """
    file_env = load_dotenv(env_path)

    def value(legacy_key: str, standard_key: str | None, default: str) -> str:
        keys = (legacy_key, standard_key) if standard_key else (legacy_key,)
        for source in (os.environ, file_env):
            for key in keys:
                if key and key in source:
                    return source[key]
        return default

    return PostgresConfig(
        host=value("TRACK_PG_HOST", "POSTGRES_HOST", "127.0.0.1"),
        port=int(value("TRACK_PG_PORT", "POSTGRES_PORT", "5432")),
        database=value("TRACK_PG_DATABASE", "POSTGRES_DB", "track_test"),
        user=value("TRACK_PG_USER", "POSTGRES_USER", "postgres"),
        password=value("TRACK_PG_PASSWORD", "POSTGRES_PASSWORD", ""),
        connect_timeout=int(value("TRACK_PG_CONNECT_TIMEOUT", None, "10")),
        sslmode=value("TRACK_PG_SSLMODE", None, "") or None,
        keepalives=value("TRACK_PG_KEEPALIVES", None, "1").lower()
        not in ("0", "false", "no"),
        keepalives_idle=int(value("TRACK_PG_KEEPALIVES_IDLE", None, "60")),
        keepalives_interval=int(value("TRACK_PG_KEEPALIVES_INTERVAL", None, "10")),
        keepalives_count=int(value("TRACK_PG_KEEPALIVES_COUNT", None, "5")),
        tcp_user_timeout=int(value("TRACK_PG_TCP_USER_TIMEOUT", None, "30000")),
    )
