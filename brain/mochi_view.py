#!/usr/bin/env python3
"""mochi-view — show one of Mochi's reports in a window: markdown rendered, an HTML report in Mochi's page style,
other text as-is.

    mochi-view PATH [TITLE]
    mochi-view --phone PATH OUT    the same page as one self-contained file, for the phone (pictures inlined)

GTK3 + WebKit2GTK and python3-markdown, all from the distro. Scripts stay off; links open in the browser.
An HTML report is normally a fragment (sections, tables, figures; no <html>, no styles): the page around it, the
look, both themes and a policy that keeps scripts and remote loads out all come from here. mochi-brain imports this
file for `page`, `phone_page` and `text_of`, so GTK is only loaded for the window.
"""
import base64
import html
import mimetypes
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

CSS = """
:root { color-scheme: light dark; --fg: #1f2328; --bg: #ffffff; --muted: #59636e; --line: #d1d9e0; --code: #f6f8fa;
        --link: #0969da; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e6edf3; --bg: #1e1f22; --muted: #9198a1; --line: #3d444d; --code: #2b2d31; --link: #4493f8; } }
body { font: 15px/1.6 system-ui, sans-serif; color: var(--fg); background: var(--bg); margin: 0; }
main { max-width: 760px; margin: 0 auto; padding: 24px 28px 48px; }
h1, h2, h3 { line-height: 1.25; margin: 1.4em 0 .5em; }
h1 { font-size: 1.7em; border-bottom: 1px solid var(--line); padding-bottom: .3em; }
h2 { font-size: 1.35em; border-bottom: 1px solid var(--line); padding-bottom: .25em; }
h1:first-child, h2:first-child { margin-top: 0; }
a { color: var(--link); }
code { font: .9em ui-monospace, monospace; background: var(--code); padding: .15em .35em; border-radius: 4px; }
pre { background: var(--code); padding: 12px 14px; border-radius: 6px; overflow-x: auto; line-height: 1.45; }
pre code { background: none; padding: 0; }
pre.plain { white-space: pre-wrap; word-wrap: break-word; background: none; padding: 0; }
blockquote { margin: 0; padding: 0 1em; color: var(--muted); border-left: 4px solid var(--line); }
table { border-collapse: collapse; margin: 1em 0; display: block; overflow-x: auto; }
th, td { border: 1px solid var(--line); padding: 6px 12px; }
th { background: var(--code); text-align: left; }
img { max-width: 100%; }
hr { border: 0; border-top: 1px solid var(--line); margin: 1.5em 0; }
li + li { margin-top: .2em; }
/* for HTML reports: the few pieces Mochi's CLAUDE.md names */
.lede { font-size: 1.1em; color: var(--muted); margin: 0 0 1.2em; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin: 1em 0; }
.stat { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }
.stat b { display: block; font-size: 1.6em; line-height: 1.2; font-variant-numeric: tabular-nums; }
.stat span { color: var(--muted); font-size: .9em; }
.tag { display: inline-block; font-size: .8em; padding: .05em .55em; border-radius: 999px; border: 1px solid var(--line);
       white-space: nowrap; }
.ok { color: var(--ok); border-color: var(--ok); } .warn { color: var(--warn); border-color: var(--warn); }
.bad { color: var(--bad); border-color: var(--bad); }
.cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; }
.card { border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; }
.card > :first-child { margin-top: 0; } .card > :last-child { margin-bottom: 0; }
ul.timeline { list-style: none; padding-left: 0; border-left: 2px solid var(--line); margin-left: .4em; }
ul.timeline li { padding-left: 1em; position: relative; }
ul.timeline li::before { content: ""; position: absolute; left: -6px; top: .55em; width: 10px; height: 10px;
                         border-radius: 50%; background: var(--line); }
ul.timeline time { color: var(--muted); font-variant-numeric: tabular-nums; margin-right: .5em; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
figure { margin: 1em 0; } figcaption { color: var(--muted); font-size: .9em; margin-top: .3em; }
details { margin: .8em 0; } summary { cursor: pointer; font-weight: 600; }
.muted { color: var(--muted); }
@media (max-width: 600px) { main { padding: 16px 16px 40px; } body { font-size: 16px; } }
"""
CSS = CSS.replace("--link: #0969da; }", "--link: #0969da; --ok: #1a7f37; --warn: #9a6700; --bad: #cf222e; }", 1) \
         .replace("--link: #4493f8; }", "--link: #4493f8; --ok: #3fb950; --warn: #d29922; --bad: #f85149; }", 1)

# No scripts, no remote pictures or fonts (a page opened on the phone must not call anywhere), no forms.
POLICY = ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; "
          "img-src data: file:; media-src data: file:; form-action 'none'\">")


def dark():
    """The desktop's own preference, so the page matches the theme around it."""
    try:
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                             capture_output=True, text=True, timeout=2).stdout
        return "dark" in out
    except Exception:
        return False


LIST_ITEM = re.compile(r"\s*([-*+]|\d+[.)])\s")


