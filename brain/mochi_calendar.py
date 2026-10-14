#!/usr/bin/env python3
"""mochi-calendar — their Google Calendars in both accounts, through the Calendar API (OAuth, scope calendar).

    mochi-calendar calendars [-a ACCOUNT]                   every calendar each account sees, with ids
    mochi-calendar agenda [--days N] [--from DATE] [--cal C] [-a ACCOUNT] [--json]   what's coming, day by day
    mochi-calendar search WORDS [--back N] [--days N] [--cal C] [-a ACCOUNT]          events matching WORDS
    mochi-calendar show EVENT [--cal C] [-a ACCOUNT]        one event in full (guests, link, description)
    mochi-calendar log [N]                                  what Mochi changed

  changing (logged to calendar.log in Mochi's workspace; there is no delete, and guests hear nothing unless asked):
    mochi-calendar add TITLE --start WHEN [--end WHEN | --for DURATION] [--where W] [--notes T] [--cal C]
                   [-a ACCOUNT] [--invite EMAIL ...] [--remind MINUTES]
    mochi-calendar edit EVENT [--title T] [--start WHEN] [--end WHEN | --for DURATION] [--where W] [--notes T]
                   [--cal C] [-a ACCOUNT] [--notify]
    mochi-calendar respond EVENT yes|no|maybe [--cal C] [-a ACCOUNT]

WHEN is local time: "2026-10-20 15:00", or a date alone for an all-day event; "today" and "tomorrow" work in place
of the date. DURATION is like 45m, 1h, 1h30m, 2d. C is a calendar's name or id ("primary" is the account's own).
EVENT is an event id as the listings print it, or a Google Calendar event link. --invite sends the guests Google's
invitation mail, and --notify tells existing guests about an edit; without them nobody but them sees the change.
`respond` tells the organizer, as Google always does. Accounts and their sign-in are mochi-google's.
"""

import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

HOME = Path.home()
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"
LOG = WORK / "calendar.log"
API = "https://www.googleapis.com/calendar/v3"


