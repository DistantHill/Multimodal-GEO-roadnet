"""Cross-track timings exclude time spent in a generator's consumer."""
from contextlib import contextmanager
from time import perf_counter


class CrossTrackTiming:
    def __init__(self) -> None:
        self.values = {name: 0.0 for name in (
            "read_s", "segment_s", "sort_s", "prepare_s", "index_s",
            "candidate_s", "gather_s", "kernel_s", "output_s", "write_s", "queue_s",
        )}
        self.values.update(pair_count=0, hit_count=0, straight_segments=0, eligible_segments=0, speed_rejected_segments=0)
        self.mode = "reading"

    @contextmanager
    def measure(self, name: str):
        started = perf_counter()
        try:
            yield
        finally:
            self.values[name] += perf_counter() - started

    def iterate(self, iterable, name: str):
        iterator = iter(iterable)
        try:
            while True:
                with self.measure(name):
                    try:
                        item = next(iterator)
                    except StopIteration:
                        return
                yield item
        finally:
            close = getattr(iterator, "close", None)
            if close:
                close()

    def summary(self) -> str:
        values = self.values.copy()
        values["output_s"] += values["write_s"]
        values["pre_intersection_s"] = sum(values[key] for key in (
            "read_s", "segment_s", "sort_s", "prepare_s", "index_s"))
        return " ".join(
            [f"mode={self.mode}"]
            + [f"{key}={value:.3f}" if key.endswith("_s") else f"{key}={value:,}"
               for key, value in values.items()]
        )
