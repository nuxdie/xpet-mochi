#!/usr/bin/env python3
"""mochi-contacts — their Google Contacts in both accounts, through the People API (OAuth, read-only).

    mochi-contacts search QUERY [--others] [-a ACCOUNT]   by name, nickname, email, phone, company or label
    mochi-contacts show QUERY [-a ACCOUNT]                everything about the contacts that match
    mochi-contacts birthdays [--days N] [-a ACCOUNT]      birthdays coming up (and how old, when the year is known)
    mochi-contacts sync [-a ACCOUNT]                      pull everyone now (otherwise the cache refreshes when 6h old)

--others includes "other contacts": people they wrote to or got mail from but never saved (names and emails only).
Read-only: nothing here changes their contacts. Everything is cached in contacts.json in Mochi's workspace (mode 600)
so a search costs nothing. Accounts and their sign-in are mochi-google's.
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

HOME = Path.home()
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"
CACHE = WORK / "contacts.json"
API = "https://people.googleapis.com/v1"
MAX_AGE = 6 * 3600
FIELDS = ("names,nicknames,emailAddresses,phoneNumbers,birthdays,organizations,relations,addresses,urls,"
          "biographies,memberships,events")


def _google():
    """The shared Google sign-in (brain/mochi_google.py; installed beside this as mochi-google)."""
    import importlib.machinery
    import importlib.util
    here = Path(__file__).resolve().parent
    path = next((here / n for n in ("mochi_google.py", "mochi-google") if (here / n).exists()), None)
    if not path:
        sys.exit(f"mochi-contacts: mochi-google is not installed beside {here}")
    spec = importlib.util.spec_from_loader("mochi_google", importlib.machinery.SourceFileLoader("mochi_google", str(path)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.PROG = "mochi-contacts"
    return mod


google = _google()
die = google.die


def paged(acc, path, params, key, scope):
    params, items = dict(params), []
    while True:
        r = google.call(acc, "GET", API + path, params=params, scope=scope)
        items += r.get(key, [])
        if not r.get("nextPageToken"):
            return items
        params["pageToken"] = r["nextPageToken"]


# ---- the cache --------------------------------------------------------------------------------------------

def simplify(p, account, other=False):
    """A person from the API as a flat dict of what Mochi reads."""
    def vals(key, field="value"):
        return [x[field] for x in p.get(key, []) if x.get(field)]
    name = next((n.get("displayName") for n in p.get("names", []) if n.get("displayName")), "")
    bday = next((b["date"] for b in p.get("birthdays", []) if b.get("date", {}).get("month")), None)
    return {"id": p.get("resourceName", ""), "account": account, "other": other, "name": name,
            "nicknames": vals("nicknames"), "emails": vals("emailAddresses"),
            "phones": [x.get("canonicalForm") or x["value"] for x in p.get("phoneNumbers", []) if x.get("value")],
            "orgs": [" · ".join(filter(None, (o.get("name"), o.get("title")))) for o in p.get("organizations", [])],
            "relations": [f"{r.get('type', 'relation')}: {r['person']}" for r in p.get("relations", []) if r.get("person")],
            "addresses": [" ".join(a["formattedValue"].split()) for a in p.get("addresses", []) if a.get("formattedValue")],
            "urls": vals("urls"), "notes": " ".join(vals("biographies")),
            "events": [f"{e.get('type', 'event')}: {date_text(e['date'])}" for e in p.get("events", []) if e.get("date")],
            "labels": [m["contactGroupMembership"].get("contactGroupResourceName", "") for m in p.get("memberships", [])
                       if "contactGroupMembership" in m],
            "birthday": bday}


def pull(acc):
    groups = {g["resourceName"]: g.get("formattedName") or g.get("name", "")
              for g in paged(acc, "/contactGroups", {"pageSize": 1000}, "contactGroups", google.CONTACTS)
              if g.get("groupType") == "USER_CONTACT_GROUP"}
    people = [simplify(p, acc) for p in paged(acc, "/people/me/connections",
                                              {"personFields": FIELDS, "pageSize": 1000}, "connections", google.CONTACTS)]
    for p in people:
        p["labels"] = [groups[g] for g in p["labels"] if g in groups]
    tok = google.load_token(acc) or {}
    if google.OTHER_CONTACTS in tok.get("scopes", []):
        people += [simplify(p, acc, other=True) for p in paged(
            acc, "/otherContacts", {"readMask": "names,emailAddresses,phoneNumbers", "pageSize": 1000},
            "otherContacts", google.OTHER_CONTACTS)]
    return people


def save(data):
    WORK.mkdir(parents=True, exist_ok=True)
    fd = os.open(CACHE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False)


def load(a, fresh=False):
    """Everyone in the accounts asked for, from the cache, pulling an account again when it is stale."""
    try:
        data = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        data = {}
    accs = google.accounts_for(getattr(a, "account", None), google.CONTACTS)
    changed = False
    for acc in accs:
        if fresh or time.time() - data.get(acc, {}).get("at", 0) > MAX_AGE:
            data[acc] = {"at": time.time(), "people": pull(acc)}
            changed = True
    if changed:
        save(data)
    return [p for acc in accs for p in data[acc]["people"]]


# ---- reading ----------------------------------------------------------------------------------------------

def norm_phone(s):
    return "".join(c for c in s if c.isdigit())


def matches(p, q):
    q = q.strip().lower()
    if not q:
        return False
    hay = [p["name"], *p["nicknames"], *p["emails"], *p["orgs"], *p["labels"]]
    if any(q in h.lower() for h in hay):
        return True
    if all(w in p["name"].lower() for w in q.split()):  # "artem tsatsin" finds "Tsatsin Artem"
        return True
    digits = norm_phone(q)
    return len(digits) >= 6 and any(digits[-9:] in norm_phone(ph) for ph in p["phones"])


def date_text(d):
    if not d.get("month"):
        return ""
    s = f"{d['day']:02d} {dt.date(2000, d['month'], 1):%b}"
    return f"{s} {d['year']}" if d.get("year") else s


def line(p):
    parts = [f"- {p['name'] or '(no name)'}"]
    if p["nicknames"]:
        parts[0] += f" ({', '.join(p['nicknames'])})"
    parts += p["phones"][:2] + p["emails"][:2]
    if p["orgs"]:
        parts.append(p["orgs"][0])
    if p["birthday"]:
        parts.append("born " + date_text(p["birthday"]))
    if p["labels"]:
        parts.append("[" + ", ".join(p["labels"]) + "]")
    parts.append(p["account"] + (" other" if p["other"] else ""))
    return " · ".join(parts)


def merge(people):
    """One entry per person across both accounts: same name and a shared email or phone, or the same name alone."""
    out = {}
    for p in people:
        key = p["name"].lower() or (p["emails"][:1] or p["phones"][:1] or [p["id"]])[0]
        if key in out:
            q = out[key]
            for k in ("nicknames", "emails", "phones", "orgs", "labels", "relations", "addresses", "urls", "events"):
                q[k] += [x for x in p[k] if x not in q[k]]
            q["birthday"] = q["birthday"] or p["birthday"]
            q["notes"] = q["notes"] or p["notes"]
            if p["account"] not in q["account"].split("+"):
                q["account"] += "+" + p["account"]
            q["other"] = q["other"] and p["other"]
        else:
            out[key] = {**p, **{k: list(p[k]) for k in ("nicknames", "emails", "phones", "orgs", "labels", "relations",
                                                       "addresses", "urls", "events")}}
    return list(out.values())


def cmd_search(a):
    hits = merge([p for p in load(a) if (a.others or not p["other"]) and matches(p, " ".join(a.query))])
    hits.sort(key=lambda p: (p["other"], p["name"].lower()))
    for p in hits[:a.limit]:
        print(line(p))
    if not hits:
        print("no match" + ("" if a.others else " (--others also looks in people they mailed but never saved)"))
    elif len(hits) > a.limit:
        print(f"…{len(hits) - a.limit} more (--limit N)")


def cmd_show(a):
    hits = merge([p for p in load(a) if matches(p, " ".join(a.query))])
    if not hits:
        die("no match")
    for p in hits[:10]:
        print(f"# {p['name'] or '(no name)'}  ({p['account']}{', other contact' if p['other'] else ''})")
        for label, k in (("nicknames", "nicknames"), ("phones", "phones"), ("emails", "emails"), ("work", "orgs"),
                         ("labels", "labels"), ("relations", "relations"), ("addresses", "addresses"),
                         ("links", "urls"), ("dates", "events")):
            if p[k]:
                print(f"{label}: {', '.join(p[k])}")
        if p["birthday"]:
            print(f"birthday: {date_text(p['birthday'])}")
        if p["notes"]:
            print(f"notes: {p['notes'][:500]}")
        print()


def upcoming(people, days, today=None):
    """(date, person, age or None) for birthdays in the next DAYS days, soonest first."""
    today = today or dt.date.today()
    out = []
    for p in people:
        b = p.get("birthday")
        if not b:
            continue
        for year in (today.year, today.year + 1):
            try:
                d = dt.date(year, b["month"], b["day"])
            except ValueError:  # 29 Feb in a common year
                d = dt.date(year, 3, 1)
            if 0 <= (d - today).days <= days:
                out.append((d, p, year - b["year"] if b.get("year") else None))
                break
    return sorted(out, key=lambda x: (x[0], x[1]["name"]))


def cmd_birthdays(a):
    rows = upcoming(merge([p for p in load(a) if not p["other"]]), a.days)
    today = dt.date.today()
    for d, p, age in rows:
        n = (d - today).days
        when = "today" if n == 0 else "tomorrow" if n == 1 else f"in {n} days"
        print(f"- {d:%a %d %b} ({when}) {p['name']}" + (f", turns {age}" if age else ""))
    if not rows:
        print(f"no birthdays in the next {a.days} days")


def cmd_sync(a):
    people = load(a, fresh=True)
    saved = sum(not p["other"] for p in people)
    print(f"{saved} contacts, {len(people) - saved} other contacts, "
          f"{sum(bool(p['birthday']) for p in people)} with a birthday")


def main():
    ap = argparse.ArgumentParser(prog="mochi-contacts", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def p(name):
        s = sub.add_parser(name)
        s.add_argument("-a", "--account")
        return s

    s = p("search")
    s.add_argument("query", nargs="+")
    s.add_argument("--others", action="store_true")
    s.add_argument("--limit", type=int, default=20)
    s = p("show")
    s.add_argument("query", nargs="+")
    s = p("birthdays")
    s.add_argument("--days", type=int, default=30)
    p("sync")
    a = ap.parse_args()
    {"search": cmd_search, "show": cmd_show, "birthdays": cmd_birthdays, "sync": cmd_sync}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
