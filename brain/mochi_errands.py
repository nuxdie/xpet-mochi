#!/usr/bin/env python3
"""mochi-errands: things to do that need them somewhere, matched against where their phone is.

Mochi writes memory/errands.json (a JSON list) from their tasks, open loops, mail and chats: anything that can only be
done at a place (post a letter, pick up a parcel, buy batteries, drop off a return). The relay (mochi-brain) reads it
every minute against the phone's latest fix and, when they are out, near one of the places, free in the calendar,
and the place is open, pokes them on Telegram with what to do. This file holds the matching; the relay holds the
rationing and the asks. The CLI is for Mochi:

  mochi-errands list                  the errands and what the relay thinks of each (done, snoozed, last poked)
  mochi-errands near [LAT LON]        what would match where they are now (or there), the time gates ignored
  mochi-errands find FILTER [--near PLACE | --at LAT LON] [--radius M]   places of a kind from OpenStreetMap, to
                                      pick concrete ones for `at`; FILTER like shop=chemist, brand=Kruidvat,
                                      amenity=post_box (PLACE: a named zone from mochi-phone, "home" by default)
  mochi-errands hours "SPEC" [WHEN]   whether an opening_hours string is open now (or at WHEN)

An errand:
  {"id": "post-weid", "do": "post the We-ID envelope", "minutes": 5,
   "at": [{"name": "PostNL point AH Utrechtsestraat", "lat": 52.36, "lon": 4.89, "r": 100, "hours": "Mo-Sa 08:00-21:00"}],
   "osm": ["amenity=post_box"], "near": 250,
   "hours": "Mo-Sa 09:00-17:00", "from": "2026-10-20", "until": "2026-10-31", "every": 7,
   "task": "Google task id", "loop": "the open-loops line it belongs to", "from_home": false}
`do` is the poke itself, in the imperative. `at` are concrete places, `osm` any place of a kind (OpenStreetMap tag
filters, looked up around them and cached); one or both. `hours` limits when the errand can be done (otherwise a
place's own opening_hours from OSM, when known). `every` (days) brings it back that long after Done.
"""
import datetime as dt
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"
ERRANDS = WORK / "memory" / "errands.json"
LATEST = WORK / "phone" / "latest.json"
STATE = WORK / "relay.json"
CACHE = WORK / "phone" / "osm-places.json"
PHONE_CONFIG = Path(os.environ.get("MOCHI_PHONE_CONFIG", HOME / ".config/mochi/phone.json"))
OVERPASS = "https://overpass-api.de/api/interpreter"
USER_AGENT = "mochi-errands/1 (a personal assistant; one lookup per area per week)"
AT_RADIUS = 150      # m: a concrete place counts as "here" within this, unless it says otherwise
NEAR_RADIUS = 250    # m: any place of a kind within this
TILE = 0.01          # degrees: OSM lookups are cached per tile (about 1.1 x 0.7 km here)
TILE_PAD = 400       # m: each lookup reaches this far past its tile, so a place just over the edge still counts
CACHE_TTL = 14 * 86400
DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


def distance_m(a_lat, a_lon, b_lat, b_lon):
    p = math.pi / 180
    h = 0.5 - math.cos((b_lat - a_lat) * p) / 2 + math.cos(a_lat * p) * math.cos(b_lat * p) * (1 - math.cos((b_lon - a_lon) * p)) / 2
    return 12742000 * math.asin(math.sqrt(h))