def loosen(text):
    """GitHub lets a list start right under a paragraph line; Python-Markdown wants a blank line first. Add it."""
    out, fence, prev = [], False, ""
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        if not fence and LIST_ITEM.match(line) and prev.strip() and not LIST_ITEM.match(prev) \
                and not prev.startswith((" ", "\t")):
            out.append("")
        out.append(line)
        prev = line
    return "\n".join(out)


WHOLE = re.compile(r"\s*(<!--.*?-->\s*)*<(!doctype|html)\b", re.I | re.S)


def page(text, suffix=".md", title="Mochi"):
    """A report as a whole page. Markdown is rendered; an HTML fragment is set in Mochi's page; a whole HTML document
    (one that brought its own <html>) keeps its looks but gets the policy too."""
    if suffix in (".html", ".htm"):
        if WHOLE.match(text):
            head = re.search(r"<head[^>]*>", text, re.I)
            return text[:head.end()] + POLICY + text[head.end():] if head else POLICY + text
        body = text
    elif suffix in (".md", ".markdown", ""):
        import markdown
        body = markdown.markdown(loosen(text), extensions=["extra", "sane_lists", "nl2br"])
    else:
        body = f'<pre class="plain">{html.escape(text)}</pre>'
    return (f"<!doctype html><html><head><meta charset=utf-8>{POLICY}"
            f"<meta name=viewport content=\"width=device-width, initial-scale=1\">"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>")


def render(path):
    return page(path.read_text(errors="replace"), path.suffix.lower(), path.stem)


SRC = re.compile(r"""(<(?:img|source)\b[^>]*?\bsrc\s*=\s*)(["'])(.*?)\2""", re.I | re.S)


def phone_page(path, limit=8 * 2**20):
    """The page as one file that opens anywhere: pictures beside the report (in reports/) go inside it as data,
    up to `limit` bytes in all; anything else is left out, since a phone can't follow a path on this machine."""
    path = Path(path)
    base, budget = path.parent.resolve(), [limit]

    def inline(m):
        src = m.group(3)
        if re.match(r"(data|https?):", src, re.I):
            return m.group(0)
        f = (base / src).resolve()
        kind = mimetypes.guess_type(f.name)[0] or ""
        if base not in f.parents or not f.is_file() or not kind.startswith(("image/", "video/")) \
                or f.stat().st_size > budget[0]:
            return f'{m.group(1)}{m.group(2)}{m.group(2)}'
        budget[0] -= f.stat().st_size
        return f'{m.group(1)}{m.group(2)}data:{kind};base64,{base64.b64encode(f.read_bytes()).decode()}{m.group(2)}'

    return SRC.sub(inline, render(path))


class _Text(HTMLParser):
    BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "ul", "ol", "table", "tr", "figure",
             "figcaption", "blockquote", "pre", "details", "summary", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.cell = [], 0, False

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head", "title"):
            self.skip += 1
        elif tag == "li":
            self.out.append("\n• ")
        elif tag in ("td", "th"):
            self.out.append(" · " if self.cell else "")
            self.cell = True
        elif tag == "img":
            alt = dict(attrs).get("alt")
            self.out.append(f"[{alt}]" if alt else "")
        elif tag == "span":  # a stat's label after its number, a tag after its word
            self.out.append(" ")
        elif tag in self.BLOCK and tag != "tr":
            self.out.append("\n\n" if tag[0] == "h" or tag == "p" else "\n")
        if tag == "tr":
            self.cell = False

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head", "title"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(re.sub(r"\s+", " ", data))


def text_of(markup):
    """An HTML report as plain text, for a Telegram message: paragraphs, bullets, table rows as 'a · b · c'."""
    p = _Text()
    p.feed(markup)
    p.close()
    lines = [re.sub(r" {2,}", " ", ln).strip() for ln in "".join(p.out).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if sys.argv[1] == "--phone":
        if len(sys.argv) != 4:
            sys.exit(__doc__)
        Path(sys.argv[3]).write_text(phone_page(Path(sys.argv[2]).expanduser()))
        return
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gtk, WebKit2
    path = Path(sys.argv[1]).expanduser().resolve()
    title = sys.argv[2] if len(sys.argv) > 2 else path.name
    Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", dark())

    win = Gtk.Window(title=title)
    win.set_default_size(820, 700)
    win.connect("destroy", Gtk.main_quit)
    win.connect("key-press-event", lambda w, e: e.keyval == 0xff1b and w.destroy())  # Esc closes

    settings = WebKit2.Settings(enable_javascript=False, enable_developer_extras=False)
    view = WebKit2.WebView.new_with_settings(settings)

    def on_policy(view, decision, kind):  # the report stays put; anything clicked goes to the real browser
        if kind == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if not uri.startswith(("file:", "about:", "data:")) or (uri.startswith("file:") and uri.split("#")[0] != base):
                subprocess.Popen(["xdg-open", uri], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                decision.ignore()
                return True
        elif kind == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
            return True
        return False

    base = path.as_uri()
    view.connect("decide-policy", on_policy)
    view.load_html(render(path), base)  # relative pictures resolve next to the report
    scroll = Gtk.ScrolledWindow()
    scroll.add(view)
    win.add(scroll)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
