"""Behavior tests for metadata planning; no production scheduler is implied."""

from dataclasses import FrozenInstanceError, replace
import json
import pickle
from types import MappingProxyType

import pytest

from track.v2.patches import (
    PatchManifest,
    ResumeSignature,
    SubtrackGroup,
    canonical_json,
    check_resume_compatibility,
    classify_parameters,
    json_digest,
    plan_subtrack_patches,
)


CONTEXT = {
    "run_id": "output-run",
    "stage": "clean",
    "input_fingerprint": "full-source-fingerprint",
    "semantic_config_digest": json_digest({"threshold": 5}),
    "code_version": "revision-one",
    "algorithm_version": "clean-one",
}


def group(key: int | str, rows: int, run: str = "source-run") -> SubtrackGroup:
    return SubtrackGroup(run, key, rows, canonical_json({"start": 0, "stop": rows}))


def plan(groups, n=10):
    return plan_subtrack_patches(groups, target_rows=n, **CONTEXT)


def keys(manifest):
    return [item["group_key"]
            for item in json.loads(manifest.input_descriptor_json)["groups"]]


@pytest.mark.parametrize("counts,expected", [
    ([], []),
    ([3, 7, 1], [[0, 1], [2]]),
    ([3, 8, 2], [[0], [1, 2]]),
    ([11, 3, 7], [[0], [1, 2]]),
    ([4, 12, 4], [[0], [1], [2]]),
    ([10, 10], [[0], [1]]),
    ([1, 1, 1], [[0, 1, 2]]),
])
def test_complete_groups_never_split_or_disappear(counts, expected):
    manifests = list(plan(group(index, rows) for index, rows in enumerate(counts)))
    assert [keys(item) for item in manifests] == expected
    assert sum(item.row_count for item in manifests) == sum(counts)
    assert all(item.partition_kind == "subtrack" for item in manifests)


def test_only_current_patch_and_one_lookahead_are_consumed():
    consumed = []

    def source():
        for index in range(100):
            consumed.append(index)
            yield group(index, 6)

    patches = plan(source())
    assert consumed == []
    assert keys(next(patches)) == [0]
    assert consumed == [0, 1]
    assert keys(next(patches)) == [1]
    assert consumed == [0, 1, 2]
    patches.close()


def test_exact_target_does_not_require_lookahead():
    def source():
        yield group(0, 10)
        raise AssertionError("must not prefetch after an exact/full patch")

    patches = plan(source())
    assert keys(next(patches)) == [0]
    patches.close()


@pytest.mark.parametrize("value", [0, -1, 1.5, True, None, "10"])
def test_invalid_target_rejected_even_with_empty_input(value):
    with pytest.raises(ValueError, match="target_rows"):
        plan([], value)


@pytest.mark.parametrize("value", [0, -1, 1.5, True, None, "1"])
def test_invalid_group_count_rejected(value):
    with pytest.raises(ValueError, match="row_count"):
        group(0, value)


@pytest.mark.parametrize("value", [True, None, 1.5, ""])
def test_invalid_group_key_rejected(value):
    with pytest.raises(ValueError, match="group_key"):
        group(value, 1)


def test_numeric_order_is_not_lexical_order():
    assert [keys(item) for item in plan([group(2, 10), group(10, 10)])] == [[2], [10]]


@pytest.mark.parametrize("key_list", [[1, 1], [2, 1], [1, 2, 1]])
def test_duplicate_or_out_of_order_identity_rejected_across_patches(key_list):
    with pytest.raises(ValueError, match="unique and strictly ordered"):
        list(plan((group(key, 10) for key in key_list)))


def test_group_identity_is_scoped_to_input_run():
    manifests = list(plan([group(1, 10, "a"), group(1, 10, "b")]))
    assert manifests[0].task_id != manifests[1].task_id


def test_invalid_input_object_is_rejected():
    with pytest.raises(ValueError, match="metadata"):
        list(plan([{"group_key": 1}]))


def test_immutable_serializable_metadata():
    original_group = group(2, 5)
    manifest = next(plan([original_group]))
    for value in (original_group, manifest):
        copied = type(value).from_dict(json.loads(json.dumps(value.to_dict())))
        assert copied == value
        assert pickle.loads(pickle.dumps(value)) == value
        with pytest.raises(FrozenInstanceError):
            value.row_count = 3
    assert manifest.task_id == copied.task_id


def test_descriptor_key_order_does_not_change_task_id():
    manifest = next(plan([group(1, 5)]))
    a = replace(manifest, input_descriptor_json='{"a":1,"b":2}')
    b = replace(manifest, input_descriptor_json=' { "b": 2, "a": 1 } ')
    assert a == b
    assert a.task_id == b.task_id


@pytest.mark.parametrize("field,value", [
    ("run_id", "other-run"),
    ("stage", "detect"),
    ("partition_kind", "grid"),
    ("input_descriptor_json", '{"other":"range"}'),
    ("input_fingerprint", "changed-source"),
    ("semantic_config_digest", json_digest({"changed": 1})),
    ("code_version", "new-code"),
    ("algorithm_version", "new-algorithm"),
    ("parent_id", "recorded-parent-task"),
])
def test_identity_includes_fixed_business_and_semantic_context(field, value):
    original = next(plan([group(1, 5)]))
    assert replace(original, **{field: value}).task_id != original.task_id


