"""Derived metrics built from Fitbit data with published formulas.

Everything here is an estimate from population-level equations, not a measurement
or medical advice. Each metric carries its formula and source so it can be checked.
"""

import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, pstdev

import health_api

PROFILE_FILE = Path(__file__).parent / "profile.json"

KCAL_PER_KG = 7700          # energy density of body-weight change (Wishnofsky 1958; approximation)
SLEEP_NEED_H = 7            # adults: >= 7 h/night (AASM/SRS consensus, Watson et al. 2015)
G = 9.80665                 # standard gravity, m/s^2
J_PER_KCAL = 4184
WALK_NET_KCAL_PER_KG_KM = 0.5   # net metabolic cost of level walking ~2.1 J/kg/m (Margaria 1963)
TRIMP_K = {"male": (0.64, 1.92), "female": (0.86, 1.67)}  # Banister 1991


# ---------- inputs ----------

def load_local_profile() -> dict:
    try:
        return json.loads(PROFILE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def fetch_body(session, since: date) -> dict:
    """Age from the Fitbit profile, height and weight history from data points."""
    jobs = {
        "profile": lambda: health_api.call(session, "GET", "users/me/profile"),
        "height": lambda: health_api.list_data_points(session, "height", None, page_size=50),
        "weight": lambda: health_api.list_data_points(
            session, "weight", f'weight.sample_time.civil_time >= "{since}"', page_size=1000, all_pages=True),
    }
    with ThreadPoolExecutor(len(jobs)) as pool:
        futures = {k: pool.submit(fn) for k, fn in jobs.items()}
    out, errors = {}, {}
    for k, f in futures.items():
        try:
            out[k] = f.result()
        except health_api.HealthApiError as e:
            out[k], errors[k] = None, str(e)
    heights = sorted((p["height"]["sampleTime"]["physicalTime"], float(p["height"]["heightMillimeters"]))
                     for p in out["height"] or [] if "height" in p)
    weights = sorted((p["weight"]["sampleTime"]["physicalTime"], float(p["weight"]["weightGrams"]) / 1000)
                     for p in out["weight"] or [] if "weight" in p)
    local = load_local_profile()
    if os.environ.get("FITBIT_SEX"):
        local["sex"] = os.environ["FITBIT_SEX"]
    return {
        "age": (out["profile"] or {}).get("age") or local.get("age"),
        "sex": local.get("sex"),
        "heightCm": heights[-1][1] / 10 if heights else local.get("heightCm"),
        "weights": [{"t": t, "kg": kg} for t, kg in weights],
        "errors": errors,
    }


def fetch_hr_days(session, days: list[date]) -> dict[str, list[tuple[float, float]]]:
    """Intraday heart-rate samples per civil day, as (epoch seconds, bpm), fetched in parallel."""
    def one(d: date):
        f = (f'heart_rate.sample_time.civil_time >= "{d}" AND '
             f'heart_rate.sample_time.civil_time < "{d + timedelta(days=1)}"')
        pts = health_api.list_data_points(session, "heart-rate", f, page_size=10000, all_pages=True)
        return sorted((datetime.fromisoformat(p["heartRate"]["sampleTime"]["physicalTime"].replace("Z", "+00:00")).timestamp(),
                       float(p["heartRate"]["beatsPerMinute"])) for p in pts if "heartRate" in p)
    with ThreadPoolExecutor(len(days) or 1) as pool:
        futures = {d.isoformat(): pool.submit(one, d) for d in days}
    out = {}
    for k, f in futures.items():
        try:
            out[k] = f.result()
        except (health_api.HealthApiError, OSError) as e:
            print(f"heart-rate fetch failed for {k}: {e}")
            out[k] = None
    return out


# ---------- math helpers ----------

def linreg(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """Least-squares slope and intercept."""
    if len(xs) < 2:
        return None
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return slope, my - slope * mx


def pearson(pairs: list[tuple[float, float]]) -> dict | None:
    """Pearson r with a 95% CI from the Fisher z-transform."""
    pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
    n = len(pairs)
    if n < 5:
        return None
    xs, ys = zip(*pairs)
    sx, sy = pstdev(xs), pstdev(ys)
    if sx == 0 or sy == 0:
        return None
    mx, my = mean(xs), mean(ys)
    r = sum((x - mx) * (y - my) for x, y in pairs) / (n * sx * sy)
    r = max(-0.999999, min(0.999999, r))
    z, se = math.atanh(r), 1 / math.sqrt(n - 3)
    lo, hi = math.tanh(z - 1.96 * se), math.tanh(z + 1.96 * se)
    return {"r": r, "n": n, "ciLow": lo, "ciHigh": hi, "significant": lo > 0 or hi < 0}


def banister_trimp(samples: list[tuple[float, float]], rest: float, hrmax: float, sex: str) -> float:
    """Sum of minutes x HRr x a*e^(b*HRr) over consecutive samples (gaps capped at 2 min)."""
    a, b = TRIMP_K[sex]
    total = 0.0
    for (t0, hr0), (t1, _) in zip(samples, samples[1:]):
        dt_min = min(t1 - t0, 120) / 60
        hrr = max(0.0, min(1.0, (hr0 - rest) / (hrmax - rest)))
        total += dt_min * hrr * a * math.exp(b * hrr)
    return total


# ---------- metrics ----------

def metric(name, value, unit="", *, digits=0, detail="", formula="", source="", status=None):
    return {"name": name, "value": None if value is None else round(value, digits), "unit": unit, "digits": digits,
            "detail": detail, "formula": formula, "source": source, "status": status}


def missing(name, why, formula="", source=""):
    return {"name": name, "value": None, "unit": "", "digits": 0, "detail": why, "formula": formula,
            "source": source, "status": "missing"}


def compute(data: dict, body: dict, hr_days: dict) -> dict:
    today = date.today().isoformat()
    days = [d for d in data["days"] if d["date"] < today]           # complete days only
    weight = body["weights"][-1]["kg"] if body["weights"] else None
    age, sex, h_cm = body["age"], body["sex"], body["heightCm"]

    def avg(key, rows=days):
        v = [r[key] for r in rows if r.get(key) is not None]
        return mean(v) if v else None

    rhr_rows = [d for d in days if d.get("restingHr") is not None]
    rhr7 = avg("restingHr", rhr_rows[-7:])
    tdee = avg("calories")
    groups = []

    # --- Energy & weight ---
    energy = []
    bmr = None
    if None not in (weight, h_cm, age) and sex in ("male", "female"):
        bmr = 10 * weight + 6.25 * h_cm - 5 * age + (5 if sex == "male" else -161)
        energy.append(metric("Resting metabolic rate", bmr, "kcal/day",
                             detail=f"Energy your body uses at complete rest ({weight:.1f} kg, {h_cm:.0f} cm, {age} y, {sex}).",
                             formula="10·W + 6.25·H − 5·A + (5 ♂ / −161 ♀)", source="Mifflin–St Jeor, 1990"))
    else:
        energy.append(missing("Resting metabolic rate", "Needs weight, height, age and sex.", source="Mifflin–St Jeor, 1990"))
    if tdee:
        energy.append(metric("Total energy expenditure", tdee, "kcal/day",
                             detail=f"Average of Fitbit's daily calories over {len([d for d in days if d.get('calories')])} complete days.",
                             formula="mean(daily total calories)", source="Fitbit measurement"))
    if bmr and tdee:
        energy.append(metric("Physical activity level", tdee / bmr, "× RMR", digits=2,
                             detail=_pal_band(tdee / bmr), formula="PAL = TEE ÷ RMR", source="FAO/WHO/UNU, 2004"))
        energy.append(metric("Activity energy", tdee - bmr, "kcal/day",
                             detail=f"{(tdee - bmr) / tdee * 100:.0f}% of your daily burn is above resting.",
                             formula="TEE − RMR (includes digestion, ≈10% of intake)", source="Energy partitioning"))

    trend = None
    pts = [(datetime.fromisoformat(w["t"].replace("Z", "+00:00")).timestamp() / 86400, w["kg"]) for w in body["weights"]]
    if len(pts) >= 2 and pts[-1][0] - pts[0][0] >= 7:
        trend = linreg([p[0] for p in pts], [p[1] for p in pts])
    if trend:
        slope_day = trend[0]
        energy.append(metric("Weight trend", slope_day * 7, "kg/week", digits=2,
                             detail=f"Least-squares line through {len(pts)} weigh-ins over {pts[-1][0] - pts[0][0]:.0f} days.",
                             formula="slope of weight ~ time", source="Ordinary least squares"))
        if tdee:
            balance = slope_day * KCAL_PER_KG
            energy.append(metric("Implied energy intake", tdee + balance, "kcal/day",
                                 detail=f"What you must be eating on average for this trend: you burn {tdee:,.0f} kcal/day and "
                                        f"{'store' if balance >= 0 else 'draw'} {abs(balance):,.0f} kcal/day "
                                        f"{'as body mass' if balance >= 0 else 'from body stores'}.",
                                 formula="intake = TEE + Δweight/day × 7700 kcal/kg",
                                 source="First law of thermodynamics; Wishnofsky 1958"))
    else:
        energy.append(missing("Weight trend & implied intake", "Needs 2+ weigh-ins at least 7 days apart.",
                              formula="intake = TEE + Δweight/day × 7700 kcal/kg"))
    if tdee:
        energy.append(metric("Intake for −0.5 kg/week", tdee - 0.5 * KCAL_PER_KG / 7, "kcal/day",
                             detail=f"A {0.5 * KCAL_PER_KG / 7:,.0f} kcal/day deficit; real loss slows as weight drops (adaptive thermogenesis).",
                             formula="TEE − 0.5 × 7700 ÷ 7", source="Energy balance; Hall et al. 2011"))
    if weight and h_cm:
        bmi = weight / (h_cm / 100) ** 2
        energy.append(metric("Body mass index", bmi, "kg/m²", digits=1, detail=_bmi_band(bmi),
                             formula="BMI = W ÷ H²", source="WHO classification"))
    groups.append({"title": "Energy & weight", "subtitle": "Thermodynamics: stored energy = energy in − energy out", "metrics": energy})

    # --- Movement & physics ---
    move = []
    steps, km = avg("steps"), avg("distanceMm")
    km = km / 1e6 if km else None
    if steps and km:
        move.append(metric("Stride length", km * 1000 / steps, "m", digits=2,
                           detail="Average metres covered per step.", formula="distance ÷ steps", source="Kinematics"))
    if km and weight:
        walk_kcal = WALK_NET_KCAL_PER_KG_KM * weight * km
        lift_m = walk_kcal * J_PER_KCAL / (weight * G)
        move.append(metric("Walking energy (net)", walk_kcal, "kcal/day",
                           detail=f"{km:.2f} km/day × {weight:.0f} kg. The same energy could lift you {lift_m:,.0f} m against gravity.",
                           formula="E ≈ 0.5 kcal·kg⁻¹·km⁻¹ × W × d;  h = E ÷ (m·g)",
                           source="Margaria 1963; Newtonian mechanics"))
        if tdee and bmr:
            move.append(metric("Walking share of activity", walk_kcal / (tdee - bmr) * 100, "%",
                               detail="How much of your above-rest burn comes from walking alone.",
                               formula="walking energy ÷ (TEE − RMR)", source="Derived"))
    groups.append({"title": "Movement & physics", "subtitle": "Work, energy and gravity", "metrics": move})

    # --- Heart ---
    heart, zones = [], []
    hrmax = 208 - 0.7 * age if age else None
    if hrmax:
        heart.append(metric("Max heart rate (predicted)", hrmax, "bpm", detail=f"Age {age}.",
                            formula="HRmax = 208 − 0.7 × age", source="Tanaka et al., 2001"))
    if hrmax and rhr7:
        hrr = hrmax - rhr7
        heart.append(metric("Heart-rate reserve", hrr, "bpm", detail=f"Using 7-day average resting HR {rhr7:.0f} bpm.",
                            formula="HRR = HRmax − HRrest", source="Karvonen et al., 1957"))
        vo2 = 15.3 * hrmax / rhr7
        heart.append(metric("VO₂max (estimated)", vo2, "ml/kg/min", digits=1, detail=_vo2_band(vo2, age),
                            formula="VO₂max ≈ 15.3 × HRmax ÷ HRrest", source="Uth et al., 2004"))
        zones = [{"zone": name, "lo": round(rhr7 + lo * hrr), "hi": round(rhr7 + hi * hrr)}
                 for name, lo, hi in [("Recovery", .5, .6), ("Aerobic base", .6, .7), ("Tempo", .7, .8),
                                      ("Threshold", .8, .9), ("Max effort", .9, 1.0)]]
    if len(rhr_rows) >= 8:
        base = [d["restingHr"] for d in rhr_rows[-29:-1]]
        latest = rhr_rows[-1]["restingHr"]
        sd = pstdev(base)
        if sd > 0:
            z = (latest - mean(base)) / sd
            heart.append(metric("Resting HR vs baseline", z, "SD", digits=1,
                                detail=f"{latest:.0f} bpm vs {mean(base):.1f} ± {sd:.1f} over {len(base)} days. "
                                       + ("Above +2 SD often signals strain, poor sleep or illness." if z > 2 else "Within normal variation." if abs(z) <= 2 else "Unusually low."),
                                formula="z = (today − mean) ÷ SD", source="Standard score; RHR as recovery marker",
                                status="warn" if z > 2 else None))
    trimp_days = []
    if hrmax and rhr7 and sex in TRIMP_K:
        for d, samples in sorted(hr_days.items()):
            if samples and d < today:
                trimp_days.append({"date": d, "trimp": round(banister_trimp(samples, rhr7, hrmax, sex), 1)})
        if trimp_days:
            heart.append(metric("Training load (7 days)", sum(t["trimp"] for t in trimp_days), "TRIMP",
                                detail=f"Banister impulse over {len(trimp_days)} days of intraday heart rate; weighs time by how hard the heart worked.",
                                formula="Σ Δt · HRr · %.2f·e^(%.2f·HRr),  HRr = (HR − HRrest) ÷ HRR" % TRIMP_K[sex],
                                source="Banister, 1991"))
    groups.append({"title": "Heart & fitness", "subtitle": "Cardiovascular physiology", "metrics": heart,
                   "zones": zones, "trimp": trimp_days})

    # --- Sleep ---
    sleep_m = []
    nights = _main_sleeps(data["sleep"])
    if nights:
        eff = [n["asleepMin"] / n["inBedMin"] * 100 for n in nights if n["inBedMin"]]
        if eff:
            e = mean(eff)
            sleep_m.append(metric("Sleep efficiency", e, "%", digits=1,
                                  detail="Healthy adults typically ≥ 85%." if e >= 85 else "Below the 85% reference; time awake in bed is high.",
                                  formula="time asleep ÷ time in bed × 100", source="AASM sleep scoring", status=None if e >= 85 else "warn"))
        last7 = nights[-7:]
        debt = sum(max(0, SLEEP_NEED_H * 60 - n["asleepMin"]) for n in last7) / 60
        sleep_m.append(metric("Sleep debt (7 nights)", debt, "h", digits=1,
                              detail=f"Shortfall against {SLEEP_NEED_H} h/night across your last {len(last7)} main sleeps.",
                              formula=f"Σ max(0, {SLEEP_NEED_H} h − sleep)", source="Watson et al., 2015 (AASM/SRS)",
                              status="warn" if debt > 5 else None))
        mids = [n["midpoint"] for n in nights if n["midpoint"] is not None and n["asleepMin"] >= 180]  # skip naps
        if len(mids) >= 3:
            sd_min = _circular_sd_minutes(mids)
            sleep_m.append(metric("Sleep timing regularity", sd_min, "min SD",
                                  detail=f"Spread of your sleep midpoint (avg {_clock(_circular_mean_minutes(mids))}). Under ~30 min is regular.",
                                  formula="circular SD of sleep midpoints", source="Circadian regularity; Phillips et al., 2017"))
    groups.append({"title": "Sleep", "subtitle": "Circadian biology", "metrics": sleep_m})

    # --- Cross-relations ---
    by_date = {d["date"]: d for d in days}
    sleep_by_date = {}
    for n in nights:
        sleep_by_date[n["date"]] = n["asleepMin"] / 60
    nxt = lambda d: (date.fromisoformat(d) + timedelta(days=1)).isoformat()
    azm = lambda r: None if r.get("azmFatBurn") is None else (r.get("azmFatBurn") or 0) + (r.get("azmCardio") or 0) + (r.get("azmPeak") or 0)
    pairs = [
        ("Steps → calories burned (same day)", "Sanity check: more movement should mean more energy.",
         [(r.get("steps"), r.get("calories")) for r in days]),
        ("Sleep → steps (that day)", "Do longer nights give more active days?",
         [(sleep_by_date.get(d), r.get("steps")) for d, r in by_date.items()]),
        ("Sleep → resting HR (that morning)", "Short sleep tends to raise resting heart rate.",
         [(sleep_by_date.get(d), r.get("restingHr")) for d, r in by_date.items()]),
        ("Active zone minutes → next-day resting HR", "Hard days can raise the next morning's resting HR.",
         [(azm(r), by_date.get(nxt(d), {}).get("restingHr")) for d, r in by_date.items()]),
        ("Average HR → calories burned", "Energy cost rises with heart rate (oxygen uptake).",
         [(r.get("hrAvg"), r.get("calories")) for r in days]),
    ]
    corr = []
    for name, why, ps in pairs:
        c = pearson(ps)
        corr.append({"name": name, "why": why, **(c or {"r": None, "n": len([p for p in ps if None not in p])})})
    groups.append({"title": "Cross-relations", "subtitle": "Pearson correlation with 95% confidence interval — correlation is not causation",
                   "metrics": [], "correlations": corr})

    return {"groups": groups, "inputs": {"age": age, "sex": sex, "heightCm": h_cm, "weightKg": weight,
                                         "restingHr7": rhr7, "days": len(days)},
            "errors": body.get("errors", {}),
            "disclaimer": "Estimates from population equations, not measurements or medical advice."}


# ---------- small helpers ----------

def _pal_band(p):
    if p < 1.4: return "Sedentary (FAO/WHO: < 1.40 is below the sedentary range)."
    if p < 1.7: return "Sedentary or light activity lifestyle (1.40–1.69)."
    if p < 2.0: return "Active or moderately active lifestyle (1.70–1.99)."
    return "Vigorous or very active lifestyle (2.00–2.40)."


def _bmi_band(b):
    if b < 18.5: return "Underweight (< 18.5). BMI ignores muscle vs fat."
    if b < 25: return "Normal range (18.5–24.9). BMI ignores muscle vs fat."
    if b < 30: return "Overweight range (25–29.9). BMI ignores muscle vs fat."
    return "Obesity range (≥ 30). BMI ignores muscle vs fat."


def _vo2_band(v, age):
    # Rough ACSM-style bands for men aged 20-29; shown only as a reference point.
    if age and age < 30:
        bands = [(33, "Poor"), (37, "Fair"), (42, "Good"), (46, "Excellent")]
        label = next((l for lim, l in bands if v < lim), "Superior")
        return f"{label} for men aged 20–29 (ACSM reference). Resting-HR method has ±~5 ml/kg/min error."
    return "Resting-HR method has ±~5 ml/kg/min error."


def _main_sleeps(sleeps: list[dict]) -> list[dict]:
    """Longest sleep per date, with time in bed and local midpoint (minutes after midnight)."""
    best = {}
    for s in sleeps:
        if not (s.get("start") and s.get("end") and s.get("asleepMin")):
            continue
        t0 = datetime.fromisoformat(s["start"].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(s["end"].replace("Z", "+00:00"))
        in_bed = (t1 - t0).total_seconds() / 60
        off = s.get("utcOffsetMin") or 0
        mid = ((t0 + (t1 - t0) / 2).hour * 60 + (t0 + (t1 - t0) / 2).minute + off) % 1440
        rec = {"date": s["date"], "asleepMin": s["asleepMin"], "inBedMin": in_bed, "midpoint": mid}
        if s["date"] not in best or rec["asleepMin"] > best[s["date"]]["asleepMin"]:
            best[s["date"]] = rec
    return [best[k] for k in sorted(best)]


def _circular_mean_minutes(mins):
    a = [m / 1440 * 2 * math.pi for m in mins]
    ang = math.atan2(mean(math.sin(x) for x in a), mean(math.cos(x) for x in a))
    return (ang / (2 * math.pi) * 1440) % 1440


def _circular_sd_minutes(mins):
    a = [m / 1440 * 2 * math.pi for m in mins]
    rbar = math.hypot(mean(math.sin(x) for x in a), mean(math.cos(x) for x in a))
    return math.sqrt(-2 * math.log(max(rbar, 1e-9))) / (2 * math.pi) * 1440


def _clock(m):
    m = int(round(m)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"
