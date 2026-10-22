"""Local web dashboard for Fitbit data via the Google Health API.

    uv run dashboard.py            # real data (run `uv run main.py login` first)
    uv run dashboard.py --demo     # synthetic data, no credentials needed
"""

import argparse
import json
import os
import random
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import health_api
import insights

STATIC_DIR = Path(__file__).parent / "static"
CACHE_TTL_SECONDS = 300
ROLLUP_CHUNK_DAYS = 14  # total-calories rollups are capped at a 14-day range
# Wall-clock offset used to bucket heart-rate samples into local minutes (default: this machine's zone).
LOCAL_OFFSET_MIN = int(os.environ.get("TZ_OFFSET_MIN", round(datetime.now().astimezone().utcoffset().total_seconds() / 60)))

# data type id -> (rollup response key, {output field: rollup field})
ROLLUPS = {
    "steps": ("steps", {"steps": "countSum"}),
    "distance": ("distance", {"distanceMm": "millimetersSum"}),
    "total-calories": ("totalCalories", {"calories": "kcalSum"}),
    "active-zone-minutes": ("activeZoneMinutes", {
        "azmFatBurn": "sumInFatBurnHeartZone", "azmCardio": "sumInCardioHeartZone", "azmPeak": "sumInPeakHeartZone",
    }),
    "heart-rate": ("heartRate", {"hrAvg": "beatsPerMinuteAvg", "hrMin": "beatsPerMinuteMin", "hrMax": "beatsPerMinuteMax"}),
    "weight": ("weight", {"weightGrams": "weightGramsAvg"}),
}


def _civil_date(d: dict | None) -> str | None:
    if not d:
        return None
    return date(d["year"], d["month"], d["day"]).isoformat()


def _num(v) -> float | None:
    # int64 fields arrive as strings
    return None if v is None else float(v)


def _rollup_days(session, data_type: str, start: date, end: date) -> dict[str, dict]:
    key, fields = ROLLUPS[data_type]
    out: dict[str, dict] = {}
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=ROLLUP_CHUNK_DAYS - 1), end)
        # Range end treated as exclusive; extra days are dropped below.
        body = health_api.daily_rollup(session, data_type, chunk_start, chunk_end + timedelta(days=1))
        for point in body.get("rollupDataPoints", []):
            day = _civil_date((point.get("civilStartTime") or {}).get("date"))
            value = point.get(key)
            if day and value and start.isoformat() <= day <= end.isoformat():
                out[day] = {name: _num(value.get(src)) for name, src in fields.items()}
        chunk_start = chunk_end + timedelta(days=1)
    return out


def _sleep_record(p: dict) -> dict:
    s = p["sleep"]
    interval = s.get("interval", {})
    summary = s.get("summary", {})
    stages = {st["type"]: _num(st.get("minutes")) for st in summary.get("stagesSummary", [])}
    return {
        "date": _civil_date((interval.get("civilEndTime") or {}).get("date")) or interval.get("endTime", "")[:10],
        "start": interval.get("startTime"),
        "end": interval.get("endTime"),
        "type": s.get("type"),
        "utcOffsetMin": int(interval.get("startUtcOffset", "0s").rstrip("s") or 0) // 60,
        "asleepMin": _num(summary.get("minutesAsleep")),
        "awakeMin": _num(summary.get("minutesAwake")),
        "stages": stages,
    }


def _exercise_record(p: dict) -> dict:
    e = p["exercise"]
    m = e.get("metricsSummary", {})
    active = e.get("activeDuration", "0s").rstrip("s")
    return {
        "start": e.get("interval", {}).get("startTime"),
        "name": e.get("displayName") or e.get("exerciseType", "").replace("_", " ").title(),
        "type": e.get("exerciseType"),
        "durationMin": round(float(active or 0) / 60, 1),
        "calories": _num(m.get("caloriesKcal")),
        "distanceMm": _num(m.get("distanceMillimeters") or m.get("distanceMillimiters")),
        "steps": _num(m.get("steps")),
        "avgHr": _num(m.get("averageHeartRateBeatsPerMinute")),
        "azm": _num(m.get("activeZoneMinutes")),
    }