def test_new_target_does_not_rewrite_registered_manifests():
    groups = [group(index, 5) for index in range(4)]
    registered = next(plan(iter(groups), 10))
    serialized = canonical_json(registered.to_dict())
    original_id = registered.task_id
    # Resume reloads fixed registered work, and plans only untouched input.
    resumed = PatchManifest.from_dict(json.loads(serialized))
    future = list(plan(iter(groups[2:]), 5))
    assert resumed.task_id == original_id
    assert keys(resumed) == [0, 1]
    assert [keys(item) for item in future] == [[2], [3]]


@pytest.mark.parametrize("payload", [
    {"value": float("nan")}, {"value": float("inf")},
    {"value": float("-inf")}, {1: "key coercion"}, {"value": {1, 2}},
])
def test_noncanonical_values_are_rejected(payload):
    with pytest.raises(ValueError):
        canonical_json(payload)


@pytest.mark.parametrize("payload", [
    '{"x":1,"x":2}', '{"x":NaN}', '[1,2]', 'null', 'not json',
])
def test_invalid_serialized_descriptor_rejected(payload):
    with pytest.raises(ValueError):
        SubtrackGroup("source", 1, 1, payload)


@pytest.mark.parametrize("field,value", [
    ("row_count", 0), ("manifest_version", True), ("manifest_version", 2),
    ("semantic_config_digest", "not-a-digest"), ("run_id", ""),
    ("parent_id", ""),
])
def test_invalid_manifest_fields_rejected(field, value):
    original = next(plan([group(1, 5)]))
    with pytest.raises(ValueError):
        replace(original, **{field: value})


def test_unknown_parameters_default_to_semantic():
    params = {"future_parameter": 12, "runtime": {"n": 10, "W": 2},
              "grid": {"algorithm_scale": 20, "scheduling_subdivision": 3}}
    allowed = [("runtime", "n"), ("runtime", "W"),
               ("grid", "scheduling_subdivision")]
    result = classify_parameters(params, execution_paths=allowed)
    assert json.loads(result.semantic_json) == {
        "future_parameter": 12, "grid": {"algorithm_scale": 20}}
    assert json.loads(result.execution_json) == {
        "runtime": {"n": 10, "W": 2}, "grid": {"scheduling_subdivision": 3}}
    params["grid"]["algorithm_scale"] = 99
    assert json.loads(result.semantic_json)["grid"]["algorithm_scale"] == 20


def test_execution_only_changes_do_not_change_resume_semantics():
    first = classify_parameters({"threshold": 5, "n": 10}, execution_paths=[("n",)])
    second = classify_parameters({"threshold": 5, "n": 20}, execution_paths=[("n",)])
    assert first.semantic_digest == second.semantic_digest
    assert first.execution_digest != second.execution_digest
    recorded = ResumeSignature("source", "code", "algorithm", first.semantic_digest)
    requested = replace(recorded, semantic_config_digest=second.semantic_digest)
    assert check_resume_compatibility(recorded, requested).compatible


@pytest.mark.parametrize("field", [
    "input_fingerprint", "code_version", "algorithm_version", "semantic_config_digest",
])
def test_each_resume_signature_change_refuses_reuse(field):
    recorded = ResumeSignature("source", "code", "algorithm", json_digest({}))
    value = json_digest({"threshold": 1}) if field.endswith("digest") else "changed"
    result = check_resume_compatibility(recorded, replace(recorded, **{field: value}))
    assert not result.compatible
    assert result.mismatches == (field,)


def test_algorithm_grid_scale_remains_semantic():
    def classify(scale, depth):
        return classify_parameters(
            {"algorithm": {"grid_scale": scale}, "scheduler": {"grid_depth": depth}},
            execution_paths=[("scheduler", "grid_depth")],
        )
    assert classify(10, 2).semantic_digest == classify(10, 4).semantic_digest
    assert classify(10, 2).semantic_digest != classify(20, 2).semantic_digest


@pytest.mark.parametrize("paths", [
    ["runtime.n"], [()], [("",)], [("n",), ("n",)], [("runtime",)],
])
def test_invalid_execution_allowlist_rejected(paths):
    with pytest.raises(ValueError):
        classify_parameters({"runtime": {"n": 10}}, execution_paths=paths)


def test_default_classification_and_optional_paths():
    params = {"empty": {}, "list": [1, 2], "nullable": None}
    result = classify_parameters(params, execution_paths=[("absent_optional_knob",)])
    assert json.loads(result.semantic_json) == params
    assert result.execution_json == "{}"


def test_config_parameter_name_with_dot_is_not_treated_as_path():
    result = classify_parameters({"runtime.n": 1, "runtime": {"n": 2}},
                                 execution_paths=[("runtime", "n")])
    assert json.loads(result.semantic_json) == {"runtime.n": 1}


def test_invalid_configuration_value_rejected_even_if_allowlisted():
    with pytest.raises(ValueError):
        classify_parameters({"n": float("nan")}, execution_paths=[("n",)])


def test_mapping_values_normalized_without_key_coercion():
    assert canonical_json(MappingProxyType({"nested": MappingProxyType({"a": 1})})) == (
        '{"nested":{"a":1}}')
