"""Static gates that keep production configuration at process boundaries."""

from __future__ import annotations

import ast
from pathlib import Path


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
_checkout_candidate = (
    PIPELINE_ROOT.parents[1]
    if len(PIPELINE_ROOT.parents) > 1
    else PIPELINE_ROOT.parent
)
REPOSITORY_ROOT = (
    _checkout_candidate
    if (_checkout_candidate / "services/pipeline").is_dir()
    else PIPELINE_ROOT
)
PRODUCTION_ROOTS = [PIPELINE_ROOT / "src"]
if REPOSITORY_ROOT != PIPELINE_ROOT:
    PRODUCTION_ROOTS.append(REPOSITORY_ROOT / "apps/backend/src")
APPROVED_ENVIRONMENT_READERS = {
    PIPELINE_ROOT / "src/track/v2/cli/_load_config.py",
}
if REPOSITORY_ROOT != PIPELINE_ROOT:
    APPROVED_ENVIRONMENT_READERS.add(REPOSITORY_ROOT / "apps/backend/src/backend/config.py")
CONFIG_CLASSES = {
    "PostgresConfig",
    "PathsConfig",
    "LoggingConfig",
    "ParserConfig",
    "ResourcesConfig",
    "PipelineConfig",
    "ProjectionConfig",
    "RefineConfig",
    "IntersectConfig",
    "ClipConfig",
    "FittingConfig",
    "NetworkConfig",
    "RuntimeConfig",
    "QdrantConfig",
    "MapConfig",
    "HealthConfig",
    "BackendConfig",
}
REMOVED_ENVIRONMENT_KEYS = {
    "DATA_ROOT",
    "PARQUET_ROOT",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_DATABASE",
    "POSTGRES_USER",
    "QDRANT_HOST",
    "TRACK_PG_HOST",
    "TRACK_PG_PORT",
    "TRACK_PG_DATABASE",
    "TRACK_PG_USER",
    "ROADNET_DIFF_DSN",
}
REMOVED_CLI_OPTIONS = {
    "--config",
    "--processing-srid",
    "--host",
    "--port",
    "--database",
    "--user",
    "--connect-timeout",
    "--sslmode",
    "--parquet-root",
}


def _production_files() -> list[Path]:
    return [
        path
        for root in PRODUCTION_ROOTS
        for path in root.rglob("*.py")
    ]


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def test_only_process_boundaries_read_environment() -> None:
    violations: list[str] = []
    for path in _production_files():
        if path in APPROVED_ENVIRONMENT_READERS:
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, (ast.Call, ast.Subscript)):
                target = node.func if isinstance(node, ast.Call) else node.value
                name = _qualified_name(target)
                if name in {"os.getenv", "os.environ"}:
                    violations.append(f"{path.relative_to(REPOSITORY_ROOT)}:{node.lineno}")
    assert not violations, "environment read outside startup boundary:\n" + "\n".join(violations)


def test_removed_environment_keys_do_not_return() -> None:
    violations: list[str] = []
    for path in _production_files():
        source = path.read_text(encoding="utf-8")
        for key in sorted(REMOVED_ENVIRONMENT_KEYS):
            if key in source:
                violations.append(f"{path.relative_to(REPOSITORY_ROOT)}: {key}")
    assert not violations, "removed per-setting environment key remains:\n" + "\n".join(violations)


def test_tunable_config_dataclasses_have_no_python_defaults() -> None:
    violations: list[str] = []
    for path in _production_files():
        for node in _tree(path).body:
            if not isinstance(node, ast.ClassDef) or node.name not in CONFIG_CLASSES:
                continue
            for statement in node.body:
                if isinstance(statement, ast.AnnAssign) and statement.value is not None:
                    violations.append(
                        f"{path.relative_to(REPOSITORY_ROOT)}:{statement.lineno} "
                        f"{node.name}.{statement.target.id}"
                    )
    assert not violations, "config field has a Python default:\n" + "\n".join(violations)


def test_leaf_functions_do_not_synthesize_implicit_config() -> None:
    violations: list[str] = []
    for path in _production_files():
        if path in APPROVED_ENVIRONMENT_READERS:
            continue
        for node in ast.walk(_tree(path)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            positional = [*node.args.posonlyargs, *node.args.args]
            defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
            parameters = [*zip(positional, defaults), *zip(node.args.kwonlyargs, node.args.kw_defaults)]
            for argument, default in parameters:
                if argument.arg not in {"config", "runtime"}:
                    continue
                if isinstance(default, ast.Constant) and default.value is None:
                    violations.append(
                        f"{path.relative_to(REPOSITORY_ROOT)}:{argument.lineno} "
                        f"{node.name}({argument.arg}=None)"
                    )
    assert not violations, "implicit leaf configuration remains:\n" + "\n".join(violations)


def test_removed_configuration_cli_options_do_not_return() -> None:
    violations: list[str] = []
    for path in _production_files():
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if _qualified_name(node.func) not in {"add_argument", "parser.add_argument"}:
                continue
            option = node.args[0]
            if isinstance(option, ast.Constant) and option.value in REMOVED_CLI_OPTIONS:
                violations.append(
                    f"{path.relative_to(REPOSITORY_ROOT)}:{node.lineno} {option.value}"
                )
    assert not violations, "removed configuration CLI option remains:\n" + "\n".join(violations)


def test_package_internal_toml_defaults_do_not_return() -> None:
    removed_paths = (
        PIPELINE_ROOT / "src/track/v2/config/pipeline.toml",
        PIPELINE_ROOT / "src/track/v2/config/default_field_specs.toml",
    )
    remaining = [str(path.relative_to(REPOSITORY_ROOT)) for path in removed_paths if path.exists()]
    assert not remaining, "package-internal TOML remains:\n" + "\n".join(remaining)