def collect(session, days: int) -> dict:
    end = date.today()
    start = end - timedelta(days=days - 1)
    by_day = {(start + timedelta(days=i)).isoformat(): {} for i in range(days)}
    errors: dict[str, str] = {}

    def attempt(name, fn):
        try:
            return fn()
        except health_api.HealthApiError as e:
            errors[name] = str(e)
            return None

    jobs = {data_type: (_rollup_days, session, data_type, start, end) for data_type in ROLLUPS}
    jobs["daily-resting-heart-rate"] = (health_api.list_data_points, session, "daily-resting-heart-rate",
                                        f'daily_resting_heart_rate.date >= "{start}"', None, True)
    jobs["sleep"] = (health_api.list_data_points, session, "sleep",
                     f'sleep.interval.civil_end_time >= "{start}"', 25, True)
    jobs["exercise"] = (health_api.list_data_points, session, "exercise",
                        f'exercise.interval.civil_start_time >= "{start}"', 25, True)
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {name: pool.submit(attempt, name, lambda job=job: job[0](*job[1:])) for name, job in jobs.items()}
    results = {name: f.result() for name, f in futures.items()}

    for data_type in ROLLUPS:
        for day, values in (results[data_type] or {}).items():
            by_day[day].update(values)

    for p in results["daily-resting-heart-rate"] or []:
        r = p.get("dailyRestingHeartRate", {})
        day = _civil_date(r.get("date"))
        if day in by_day:
            by_day[day]["restingHr"] = _num(r.get("beatsPerMinute"))

    sleep = results["sleep"] or []
    exercise = results["exercise"] or []

    return {
        "range": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "days": [{"date": d, **v} for d, v in by_day.items()],
        "sleep": sorted((_sleep_record(p) for p in sleep if "sleep" in p), key=lambda r: r["end"] or ""),
        "exercise": sorted((_exercise_record(p) for p in exercise if "exercise" in p),
                           key=lambda r: r["start"] or "", reverse=True),
        "errors": errors,
        "demo": False,
    }


