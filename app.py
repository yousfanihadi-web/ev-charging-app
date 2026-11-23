"""
Smart EV Charging Platform - minimal working app
Frontend (static/index.html) + Backend (Flask REST API) + Database (local SQLite file).

The architecture's SQS/SNS bus is replaced by a tiny in-process event bus (publish/subscribe).
Every event is also stored in the `events` table. To move to AWS later, only publish() changes.
"""
import sqlite3, time, json, os
from flask import Flask, jsonify, request, g, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("EV_DB", os.path.join(BASE, "ev.db"))
app = Flask(__name__, static_folder="static")

SIM_SPEED = 60          # 1 real second = 1 simulated minute, so a demo session is quick
BATTERY_KWH = 60.0      # demo car battery
START_SOC = 20.0        # battery % when charging starts
SERVICE_FEE = 0.50


# ---------------- database ----------------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS stations(
        id INTEGER PRIMARY KEY, name TEXT, type TEXT, max_kw REAL,
        grid_limit_kw REAL, connectors INTEGER, rate REAL, distance_km REAL);
    CREATE TABLE IF NOT EXISTS sessions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, station_id INTEGER, connector INTEGER,
        status TEXT, started_at REAL, ended_at REAL,
        energy_kwh REAL DEFAULT 0, power_kw REAL DEFAULT 0, rate REAL,
        service_fee REAL, total REAL, card TEXT);
    CREATE TABLE IF NOT EXISTS meter_readings(      -- stands in for the time-series DB
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, ts REAL,
        kw REAL, voltage REAL, temp REAL);
    CREATE TABLE IF NOT EXISTS events(              -- stands in for the SQS/SNS bus
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, topic TEXT, payload TEXT);
    """)
    if con.execute("SELECT COUNT(*) FROM stations").fetchone()[0] == 0:
        con.executemany("INSERT INTO stations VALUES(?,?,?,?,?,?,?,?)", [
            (1, "Station A", "50 kW DC", 50, 38, 6, 0.30, 0.4),   # grid allows only 38 kW
            (2, "Station B", "22 kW AC", 22, 22, 4, 0.22, 1.1),
        ])
    con.commit(); con.close()


# ---------------- local event bus (SQS/SNS placeholder) ----------------
SUBSCRIBERS = {}

def subscribe(topic, fn):
    SUBSCRIBERS.setdefault(topic, []).append(fn)

def publish(topic, payload):
    """Swap this body for sns.publish(...) when moving to AWS."""
    db().execute("INSERT INTO events(ts,topic,payload) VALUES(?,?,?)",
                 (time.time(), topic, json.dumps(payload)))
    for fn in SUBSCRIBERS.get(topic, []):
        fn(payload)

def on_meter_reading(p):
    """Telemetry 'Lambda': would write to the time-series DB. Also a simple fault check."""
    db().execute("INSERT INTO meter_readings(session_id,ts,kw,voltage,temp) VALUES(?,?,?,?,?)",
                 (p["session_id"], time.time(), p["kw"], 400.0, p["temp"]))
    if p["temp"] > 75:                                  # 'Fault Lambda'
        publish("fault.detected", {"session_id": p["session_id"], "temp": p["temp"]})

def on_session_completed(p):
    """Billing service: reacts to session.completed and calculates the charge."""
    s = db().execute("SELECT * FROM sessions WHERE id=?", (p["session_id"],)).fetchone()
    total = round(s["energy_kwh"] * s["rate"] + s["service_fee"], 2)
    db().execute("UPDATE sessions SET total=?, card=? WHERE id=?", (total, "Card ending 4242", s["id"]))

subscribe("meter.reading", on_meter_reading)
subscribe("session.completed", on_session_completed)


# ---------------- helpers ----------------
def load_balancer_limit(station, active_count):
    """Load balancer 'Lambda': grid-aware power limit shared across active sessions."""
    share = station["grid_limit_kw"] / max(active_count, 1)
    return round(min(station["max_kw"], share), 1)

def active_count(station_id):
    return db().execute("SELECT COUNT(*) FROM sessions WHERE station_id=? AND status='active'",
                        (station_id,)).fetchone()[0]

def session_view(s):
    """Compute live values from elapsed time; also emits a meter.reading event while active."""
    st = db().execute("SELECT * FROM stations WHERE id=?", (s["station_id"],)).fetchone()
    out = dict(s)
    out["station_name"] = st["name"]
    out["limited"] = False
    if s["status"] == "active":
        elapsed_h = (time.time() - s["started_at"]) * SIM_SPEED / 3600
        kw = load_balancer_limit(st, active_count(st["id"]))
        energy = min(kw * elapsed_h, BATTERY_KWH * (100 - START_SOC) / 100)
        soc = START_SOC + energy / BATTERY_KWH * 100
        db().execute("UPDATE sessions SET energy_kwh=?, power_kw=? WHERE id=?", (energy, kw, s["id"]))
        publish("meter.reading", {"session_id": s["id"], "kw": kw, "temp": 35 + kw / 4})
        db().commit()
        out.update(energy_kwh=energy, power_kw=kw, limited=kw < st["max_kw"])
        left_kwh = BATTERY_KWH * (100 - soc) / 100
        out["minutes_left"] = round(left_kwh / kw * 60) if kw else 0
        out["soc"] = min(round(soc), 100)
        out["cost"] = round(energy * s["rate"], 2)
    else:
        out["soc"] = round(START_SOC + s["energy_kwh"] / BATTERY_KWH * 100)
    out["duration_min"] = round(((s["ended_at"] or time.time()) - s["started_at"]) * SIM_SPEED / 60)
    return out


# ---------------- REST API ----------------
@app.get("/api/stations")
def stations():
    rows = db().execute("SELECT * FROM stations ORDER BY distance_km").fetchall()
    res = []
    for r in rows:
        d = dict(r)
        d["free"] = r["connectors"] - active_count(r["id"])
        res.append(d)
    return jsonify(res)

@app.post("/api/sessions")
def start_session():
    sid = (request.json or {}).get("station_id")
    st = db().execute("SELECT * FROM stations WHERE id=?", (sid,)).fetchone()
    if not st:
        return jsonify(error="Station not found"), 404
    n = active_count(sid)
    if n >= st["connectors"]:
        return jsonify(error="No free connectors"), 409
    cur = db().execute(
        "INSERT INTO sessions(station_id,connector,status,started_at,rate,service_fee) VALUES(?,?,?,?,?,?)",
        (sid, n + 1, "active", time.time(), st["rate"], SERVICE_FEE))
    publish("session.started", {"session_id": cur.lastrowid, "station_id": sid})
    db().commit()
    s = db().execute("SELECT * FROM sessions WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(session_view(s)), 201

@app.get("/api/sessions/<int:sid>")
def get_session(sid):
    s = db().execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    return (jsonify(session_view(s)), 200) if s else (jsonify(error="Not found"), 404)

@app.get("/api/sessions/active")
def active_session():
    s = db().execute("SELECT * FROM sessions WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
    return jsonify(session_view(s) if s else None)

@app.post("/api/sessions/<int:sid>/stop")
def stop_session(sid):
    s = db().execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not s:
        return jsonify(error="Not found"), 404
    if s["status"] == "active":
        session_view(s)                                  # final energy update
        db().execute("UPDATE sessions SET status='completed', ended_at=? WHERE id=?", (time.time(), sid))
        publish("session.completed", {"session_id": sid})
        db().commit()
    s = db().execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    return jsonify(session_view(s))

@app.get("/api/history")
def history():
    rows = db().execute("SELECT * FROM sessions WHERE status='completed' ORDER BY id DESC").fetchall()
    return jsonify([session_view(r) for r in rows])

@app.get("/api/events")
def events():
    rows = db().execute("SELECT * FROM events ORDER BY id DESC LIMIT 30").fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/version")
def version():
    """Used by the pipeline to verify that production runs the new build."""
    try:
        with open(os.path.join(BASE, "VERSION")) as f:
            return jsonify(version=f.read().strip())
    except OSError:
        return jsonify(version="unknown")


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)  # nosec B104