def _google():
    """The shared Google sign-in (brain/mochi_google.py; installed beside this as mochi-google)."""
    import importlib.machinery
    import importlib.util
    here = Path(__file__).resolve().parent
    path = next((here / n for n in ("mochi_google.py", "mochi-google") if (here / n).exists()), None)
    if not path:
        sys.exit(f"mochi-calendar: mochi-google is not installed beside {here}")
    spec = importlib.util.spec_from_loader("mochi_google", importlib.machinery.SourceFileLoader("mochi_google", str(path)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.PROG = "mochi-calendar"
    return mod


google = _google()
die, ApiError = google.die, google.ApiError


def call(account, method, path, **kw):
    return google.call(account, method, API + path, scope=google.CALENDAR, **kw)


def accounts(a):
    return google.accounts_for(getattr(a, "account", None), google.CALENDAR)


def local_tz():
    """The machine's IANA zone (Europe/Amsterdam), which the API wants beside a local dateTime."""
    try:
        return Path("/etc/timezone").read_text().strip()
    except OSError:
        pass
    target = os.path.realpath("/etc/localtime")
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else "UTC"


# ---- times ------------------------------------------------------------------------------------------------

def parse_when(s, today=None):
    """(date, time or None) from "2026-10-20 15:00", "2026-10-20", "tomorrow 9:30"."""
    today = today or dt.date.today()
    s = re.sub(r"^(\d{4}-\d\d-\d\d)t", r"\1 ", s.strip().lower())  # ISO's 2026-10-20T15:00 too
    parts = s.split()
    if not parts:
        die("empty time")
    word = {"today": today, "tomorrow": today + dt.timedelta(days=1)}.get(parts[0])
    try:
        day = word or dt.date.fromisoformat(parts[0])
    except ValueError:
        die(f"WHEN is like '2026-10-20 15:00', '2026-10-20' or 'tomorrow 9:30', not {s!r}")
    if len(parts) == 1:
        return day, None
    m = re.fullmatch(r"(\d{1,2}):(\d\d)", parts[1])
    if len(parts) > 2 or not m or int(m[1]) > 23 or int(m[2]) > 59:
        die(f"the time in {s!r} is like 15:00")
    return day, dt.time(int(m[1]), int(m[2]))


def parse_duration(s):
    m = re.fullmatch(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?", s.strip().lower())
    if not m or not any(m.groups()):
        die(f"DURATION is like 45m, 1h, 1h30m or 2d, not {s!r}")
    d, h, mi = (int(x or 0) for x in m.groups())
    return dt.timedelta(days=d, hours=h, minutes=mi)


def span(start, end=None, dur=None, tz=None):
    """The API's start/end for a WHEN and an optional end or duration."""
    tz = tz or local_tz()
    sd, st = parse_when(start)
    if st is None:  # all day: the end date is exclusive
        if end:
            ed, et = parse_when(end)
            if et is not None:
                die("an all-day event ends on a date, not a time")
            ed += dt.timedelta(days=1)
        else:
            ed = sd + (dt.timedelta(days=max(1, (dur or dt.timedelta(days=1)).days)))
        if ed <= sd:
            die("the end is before the start")
        return {"date": sd.isoformat()}, {"date": ed.isoformat()}
    s = dt.datetime.combine(sd, st)
    if end:
        ed, et = parse_when(end)
        if et is None:
            die("--end needs a time for a timed event")
        e = dt.datetime.combine(ed, et)
    else:
        e = s + (dur or dt.timedelta(hours=1))
    if e <= s:
        die("the end is before the start")
    fmt = "%Y-%m-%dT%H:%M:00"
    return {"dateTime": s.strftime(fmt), "timeZone": tz}, {"dateTime": e.strftime(fmt), "timeZone": tz}


def when_of(ev):
    """(sort key, day, 'HH:MM–HH:MM' or 'all day') in local time."""
    s, e = ev.get("start", {}), ev.get("end", {})
    if "date" in s:
        sd = dt.date.fromisoformat(s["date"])
        ed = dt.date.fromisoformat(e.get("date", s["date"])) - dt.timedelta(days=1)
        label = "all day" if ed <= sd else f"all day, to {ed:%a %d %b}"
        return s["date"], sd, label
    sdt = dt.datetime.fromisoformat(s["dateTime"].replace("Z", "+00:00")).astimezone()
    edt = dt.datetime.fromisoformat(e["dateTime"].replace("Z", "+00:00")).astimezone() if e.get("dateTime") else sdt
    end = f"{edt:%H:%M}" if edt.date() == sdt.date() else f"{edt:%a %d %b %H:%M}"
    return sdt.isoformat(), sdt.date(), f"{sdt:%H:%M}–{end}"


def line(ev, cal=None, account=None):
    _, _, label = when_of(ev)
    parts = [f"- {label} {ev.get('summary') or '(no title)'}"]
    if ev.get("location"):
        parts.append(" ".join(ev["location"].split())[:60])
    me = next((x for x in ev.get("attendees", []) if x.get("self")), None)
    if me and me.get("responseStatus") in ("needsAction", "tentative", "declined"):
        parts.append({"needsAction": "NOT ANSWERED", "tentative": "maybe", "declined": "declined"}[me["responseStatus"]])
    if ev.get("status") == "cancelled":
        parts.append("CANCELLED")
    if cal:
        parts.append(cal)
    if account:
        parts.append(account)
    parts.append(f"id={ev['id']}")
    return " · ".join(parts)


# ---- reading ----------------------------------------------------------------------------------------------

def calendars(acc):
    items = call(acc, "GET", "/users/me/calendarList", params={"maxResults": 250}).get("items", [])
    return [{"id": c["id"], "name": c.get("summaryOverride") or c.get("summary") or c["id"], "role": c.get("accessRole"),
             "primary": c.get("primary", False), "selected": c.get("selected", False)} for c in items]


def pick_cals(acc, name=None):
    cals = calendars(acc)
    if not name:
        return [c for c in cals if c["selected"] or c["primary"]]
    if name == "primary":
        return [c for c in cals if c["primary"]]
    hit = [c for c in cals if name in (c["id"], c["name"]) or c["name"].lower() == name.lower()]
    return hit


def events(acc, cal_id, start, end, q=None, limit=250):
    params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
              "orderBy": "startTime", "maxResults": min(limit, 2500)}
    if q:
        params["q"] = q
    return call(acc, "GET", f"/calendars/{urllib.parse.quote(cal_id, safe='')}/events", params=params).get("items", [])


def gather(a, start, end, q=None):
    rows = []
    for acc in accounts(a):
        cals = pick_cals(acc, a.cal)
        for c in cals:
            for ev in events(acc, c["id"], start, end, q):
                rows.append((acc, c, ev))
    seen, out = set(), []
    for acc, c, ev in sorted(rows, key=lambda r: when_of(r[2])[0]):
        key = ev.get("iCalUID", ev["id"]) + ev.get("start", {}).get("dateTime", ev.get("start", {}).get("date", ""))
        if key in seen:  # the same meeting in both accounts' calendars
            continue
        seen.add(key)
        out.append((acc, c, ev))
    if a.cal and not any(pick_cals(acc, a.cal) for acc in accounts(a)):
        die(f"no calendar {a.cal!r} (see `mochi-calendar calendars`)")
    return out


def midnight(d):
    return dt.datetime.combine(d, dt.time()).astimezone()


def cmd_calendars(a):
    for acc in accounts(a):
        print(f"## {acc}")
        for c in calendars(acc):
            flags = [c["role"]] + (["primary"] if c["primary"] else []) + ([] if c["selected"] else ["hidden"])
            print(f"- {c['name']} · {', '.join(flags)} · id={c['id']}")


def cmd_agenda(a):
    first = parse_when(a.from_)[0] if a.from_ else dt.date.today()
    rows = gather(a, midnight(first), midnight(first + dt.timedelta(days=a.days)))
    if a.json:
        print(json.dumps([{"account": acc, "calendar": c["name"], **ev} for acc, c, ev in rows], ensure_ascii=False))
        return
    if not rows:
        print(f"nothing in {a.days} days from {first}")
    day = None
    for acc, c, ev in rows:
        d = max(when_of(ev)[1], first)
        if d != day:
            day = d
            print(f"\n## {d:%a %d %b}" + (" (today)" if d == dt.date.today() else ""))
        print(line(ev, c["name"], acc))


def cmd_search(a):
    today = dt.date.today()
    rows = gather(a, midnight(today - dt.timedelta(days=a.back)), midnight(today + dt.timedelta(days=a.days)),
                  " ".join(a.words))
    for acc, c, ev in rows:
        print(f"{when_of(ev)[1]:%a %Y-%m-%d} {line(ev, c['name'], acc)[2:]}")
    if not rows:
        print("no match")


def event_id(s):
    """An event id from an id or a calendar.google.com link (?eid= is base64 of 'EVENTID CALENDAR')."""
    m = re.search(r"[?&]eid=([A-Za-z0-9_-]+)", s)
    if m:
        raw = m[1] + "=" * (-len(m[1]) % 4)
        try:
            return base64.urlsafe_b64decode(raw).decode().split()[0]
        except (ValueError, UnicodeDecodeError):
            pass
    return s.strip()


def find(a, ref):
    """(account, calendar, event) for EVENT, trying the primary calendars first, then the rest."""
    eid = event_id(ref)
    for acc in accounts(a):
        cals = pick_cals(acc, a.cal) if a.cal else sorted(calendars(acc), key=lambda c: not c["primary"])
        for c in cals:
            try:
                ev = google.request(acc, "GET", f"{API}/calendars/{urllib.parse.quote(c['id'], safe='')}/events/{eid}",
                                    scope=google.CALENDAR)
                return acc, c, ev
            except ApiError as e:
                if e.code not in (403, 404, 410):
                    die(f"{acc}: {e}")
    die(f"no event {eid}" + (f" in {a.cal}" if a.cal else ""))


def cmd_show(a):
    acc, c, ev = find(a, a.event)
    _, day, label = when_of(ev)
    print(f"# {ev.get('summary') or '(no title)'}\n{day:%a %Y-%m-%d} {label} · {c['name']} · {acc} · id={ev['id']}")
    for k, name in (("location", "where"), ("hangoutLink", "meet"), ("htmlLink", "link")):
        if ev.get(k):
            print(f"{name}: {ev[k]}")
    org = ev.get("organizer", {})
    if org and not org.get("self"):
        print(f"organizer: {org.get('displayName') or org.get('email')}")
    if ev.get("recurringEventId"):
        print("part of a recurring series")
    for x in ev.get("attendees", []):
        print(f"  guest: {x.get('displayName') or x.get('email')} ({x.get('responseStatus')})"
              + (" — them" if x.get("self") else ""))
    if ev.get("description"):
        print("\n" + re.sub(r"<[^>]+>", "", ev["description"]).strip()[:3000])


# ---- changing ---------------------------------------------------------------------------------------------

def log(what, acc, cal, ev, extra=""):
    WORK.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as fh:
        fh.write(f"{dt.datetime.now():%Y-%m-%d %H:%M} {what} account={acc} calendar={cal['name']!r} "
                 f"title={ev.get('summary')!r} when={when_of(ev)[1]} {when_of(ev)[2]} id={ev.get('id')}"
                 f"{' ' + extra if extra else ''}\n")


def writable(acc, cal):
    if cal["role"] not in ("owner", "writer"):
        die(f"{cal['name']} is read-only for {acc}")


def cmd_add(a):
    if not a.title.strip():
        die("empty title")
    accs = accounts(a)
    if len(accs) > 1:
        die(f"which account? -a {' or -a '.join(accs)}")
    acc = accs[0]
    cals = pick_cals(acc, a.cal or "primary")
    if not cals:
        die(f"no calendar {a.cal!r} in {acc} (see `mochi-calendar calendars`)")
    cal = cals[0]
    writable(acc, cal)
    start, end = span(a.start, a.end, parse_duration(a.for_) if a.for_ else None)
    body = {"summary": a.title.strip(), "start": start, "end": end}
    if a.where:
        body["location"] = a.where
    if a.notes:
        body["description"] = a.notes
    if a.invite:
        body["attendees"] = [{"email": e} for e in a.invite]
    if a.remind is not None:
        body["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": a.remind}]}
    ev = call(acc, "POST", f"/calendars/{urllib.parse.quote(cal['id'], safe='')}/events", body=body,
              params={"sendUpdates": "all" if a.invite else "none"})
    log("added", acc, cal, ev, f"invited={','.join(a.invite)}" if a.invite else "")
    print(f"added: {line(ev, cal['name'], acc)}\n  {ev.get('htmlLink', '')}")


