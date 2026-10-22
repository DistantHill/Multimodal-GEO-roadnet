from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier

import pytest

from pipeline.artifacts import write_json as write_artifact_json
from pipeline.labels import write_json as write_label_json


@pytest.mark.parametrize("writer", [write_artifact_json, write_label_json],
                         ids=["artifacts", "labels"])
def test_concurrent_json_writers_publish_complete_payloads(tmp_path, monkeypatch, writer):
    destination = tmp_path / "published" / "latest.json"
    payloads = [{"run": "first", "text": "路网甲", "items": list(range(200))},
                {"run": "second", "text": "路网乙", "items": list(range(500, 800))}]
    before_publish = Barrier(2)
    pending = []
    original_replace = Path.replace

    def synchronized_replace(source, target):
        if Path(target) == destination:
            pending.append(source)
            # Both writers finish their own temporary file before either publishes.
            before_publish.wait(timeout=10)
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", synchronized_replace)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(writer, destination, payload) for payload in payloads]
        for future in futures:
            future.result(timeout=15)

    assert len(set(pending)) == 2
    assert all(path.parent == destination.parent for path in pending)
    content = destination.read_text(encoding="utf-8")
    result = json.loads(content)
    assert result in payloads
    assert content == json.dumps(result, ensure_ascii=False, indent=2,
                                 sort_keys=True, allow_nan=False) + "\n"
    assert set(destination.parent.iterdir()) == {destination}


@pytest.mark.parametrize("writer", [write_artifact_json, write_label_json],
                         ids=["artifacts", "labels"])
@pytest.mark.parametrize("failure", ["serialization", "replace"])
def test_failed_json_publication_keeps_previous_file_and_cleans_temporary(
        tmp_path, monkeypatch, writer, failure):
    destination = tmp_path / "latest.json"
    previous = '{"run": "complete"}\n'
    destination.write_text(previous, encoding="utf-8")
    payload = {"run": "new"}

    if failure == "serialization":
        payload["score"] = float("nan")
        expected_error = ValueError
    else:
        def fail_replace(source, target):
            raise OSError("simulated publication failure")

        monkeypatch.setattr(Path, "replace", fail_replace)
        expected_error = OSError

    with pytest.raises(expected_error):
        writer(destination, payload)

    assert destination.read_text(encoding="utf-8") == previous
    assert set(tmp_path.iterdir()) == {destination}
