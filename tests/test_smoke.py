"""
Smoke tests: the service imports, lifespan runs, /health and /api/v1/stats
are reachable. These don't talk to EPICS, but they do run under the
`test_ioc` session fixture so every test file sees the same CA address.
"""


def test_imports():
    """Package imports without executing the app."""
    import direct_control  # noqa: F401
    import direct_control.config  # noqa: F401
    import direct_control.models  # noqa: F401
    import direct_control.protocols  # noqa: F401


def test_health_endpoint_returns_200(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("healthy", "degraded")
    # Mock coordination reports available → healthy.
    assert body["coordination_service_available"] is True


def test_stats_endpoint_returns_200(client):
    r = client.get("/api/v1/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "direct_control"
    assert "pv_socket" in body
    assert "device_socket" in body


def test_unsupported_accept_returns_406(client):
    r = client.get("/api/v1/pv/IOC:m1/value", headers={"accept": "image/png"})
    assert r.status_code == 406