def cmd_edit(a):
    acc, cal, ev = find(a, a.event)
    writable(acc, cal)
    body = {}
    if a.title is not None:
        body["summary"] = a.title
    if a.where is not None:
        body["location"] = a.where
    if a.notes is not None:
        body["description"] = a.notes
    if a.start or a.end or a.for_:
        if not a.start:
            die("--end/--for need --start too (give the start as it is)")
        start, end = span(a.start, a.end, parse_duration(a.for_) if a.for_ else None)
        # PATCH merges: switching between all-day and timed must clear the other kind's fields
        body["start"], body["end"] = ({"date": None, **start, **({"dateTime": None, "timeZone": None} if "date" in start else {})}
                                      for start in (start, end))
    if not body:
        die("nothing to change")
    guests = [x for x in ev.get("attendees", []) if not x.get("self")]
    new = call(acc, "PATCH", f"/calendars/{urllib.parse.quote(cal['id'], safe='')}/events/{ev['id']}", body=body,
               params={"sendUpdates": "all" if a.notify else "none"})
    log("edited " + ",".join(body), acc, cal, new, f"was={when_of(ev)[1]} {when_of(ev)[2]}" + (" notified" if a.notify else ""))
    print(line(new, cal["name"], acc))
    if guests and not a.notify:
        print(f"  {len(guests)} guests were not told (--notify tells them)")


