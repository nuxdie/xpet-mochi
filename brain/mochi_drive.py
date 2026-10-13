#!/usr/bin/env python3
"""mochi-drive — their Google Drive, Docs and Sheets, through the Google APIs (OAuth, scope drive).

    mochi-drive auth ACCOUNT [--no-browser]   once per account: sign in as them in a browser, keeps a refresh token
    mochi-drive accounts                      which accounts are signed in, as whom, how full
    mochi-drive search WORDS [--type T] [--name] [--in FOLDER] [--limit N] [-a ACCOUNT]
    mochi-drive recent [--days N] [--limit N] [-a ACCOUNT]    what changed lately, newest first
    mochi-drive ls [FOLDER] [-a ACCOUNT]      a folder (default: My Drive)
    mochi-drive info FILE                     one file's details
    mochi-drive read FILE [--max N]           a Doc as Markdown, a Sheet tab by tab, Slides/PDF/text as text
    mochi-drive sheet FILE [RANGE] [--json]   a Sheet's tabs, or the values in RANGE ("Tab!A1:D20")
    mochi-drive download FILE [--as FORMAT]   into ~/.local/share/mochi/drive/ (Google files: pdf docx xlsx csv md txt pptx)
    mochi-drive log [N]                       what Mochi changed

  changing (every change goes to drive.log in Mochi's workspace; nothing is ever trashed, deleted or shared):
    mochi-drive create-doc TITLE [--from FILE.md] [--folder F]      a new Doc, from Markdown or empty
    mochi-drive create-sheet TITLE [--from FILE.csv] [--folder F]   a new Sheet, from CSV or empty
    mochi-drive upload PATH [--name N] [--folder F] [--convert]     a file as is (or converted to a Google file)
    mochi-drive mkdir NAME [--folder F]
    mochi-drive doc-append FILE (TEXT | --from FILE)                text at the end of a Doc
    mochi-drive doc-replace FILE OLD NEW [--match-case]             every OLD in a Doc becomes NEW
    mochi-drive write-cells FILE RANGE (--values JSON | --csv FILE) overwrite cells (typed as if by hand: =SUM() works)
    mochi-drive append-rows FILE RANGE (--values JSON | --csv FILE) rows after the last row of the table in RANGE
    mochi-drive rename FILE NAME  |  move FILE FOLDER

FILE and FOLDER are an id or a docs.google.com / drive.google.com link. Without -a, reading commands look in every
signed-in account and a FILE is found in whichever account has it. Accounts are named as in mail.accounts in
~/.config/mochi/sources.json (artem, nuxdie), or drive.accounts if that is set.

App passwords only open IMAP/SMTP/CalDAV; Drive, Docs and Sheets need OAuth. This uses the same OAuth client as
mochi-tasks (the "Mochi" project in Google Cloud, with the Drive, Docs and Sheets APIs on), one token per account
in ~/.config/mochi/google-drive.ACCOUNT.token.json (mode 600).
"""

import argparse
import base64
import csv
import datetime as dt
import hashlib
import http.server
import io
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()
WORK = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "mochi"
LOG = WORK / "drive.log"
DOWNLOADS = WORK / "drive"
CONF_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "mochi"
CONFIG = CONF_DIR / "sources.json"
DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
DOCS = "https://docs.googleapis.com/v1"
SHEETS = "https://sheets.googleapis.com/v4"
SCOPE = "https://www.googleapis.com/auth/drive"  # Docs and Sheets APIs accept it too
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
FIELDS = "id,name,mimeType,modifiedTime,size,webViewLink,parents,owners(emailAddress),lastModifyingUser(displayName,me)"
G = "application/vnd.google-apps."
TYPES = {"doc": G + "document", "sheet": G + "spreadsheet", "slides": G + "presentation", "folder": G + "folder",
         "form": G + "form", "pdf": "application/pdf"}
