from backend.main import health


def test_health_endpoint_payload() -> None:
    assert health() == {"status": "ok", "service": "backend"}
