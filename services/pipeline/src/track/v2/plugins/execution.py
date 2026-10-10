"""Sequential candidate execution with Detect-owned, repeatable evidence handles."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from contextlib import ExitStack
import json
from typing import Any
import numpy as np

from ..models import TurnBatch, CrossCandidateBatch
from .discovery import candidate_plugins


@dataclass(frozen=True)
class CandidateResult:
    algorithm_id: str
    instance_id: str
    evidence_kind: str
    candidates: Any
    events: Any
    params: Any
    evidence_offset: int = 0

    @property
    def source_key(self):
        return f"{self.algorithm_id}:{self.instance_id}"


class CompositeRelation:
    """Ordered evidence concatenation; references are rebased without copying rows."""
    def __init__(self, parts):
        self.parts = tuple(parts)
        self.supports_secondary_sources = bool(self.parts) and all(getattr(events, "supports_secondary_sources", isinstance(events, CrossCandidateBatch) or hasattr(events, "iter_source_batches")) for events, _ in self.parts)

    def __len__(self):
        return sum(len(events) for events, _ in self.parts)

    def iter_batches(self, *, indices=None):
        for events, offset in self.parts:
            iterator = iter((events,)) if isinstance(events, (TurnBatch, CrossCandidateBatch)) else events.iter_batches()
            try:
                for batch in iterator:
                    batch = replace(batch, calculation_index=batch.calculation_index + offset)
                    if indices is not None:
                        batch = batch.take(np.flatnonzero(np.isin(batch.calculation_index, indices)))
                    if len(batch):
                        yield batch
            finally:
                close = getattr(iterator, "close", None)
                if close:
                    close()

    def iter_coordinate_batches(self, *, include_source=True):
        for events, offset in self.parts:
            if isinstance(events, (TurnBatch, CrossCandidateBatch)):
                source = events.source_id if isinstance(events, TurnBatch) else events.from_source_id
                batches = iter(((events.xy, events.calculation_index, source),))
            else:
                batches = events.iter_coordinate_batches(include_source=include_source)
            try:
                for columns in batches:
                    yield (columns[0], columns[1] + offset) + ((columns[2],) if include_source else ())
            finally:
                close = getattr(batches, "close", None)
                if close:
                    close()

    def iter_source_batches(self):
        for events, offset in self.parts:
            batches = iter(((events.calculation_index, events.from_source_id, events.to_source_id),)) if isinstance(events, CrossCandidateBatch) else events.iter_source_batches()
            try:
                for indices, from_source, to_source in batches:
                    yield indices + offset, from_source, to_source
            finally:
                close = getattr(batches, "close", None)
                if close:
                    close()

    def close(self):
        # Actual relations belong to the execution context; views never own them.
        pass


class FilteredTurnRelation(CompositeRelation):
    """Repeatable strict selection preserving the extraction's local evidence IDs."""
    def __init__(self, events, threshold):
        super().__init__(((events, 0),))
        self.threshold = threshold
        self._count = sum(len(batch) for batch in self.iter_batches())

    def __len__(self):
        return self._count

    def iter_batches(self, *, indices=None):
        batches = super().iter_batches(indices=indices)
        try:
            for batch in batches:
                selected = batch.take(np.flatnonzero(np.abs(batch.turn_deg) > self.threshold))
                if len(selected):
                    yield selected
        finally:
            batches.close()

    def iter_coordinate_batches(self, *, include_source=True):
        for batch in self.iter_batches():
            yield (batch.xy, batch.calculation_index) + ((batch.source_id,) if include_source else ())


class DetectExecutionCache:
    """One evaluation batch's shared candidate handles; never persisted between batches."""
    def __init__(self, requests):
        self.requests = {identity: tuple(names) for identity, names in requests.items()}
        self._results = {}
        self._resources = ExitStack()
        self._closed = False

    def get_or_run(self, algorithm_id, plugin, context, params, requested):
        if self._closed:
            raise RuntimeError("candidate batch cache is closed")
        reader = context.reader
        descriptor = getattr(reader, "descriptor", None)
        key = (str(getattr(descriptor, "refine_id", context.refine_id)),
               str(getattr(reader, "time_start", None)), str(getattr(reader, "time_end", None)),
               repr(context.spatial_context), context.cfg.source_table or context.cfg.db_table,
               algorithm_id, json.dumps({name: asdict(value) for name, value in params.items()}, sort_keys=True))
        if key not in self._results:
            planned = tuple(name for name in self.requests.get(algorithm_id, requested) if name in params)
            if len(set(planned)) != len(planned) or any(name not in params for name in planned) or not set(requested).issubset(planned):
                raise ValueError(f"{algorithm_id}: invalid batch candidate requests")
            generated = plugin.run(context, params, planned)
            if set(generated) != set(planned):
                raise ValueError(f"{algorithm_id}: plugin returned unexpected batch instances")
            if any(result.algorithm_id != algorithm_id or result.instance_id != name for name, result in generated.items()):
                raise ValueError("candidate batch result identity does not match request")
            self.adopt(context.resources)
            self._results[key] = generated
        generated = self._results[key]
        if any(name not in generated for name in requested):
            raise ValueError(f"{algorithm_id}: instance was not planned in this candidate batch")
        return {name: generated[name] for name in requested}

    def adopt(self, resources):
        self._resources.callback(resources.pop_all().close)

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self._resources.close()
            finally:
                self._results.clear()

    def __enter__(self):
        if self._closed:
            raise RuntimeError("candidate batch cache is closed")
        return self

    def __exit__(self, *_args):
        self.close()


def execute_candidates(config, context, *, requests=None, candidate_cache=None):
    registry = candidate_plugins()
    outputs = []
    offsets = {}
    for algorithm_id in config.candidates:
        definition = config.candidate[algorithm_id]
        requested = tuple(definition.output_instances if requests is None else requests.get(algorithm_id, ()))
        if not requested:
            continue
        if len(set(requested)) != len(requested) or any(name not in definition.params for name in requested):
            raise ValueError(f"{algorithm_id}: invalid requested instances")
        plugin = registry[algorithm_id]
        if plugin.output_type != "candidate_points":
            raise ValueError(f"{algorithm_id}: unsupported candidate output")
        results = plugin.run(context, definition.params, requested) if candidate_cache is None else candidate_cache.get_or_run(algorithm_id, plugin, context, definition.params, requested)
        if set(results) != set(requested):
            raise ValueError(f"{algorithm_id}: plugin returned unexpected instances")
        for name in requested:
            result = results[name]
            if result.algorithm_id != algorithm_id or result.instance_id != name:
                raise ValueError("candidate plugin result identity does not match request")
            if result.evidence_kind not in {"turn", "orthogonal_cross_track"}:
                raise ValueError("candidate plugin requires an existing evidence adapter")
            offset = offsets.get(result.evidence_kind, 0)
            outputs.append(replace(result, evidence_offset=offset))
            offsets[result.evidence_kind] = offset + len(result.events)
    return tuple(outputs)