def cmd_respond(a):
    acc, cal, ev = find(a, a.event)
    status = {"yes": "accepted", "no": "declined", "maybe": "tentative"}[a.answer]
    att = ev.get("attendees", [])
    me = next((x for x in att if x.get("self")), None)
    if not me:
        die("they are not a guest of this event (nothing to answer)")
    me["responseStatus"] = status
    new = call(acc, "PATCH", f"/calendars/{urllib.parse.quote(cal['id'], safe='')}/events/{ev['id']}",
               body={"attendees": att}, params={"sendUpdates": "all"})
    log(f"answered {a.answer}", acc, cal, new)
    print(line(new, cal["name"], acc))


def cmd_log(a):
    if LOG.exists():
        sys.stdout.write("".join(LOG.read_text().splitlines(True)[-a.n:]))
    else:
        print("nothing changed yet")


def main():
    ap = argparse.ArgumentParser(prog="mochi-calendar", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def p(name, *pos):
        s = sub.add_parser(name)
        s.add_argument("-a", "--account")
        s.add_argument("--cal")
        for x in pos:
            s.add_argument(x)
        return s

    p("calendars")
    s = p("agenda")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--from", dest="from_")
    s.add_argument("--json", action="store_true")
    s = p("search")
    s.add_argument("words", nargs="+")
    s.add_argument("--back", type=int, default=30, help="days into the past")
    s.add_argument("--days", type=int, default=180, help="days ahead")
    p("show", "event")
    s = p("add", "title")
    s.add_argument("--start", required=True)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--end")
    g.add_argument("--for", dest="for_")
    s.add_argument("--where")
    s.add_argument("--notes")
    s.add_argument("--invite", nargs="+", default=[])
    s.add_argument("--remind", type=int)
    s = p("edit", "event")
    s.add_argument("--title")
    s.add_argument("--start")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--end")
    g.add_argument("--for", dest="for_")
    s.add_argument("--where")
    s.add_argument("--notes")
    s.add_argument("--notify", action="store_true")
    s = p("respond", "event")
    s.add_argument("answer", choices=["yes", "no", "maybe"])
    s = sub.add_parser("log")
    s.add_argument("n", nargs="?", type=int, default=20)
    a = ap.parse_args()
    {"calendars": cmd_calendars, "agenda": cmd_agenda, "search": cmd_search, "show": cmd_show, "add": cmd_add,
     "edit": cmd_edit, "respond": cmd_respond, "log": cmd_log}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
