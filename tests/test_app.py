"""Unit tests. The pipeline fails if any of these fail."""
import pytest
import app as ev


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "DB_PATH", str(tmp_path / "test.db"))  # fresh database per test
    ev.init_db()
    return ev.app.test_client()


def test_load_balancer_limits_power_to_grid():
    station = {"max_kw": 50, "grid_limit_kw": 38}
    assert ev.load_balancer_limit(station, 1) == 38      # grid limit wins over charger max


def test_load_balancer_shares_power_between_cars():
    station = {"max_kw": 50, "grid_limit_kw": 38}
    assert ev.load_balancer_limit(station, 2) == 19


def test_load_balancer_never_exceeds_charger_max():
    station = {"max_kw": 22, "grid_limit_kw": 100}
    assert ev.load_balancer_limit(station, 1) == 22


def test_stations_listed_with_free_connectors(client):
    stations = client.get("/api/stations").get_json()
    assert len(stations) == 2
    assert stations[0]["free"] == stations[0]["connectors"]


def test_start_and_stop_session_creates_bill(client):
    r = client.post("/api/sessions", json={"station_id": 1})
    assert r.status_code == 201
    sid = r.get_json()["id"]
    done = client.post(f"/api/sessions/{sid}/stop").get_json()
    assert done["status"] == "completed"
    assert done["total"] >= ev.SERVICE_FEE               # billing reacted to session.completed


def test_unknown_station_returns_404(client):
    assert client.post("/api/sessions", json={"station_id": 999}).status_code == 404


def test_events_are_recorded(client):
    sid = client.post("/api/sessions", json={"station_id": 1}).get_json()["id"]
    client.post(f"/api/sessions/{sid}/stop")
    topics = [e["topic"] for e in client.get("/api/events").get_json()]
    assert "session.started" in topics and "session.completed" in topics


def test_version_endpoint(client):
    assert client.get("/api/version").get_json()["version"] == open("VERSION").read().strip()