def heart_rate_samples(session, hours: int) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    points = health_api.list_data_points(
        session, "heart-rate", f'heart_rate.sample_time.physical_time >= "{since}"', page_size=10000, all_pages=True)
    samples = sorted(
        ({"t": p["heartRate"]["sampleTime"]["physicalTime"], "bpm": _num(p["heartRate"].get("beatsPerMinute"))}
         for p in points if "heartRate" in p),
        key=lambda s: s["t"])
    return {"hours": hours, "samples": samples, "latest": samples[-1] if samples else None,
            "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "demo": False}


def heart_art(session, days: int = 8) -> dict:
    """Per-minute mean heart rate for each of the last `days` civil days (today included)."""
    today = date.today()
    return heart_art_from_samples(insights.fetch_hr_days(session, [today - timedelta(days=i) for i in range(days - 1, -1, -1)]))


def heart_art_from_samples(raw: dict) -> dict:
    out = []
    for day, samples in sorted(raw.items()):
        sums, counts = [0.0] * 1440, [0] * 1440
        for ts, bpm in samples or []:
            local = datetime.fromtimestamp(ts, timezone.utc) + timedelta(minutes=LOCAL_OFFSET_MIN)
            m = local.hour * 60 + local.minute
            sums[m] += bpm
            counts[m] += 1
        out.append({"date": day, "bpm": [round(s / c) if c else None for s, c in zip(sums, counts)]})
    return {"days": out, "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "demo": False}


def demo_heart_art(days: int = 8) -> dict:
    rng = random.Random(7)
    today = date.today()
    out = []
    for i in range(days - 1, -1, -1):
        bpm, row = 62.0, []
        for m in range(1440):
            awake = 7 * 60 <= m < 23 * 60
            target = (78 if awake else 58) + (45 if awake and rng.random() < 0.004 else 0)
            bpm += (target - bpm) * 0.08 + rng.gauss(0, 1.8)
            row.append(round(bpm) if not (i == 0 and m > datetime.now().hour * 60) else None)
        out.append({"date": (today - timedelta(days=i)).isoformat(), "bpm": row})
    return {"days": out, "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "demo": True}


def demo_heart_rate(hours: int) -> dict:
    rng = random.Random(int(time.time() // 30))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    samples, bpm = [], 72.0
    for i in range(hours * 60, -1, -1):
        bpm = min(150, max(55, bpm + rng.gauss(0, 2.5) + (72 - bpm) * 0.05))
        samples.append({"t": (now - timedelta(minutes=i, seconds=40)).isoformat().replace("+00:00", "Z"), "bpm": round(bpm)})
    return {"hours": hours, "samples": samples, "latest": samples[-1],
            "fetchedAt": now.isoformat(), "demo": True}


def demo_data(days: int) -> dict:
    rng = random.Random(42)
    end = date.today()
    start = end - timedelta(days=days - 1)
    rows, sleep, exercise = [], [], []
    weight = 74.0
    for i in range(days):
        d = start + timedelta(days=i)
        weekend = d.weekday() >= 5
        steps = max(1500, int(rng.gauss(7000 if weekend else 9500, 2500)))
        fat, cardio, peak = rng.randint(5, 40), rng.randint(0, 25), rng.choice([0, 0, 0, 3, 8])
        weight += rng.gauss(-0.02, 0.15)
        rows.append({
            "date": d.isoformat(), "steps": steps, "distanceMm": steps * 760.0,
            "calories": round(1750 + steps * 0.045 + rng.gauss(0, 60)),
            "azmFatBurn": fat, "azmCardio": cardio * 2, "azmPeak": peak * 2,
            "hrAvg": round(rng.gauss(72, 3), 1), "hrMin": rng.randint(50, 57), "hrMax": rng.randint(130, 172),
            "weightGrams": round(weight * 1000) if rng.random() < 0.6 else None,
            "restingHr": rng.randint(56, 63),
        })
        bed = datetime.combine(d - timedelta(days=1), datetime.min.time()) + timedelta(hours=22, minutes=rng.randint(30, 120))
        deep, rem, light = rng.randint(45, 100), rng.randint(60, 120), rng.randint(180, 260)
        awake = rng.randint(20, 60)
        total = deep + rem + light + awake
        sleep.append({
            "date": d.isoformat(), "start": bed.isoformat() + "Z", "end": (bed + timedelta(minutes=total)).isoformat() + "Z",
            "type": "STAGES", "asleepMin": deep + rem + light, "awakeMin": awake,
            "stages": {"DEEP": deep, "LIGHT": light, "REM": rem, "AWAKE": awake},
        })
        if rng.random() < 0.45:
            kind = rng.choice([("Run", "RUNNING", 9.5), ("Walk", "WALKING", 4.0), ("Bike", "BIKING", 7.0),
                               ("Weights", "WEIGHTLIFTING", 5.0)])
            mins = rng.randint(20, 70)
            exercise.append({
                "start": (datetime.combine(d, datetime.min.time()) + timedelta(hours=rng.choice([7, 18]))).isoformat() + "Z",
                "name": kind[0], "type": kind[1], "durationMin": mins, "calories": round(mins * kind[2]),
                "distanceMm": mins * 150_000.0 if kind[1] != "WEIGHTLIFTING" else None,
                "steps": mins * 140 if kind[1] in ("RUNNING", "WALKING") else None,
                "avgHr": rng.randint(110, 160), "azm": rng.randint(5, mins),
            })
    return {
        "range": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "days": rows, "sleep": sleep, "exercise": exercise[::-1], "errors": {}, "demo": True,
    }


class Handler(BaseHTTPRequestHandler):
    demo = False
    _cache: dict = {}
    _lock = threading.Lock()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send(200, "text/html; charset=utf-8", (STATIC_DIR / "index.html").read_bytes())
        if url.path == "/api/dashboard":
            qs = parse_qs(url.query)
            days = min(max(int(qs.get("days", ["30"])[0]), 1), 365)
            fresh = qs.get("refresh", ["0"])[0] == "1"
            try:
                data = self._dashboard(days, fresh)
            except health_api.HealthApiError as e:
                return self._json(401, {"error": str(e)})
            return self._json(200, data)
        if url.path == "/api/insights":
            days = min(max(int(parse_qs(url.query).get("days", ["30"])[0]), 7), 365)
            if self.demo:
                return self._json(404, {"error": "Insights need real data; run without --demo."})
            try:
                data = self._insights(days)
            except health_api.HealthApiError as e:
                return self._json(401, {"error": str(e)})
            return self._json(200, data)
        if url.path == "/api/heartart":
            try:
                data = demo_heart_art() if self.demo else self._cached(("art",), lambda: heart_art(health_api.load_session()))
            except health_api.HealthApiError as e:
                return self._json(401, {"error": str(e)})
            return self._json(200, data)
        if url.path == "/api/heartrate":
            hours = min(max(int(parse_qs(url.query).get("hours", ["3"])[0]), 1), 24)
            try:
                data = demo_heart_rate(hours) if self.demo else heart_rate_samples(health_api.load_session(), hours)
            except health_api.HealthApiError as e:
                return self._json(401, {"error": str(e)})
            return self._json(200, data)
        self._send(404, "text/plain", b"not found")

    def _dashboard(self, days: int, fresh: bool) -> dict:
        if self.demo:
            return demo_data(days)
        with self._lock:
            hit = self._cache.get(days)
            if hit and not fresh and time.time() - hit[0] < CACHE_TTL_SECONDS:
                return hit[1]
            data = collect(health_api.load_session(), days)
            self._cache[days] = (time.time(), data)
            return data

    def _cached(self, key, fn):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
        value = fn()
        self._cache[key] = (time.time(), value)
        return value

    def _insights(self, days: int) -> dict:
        key = ("insights", days)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
        data = self._dashboard(days, False)
        session = health_api.load_session()
        today = date.today()
        with ThreadPoolExecutor(2) as pool:
            body = pool.submit(insights.fetch_body, session, today - timedelta(days=90))
            hr = pool.submit(insights.fetch_hr_days, session, [today - timedelta(days=i) for i in range(7, 0, -1)])
            result = insights.compute(data, body.result(), hr.result())
        self._cache[key] = (time.time(), result)
        return result

    def _json(self, status: int, payload: dict):
        self._send(status, "application/json", json.dumps(payload).encode())

    def _send(self, status: int, ctype: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Fitbit dashboard (Google Health API)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--demo", action="store_true", help="Serve synthetic data instead of calling the API")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    Handler.demo = args.demo
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Dashboard at {url}{' (demo data)' if args.demo else ''}  — Ctrl+C to stop")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