SHORT = {**{v: k for k, v in TYPES.items()}, "application/vnd.google-makersuite.prompt": "ai-studio-prompt"}
EXPORTS = {"pdf": "application/pdf", "txt": "text/plain", "md": "text/markdown", "csv": "text/csv",
           "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
           "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
           "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"HTTP {code} {msg}")
        self.code = code


def die(msg):
    sys.exit(f"mochi-drive: {msg}")


# ---- config and tokens ------------------------------------------------------------------------------------

def conf():
    try:
        c = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        c = {}
    d = c.get("drive", {})
    accounts = d.get("accounts") or c.get("mail", {}).get("accounts") or {}
    client = d.get("client_file") or c.get("tasks", {}).get("client_file") or CONF_DIR / "google-oauth-client.json"
    return accounts, Path(os.path.expanduser(client))


def token_path(account):
    return CONF_DIR / f"google-drive.{account}.token.json"


def signed_in():
    return [a for a in conf()[0] if token_path(a).exists()]


def client():
    path = conf()[1]
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        die(f"no OAuth client at {path} (see `mochi-drive --help`)")
    c = data.get("installed") or data.get("web") or data
    if not c.get("client_id"):
        die(f"{path} has no client_id")
    return c


def post_form(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            err = json.loads(body)
        except ValueError:
            err = {"error": body[:200]}
        return {"_error": err.get("error"), "_detail": err.get("error_description", "")}


def save_token(account, tok):
    path = token_path(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tok, f)


def access_token(account):
    try:
        tok = json.loads(token_path(account).read_text())
    except (OSError, ValueError):
        die(f"{account} is not signed in (they run `mochi-drive auth {account}` once)")
    if tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 60:
        return tok["access_token"]
    c = client()
    r = post_form(TOKEN_URL, {"client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
                              "refresh_token": tok.get("refresh_token", ""), "grant_type": "refresh_token"})
    if r.get("_error"):
        hint = f" — the sign-in expired or was revoked; they run `mochi-drive auth {account}` again" if r["_error"] == "invalid_grant" else ""
        die(f"{account}: token refresh failed: {r['_error']} {r['_detail']}{hint}")
    tok.update(access_token=r["access_token"], expires_at=time.time() + int(r.get("expires_in", 3600)))
    save_token(account, tok)
    return tok["access_token"]


def request(account, method, url, body=None, params=None, raw=None, ctype="application/json"):
    """One API call as ACCOUNT. body is JSON; raw is bytes sent as is. Returns parsed JSON, or bytes for media."""
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data,
                                 headers={"Authorization": "Bearer " + access_token(account), "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = r.read()
            if "json" in r.headers.get("Content-Type", ""):
                return json.loads(out) if out else {}
            return out
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        raise ApiError(e.code, msg) from None
    except urllib.error.URLError as e:
        die(str(e.reason))


def call(account, method, url, **kw):
    try:
        return request(account, method, url, **kw)
    except ApiError as e:
        die(f"{account}: {method} {url.split('googleapis.com')[-1]}: {e}")


# ---- files ------------------------------------------------------------------------------------------------

def file_id(s):
    """An id from an id or any Drive/Docs/Sheets link."""
    m = re.search(r"/(?:d|folders)/([A-Za-z0-9_-]{10,})", s) or re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", s)
    return m.group(1) if m else s.strip()


def accounts_for(a):
    have = signed_in()
    if getattr(a, "account", None):
        if a.account not in conf()[0]:
            die(f"no account {a.account!r} (have: {', '.join(conf()[0]) or 'none in sources.json'})")
        return [a.account]
    if not have:
        die("no account is signed in (they run `mochi-drive auth ACCOUNT` once; accounts: "
            + ", ".join(conf()[0]) + ")")
    return have


def find(a, ref, fields=FIELDS):
    """(account, file) for FILE, trying each signed-in account unless -a names one."""
    fid = file_id(ref)
    for acc in accounts_for(a):
        try:
            return acc, request(acc, "GET", f"{DRIVE}/files/{fid}", params={"fields": fields, "supportsAllDrives": "true"})
        except ApiError as e:
            if e.code not in (403, 404):
                die(f"{acc}: {e}")
    die(f"no file {fid} in {', '.join(accounts_for(a))}")


def kind(f):
    return SHORT.get(f.get("mimeType", ""), f.get("mimeType", "").replace(G, "google-"))


def line(f, account=None, today=None):
    mod = f.get("modifiedTime", "")[:16].replace("T", " ")
    who = f.get("lastModifyingUser") or {}
    by = "by them" if who.get("me") else f"by {who['displayName']}" if who.get("displayName") else ""
    parts = [f"- {f.get('name')}", kind(f), f"modified {mod} {by}".strip()]
    if f.get("size"):
        parts.append(human(int(f["size"])))
    if account:
        parts.append(account)
    parts.append(f"id={f['id']}")
    return " · ".join(parts)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def quote(s):
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def query(words=None, type_=None, name_only=False, folder=None, since=None):
    q = ["trashed = false"]
    if words:
        q.append(f"{'name' if name_only else 'fullText'} contains {quote(words)}")
    if type_:
        if type_ not in TYPES:
            die(f"--type is one of {', '.join(TYPES)}")
        q.append(f"mimeType = {quote(TYPES[type_])}")
    if folder:
        q.append(f"{quote(file_id(folder))} in parents")
    if since:
        q.append(f"modifiedTime > {quote(since)}")
    return " and ".join(q)


def list_files(account, q, limit, order=None):
    params = {"q": q, "fields": f"nextPageToken,files({FIELDS})", "pageSize": min(limit, 100),
              "supportsAllDrives": "true", "includeItemsFromAllDrives": "true", "corpora": "allDrives"}
    if order:
        params["orderBy"] = order
    files = []
    while len(files) < limit:
        r = call(account, "GET", f"{DRIVE}/files", params=params)
        files += r.get("files", [])
        if not r.get("nextPageToken"):
            break
        params["pageToken"] = r["nextPageToken"]
    return files[:limit]


def print_files(rows, empty="nothing"):
    if not rows:
        print(empty)
    for acc, f in rows:
        print(line(f, acc))


def cmd_search(a):
    q = query(" ".join(a.words), a.type, a.name, a.folder)
    order = None if a.words and not a.name else "modifiedTime desc"  # fullText results come by relevance and can't be sorted
    rows = [(acc, f) for acc in accounts_for(a) for f in list_files(acc, q, a.limit, order)]
    print_files(rows, "no match")


def cmd_recent(a):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=a.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = query(since=since) + f" and mimeType != {quote(TYPES['folder'])}"
    rows = [(acc, f) for acc in accounts_for(a) for f in list_files(acc, q, a.limit, "modifiedTime desc")]
    rows.sort(key=lambda r: r[1].get("modifiedTime", ""), reverse=True)
    print_files(rows[:a.limit], f"nothing changed in {a.days} days")


def cmd_ls(a):
    folder = a.folder or "root"
    if folder == "root":
        accs = accounts_for(a)
    else:
        acc, _ = find(a, folder, "id")
        accs = [acc]
    for acc in accs:
        files = list_files(acc, query(folder=folder), 500, "folder,name")
        print(f"## {acc}: {len(files)}")
        for f in files:
            print(line(f))


def cmd_info(a):
    acc, f = find(a, a.file, FIELDS + ",description,createdTime,shared,capabilities(canEdit)")
    print(line(f, acc))
    print(f"  link: {f.get('webViewLink', '')}")
    print(f"  owner: {', '.join(o['emailAddress'] for o in f.get('owners', [])) or 'a shared drive'}"
          f" · created {f.get('createdTime', '')[:10]} · {'shared' if f.get('shared') else 'not shared'}"
          f" · {'editable' if f.get('capabilities', {}).get('canEdit') else 'read-only'} for {acc}")
    if f.get("description"):
        print(f"  description: {f['description']}")


# ---- reading contents -------------------------------------------------------------------------------------

def sheet_tabs(acc, sid):
    r = call(acc, "GET", f"{SHEETS}/spreadsheets/{sid}", params={"fields": "properties.title,sheets.properties"})
    return r["properties"]["title"], [s["properties"] for s in r.get("sheets", [])]


def values(acc, sid, ranges):
    r = call(acc, "GET", f"{SHEETS}/spreadsheets/{sid}/values:batchGet",
             params={"ranges": ranges, "valueRenderOption": "FORMATTED_VALUE"})
    return [(v.get("range", ""), v.get("values", [])) for v in r.get("valueRanges", [])]


def tsv(rows, max_rows=None):
    out = ["\t".join(str(c).replace("\t", " ").replace("\n", " ") for c in row) for row in rows[:max_rows]]
    if max_rows and len(rows) > max_rows:
        out.append(f"…{len(rows) - max_rows} more rows")
    return "\n".join(out)


def tab_range(title):
    return "'" + title.replace("'", "''") + "'"


def read_text(acc, f, max_rows):
    m = f["mimeType"]
    fid = f["id"]
    if m == TYPES["doc"]:
        return call(acc, "GET", f"{DRIVE}/files/{fid}/export", params={"mimeType": "text/markdown"}).decode(errors="replace")
    if m == TYPES["sheet"]:
        _, tabs = sheet_tabs(acc, fid)
        out = []
        for (rng, rows), t in zip(values(acc, fid, [tab_range(t["title"]) for t in tabs]), tabs):
            out.append(f"## {t['title']} ({len(rows)} rows)\n{tsv(rows, max_rows)}")
        return "\n\n".join(out)
    if m.startswith(G):
        return call(acc, "GET", f"{DRIVE}/files/{fid}/export", params={"mimeType": "text/plain"}).decode(errors="replace")
    data = call(acc, "GET", f"{DRIVE}/files/{fid}", params={"alt": "media", "supportsAllDrives": "true"})
    if m == "application/pdf":
        p = subprocess.run(["pdftotext", "-layout", "-", "-"], input=data, capture_output=True, timeout=60)
        return p.stdout.decode(errors="replace") if p.returncode == 0 else die("pdftotext failed: " + p.stderr.decode()[-200:])
    if m.startswith("text/") or m in ("application/json", "application/xml"):
        return data.decode(errors="replace")
    die(f"{f['name']} is {m}; `mochi-drive download {fid}` and look at the file")


def cmd_read(a):
    acc, f = find(a, a.file)
    text = read_text(acc, f, a.rows)
    print(f"# {f['name']} ({kind(f)}, {acc}, modified {f.get('modifiedTime', '')[:16].replace('T', ' ')})\n")
    print(text[:a.max] + (f"\n…cut at {a.max} of {len(text)} characters (--max N)" if len(text) > a.max else ""))


def cmd_sheet(a):
    acc, f = find(a, a.file, "id,name,mimeType")
    if f["mimeType"] != TYPES["sheet"]:
        die(f"{f['name']} is not a Google Sheet")
    if not a.range:
        title, tabs = sheet_tabs(acc, f["id"])
        print(f"# {title} ({acc})")
        for t in tabs:
            g = t.get("gridProperties", {})
            print(f"- {t['title']} · {g.get('rowCount')}x{g.get('columnCount')} · sheetId={t['sheetId']}")
        return
    (rng, rows), = values(acc, f["id"], [a.range])
    if a.json:
        print(json.dumps({"range": rng, "values": rows}, ensure_ascii=False))
    else:
        print(f"# {rng}\n{tsv(rows)}")


def safe_name(name):
    return re.sub(r"[/\\\x00]", "_", name).strip(". ") or "file"


def cmd_download(a):
    acc, f = find(a, a.file)
    m = f["mimeType"]
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    if m.startswith(G):
        fmt = a.as_ or {"document": "docx", "spreadsheet": "xlsx", "presentation": "pptx"}.get(m[len(G):], "pdf")
        if fmt not in EXPORTS:
            die(f"--as is one of {', '.join(EXPORTS)}")
        data = call(acc, "GET", f"{DRIVE}/files/{f['id']}/export", params={"mimeType": EXPORTS[fmt]})
        name = f"{safe_name(f['name'])}.{fmt}"
    else:
        data = call(acc, "GET", f"{DRIVE}/files/{f['id']}", params={"alt": "media", "supportsAllDrives": "true"})
        name = safe_name(f["name"])
    path = DOWNLOADS / name
    path.write_bytes(data)
    print(f"{path} ({human(len(data))})")


# ---- changing ---------------------------------------------------------------------------------------------

def log(what, acc, f, extra=""):
    WORK.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as fh:
        fh.write(f"{dt.datetime.now():%Y-%m-%d %H:%M} {what} account={acc} name={f.get('name')!r} id={f.get('id')}"
                 f"{' ' + extra if extra else ''}\n")


def write_account(a, folder=None):
    """The account a new file goes to: -a, else the folder's owner, else the only signed-in one."""
    if folder:
        return find(a, folder, "id")[0]
    accs = accounts_for(a)
    if len(accs) > 1:
        die(f"which account? -a {' or -a '.join(accs)}")
    return accs[0]


def multipart(meta, data, ctype):
    boundary = "mochi" + secrets.token_hex(12)
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
            f"--{boundary}\r\nContent-Type: {ctype}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/related; boundary={boundary}"


def create(a, meta, data=None, ctype=None):
    acc = write_account(a, a.folder)
    if a.folder:
        meta["parents"] = [file_id(a.folder)]
    params = {"fields": FIELDS, "supportsAllDrives": "true"}
    if data is None:
        f = call(acc, "POST", f"{DRIVE}/files", body=meta, params=params)
    else:
        raw, ct = multipart(meta, data, ctype)
        f = call(acc, "POST", f"{UPLOAD}/files", raw=raw, ctype=ct, params={**params, "uploadType": "multipart"})
    log("created " + kind(f), acc, f)
    print(line(f, acc))
    print(f"  {f.get('webViewLink', '')}")


def read_arg(path):
    try:
        return Path(path).read_bytes()
    except OSError as e:
        die(str(e))


def cmd_create_doc(a):
    create(a, {"name": a.title, "mimeType": TYPES["doc"]}, *((read_arg(a.from_), "text/markdown") if a.from_ else ()))


def cmd_create_sheet(a):
    create(a, {"name": a.title, "mimeType": TYPES["sheet"]}, *((read_arg(a.from_), "text/csv") if a.from_ else ()))


def cmd_upload(a):
    p = Path(a.path)
    meta = {"name": a.name or p.name}
    ctype = {".md": "text/markdown", ".csv": "text/csv", ".txt": "text/plain", ".pdf": "application/pdf",
             ".docx": EXPORTS["docx"], ".xlsx": EXPORTS["xlsx"], ".pptx": EXPORTS["pptx"],
             ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".html": "text/html"
             }.get(p.suffix.lower(), "application/octet-stream")
    if a.convert:
        to = {".md": "doc", ".txt": "doc", ".docx": "doc", ".html": "doc", ".csv": "sheet", ".xlsx": "sheet",
              ".pptx": "slides"}.get(p.suffix.lower())
        if not to:
            die(f"--convert takes .md .txt .docx .html .csv .xlsx .pptx, not {p.suffix}")
        meta["mimeType"] = TYPES[to]
        if not a.name:
            meta["name"] = p.stem
    create(a, meta, read_arg(p), ctype)


def cmd_mkdir(a):
    create(a, {"name": a.name, "mimeType": TYPES["folder"]})


def utf16_len(s):
    return len(s.encode("utf-16-le")) // 2


def need(f, mime, what):
    if f["mimeType"] != TYPES[mime]:
        die(f"{f['name']} is {kind(f)}, not a Google {what}")


def cmd_doc_append(a):
    text = read_arg(a.from_).decode() if a.from_ else a.text
    if not text:
        die("nothing to append (TEXT or --from FILE)")
    acc, f = find(a, a.file, "id,name,mimeType")
    need(f, "doc", "Doc")
    text = text.rstrip("\n")
    doc = call(acc, "GET", f"{DOCS}/documents/{f['id']}", params={"fields": "body.content(endIndex)"})
    end = doc["body"]["content"][-1]["endIndex"] - 1  # before the body's final newline
    new = {"startIndex": end + 1, "endIndex": end + 1 + utf16_len(text)}  # Docs counts UTF-16 units
    # A new paragraph inherits the last one's style; a line after a list or a heading should be plain text.
    call(acc, "POST", f"{DOCS}/documents/{f['id']}:batchUpdate", body={"requests": [
        {"insertText": {"location": {"index": end}, "text": "\n" + text}},
        {"deleteParagraphBullets": {"range": new}},
        {"updateParagraphStyle": {"range": new, "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                  "fields": "namedStyleType"}}]})
    log("appended", acc, f, f"chars={len(text)}")
    print(f"appended {len(text)} characters to {f['name']}")


def cmd_doc_replace(a):
    if not a.old:
        die("OLD is empty")
    acc, f = find(a, a.file, "id,name,mimeType")
    need(f, "doc", "Doc")
    r = call(acc, "POST", f"{DOCS}/documents/{f['id']}:batchUpdate", body={"requests": [{"replaceAllText": {
        "containsText": {"text": a.old, "matchCase": a.match_case}, "replaceText": a.new}}]})
    n = (r.get("replies") or [{}])[0].get("replaceAllText", {}).get("occurrencesChanged", 0)
    if n:
        log("replaced", acc, f, f"count={n} old={a.old[:60]!r} new={a.new[:60]!r}")
    print(f"{n} replaced in {f['name']}")


def rows_arg(a):
    if a.csv:
        return list(csv.reader(io.StringIO(read_arg(a.csv).decode())))
    if a.values:
        try:
            v = json.loads(a.values)
        except ValueError as e:
            die(f"--values is JSON rows like '[[\"a\", 1], [\"b\", 2]]': {e}")
        if not (isinstance(v, list) and all(isinstance(r, list) for r in v)):
            die("--values is a list of rows, each a list of cells")
        return v
    die("--values JSON or --csv FILE")


def cmd_sheet_write(a, append=False):
    rows = rows_arg(a)
    acc, f = find(a, a.file, "id,name,mimeType")
    need(f, "sheet", "Sheet")
    rng = urllib.parse.quote(a.range, safe="")
    if append:
        r = call(acc, "POST", f"{SHEETS}/spreadsheets/{f['id']}/values/{rng}:append", body={"values": rows},
                 params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"})
        where = r.get("updates", {}).get("updatedRange", a.range)
    else:
        r = call(acc, "PUT", f"{SHEETS}/spreadsheets/{f['id']}/values/{rng}", body={"values": rows},
                 params={"valueInputOption": "USER_ENTERED"})
        where = r.get("updatedRange", a.range)
    log("appended rows" if append else "wrote cells", acc, f, f"range={where!r} rows={len(rows)}")
    print(f"{'appended' if append else 'wrote'} {len(rows)} rows: {where}")


def cmd_rename(a):
    acc, f = find(a, a.file, "id,name")
    g = call(acc, "PATCH", f"{DRIVE}/files/{f['id']}", body={"name": a.name}, params={"fields": FIELDS, "supportsAllDrives": "true"})
    log("renamed", acc, g, f"from={f['name']!r}")
    print(line(g, acc))


def cmd_move(a):
    acc, f = find(a, a.file, "id,name,parents")
    to = file_id(a.folder)
    g = call(acc, "PATCH", f"{DRIVE}/files/{f['id']}", params={
        "addParents": to, "removeParents": ",".join(f.get("parents", [])), "fields": FIELDS, "supportsAllDrives": "true"},
        body={})
    log("moved", acc, g, f"from={','.join(f.get('parents', []))} to={to}")
    print(line(g, acc))


def cmd_log(a):
    if LOG.exists():
        sys.stdout.write("".join(LOG.read_text().splitlines(True)[-a.n:]))
    else:
        print("nothing changed yet")


# ---- auth -------------------------------------------------------------------------------------------------

def cmd_auth(a):
    accounts = conf()[0]
    if a.account not in accounts:
        die(f"no account {a.account!r}; name one of: {', '.join(accounts) or '(none: add drive.accounts to sources.json)'}")
    email = accounts[a.account]
    c = client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") != state:
                self.send_response(400)
                self.end_headers()
                return
            got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"Mochi can open {email}'s Drive now. You can close this tab.".encode() if "code" in q
                             else f"Not signed in: {q.get('error')}".encode())

        def log_message(self, *_):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state, "login_hint": email,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print(f"Open this in a browser where {email} is signed in to Google:\n\n{url}\n", flush=True)
    if not a.no_browser:
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    srv.timeout = 10
    deadline = time.time() + 600
    while not got and time.time() < deadline:
        srv.handle_request()
    if "code" not in got:
        die(f"sign-in failed: {got.get('error', 'no answer')}")
    r = post_form(TOKEN_URL, {"client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
                              "code": got["code"], "code_verifier": verifier, "redirect_uri": redirect,
                              "grant_type": "authorization_code"})
    if r.get("_error"):
        die(f"token exchange failed: {r['_error']} {r['_detail']}")
    if not r.get("refresh_token"):
        die("Google returned no refresh token; remove Mochi's access at myaccount.google.com/permissions and run auth again")
    if SCOPE not in r.get("scope", "").split():
        die("Drive access was not granted (tick the Drive box on Google's consent page)")
    old = token_path(a.account).read_bytes() if token_path(a.account).exists() else None
    save_token(a.account, {"refresh_token": r["refresh_token"], "access_token": r["access_token"],
                           "expires_at": time.time() + int(r.get("expires_in", 3600))})
    me = call(a.account, "GET", f"{DRIVE}/about", params={"fields": "user(emailAddress)"})["user"]["emailAddress"]
    if me.lower() != email.lower():
        if old:
            token_path(a.account).write_bytes(old)
        else:
            token_path(a.account).unlink()
        die(f"signed in as {me}, not {email}; run it again and pick {email}")
    print(f"signed in as {me}")
    cmd_accounts(a)


def cmd_accounts(a):
    for acc, email in conf()[0].items():
        if not token_path(acc).exists():
            print(f"- {acc} ({email}): not signed in — `mochi-drive auth {acc}`")
            continue
        try:
            r = request(acc, "GET", f"{DRIVE}/about", params={"fields": "user(emailAddress),storageQuota"})
        except ApiError as e:
            print(f"- {acc} ({email}): {e}")
            continue
        q = r.get("storageQuota", {})
        used = human(int(q.get("usage", 0)))
        print(f"- {acc} ({r['user']['emailAddress']}): ok, {used} used"
              + (f" of {human(int(q['limit']))}" if q.get("limit") else ""))


def main():
    ap = argparse.ArgumentParser(prog="mochi-drive", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def p(name, *args):
        s = sub.add_parser(name)
        s.add_argument("-a", "--account")
        for x in args:
            s.add_argument(x)
        return s

    s = sub.add_parser("auth")
    s.add_argument("account")
    s.add_argument("--no-browser", action="store_true")
    sub.add_parser("accounts")
    s = p("search")
    s.add_argument("words", nargs="*")
    s.add_argument("--type")
    s.add_argument("--name", action="store_true", help="match file names only, not contents")
    s.add_argument("--in", dest="folder")
    s.add_argument("--limit", type=int, default=25)
    s = p("recent")
    s.add_argument("--days", type=float, default=7)
    s.add_argument("--limit", type=int, default=25)
    s = p("ls")
    s.add_argument("folder", nargs="?")
    p("info", "file")
    s = p("read", "file")
    s.add_argument("--max", type=int, default=60000)
    s.add_argument("--rows", type=int, default=300, help="rows per Sheet tab")
    s = p("sheet", "file")
    s.add_argument("range", nargs="?")
    s.add_argument("--json", action="store_true")
    s = p("download", "file")
    s.add_argument("--as", dest="as_")
    for name in ("create-doc", "create-sheet"):
        s = p(name, "title")
        s.add_argument("--from", dest="from_")
        s.add_argument("--folder")
    s = p("upload", "path")
    s.add_argument("--name")
    s.add_argument("--folder")
    s.add_argument("--convert", action="store_true")
    s = p("mkdir", "name")
    s.add_argument("--folder")
    s = p("doc-append", "file")
    s.add_argument("text", nargs="?")
    s.add_argument("--from", dest="from_")
    s = p("doc-replace", "file", "old", "new")
    s.add_argument("--match-case", action="store_true")
    for name in ("write-cells", "append-rows"):
        s = p(name, "file", "range")
        s.add_argument("--values")
        s.add_argument("--csv")
    p("rename", "file", "name")
    p("move", "file", "folder")
    s = sub.add_parser("log")
    s.add_argument("n", nargs="?", type=int, default=20)
    a = ap.parse_args()
    {"auth": cmd_auth, "accounts": cmd_accounts, "search": cmd_search, "recent": cmd_recent, "ls": cmd_ls,
     "info": cmd_info, "read": cmd_read, "sheet": cmd_sheet, "download": cmd_download,
     "create-doc": cmd_create_doc, "create-sheet": cmd_create_sheet, "upload": cmd_upload, "mkdir": cmd_mkdir,
     "doc-append": cmd_doc_append, "doc-replace": cmd_doc_replace,
     "write-cells": cmd_sheet_write, "append-rows": lambda a: cmd_sheet_write(a, append=True),
     "rename": cmd_rename, "move": cmd_move, "log": cmd_log}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
