"""Shared PostgreSQL connection config for track.v2 CLI modules.

ZH: 所有 CLI 模块共用的数据库连接配置。默认值从 ``track/v2/.env`` 读取，
    避免在每个 CLI 中硬编码敏感信息。

EN: Shared database connection config used by all CLI modules. Defaults are
    read from ``track/v2/.env`` so sensitive info isn't hardcoded in every CLI.
"""

from __future__ import annotations

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
    """Build a PostgresConfig populated from .env defaults.

    ZH: 从 .env 文件读取默认数据库连接参数。返回的 config 中密码已填充，
        CLI 仍可通过 ``--ask-password`` 覆盖。

    EN: Read default DB connection parameters from .env. The returned config
        includes the password; CLIs may still override via ``--ask-password``.
    """
    env = load_dotenv(env_path)
    return PostgresConfig(
        host=env.get("TRACK_PG_HOST", "127.0.0.1"),
        port=int(env.get("TRACK_PG_PORT", "5432")),
        database=env.get("TRACK_PG_DATABASE", "track_test"),
        user=env.get("TRACK_PG_USER", "postgres"),
        password=env.get("TRACK_PG_PASSWORD", "asdf"),
        connect_timeout=int(env.get("TRACK_PG_CONNECT_TIMEOUT", "10")),
        sslmode=env.get("TRACK_PG_SSLMODE") or None,
        keepalives=env.get("TRACK_PG_KEEPALIVES", "1") not in ("0", "false", "no"),
        keepalives_idle=int(env.get("TRACK_PG_KEEPALIVES_IDLE", "60")),
        keepalives_interval=int(env.get("TRACK_PG_KEEPALIVES_INTERVAL", "10")),
        keepalives_count=int(env.get("TRACK_PG_KEEPALIVES_COUNT", "5")),
        tcp_user_timeout=int(env.get("TRACK_PG_TCP_USER_TIMEOUT", "30000")),
    )
