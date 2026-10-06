#!/usr/bin/env python3
"""mochi-view — show one of Mochi's reports in a window: markdown rendered, other text as-is.

    mochi-view PATH [TITLE]

GTK3 + WebKit2GTK and python3-markdown, all from the distro. Scripts stay off; links open in the browser.
"""
import html
import re
import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2  # noqa: E402

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
th { background: var(--code); }
img { max-width: 100%; }
hr { border: 0; border-top: 1px solid var(--line); margin: 1.5em 0; }
li + li { margin-top: .2em; }
"""


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


def render(path):
    text = path.read_text(errors="replace")
    suffix = path.suffix.lower()
    if suffix == ".html":
        return text
    if suffix in (".md", ".markdown", ""):
        import markdown
        body = markdown.markdown(loosen(text), extensions=["extra", "sane_lists", "nl2br"])
    else:
        body = f'<pre class="plain">{html.escape(text)}</pre>'
    return f"<!doctype html><meta charset=utf-8><style>{CSS}</style><main>{body}</main>"


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
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