def jload(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def jsave(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(path)


def eid(e):
    return re.sub(r"[^a-zA-Z0-9_-]", "-", str(e.get("id") or ""))[:32]


def load(path=ERRANDS):
    """The errands Mochi wrote, and what's wrong with the file if anything (the relay shows that to Mochi)."""
    if not Path(path).exists():
        return [], ""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        return [], f"can't parse {Path(path).name}: {e}"
    if isinstance(data, dict):
        data = data.get("errands", [])
    if not isinstance(data, list):
        return [], f"{Path(path).name} must be a JSON list of errands"
    out, seen = [], set()
    for e in data:
        if isinstance(e, dict) and eid(e) and eid(e) not in seen and e.get("do") and (e.get("at") or e.get("osm")):
            seen.add(eid(e))
            out.append(e)
    return out, ""


def day(value):
    try:
        return dt.date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def in_season(e, today=None):
    today = today or dt.date.today()
    start, end = day(e.get("from")), day(e.get("until"))
    return not (start and today < start) and not (end and today > end)


# ---- opening hours: the common part of OSM's opening_hours, enough for shops and post offices ----------------

def _days(spec):
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            if a not in DAYS or b not in DAYS:
                return None
            i, j = DAYS.index(a), DAYS.index(b)
            out.update(range(i, j + 1) if i <= j else [*range(i, 7), *range(0, j + 1)])
        elif part in DAYS:
            out.add(DAYS.index(part))
        else:
            return None
    return out


def _mins(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def open_at(spec, when=None, for_minutes=0):
    """True/False if `spec` (an OSM opening_hours value, simple forms only) says the place is open at `when` and
    stays open for_minutes more; None when the spec is empty or beyond what this understands (then don't judge)."""
    if not spec or not isinstance(spec, str):
        return None
    when = when or dt.datetime.now()
    spec = spec.strip()
    if spec == "24/7":
        return True
    wd, t = when.weekday(), when.hour * 60 + when.minute
    rules = {}  # weekday -> list of (start, end) minutes; a later rule for a day replaces an earlier one (OSM's way)
    for rule in [r.strip() for r in spec.split(";") if r.strip()]:
        if rule.startswith("PH") or rule.startswith("SH"):
            continue  # public/school holidays: not known here; the ordinary days still are
        m = re.fullmatch(r"([A-Za-z,\- ]+?)?\s*((?:\d{1,2}:\d{2}-\d{1,2}:\d{2}\+?\s*,?\s*)+|off|closed)", rule)
        if not m:
            return None
        days = _days(m.group(1).replace(" ", "")) if m.group(1) else set(range(7))
        if days is None:
            return None
        spans = []
        if m.group(2) not in ("off", "closed"):
            for span in re.findall(r"\d{1,2}:\d{2}-\d{1,2}:\d{2}", m.group(2)):
                a, b = span.split("-")
                spans.append((_mins(a), _mins(b)))
        for d in days:
            rules[d] = spans
    if not rules:
        return None

    def inside(d, minute):
        for a, b in rules.get(d, []):
            if b <= a:  # past midnight: 18:00-02:00
                if minute >= a:
                    return min(b + 24 * 60, 48 * 60)
            elif a <= minute < b:
                return b
        for a, b in rules.get((d - 1) % 7, []):  # yesterday's late spell running into today
            if b <= a and minute < b:
                return b
        return None

    close = inside(wd, t)
    return close is not None and close - t >= for_minutes


# ---- places of a kind, from OpenStreetMap ---------------------------------------------------------------

def parse_filter(f):
    """'shop=chemist' -> ('shop', 'chemist'); 'brand~Kruidvat' -> regex match; 'amenity' -> any value."""
    f = str(f).strip()
    for op in ("~", "="):
        if op in f:
            k, v = f.split(op, 1)
            return k.strip(), op, v.strip()
    return f, "", ""


def overpass_query(filters, s, w, n, e):
    parts = []
    for f in filters:
        k, op, v = parse_filter(f)
        if not re.fullmatch(r"[A-Za-z_:]+", k):
            continue
        val = v.replace('"', "")
        cond = f'["{k}"]' if not op else f'["{k}"="{val}"]' if op == "=" else f'["{k}"~"{val}",i]'
        parts.append(f"nwr{cond}({s:.5f},{w:.5f},{n:.5f},{e:.5f});")
    return f"[out:json][timeout:25];({''.join(parts)});out center tags 300;"


def osm_fetch(filters, s, w, n, e, timeout=40):
    q = overpass_query(filters, s, w, n, e)
    req = urllib.request.Request(OVERPASS, data=urllib.parse.urlencode({"data": q}).encode(),
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read().decode())
    out = []
    for el in j.get("elements", []):
        lat, lon = el.get("lat") or (el.get("center") or {}).get("lat"), el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        tags = el.get("tags") or {}
        name = tags.get("name") or tags.get("brand") or tags.get("operator") or \
            next((f"{k}={tags[k]}" for k in ("amenity", "shop") if k in tags), "?")
        addr = " ".join(x for x in (tags.get("addr:street"), tags.get("addr:housenumber")) if x)
        out.append({"name": name, "addr": addr, "lat": lat, "lon": lon,
                    "hours": tags.get("opening_hours") or "",
                    "osm": f"{el.get('type')}/{el.get('id')}"})
    return out


def tile_of(lat, lon):
    return math.floor(lat / TILE), math.floor(lon / TILE)


def places_of_kind(filters, lat, lon, cache=None, fetch=osm_fetch, t=None):
    """Every place matching the filters in the tile around (lat, lon), from the cache or one Overpass call. The
    cache is a dict (saved by the caller); None means "the lookup failed, try again later"."""
    filters = [filters] if isinstance(filters, str) else list(filters or [])
    if not filters:
        return []
    t = t or time.time()
    cache = cache if cache is not None else {}
    ty, tx = tile_of(lat, lon)
    key = f"{'|'.join(sorted(filters))}@{ty},{tx}"
    hit = cache.get(key)
    if hit and t - hit.get("t", 0) < CACHE_TTL:
        return hit["places"]
    pad_lat = TILE_PAD / 111_000
    pad_lon = TILE_PAD / (111_000 * max(0.2, math.cos(math.radians(lat))))
    try:
        places = fetch(filters, ty * TILE - pad_lat, tx * TILE - pad_lon, (ty + 1) * TILE + pad_lat, (tx + 1) * TILE + pad_lon)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    cache[key] = {"t": t, "places": places}
    if len(cache) > 400:
        for k in sorted(cache, key=lambda k: cache[k].get("t", 0))[:100]:
            del cache[k]
    return places


# ---- the match ---------------------------------------------------------------------------------------------

def zones():
    return (jload(PHONE_CONFIG, {}).get("places") or {})


def resolve_at(spec):
    """An `at` entry: a dict with lat/lon, or the name of a mochi-phone zone."""
    if isinstance(spec, str):
        z = zones().get(spec)
        return {"name": spec, "lat": z[0], "lon": z[1], "r": z[2] if len(z) > 2 else AT_RADIUS} if z else None
    if isinstance(spec, dict) and spec.get("lat") is not None and spec.get("lon") is not None:
        return spec
    return None


def nearest(e, lat, lon, acc=0, cache=None, fetch=osm_fetch, when=None, check_hours=True, skip=()):
    """The closest place this errand can be done at from (lat, lon), as {name, lat, lon, d, hours}, or None.
    Places closed now (or closing before the errand is done) don't count, nor ones named in `skip`."""
    minutes = int(e.get("minutes") or 10)
    slack = min(acc or 0, 80)
    found = []
    for spec in e.get("at") or []:
        p = resolve_at(spec)
        if p:
            d = distance_m(lat, lon, float(p["lat"]), float(p["lon"]))
            if d <= float(p.get("r") or AT_RADIUS) + slack:
                found.append(dict(p, d=d))
    if e.get("osm"):
        for p in places_of_kind(e["osm"], lat, lon, cache, fetch) or []:
            d = distance_m(lat, lon, p["lat"], p["lon"])
            if d <= float(e.get("near") or NEAR_RADIUS) + slack:
                found.append(dict(p, d=d))
    for p in sorted(found, key=lambda p: p["d"]):
        if p.get("name") in skip or p.get("osm") in skip:
            continue
        if check_hours and open_at(p.get("hours"), when, minutes) is False:
            continue
        return p
    return None


def doable_now(e, when=None):
    """The errand's own window (`hours`), the season (`from`/`until`)."""
    when = when or dt.datetime.now()
    if not in_season(e, when.date()):
        return False
    return open_at(e.get("hours"), when, int(e.get("minutes") or 10)) is not False


def maps_link(p, exact=False):
    """A Maps link for the place. Not exact: a search by its name alone, which Google answers near wherever the phone
    is, so no coordinates or street cross Telegram; exact (for the desk) points at the place itself."""
    if exact:
        return f"https://www.google.com/maps/search/?api=1&query={p['lat']:.6f}%2C{p['lon']:.6f}"
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(str(p.get("name") or "")[:60])


# ---- their time: the calendar between now and the errand's end ------------------------------------------

LOG_CALENDARS = ("building", "spending", "sidetracking", "sleep", "holiday", "f1")  # logs and background, never "busy"
SOURCES = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi" / "sources.json"


def log_calendar(name):
    keys = set(LOG_CALENDARS)
    for k, c in ((jload(SOURCES, {}).get("calendar") or {}).get("caldav") or {}).items():
        if isinstance(c, dict) and c.get("log"):
            keys.add(k.lower().split("-")[0])
    return any(k in str(name or "").lower() for k in keys)


def agenda(timeout=60):
    """Today's and tomorrow's events from mochi-calendar, or None when it can't be read."""
    import subprocess
    try:
        p = subprocess.run(["mochi-calendar", "agenda", "--days", "2", "--json"], capture_output=True, text=True, timeout=timeout)
        return json.loads(p.stdout) if p.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def busy_spans(rows):
    """(start, end, title) for what holds them: timed events in the plan calendars they haven't declined and that
    aren't marked free."""
    out = []
    for ev in rows or []:
        if log_calendar(ev.get("calendar")) or ev.get("transparency") == "transparent" or ev.get("status") == "cancelled":
            continue
        if any(a.get("self") and a.get("responseStatus") == "declined" for a in ev.get("attendees") or []):
            continue
        s, e = (ev.get("start") or {}).get("dateTime"), (ev.get("end") or {}).get("dateTime")
        if not s or not e:
            continue  # all-day: a holiday, a trip's span, a birthday; not an hour they're held
        try:
            out.append((dt.datetime.fromisoformat(s), dt.datetime.fromisoformat(e), str(ev.get("summary") or "")))
        except ValueError:
            continue
    return sorted(out)


def free_until(spans, when=None):
    """When their next commitment starts (None: nothing in the window), or `when` itself if one is on now."""
    when = when or dt.datetime.now().astimezone()
    for s, e, _ in spans:
        if s <= when < e:
            return when
        if s > when:
            return s
    return None


# ---- CLI ---------------------------------------------------------------------------------------------------

def fix():
    loc = (jload(LATEST, {}).get("loc") or {})
    return loc if loc.get("lat") is not None else None


def cmd_list(_args):
    errands, err = load()
    if err:
        print(f"! {err}")
    st = (jload(STATE, {}).get("errands") or {}).get("items") or {}
    if not errands:
        print(f"no errands ({ERRANDS})")
    for e in errands:
        s = st.get(eid(e)) or {}
        marks = []
        for k in ("done", "dropped", "snoozed", "poked"):
            if s.get(k):
                marks.append(f"{k} {dt.datetime.fromtimestamp(s[k]):%m-%d %H:%M}")
        if not in_season(e):
            marks.append("out of season")
        where = ", ".join(p if isinstance(p, str) else p.get("name", "?") for p in e.get("at") or [])
        kinds = e.get("osm") if isinstance(e.get("osm"), str) else ", ".join(e.get("osm") or [])
        print(f"{eid(e):24} {e['do'][:70]}\n{'':24} at: {where or '-'}  kinds: {kinds or '-'}  "
              f"{(e.get('minutes') or 10)} min  {' · '.join(marks) or 'waiting'}")


def cmd_near(args):
    pos = [a for a in args if not a.startswith("--")]
    if len(pos) >= 2:
        lat, lon, acc = float(pos[0]), float(pos[1]), 0
    else:
        loc = fix()
        if not loc:
            sys.exit("no fix from the phone yet")
        lat, lon, acc = loc["lat"], loc["lon"], loc.get("acc") or 0
        print(f"from {loc.get('address') or f'{lat},{lon}'} ({loc.get('zone')}, fix {int((time.time() - (loc.get('t') or 0)) / 60)} min old)")
    cache = jload(CACHE, {})
    errands, err = load()
    if err:
        print(f"! {err}")
    hits = 0
    for e in errands:
        p = nearest(e, lat, lon, acc, cache, check_hours=False)
        if p:
            hits += 1
            o = open_at(p.get("hours"), None, int(e.get("minutes") or 10))
            print(f"{eid(e)}: {p['name']} {p['d']:.0f} m ({'open' if o else 'closed' if o is False else 'hours unknown'})"
                  f" — {e['do']}\n    {maps_link(p)}")
    jsave(CACHE, cache)
    if not hits:
        print("nothing doable from here")


def cmd_find(args):
    pos, i, radius, at = [], 0, 1000, None
    while i < len(args):
        a = args[i]
        if a == "--radius":
            radius, i = float(args[i + 1]), i + 2
        elif a == "--at":
            at, i = (float(args[i + 1]), float(args[i + 2])), i + 3
        elif a == "--near":
            z = zones().get(args[i + 1])
            if not z:
                sys.exit(f"no place {args[i + 1]!r} (mochi-phone place --list)")
            at, i = (z[0], z[1]), i + 2
        else:
            pos.append(a)
            i += 1
    if not pos:
        sys.exit(__doc__)
    if not at:
        z = zones().get("home")
        loc = fix()
        at = (z[0], z[1]) if z else (loc["lat"], loc["lon"]) if loc else sys.exit("where? --near PLACE or --at LAT LON")
    lat, lon = at
    pad_lat, pad_lon = radius / 111_000, radius / (111_000 * math.cos(math.radians(lat)))
    try:
        places = osm_fetch(pos, lat - pad_lat, lon - pad_lon, lat + pad_lat, lon + pad_lon)
    except (urllib.error.URLError, OSError, ValueError) as e:
        sys.exit(f"overpass: {e}")
    places = sorted((dict(p, d=distance_m(lat, lon, p["lat"], p["lon"])) for p in places), key=lambda p: p["d"])
    for p in [p for p in places if p["d"] <= radius][:30]:
        print(json.dumps({"name": p["name"], "lat": round(p["lat"], 6), "lon": round(p["lon"], 6),
                          **({"hours": p["hours"]} if p.get("hours") else {})}, ensure_ascii=False) + f"   # {p['d']:.0f} m")
    if not places:
        print("nothing found")


def cmd_hours(args):
    if not args:
        sys.exit('hours "Mo-Fr 09:00-18:00; Sa 10:00-17:00" [YYYY-MM-DD HH:MM]')
    when = dt.datetime.fromisoformat(args[1]) if len(args) > 1 else None
    r = open_at(args[0], when)
    print({True: "open", False: "closed", None: "can't tell from that"}[r])


def main(argv):
    cmds = {"list": cmd_list, "near": cmd_near, "find": cmd_find, "hours": cmd_hours}
    if len(argv) < 2 or argv[1] not in cmds:
        print(__doc__)
        return 0 if len(argv) < 2 else 1
    cmds[argv[1]](argv[2:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
