"""Immutable task descriptions and lazy complete-subtrack planning.

These primitives do not read rows, dispatch work, or persist task state. A caller
must persist each planned manifest before dispatch and reuse it on resume: a new
target size is only for not-yet-planned input. Grid readers and merge semantics
are deliberately outside this module.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any


def _json_value(value: Any) -> Any:
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if type(value) is list:
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            result[key] = _json_value(item)
        return result
    raise ValueError(f"Unsupported JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Stable strict JSON; no key coercion, NaN, or implicit custom encoders."""
    # Materialize only the supplied descriptor/configuration, never source rows.
    return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _object_json(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a JSON object string")
    decoded = json.loads(value, object_pairs_hook=_unique_object)
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} must describe a JSON object")
    return canonical_json(decoded)


def json_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()


def _nonempty(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")


def _positive_int(value: int, label: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


@dataclass(frozen=True, slots=True)
class SubtrackGroup:
    """One complete group, scoped to its input run, with no in-memory row data.

    ``input_range_json`` describes a fixed range for a reader (e.g. sequence
    endpoints); it is data, not executable SQL. Keys may be integers or strings.
    The planner orders integer keys numerically and string keys lexically.
    """

    input_run_id: str
    group_key: int | str
    row_count: int
    input_range_json: str

    def __post_init__(self) -> None:
        _nonempty(self.input_run_id, "input_run_id")
        if type(self.group_key) not in (int, str):
            raise ValueError("group_key must be an integer or string")
        if isinstance(self.group_key, str):
            _nonempty(self.group_key, "group_key")
        _positive_int(self.row_count, "row_count")
        object.__setattr__(self, "input_range_json",
                           _object_json(self.input_range_json, "input_range_json"))

    @property
    def order_key(self) -> tuple[str, int, int | str]:
        return (self.input_run_id, 0 if type(self.group_key) is int else 1,
                self.group_key)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SubtrackGroup:
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class PatchManifest:
    """Fixed task identity; execution tuning and machine details are excluded.

    The descriptor must include all input identity/range information, including
    any grid boundary/ownership rule needed by its reader. Once registered it
    must not be regenerated from changed ``n``. Parent identity records explicit
    splitting; this class does not implement grid planning or parent completion.
    """

    run_id: str
    stage: str
    partition_kind: str
    input_descriptor_json: str
    input_fingerprint: str
    semantic_config_digest: str
    code_version: str
    algorithm_version: str
    row_count: int
    parent_id: str | None = None
    manifest_version: int = 1

    def __post_init__(self) -> None:
        for name in ("run_id", "stage", "partition_kind", "input_fingerprint",
                     "code_version", "algorithm_version"):
            _nonempty(getattr(self, name), name)
        digest = self.semantic_config_digest
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise ValueError("semantic_config_digest must be a SHA-256 hex digest")
        _positive_int(self.row_count, "row_count")
        if type(self.manifest_version) is not int or self.manifest_version != 1:
            raise ValueError("Unsupported manifest_version")
        if self.parent_id is not None:
            _nonempty(self.parent_id, "parent_id")
        object.__setattr__(self, "input_descriptor_json",
                           _object_json(self.input_descriptor_json,
                                        "input_descriptor_json"))

    @property
    def task_id(self) -> str:
        return json_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PatchManifest:
        return cls(**dict(value))


def plan_subtrack_patches(
    groups: Iterable[SubtrackGroup],
    *,
    target_rows: int,
    run_id: str,
    stage: str,
    input_fingerprint: str,
    semantic_config_digest: str,
    code_version: str,
    algorithm_version: str,
    parent_id: str | None = None,
) -> Iterator[PatchManifest]:
    """Pack complete groups lazily, retaining current patch plus one lookahead.

    Input must be strictly ascending by ``SubtrackGroup.order_key``. This makes
    duplicate identities detectable without an ever-growing seen-key set. The
    producer supplies metadata, not row lists. A group larger than the soft row
    target runs alone; this does NOT assert it fits the runtime memory budget.
    """
    _positive_int(target_rows, "target_rows")
    # Validate context even for empty input, without consuming the iterable.
    context = dict(run_id=run_id, stage=stage, partition_kind="subtrack",
                   input_fingerprint=input_fingerprint,
                   semantic_config_digest=semantic_config_digest,
                   code_version=code_version, algorithm_version=algorithm_version,
                   parent_id=parent_id)
    PatchManifest(input_descriptor_json="{}", row_count=1, **context)

    def iterate() -> Iterator[PatchManifest]:
        current: list[SubtrackGroup] = []
        rows = 0
        previous: tuple[str, int, int | str] | None = None

        def manifest() -> PatchManifest:
            return PatchManifest(
                input_descriptor_json=canonical_json(
                    {"groups": [group.to_dict() for group in current]}),
                row_count=rows, **context)

        for group in groups:
            if not isinstance(group, SubtrackGroup):
                raise ValueError("Input must contain SubtrackGroup metadata")
            if previous is not None and group.order_key <= previous:
                raise ValueError("Group identities must be unique and strictly ordered")
            previous = group.order_key
            if current and rows + group.row_count > target_rows:
                yield manifest()
                current = []
                rows = 0
            current.append(group)
            rows += group.row_count
            if rows >= target_rows:
                yield manifest()
                current = []
                rows = 0
        if current:
            yield manifest()

    return iterate()


@dataclass(frozen=True, slots=True)
class ParameterClassification:
    semantic_json: str
    execution_json: str

    def __post_init__(self) -> None:
        for name in ("semantic_json", "execution_json"):
            object.__setattr__(self, name, _object_json(getattr(self, name), name))

    @property
    def semantic_digest(self) -> str:
        return json_digest(json.loads(self.semantic_json))

    @property
    def execution_digest(self) -> str:
        return json_digest(json.loads(self.execution_json))


def classify_parameters(
    parameters: Mapping[str, Any],
    *,
    execution_paths: Iterable[tuple[str, ...]] = (),
) -> ParameterClassification:
    """Unknown parameters are semantic; only explicitly reviewed leaves may vary.

    Paths are tuples, e.g. ``('runtime', 'n')``; no wildcard or subtree exclusion.
    A caller must prove allowed execution changes preserve algorithm semantics.
    Algorithm grid scale/halo must remain semantic; a separately named scheduling
    subdivision limit may be allowlisted only with equivalent ownership/merging.
    Absent allowlisted paths are allowed so a schema can include optional knobs.
    An execution leaf is a JSON scalar or a flat list of JSON scalars (including
    an empty list). Objects, even empty ones, and lists containing objects/lists
    are subtrees and cannot be excluded as a whole.
    """
    if not isinstance(parameters, Mapping):
        raise ValueError("parameters must be a JSON object")
    # Round-trip isolates mutable caller data and validates all values first.
    normalized = json.loads(canonical_json(dict(parameters)))
    allowed: set[tuple[str, ...]] = set()
    for path in execution_paths:
        if (not isinstance(path, tuple) or not path
                or any(type(part) is not str or not part for part in path)):
            raise ValueError("execution_paths must contain nonempty string tuples")
        if path in allowed:
            raise ValueError(f"Duplicate execution path: {path}")
        allowed.add(path)

    def split(node: dict[str, Any], prefix: tuple[str, ...]) -> tuple[dict, dict]:
        semantic: dict[str, Any] = {}
        execution: dict[str, Any] = {}
        for key, value in node.items():
            path = (*prefix, key)
            if path in allowed:
                if isinstance(value, dict) or (
                    isinstance(value, list)
                    and any(isinstance(item, (dict, list)) for item in value)
                ):
                    raise ValueError("Execution allowlist must name leaves, not subtrees")
                execution[key] = value
            elif isinstance(value, dict) and value:
                child_semantic, child_execution = split(value, path)
                if child_semantic:
                    semantic[key] = child_semantic
                if child_execution:
                    execution[key] = child_execution
            else:
                semantic[key] = value
        return semantic, execution

    semantic, execution = split(normalized, ())
    return ParameterClassification(canonical_json(semantic), canonical_json(execution))


@dataclass(frozen=True, slots=True)
class ResumeSignature:
    input_fingerprint: str
    code_version: str
    algorithm_version: str
    semantic_config_digest: str

    def __post_init__(self) -> None:
        for name in ("input_fingerprint", "code_version", "algorithm_version"):
            _nonempty(getattr(self, name), name)
        digest = self.semantic_config_digest
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise ValueError("semantic_config_digest must be a SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class ResumeCompatibility:
    mismatches: tuple[str, ...]

    @property
    def compatible(self) -> bool:
        return not self.mismatches


def check_resume_compatibility(
    recorded: ResumeSignature, requested: ResumeSignature,
) -> ResumeCompatibility:
    """Compare immutable inputs/semantics, never execution knobs or hardware.

    Compatibility does not grant a run lock or validate completed output files;
    those checks belong to the persistence/coordinator integration.
    """
    return ResumeCompatibility(tuple(
        name for name in ("input_fingerprint", "code_version", "algorithm_version",
                          "semantic_config_digest")
        if getattr(recorded, name) != getattr(requested, name)
    ))
