// xpet — a small desktop pig that lives on top of your windows.
// Xlib for windows/input, XShape for click-through, XRandR for monitors, Cairo for drawing.

#include "art.hpp"
#include "art3d.hpp"
#include "ipc.hpp"
#include "wardrobe.hpp"

#include <X11/Xatom.h>
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/extensions/Xrandr.h>
#include <X11/extensions/shape.h>
#include <cairo/cairo-xlib.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <deque>
#include <fstream>
#include <map>
#include <random>
#include <sstream>
#include <string>
#include <vector>

namespace {

constexpr int SCALE = 3;
constexpr int WIN_W = 300, WIN_H = 220;  // room for a bubble above the pig and for a tumble in the air
// The camera looks down at the pig, so whatever is nearer to you than the pig's centre (its near feet, a bowl
// in front of it, its head when it faces you) projects below the ground line. The window hangs this far below
// the ledge the pig stands on so that is never cut off.
constexpr int GROUND_MARGIN = 24;
constexpr int SPR_W = art::CW * SCALE, SPR_H = art::CH * SCALE;
constexpr int SPR_X = (WIN_W - SPR_W) / 2, SPR_Y = WIN_H - SPR_H;
constexpr double GRAVITY = 0.9, MAX_FALL = 28;
constexpr Window FLOOR = (Window)-1;

// Stat rates, per second.
constexpr double FOOD_DECAY = 100.0 / (8 * 3600);
constexpr double FUN_DECAY = 100.0 / (10 * 3600);
constexpr double ENERGY_DECAY = 100.0 / (6 * 3600);
constexpr double ENERGY_GAIN = 100.0 / (40 * 60);

volatile sig_atomic_t g_running = 1;
const struct Stats* g_stats = nullptr;  // saved if the X server goes away
bool g_showDetails = true;              // filenames/commands in work bubbles

std::mt19937 rng{std::random_device{}()};
double frand(double a = 0, double b = 1) { return std::uniform_real_distribution<double>(a, b)(rng); }
bool chance(double p) { return frand() < p; }
const std::string& pick(const std::vector<std::string>& v) {
    return v[std::uniform_int_distribution<size_t>(0, v.size() - 1)(rng)];
}
double clampd(double v, double lo, double hi) { return std::max(lo, std::min(hi, v)); }

double now() {
    using namespace std::chrono;
    return duration<double>(steady_clock::now().time_since_epoch()).count();
}

namespace lines {
const std::vector<std::string> happy = {"oink", "oink~", ":3", "snrrf", "*wiggles*", "squee"};
const std::vector<std::string> hungry = {"hungry...", "food? :(", "*stomach growls*", "the bowl is empty"};
const std::vector<std::string> sad = {"...", "pay attention to me", "hmph", "*sigh*"};
const std::vector<std::string> picked = {"hey!", "put me down!", "oink?!", "wheee"};
const std::vector<std::string> oof = {"oof", "ow!", "!!"};
const std::vector<std::string> pet = {"oink", "<3", "snrrf~", ":3"};
const std::vector<std::string> tooMuch = {"ok ok", "enough!", "snort"};
const std::vector<std::string> grumpy = {"...why", "I was napping", "hrmph"};
}  // namespace lines

// ---- persistent stats ------------------------------------------------------

struct Stats {
    double food = 80, fun = 80, energy = 80;
    std::string name = "Mochi";

    void clamp() {
        food = clampd(food, 0, 100);
        fun = clampd(fun, 0, 100);
        energy = clampd(energy, 0, 100);
    }
};

std::string configDir() {
    const char* x = getenv("XDG_CONFIG_HOME");
    std::string base = (x && *x) ? x : std::string(getenv("HOME") ? getenv("HOME") : ".") + "/.config";
    return base + "/xpet";
}

// Returns seconds since the last save (0 if there was none).
double loadStats(Stats& s) {
    std::ifstream f(configDir() + "/state");
    if (!f) return 0;
    std::string key;
    long saved = 0;
    while (f >> key) {
        if (key == "food") f >> s.food;
        else if (key == "fun") f >> s.fun;
        else if (key == "energy") f >> s.energy;
        else if (key == "time") f >> saved;
        else if (key == "name") { f >> std::ws; std::getline(f, s.name); }
    }
    s.clamp();
    return saved ? std::max<double>(0, time(nullptr) - saved) : 0;
}

void saveStats(const Stats& s) {
    std::string dir = configDir();
    mkdir(dir.c_str(), 0755);
    std::string tmp = dir + "/state.tmp";
    {
        std::ofstream f(tmp);
        f << "food " << s.food << "\nfun " << s.fun << "\nenergy " << s.energy << "\ntime "
          << (long)time(nullptr) << "\nname " << s.name << "\n";
    }
    rename(tmp.c_str(), (dir + "/state").c_str());
}

// ---- the desktop: monitors, windows and the ledges the pig can stand on ----

struct Seg {
    int x1, x2, y;
    Window id;  // client window, or FLOOR
};

struct Rect {
    int x, y, w, h;
};

using Intervals = std::vector<std::pair<int, int>>;

void subtract(Intervals& iv, int a, int b) {
    Intervals out;
    for (auto [x1, x2] : iv) {
        if (b < x1 || a > x2) { out.push_back({x1, x2}); continue; }
        if (a > x1) out.push_back({x1, a - 1});
        if (b < x2) out.push_back({b + 1, x2});
    }
    iv.swap(out);
}

class Desktop {
public:
    Display* dpy;
    Window root;
    std::vector<Rect> mons;
    std::vector<Seg> segs;
    std::map<Window, Rect> rects;  // visible rect of each usable client
    bool fullscreenActive = false;

    explicit Desktop(Display* d) : dpy(d), root(DefaultRootWindow(d)) {
        const char* names[] = {"_NET_CLIENT_LIST_STACKING", "_NET_CURRENT_DESKTOP", "_NET_WM_DESKTOP",
                               "_NET_WM_STATE", "_NET_WM_STATE_HIDDEN", "_NET_WM_STATE_FULLSCREEN",
                               "_NET_WM_WINDOW_TYPE", "_NET_WM_WINDOW_TYPE_DESKTOP", "_GTK_FRAME_EXTENTS",
                               "_NET_ACTIVE_WINDOW"};
        XInternAtoms(dpy, (char**)names, N_ATOMS, False, atoms);
        XSelectInput(dpy, root, PropertyChangeMask);
        refreshMonitors();
    }

    bool isWatchedRootProp(Atom a) const {
        return a == atoms[STACKING] || a == atoms[CURRENT_DESKTOP] || a == atoms[ACTIVE];
    }

    void refreshMonitors() {
        int n = 0;
        XRRMonitorInfo* m = XRRGetMonitors(dpy, root, True, &n);
        mons.clear();
        for (int i = 0; i < n; ++i) mons.push_back({m[i].x, m[i].y, m[i].width, m[i].height});
        if (m) XRRFreeMonitors(m);
        if (mons.empty())
            mons.push_back({0, 0, DisplayWidth(dpy, DefaultScreen(dpy)), DisplayHeight(dpy, DefaultScreen(dpy))});
    }

    bool inMonitors(double x, double y) const {
        for (auto& m : mons)
            if (x >= m.x && x < m.x + m.w && y >= m.y && y < m.y + m.h) return true;
        return false;
    }

    const Rect& monitorNear(double x, double y) const {
        const Rect* best = &mons[0];
        double bd = 1e18;
        for (auto& m : mons) {
            double dx = std::max({m.x - x, 0.0, x - (m.x + m.w)});
            double dy = std::max({m.y - y, 0.0, y - (m.y + m.h)});
            if (dx * dx + dy * dy < bd) { bd = dx * dx + dy * dy; best = &m; }
        }
        return *best;
    }

    // Window list, desktops, minimised state etc. Cheap enough to do about once a second.
    void slowRefresh() {
        auto cur = prop32(root, atoms[CURRENT_DESKTOP]);
        unsigned long curDesk = cur.empty() ? 0 : cur[0] & 0xFFFFFFFF;
        std::map<Window, Client> next;
        order.clear();
        for (unsigned long w : prop32(root, atoms[STACKING])) {
            if (has(prop32(w, atoms[TYPE]), atoms[TYPE_DESKTOP])) continue;
            if (has(prop32(w, atoms[STATE]), atoms[HIDDEN])) continue;
            auto d = prop32(w, atoms[WM_DESKTOP]);
            if (!d.empty() && (d[0] & 0xFFFFFFFF) != 0xFFFFFFFF && (d[0] & 0xFFFFFFFF) != curDesk) continue;
            Client c;
            c.frame = frameOf(w);
            if (!c.frame) continue;
            XWindowAttributes wa;
            if (!XGetWindowAttributes(dpy, c.frame, &wa) || wa.map_state != IsViewable) continue;
            auto g = prop32(w, atoms[GTK_EXTENTS]);
            if (g.size() == 4) { c.gl = g[0]; c.gr = g[1]; c.gt = g[2]; c.gb = g[3]; }
            next[w] = c;
            order.push_back(w);
        }
        clients.swap(next);

        fullscreenActive = false;
        auto act = prop32(root, atoms[ACTIVE]);
        if (!act.empty() && act[0]) fullscreenActive = has(prop32(act[0], atoms[STATE]), atoms[FULLSCREEN]);
    }

    // The focused client window (0 if none) and a window's title, for the pig to look at what you're doing.
    Window activeClient() {
        auto a = prop32(root, atoms[ACTIVE]);
        return a.empty() ? 0 : (Window)a[0];
    }
    std::string title(Window w) {
        if (!w) return "";
        Atom name = XInternAtom(dpy, "_NET_WM_NAME", False), utf8 = XInternAtom(dpy, "UTF8_STRING", False), type;
        int fmt;
        unsigned long n, after;
        unsigned char* data = nullptr;
        std::string out;
        if (XGetWindowProperty(dpy, w, name, 0, 256, False, utf8, &type, &fmt, &n, &after, &data) == Success && data) {
            out.assign((char*)data, n);
            XFree(data);
        }
        if (out.empty()) {
            char* t = nullptr;
            if (XFetchName(dpy, w, &t) && t) { out = t; XFree(t); }
        }
        return out;
    }

    // Just the geometry. Done every couple of frames so the pig rides along when you drag a window.
    void fastRefresh() {
        rects.clear();
        std::vector<std::pair<Window, Rect>> stack;
        for (Window w : order) {
            const Client& c = clients[w];
            Window r;
            int x, y;
            unsigned ww, hh, bw, depth;
            if (!XGetGeometry(dpy, c.frame, &r, &x, &y, &ww, &hh, &bw, &depth)) continue;
            Rect rc{x + c.gl, y + c.gt, (int)ww - c.gl - c.gr, (int)hh - c.gt - c.gb};
            if (rc.w <= 0 || rc.h <= 0) continue;
            rects[w] = rc;
            stack.push_back({w, rc});
        }
        buildSegments(stack);
    }

    const Seg* segAt(Window id, double x, double y) const {
        for (auto& s : segs)
            if (s.id == id && std::fabs(s.y - y) < 0.5 && x >= s.x1 && x <= s.x2) return &s;
        return nullptr;
    }

    const Seg* floorBelow(double x) const {
        for (auto& s : segs)
            if (s.id == FLOOR && x >= s.x1 && x <= s.x2) return &s;
        return nullptr;
    }

    int lowestFloor() const {
        int y = 0;
        for (auto& m : mons) y = std::max(y, m.y + m.h);
        return y;
    }

private:
    enum {
        STACKING, CURRENT_DESKTOP, WM_DESKTOP, STATE, HIDDEN, FULLSCREEN, TYPE, TYPE_DESKTOP, GTK_EXTENTS,
        ACTIVE, N_ATOMS
    };
    Atom atoms[N_ATOMS];

    struct Client {
        Window frame = 0;
        int gl = 0, gr = 0, gt = 0, gb = 0;  // invisible CSD shadow margins
    };
    std::map<Window, Client> clients;
    std::vector<Window> order;  // bottom to top

    static bool has(const std::vector<unsigned long>& v, Atom a) { return std::find(v.begin(), v.end(), a) != v.end(); }

    std::vector<unsigned long> prop32(Window w, Atom prop) {
        Atom type;
        int fmt;
        unsigned long n, after;
        unsigned char* data = nullptr;
        std::vector<unsigned long> out;
        if (XGetWindowProperty(dpy, w, prop, 0, 4096, False, AnyPropertyType, &type, &fmt, &n, &after, &data) ==
                Success && data) {
            if (fmt == 32) out.assign((unsigned long*)data, (unsigned long*)data + n);
            XFree(data);
        }
        return out;
    }

    // The window manager's frame: the ancestor that is a direct child of the root.
    Window frameOf(Window w) {
        for (int i = 0; i < 16; ++i) {
            Window r, parent, *kids = nullptr;
            unsigned n;
            if (!XQueryTree(dpy, w, &r, &parent, &kids, &n)) return 0;
            if (kids) XFree(kids);
            if (parent == root) return w;
            w = parent;
        }
        return 0;
    }

    // Every window's top edge is a ledge, minus the parts covered by windows above it,
    // clipped to where there's actually a monitor. Monitor bottoms are the floor.
    void buildSegments(const std::vector<std::pair<Window, Rect>>& stack) {
        segs.clear();
        for (size_t i = 0; i < stack.size(); ++i) {
            const Rect& r = stack[i].second;
            int y = r.y;
            Intervals iv{{r.x, r.x + r.w - 1}};
            for (size_t j = i + 1; j < stack.size(); ++j) {
                const Rect& a = stack[j].second;
                if (a.y <= y && y < a.y + a.h) subtract(iv, a.x, a.x + a.w - 1);
            }
            addClipped(iv, y, stack[i].first);
            // And a floor inside the window, just above its bottom edge, so it can walk around in there too
            // (only in windows tall enough to stand in without poking out of the top).
            if (r.h >= 170) {
                int fy = r.y + r.h - 3;
                Intervals fv{{r.x + 6, r.x + r.w - 7}};
                for (size_t j = i + 1; j < stack.size(); ++j) {
                    const Rect& a = stack[j].second;
                    if (a.y <= fy && fy < a.y + a.h) subtract(fv, a.x, a.x + a.w - 1);
                }
                addClipped(fv, fy, stack[i].first);
            }
        }
        for (auto& m : mons) {
            int bottom = m.y + m.h;
            Intervals iv{{m.x, m.x + m.w - 1}};
            for (auto& o : mons)
                if (o.y == bottom) subtract(iv, o.x, o.x + o.w - 1);
            for (auto [a, b] : iv) segs.push_back({a, b, bottom, FLOOR});
        }
        // Merge touching pieces (e.g. floors of side-by-side monitors).
        std::sort(segs.begin(), segs.end(), [](const Seg& a, const Seg& b) {
            return std::tie(a.id, a.y, a.x1) < std::tie(b.id, b.y, b.x1);
        });
        std::vector<Seg> merged;
        for (auto& s : segs) {
            if (!merged.empty() && merged.back().id == s.id && merged.back().y == s.y && s.x1 <= merged.back().x2 + 1)
                merged.back().x2 = std::max(merged.back().x2, s.x2);
            else
                merged.push_back(s);
        }
        segs.swap(merged);
    }

    void addClipped(const Intervals& iv, int y, Window id) {
        for (auto& m : mons) {
            if (!(m.y <= y - 1 && y - 1 < m.y + m.h)) continue;
            for (auto [a, b] : iv) {
                int x1 = std::max(a, m.x), x2 = std::min(b, m.x + m.w - 1);
                if (x2 - x1 >= 24) segs.push_back({x1, x2, y, id});
            }
        }
    }
};

// ---- the panel: right-click menu and Mochi's questions ----------------------------
//
// One small window beside the pig. A plain menu is just a list; a question carries its text at the top
// ("Mochi asks") and the answers below. It is placed next to the pig, never on top of it or its speech
// bubble: to the right if there's room, else to the left, else above.

struct Avoid {
    int x, y, w, h;  // root coords: where the pig (and its bubble) is
};

class Menu {
public:
    bool open = false;

    explicit Menu(Display* d) : dpy(d) {}

    void show(std::vector<std::string> items, const Rect& mon, const Avoid& pig, std::string title = "",
              std::string body = "") {
        close();
        labels = std::move(items);
        heading = std::move(title);
        hover = -1;
        W = heading.empty() ? 210 : 290;
        // Measure the header on a scratch surface so the window can be sized to it.
        bodyLines.clear();
        headerH = 0;
        if (!heading.empty()) {
            cairo_surface_t* tmp = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, 4, 4);
            cairo_t* cr = cairo_create(tmp);
            cairo_select_font_face(cr, "Sans", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_NORMAL);
            cairo_set_font_size(cr, 13);
            bodyLines = wrap(cr, body, W - 2 * PAD_X, 6);
            cairo_destroy(cr);
            cairo_surface_destroy(tmp);
            headerH = 10 + 16 + (bodyLines.empty() ? 0 : 6 + 17 * (int)bodyLines.size()) + 10;
        }
        int h = headerH + PAD * 2 + ITEM_H * (int)labels.size();
        // Placement: beside the pig, bottom edge level with its feet.
        int x = pig.x + pig.w + 8, y = pig.y + pig.h - h;
        if (x + W > mon.x + mon.w - 4) x = pig.x - W - 8;
        if (x < mon.x + 4) { x = pig.x + pig.w / 2 - W / 2; y = pig.y - h - 8; }
        x = (int)clampd(x, mon.x + 4, mon.x + mon.w - W - 4);
        y = (int)clampd(y, mon.y + 4, mon.y + mon.h - h - 4);
        XSetWindowAttributes swa{};
        swa.override_redirect = True;
        swa.event_mask = ExposureMask | ButtonPressMask | ButtonReleaseMask | PointerMotionMask;
        int scr = DefaultScreen(dpy);
        win = XCreateWindow(dpy, DefaultRootWindow(dpy), x, y, W, h, 0, CopyFromParent, InputOutput,
                            CopyFromParent, CWOverrideRedirect | CWEventMask, &swa);
        surf = cairo_xlib_surface_create(dpy, win, DefaultVisual(dpy, scr), W, h);
        height = h;
        XMapRaised(dpy, win);
        XSync(dpy, False);
        XGrabPointer(dpy, win, False, ButtonPressMask | ButtonReleaseMask | PointerMotionMask, GrabModeAsync,
                     GrabModeAsync, None, None, CurrentTime);
        open = true;
        draw();
    }

    void close() {
        if (!open) return;
        XUngrabPointer(dpy, CurrentTime);
        cairo_surface_destroy(surf);
        XDestroyWindow(dpy, win);
        open = false;
    }

    void raise() {
        if (open) XRaiseWindow(dpy, win);
    }

    // Returns the chosen label, or "" if nothing was chosen. Closes on any click.
    std::string handle(const XEvent& e) {
        if (!open) return "";
        if (e.type == Expose && e.xexpose.window == win) draw();
        if (e.type == MotionNotify) {
            int h = itemAt(e.xmotion.x, e.xmotion.y);
            if (h != hover) { hover = h; draw(); }
        }
        if (e.type == ButtonPress) {
            if (e.xbutton.button == 4 || e.xbutton.button == 5) {  // wheel: flip between questions
                close();
                return e.xbutton.button == 4 ? PREV : NEXT;
            }
            if (e.xbutton.button != 1) return "";  // other buttons: ignore, keep the panel
            int i = itemAt(e.xbutton.x, e.xbutton.y);
            close();
            if (i >= 0) return labels[i];
        }
        return "";
    }

    bool owns(Window w) const { return open && w == win; }

    static constexpr const char* PREV = "<  Previous question";
    static constexpr const char* NEXT = "Next question  >";

    static std::vector<std::string> wrap(cairo_t* cr, const std::string& text, double maxW, int maxLines) {
        std::vector<std::string> out;
        std::string cur, word;
        std::istringstream ss(text);
        bool more = false;
        while (ss >> word) {
            if ((int)out.size() == maxLines) { more = true; break; }
            std::string trial = cur.empty() ? word : cur + " " + word;
            cairo_text_extents_t e;
            cairo_text_extents(cr, trial.c_str(), &e);
            if (e.x_advance <= maxW || cur.empty()) cur = trial;
            else { out.push_back(cur); cur = word; }
        }
        if (!cur.empty()) {
            if ((int)out.size() < maxLines) out.push_back(cur);
            else more = true;
        }
        if (more && !out.empty()) out.back() += "…";
        return out;
    }

private:
    static constexpr int ITEM_H = 28, PAD = 6, PAD_X = 14;
    int W = 210;
    Display* dpy;
    Window win = 0;
    cairo_surface_t* surf = nullptr;
    int height = 0, hover = -1, headerH = 0;
    std::vector<std::string> labels, bodyLines;
    std::string heading;

    static bool hasIcon(const std::string& l) {
        return l.find("for you") != std::string::npos || l.rfind("Give me", 0) == 0 || l.rfind("Open a chat", 0) == 0 ||
               l.rfind("Shoo", 0) == 0 || l == "Quit";
    }

    // Tiny vector icons, drawn in the current colour, centred on (cx, cy), about 14 px.
    static void icon(cairo_t* cr, const std::string& l, double cx, double cy, bool quiet) {
        (void)quiet;
        cairo_new_path(cr);
        if (l.find("for you") != std::string::npos) {  // a bell, filled yellow: something waits for you
            cairo_set_source_rgb(cr, 0.98, 0.78, 0.22);
            cairo_arc(cr, cx, cy - 1, 5, M_PI, 2 * M_PI);
            cairo_line_to(cr, cx + 6, cy + 4);
            cairo_line_to(cr, cx - 6, cy + 4);
            cairo_close_path(cr);
            cairo_fill_preserve(cr);
            cairo_set_source_rgb(cr, 0.16, 0.12, 0.10);
            cairo_stroke(cr);
            cairo_arc(cr, cx, cy + 6.5, 1.8, 0, 2 * M_PI);
            cairo_fill(cr);
        } else if (l.rfind("Give me", 0) == 0) {  // a note with a tick
            cairo_rectangle(cr, cx - 5, cy - 6.5, 10, 13);
            cairo_stroke(cr);
            cairo_move_to(cr, cx - 2.5, cy + 0.5);
            cairo_line_to(cr, cx - 0.5, cy + 2.5);
            cairo_line_to(cr, cx + 3, cy - 2);
            cairo_stroke(cr);
            cairo_move_to(cr, cx - 3, cy - 3.5);
            cairo_line_to(cr, cx + 3, cy - 3.5);
            cairo_stroke(cr);
        } else if (l.rfind("Open a chat", 0) == 0) {  // a speech bubble
            cairo_new_sub_path(cr);
            cairo_arc(cr, cx - 3, cy - 3, 3, M_PI, 1.5 * M_PI);
            cairo_arc(cr, cx + 3, cy - 3, 3, 1.5 * M_PI, 2 * M_PI);
            cairo_arc(cr, cx + 3, cy + 1, 3, 0, 0.5 * M_PI);
            cairo_line_to(cr, cx - 1, cy + 4);
            cairo_line_to(cr, cx - 3, cy + 7);
            cairo_line_to(cr, cx - 3.5, cy + 4);
            cairo_arc(cr, cx - 3, cy + 1, 3, 0.5 * M_PI, M_PI);
            cairo_close_path(cr);
            cairo_stroke(cr);
        } else if (l.rfind("Shoo", 0) == 0) {  // out the door
            cairo_move_to(cr, cx + 1, cy - 6);
            cairo_line_to(cr, cx - 5, cy - 6);
            cairo_line_to(cr, cx - 5, cy + 6);
            cairo_line_to(cr, cx + 1, cy + 6);
            cairo_stroke(cr);
            cairo_move_to(cr, cx - 1, cy);
            cairo_line_to(cr, cx + 7, cy);
            cairo_stroke(cr);
            cairo_move_to(cr, cx + 4, cy - 3);
            cairo_line_to(cr, cx + 7, cy);
            cairo_line_to(cr, cx + 4, cy + 3);
            cairo_stroke(cr);
        } else if (l == "Quit") {  // power
            cairo_new_sub_path(cr);
            cairo_arc(cr, cx, cy + 0.5, 5.5, -0.35 * M_PI, 1.35 * M_PI);
            cairo_stroke(cr);
            cairo_move_to(cr, cx, cy - 6.5);
            cairo_line_to(cr, cx, cy - 0.5);
            cairo_stroke(cr);
        }
    }

    int itemAt(int x, int y) const {
        int top = headerH + PAD;
        if (x < 0 || x >= W || y < top || y >= height - PAD) return -1;
        return (y - top) / ITEM_H;
    }

    void draw() {
        cairo_t* cr = cairo_create(surf);
        cairo_set_source_rgb(cr, 0.16, 0.12, 0.10);
        cairo_paint(cr);
        cairo_set_source_rgb(cr, 1.0, 0.96, 0.90);
        cairo_rectangle(cr, 2, 2, W - 4, height - 4);
        cairo_fill(cr);
        if (headerH) {  // a warm yellow header: the question
            cairo_set_source_rgb(cr, 1.0, 0.93, 0.72);
            cairo_rectangle(cr, 2, 2, W - 4, headerH - 2);
            cairo_fill(cr);
            cairo_set_source_rgb(cr, 0.16, 0.12, 0.10);
            cairo_select_font_face(cr, "Sans", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_BOLD);
            cairo_set_font_size(cr, 13);
            cairo_move_to(cr, PAD_X, 10 + 13);
            cairo_show_text(cr, heading.c_str());
            cairo_select_font_face(cr, "Sans", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_NORMAL);
            for (size_t i = 0; i < bodyLines.size(); ++i) {
                cairo_move_to(cr, PAD_X, 10 + 16 + 6 + 13 + 17 * i);
                cairo_show_text(cr, bodyLines[i].c_str());
            }
            cairo_set_source_rgb(cr, 0.16, 0.12, 0.10);
            cairo_rectangle(cr, 2, headerH, W - 4, 1);
            cairo_fill(cr);
        }
        cairo_select_font_face(cr, "Sans", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_BOLD);
        cairo_set_font_size(cr, 13);
        for (int i = 0; i < (int)labels.size(); ++i) {
            int y = headerH + PAD + i * ITEM_H;
            bool quiet = labels[i] == "Later" || labels[i] == "Cancel" || labels[i] == PREV || labels[i] == NEXT;
            if (i == hover) {
                cairo_set_source_rgb(cr, 0.95, 0.65, 0.35);
                cairo_rectangle(cr, 4, y, W - 8, ITEM_H);
                cairo_fill(cr);
            }
            cairo_set_source_rgb(cr, quiet ? 0.5 : 0.16, quiet ? 0.42 : 0.12, quiet ? 0.34 : 0.10);
            cairo_select_font_face(cr, "Sans", CAIRO_FONT_SLANT_NORMAL, quiet ? CAIRO_FONT_WEIGHT_NORMAL : CAIRO_FONT_WEIGHT_BOLD);
            double ix = PAD_X + 1, iy = y + ITEM_H / 2.0;  // icon centre
            cairo_save(cr);
            cairo_set_line_width(cr, 1.6);
            cairo_set_line_cap(cr, CAIRO_LINE_CAP_ROUND);
            icon(cr, labels[i], ix + 7, iy, quiet);
            cairo_restore(cr);
            cairo_move_to(cr, PAD_X + (hasIcon(labels[i]) ? 24 : 0), y + 18);
            cairo_show_text(cr, labels[i].c_str());
        }
        cairo_destroy(cr);
        cairo_surface_flush(surf);
    }
};

// ---- the pig -------------------------------------------------------------------

enum class St { Idle, Walk, Sit, Sleep, Air, Drag, Eat, Play, Away, Work };

struct Particle {
    double x, y, vx, vy, life;
    int kind;  // 0 heart, 1 z
};

// Something the brain wants from you: an offer ("shall I...?") or a notice ("done, want to see?").
// Quiet ones wait in the right-click menu behind a small dot; urgent ones get a bubble and a hop
// until you click the pig and answer. The brain withdraws what you never got to.
struct Ask {
    std::string id, text;
    std::string options;  // menu items, '|'-separated; the brain decides what they are
    double at;
    bool urgent;
};

enum class Bub { Plain, Stats, Ask };

// What Claude (or anything else talking over the socket) is doing right now.
enum class Act { Off, Think, Read, Search, Write, Run, Web, Agent, Plan, Tool, Alert, Done, Compact };

struct Session {
    bool busy = false;  // between a prompt and Stop
    Act act = Act::Off;
    std::string tool, detail;
    int count = 0;  // same kind of tool in a row ("reading 3 files")
    double last = 0, actAt = 0;
    long pid = 0;
    int edits = 0, errors = 0;  // this turn: files touched, tools that failed
    bool sulked = false;        // already sulked about this turn's failures
};

Act actForTool(const std::string& t, std::string& detail) {
    if (t == "Read") return Act::Read;
    if (t == "Grep" || t == "Glob" || t == "LS") return Act::Search;
    if (t == "Edit" || t == "Write" || t == "MultiEdit" || t == "NotebookEdit") return Act::Write;
    if (t == "Bash" || t == "BashOutput" || t == "KillShell") return Act::Run;
    if (t == "WebFetch" || t == "WebSearch") return Act::Web;
    if (t == "Agent" || t == "Task") return Act::Agent;
    if (t == "TodoWrite" || t == "EnterPlanMode" || t == "ExitPlanMode") return Act::Plan;
    // mcp__claude_ai_Gmail__search_threads -> "Gmail"
    std::string name = t;
    if (name.rfind("mcp__", 0) == 0) {
        name = name.substr(5, name.find("__", 5) - 5);
        for (const char* pre : {"claude_ai_", "plugin_"})
            if (name.rfind(pre, 0) == 0) name = name.substr(strlen(pre));
        std::replace(name.begin(), name.end(), '_', ' ');
    }
    if (detail.empty()) detail = name;
    return Act::Tool;
}

class Pet {
public:
    Pet(Display* d, Desktop& desk, Stats& stats) : dpy(d), D(desk), s(stats), menu(d) { createWindow(); }

    ~Pet() {
        menu.close();
        cairo_surface_destroy(back);
        cairo_surface_destroy(winSurf);
        XDestroyWindow(dpy, win);
    }

    void greet(double awaySecs) {
        if (s.food < 25) say("where's my food?");
        else if (awaySecs > 6 * 3600) say("you're back!");
        else say("hi!");
    }

    void dropInAtPointer() {
        int rx, ry;
        pointer(rx, ry);
        const Rect& m = D.monitorNear(rx, ry);
        px = clampd(rx, m.x + 40, m.x + m.w - 40);
        py = m.y;
        vx = 0;
        vy = 0;
        set(St::Air);
    }

    void onRootProperty() { raiseAll(); }

    void onMessage(const json::Flat& m) {
        auto get = [&](const char* k) {
            auto it = m.find(k);
            return it == m.end() ? std::string() : it->second;
        };
        std::string ev = get("event");
        if (ev == "say") {
            std::string text = get("text");
            double secs = clampd(atof(get("secs").c_str()), 0, 60);
            if (!text.empty()) say(text, secs > 0 ? secs : 4);
            return;
        }
        if (ev == "trick") {  // any action in art3d::ACTIONS, by name: for trying things out, and for the brain
            if (const art3d::ActionInfo* a = art3d::actionNamed(get("name")); a && grounded() && st != St::Sleep)
                anim.start(a->action, atof(get("secs").c_str()) > 0 ? clampd(atof(get("secs").c_str()), 0.2, 10) : a->secs);
            return;
        }
        if (ev == "wear") {  // a costume by name, "none", or "auto" (back to the calendar in wardrobe.hpp)
            std::string c = get("costume");
            costumeForced = c != "auto" && !c.empty();
            costume = costumeForced ? wardrobe::named(c) : wardrobe::today();
            return;
        }
        if (ev == "emote") {
            std::string e = get("emote");
            if (e == "happy") { happyLeft = 2; spawn(0); spawn(0); }
            else if (e == "oof") say(pick(lines::oof), 2);
            else if (e == "sleep") goSleep();
            return;
        }
        if (ev == "ask" || ev == "offer" || ev == "notice" || ev == "withdraw") {
            std::string id = get("id");
            if (id.empty()) return;
            std::string options = get("options");
            if (options.empty()) options = ev == "notice" ? "Show me|Chat about it|Dismiss" : "Yes, do it|Not now|Never|Chat about it";
            bool had = false;
            for (auto& a : asks)
                if (a.id == id) {
                    had = true;
                    if (ev != "withdraw") { a.text = get("text"); a.options = options; }  // re-sent: keep its place
                }
            if (ev == "withdraw") {
                asks.erase(std::remove_if(asks.begin(), asks.end(), [&](const Ask& a) { return a.id == id; }), asks.end());
            } else if (!had && !get("text").empty()) {
                bool urgent = get("urgent") == "1";
                asks.push_back({id, get("text"), options, now(), urgent});
                if (urgent) {
                    if (st == St::Sleep) { say("mrrp?", 2); set(St::Idle, 2); }
                    else askHop = 2.5;
                }
            }
            return;
        }
        std::string id = get("session");
        if (id.empty()) id = get("src").empty() ? "anon" : get("src");
        double t = now();
        if (ev == "session_end") { sessions.erase(id); return; }
        Session& se = sessions[id];
        se.last = t;
        if (long pid = atol(get("pid").c_str())) se.pid = pid;
        auto setAct = [&](Act a, const std::string& tool, const std::string& detail) {
            se.count = (a == se.act && tool == se.tool) ? se.count + 1 : 1;
            se.act = a;
            se.tool = tool;
            se.detail = detail;
            se.actAt = t;
        };
        if (ev == "session_start") {
            if (shown == Act::Off && st != St::Sleep) say("!", 1.5);
        } else if (ev == "prompt") {
            se.busy = true;
            se.edits = se.errors = 0;
            se.sulked = false;
            setAct(Act::Think, "", "");
        } else if (ev == "tool_start") {
            se.busy = true;
            std::string tool = get("tool"), detail = get("detail");
            Act a = actForTool(tool, detail);
            if (a == Act::Write) ++se.edits;
            setAct(a, tool, detail);
        } else if (ev == "tool_end") {
            if (!get("error").empty()) {
                ++se.errors;
                if (id == shownSession) { say(pick(lines::oof), 1.5); s.fun -= 0.5; }
                if (se.errors >= 3 && !se.sulked && id == shownSession) {  // a job that keeps failing: sulk a little
                    se.sulked = true;
                    anim.start(art3d::Action::Sulk, 3.5);
                    say("hmm, that keeps failing", 3);
                }
            }
            if (se.act != Act::Alert) setAct(se.busy ? Act::Think : Act::Off, "", "");
        } else if (ev == "subagent_stop") {
            if (se.act == Act::Agent) setAct(Act::Think, "", "");
        } else if (ev == "notify") {
            // Claude Code notifications: a permission prompt is worth acting out; "waiting for your input" after
            // a turn is just the normal end of a turn (Stop already covers it), so it doesn't count as needing you.
            std::string d = get("detail");
            bool idle = d.find("waiting for your input") != std::string::npos || d.find("idle") != std::string::npos;
            if (idle) { se.busy = false; if (se.act != Act::Done) setAct(Act::Off, "", ""); }
            else setAct(Act::Alert, "", d);
        } else if (ev == "stop") {
            se.busy = false;
            setAct(Act::Done, "", "");
            if (id == shownSession || shown == Act::Off) {
                if (se.edits >= 5) { anim.start(art3d::Action::Cheer, 1.6); say("done! " + std::to_string(se.edits) + " files", 3); spawn(0); spawn(0); }
                else if (se.errors >= 3) { say("done, but that was rough", 3); }
            }
        } else if (ev == "compact") {
            setAct(Act::Compact, "", "");
        }
    }

    void handle(const XEvent& e) {
        if (menu.open) {
            std::string choice = menu.handle(e);
            if (!menu.open) {
                bool wasAsk = askMenu;
                askMenu = false;
                if (!choice.empty()) wasAsk ? answerAsk(choice) : act(choice);
            }
            return;
        }
        if (e.xany.window != win) return;
        switch (e.type) {
            case ButtonPress:
                if (e.xbutton.button == 1) {
                    pressed = true;
                    dragging = false;
                    pressX = e.xbutton.x_root;
                    pressY = e.xbutton.y_root;
                    dragOffX = px - pressX;
                    dragOffY = py - pressY;
                }
                break;
            case MotionNotify:
                if (!pressed) break;
                if (!dragging && std::hypot(e.xmotion.x_root - pressX, e.xmotion.y_root - pressY) > 4) {
                    dragging = true;
                    if (st == St::Sleep) say(pick(lines::grumpy));
                    else if (chance(0.5)) say(pick(lines::picked));
                    set(St::Drag);
                }
                if (dragging) {
                    px = e.xmotion.x_root + dragOffX;
                    py = e.xmotion.y_root + dragOffY;
                }
                break;
            case ButtonRelease:
                if (e.xbutton.button == 1 && pressed) {
                    pressed = false;
                    if (dragging) {
                        dragging = false;
                        vx = clampd(dragVx, -30, 30);
                        vy = clampd(dragVy, -30, 30);
                        thrown = std::hypot(vx, vy) > 9;
                        if (!thrown) placedAt = now();  // set down gently: it will stay where you put it for a while
                        if (std::fabs(vx) > 1) dir = vx > 0 ? 1 : -1;  // fly nose first
                        set(St::Air);
                    } else if (!asks.empty() && (asks.front().urgent || now() < previewUntil) && !hidden) {
                        if (now() >= previewUntil) askIndex = 0;
                        openAskMenu(e.xbutton.x_root, e.xbutton.y_root);  // answer the question it showed
                    } else {
                        onPet();
                    }
                } else if (e.xbutton.button == 3) {
                    openMenu(e.xbutton.x_root, e.xbutton.y_root);
                } else if (e.xbutton.button == 2) {
                    say(statusLine(), 5);
                } else if ((e.xbutton.button == 4 || e.xbutton.button == 5) && !asks.empty()) {
                    // Wheel: flip through its questions without opening the panel; a click then opens that one.
                    size_t n = asks.size();
                    if (now() < previewUntil) askIndex = (askIndex + (e.xbutton.button == 5 ? 1 : n - 1)) % n;
                    if (askIndex >= n) askIndex = 0;
                    say((n > 1 ? std::to_string(askIndex + 1) + "/" + std::to_string(n) + ": " : "") + asks[askIndex].text, 6);
                    previewUntil = now() + 6;
                }
                break;
        }
    }

    void update(double dt) {
        ++tick;
        updateStats(dt);

        // Hide while a fullscreen app is focused (videos, games), or when sent away.
        bool shouldHide = st == St::Away || D.fullscreenActive;
        if (shouldHide != hidden) {
            hidden = shouldHide;
            if (hidden) { menu.close(); XUnmapWindow(dpy, win); }
            else { XMapRaised(dpy, win); }
        }
        if (st == St::Away) {
            if ((awayLeft -= dt) <= 0) { dropInAtPointer(); say("I'm back"); }
            return;
        }

        if (st == St::Drag) {
            dragVx = px - lastPx;
            dragVy = py - lastPy;
        } else if (st == St::Air) {
            airStep();
        } else {
            groundStep(dt);
        }
        lastPx = px;
        lastPy = py;

        for (auto& p : particles) { p.x += p.vx; p.y += p.vy; p.life -= dt; }
        particles.erase(std::remove_if(particles.begin(), particles.end(), [](auto& p) { return p.life <= 0; }),
                        particles.end());
        if (st == St::Sleep && tick % 40 == 0) spawn(1);
        updateActivity();
        if (bubbleLeft > 0) bubbleLeft -= dt;
        animate(dt);
        if (happyLeft > 0) happyLeft -= dt;
        if (askHop > 0) askHop -= dt;
        if (tick % 300 == 0)  // the brain normally withdraws these; this is in case it died
            asks.erase(std::remove_if(asks.begin(), asks.end(), [](const Ask& a) { return now() - a.at > 48 * 3600; }),
                       asks.end());
        if (tick % 1800 == 0 && !costumeForced) costume = wardrobe::today();  // once a minute: the calendar turns
        if (blink > 0) blink -= dt;
        else if ((nextBlink -= dt) <= 0) { blink = 0.13; nextBlink = chance(0.2) ? 0.3 : frand(2.5, 7); }
        if ((chatLeft -= dt) <= 0) chatter();

        int wx = (int)std::lround(px) - WIN_W / 2, wy = (int)std::lround(py) - WIN_H + GROUND_MARGIN;
        if (wx != winX || wy != winY) {
            winX = wx;
            winY = wy;
            XMoveWindow(dpy, win, wx, wy);
        }
        if (tick % 30 == 0) raiseAll();
    }

    // Little things it does on its own, and how it reacts: all one-shot actions on the animator.
    void animate(double dt) {
        if (happyLeft > 0 && prevHappy <= 0 && !anim.busy() && grounded()) anim.start(art3d::Action::Wiggle, 1.0);
        prevHappy = happyLeft;
        if (askHop > 0 && !anim.busy() && grounded()) anim.start(art3d::Action::Hop, 0.5);
        if (st == St::Drag && !anim.busy() && chance(dt / 3)) anim.start(art3d::Action::Struggle, 1.1);
        bool idle = (st == St::Idle || st == St::Sit) && !anim.busy() && bubbleLeft <= 0;
        if (idle && (idleActionIn -= dt) <= 0) {
            idleActionIn = frand(3, 9);
            double r = frand();
            if (st == St::Sit) {
                if (r < 0.3) anim.start(art3d::Action::LookAround, 2.4);
                else if (r < 0.5) anim.start(art3d::Action::Scratch, 1.7);
                else if (r < 0.65) anim.start(art3d::Action::Shake, 0.9);
            } else {
                if (r < 0.25) anim.start(art3d::Action::Sniff, 1.7);
                else if (r < 0.42) anim.start(art3d::Action::LookAround, 2.4);
                else if (r < 0.57) anim.start(art3d::Action::Stretch, 2.2);
                else if (r < 0.7) anim.start(art3d::Action::Shake, 0.9);
                else if (r < 0.8) anim.start(art3d::Action::Scratch, 1.7);
                else if (r < 0.9 && s.fun > 50) anim.start(art3d::Action::Hop, 0.55);
                else anim.start(art3d::Action::Wiggle, 0.9);
            }
        }
        if (st == St::Play && !anim.busy() && std::fabs(vx) < 0.1 && chance(dt / 2)) anim.start(art3d::Action::Hop, 0.5);
        attention(dt);
    }

    // Needing you and being ignored: after a while it walks right up to the screen, big head at the glass, looks
    // at you for a few seconds, then walks back. Repeats now and then until you deal with it.
    bool wantsAttention() const { return (!asks.empty() && asks.front().urgent) || shown == Act::Alert; }
    bool closeupOn() const { return closeT >= 0; }
    void closeup(double dt, double t) {
        bool wants = wantsAttention();
        if (!wants) attentionSince = 0;
        else if (!attentionSince) attentionSince = t;
        if (closeT < 0) {
            bool can = grounded() && st != St::Sleep && st != St::Work && !menu.open && !hidden;
            if (wants && can && t - attentionSince > 25 && t - lastCloseup > 100) closeT = 0;
            approach += (0 - approach) * 0.2;
            return;
        }
        closeT += dt;
        if (!wants && closeT < CLOSE_IN + CLOSE_STARE) closeT = CLOSE_IN + CLOSE_STARE;  // dealt with: head back
        double target = closeT < CLOSE_IN ? closeT / CLOSE_IN
                        : closeT < CLOSE_IN + CLOSE_STARE ? 1.0
                        : std::max(0.0, 1 - (closeT - CLOSE_IN - CLOSE_STARE) / CLOSE_OUT);
        approach = target * target * (3 - 2 * target);
        if (closeT >= CLOSE_IN + CLOSE_STARE + CLOSE_OUT) { closeT = -1; lastCloseup = t; approach = 0; }
    }

    // What it looks at: your pointer when it moves, the window you're working in when that changes (focus or
    // title: typing in a terminal or editor changes titles), and you, when it talks to you, when you pet it, and
    // now and then when things are quiet.
    void attention(double dt) {
        double t = now();
        closeup(dt, t);
        if (tick % 4 == 0 && !hidden) {  // pointer, sampled a few times a second (it's a round trip)
            int rx, ry;
            pointer(rx, ry);
            if (std::abs(rx - ptrX) > 2 || std::abs(ry - ptrY) > 2) ptrMovedAt = t;
            ptrX = rx;
            ptrY = ry;
        }
        if (tick % 15 == 0 && !hidden) {  // the focused window: where it is and whether it's doing something
            Window w = D.activeClient();
            if (w == win) w = 0;
            std::string title = w ? D.title(w) : "";
            if (w != activeWin) winFocusAt = t;
            if (w != activeWin || title != activeTitle) winActivityAt = t;
            activeWin = w;
            activeTitle = title;
            auto it = D.rects.find(w);
            if (it != D.rects.end()) {
                activeCx = it->second.x + it->second.w / 2;
                activeCy = it->second.y + std::min(it->second.h / 2, 300);  // the upper part, where the work usually is
            } else activeWin = 0;
        }
        // Sometimes it goes over to sit on the window you've settled into.
        if (activeWin && activeWin != plat && t - winFocusAt > 20 && (st == St::Idle || st == St::Sit) && !anim.busy() &&
            t - lastVisit > 240 && D.rects.count(activeWin) && chance(dt / 15)) {
            const Rect& r = D.rects[activeWin];
            if (std::hypot(r.x + r.w / 2.0 - px, r.y - py) < 1400) {
                visitWin = activeWin;
                lastVisit = t;
                playLeft = 25;
                set(St::Play);
            }
        }
        double headX = px, headY = py - 60;
        if (!pressed && t - ptrMovedAt < 1.5 && std::hypot(ptrX - headX, ptrY - headY) < 45 && t - snuffleAt > 8 &&
            !anim.busy() && (st == St::Idle || st == St::Sit || st == St::Walk)) {  // your finger at its nose
            snuffleAt = t;
            anim.start(art3d::Action::Snuffle, 1.4);
        }
        bool talkingToYou = bubbleLeft > 0 || happyLeft > 0 || askHop > 0 || (closeupOn() && closeT < CLOSE_IN + CLOSE_STARE);
        double ptrD = std::hypot(ptrX - headX, ptrY - headY);
        bool ptrFresh = t - ptrMovedAt < 0.5 && ptrD > 90 && ptrD < 1100;
        bool winFresh = activeWin && t - winActivityAt < 1.0 && std::hypot(activeCx - headX, activeCy - headY) < 1800;
        // Something started happening: sometimes that catches its eye, for a few seconds, then it loses interest.
        if (t >= nextInterestAt && (ptrFresh || winFresh)) {
            if (chance(0.55)) interestUntil = t + frand(2, 6);
            nextInterestAt = std::max(interestUntil, t) + frand(5, 25);
        }
        if (t - glanceAt > glanceIn) { glanceAt = t; glanceIn = frand(18, 45); }  // and a look at you every so often
        bool glancing = t - glanceAt < 3.0;
        bool interested = t < interestUntil;
        if (talkingToYou || (glancing && !interested)) {
            lookKind = 2;
            lookW = 1;
        } else if (interested && t - ptrMovedAt < 2.5 && ptrD > 90 && ptrD < 1100) {
            lookKind = 1;
            lookX = ptrX - headX;
            lookY = ptrY - headY;
            lookW = clampd(1 - (ptrD - 500) / 600, 0.35, 1);
        } else if (interested && activeWin && std::hypot(activeCx - headX, activeCy - headY) < 1800) {
            lookKind = 1;
            lookX = activeCx - headX;
            lookY = activeCy - headY;
            lookW = 0.9;
        } else {
            lookKind = 0;
            lookW = 0;
        }
        (void)dt;
    }

    void render(bool compositor) {
        if (hidden) return;
        cairo_t* cr = cairo_create(back);
        cairo_set_operator(cr, CAIRO_OPERATOR_CLEAR);
        cairo_paint(cr);
        cairo_set_operator(cr, CAIRO_OPERATOR_OVER);

        art::Pose pose = currentPose();
        art::Eyes eyes = art::Eyes::Open;
        bool working = st == St::Work;
        if (happyLeft > 0 || st == St::Play || (working && shown == Act::Done)) eyes = art::Eyes::Happy;
        if (blink > 0 || (st == St::Eat && (tick / 8) % 2)) eyes = art::Eyes::Closed;
        if (anim.action == art3d::Action::Rouse && anim.actionT < anim.actionLen * art3d::ROUSE_YAWN) eyes = art::Eyes::Closed;
        bool mirror = dir < 0;
        art::Prop prop = art::Prop::Nothing;
        if (working) {
            if (shown == Act::Read || shown == Act::Search) prop = art::Prop::Book;
            if (shown == Act::Write) prop = art::Prop::Laptop;
            if (shown == Act::Run) prop = art::Prop::Terminal;
            if (shown == Act::Web) prop = art::Prop::Globe;
        }
        double t = now();
        if (prop != art::Prop::Nothing) {
            if (deskProp == art::Prop::Nothing) {  // work starts: put the prop down where the pig will face
                deskYaw = mirror ? 225 : 315;
            }
            if (prop != deskProp) art3d::propPlacement(prop, deskYaw, deskX, deskZ, deskPropYaw);
            deskProp = prop;
            deskLeaveAt = t + 18;  // stay at the desk a while after the work stops: it often picks up again
        } else if (deskProp != art::Prop::Nothing && (t >= deskLeaveAt || !grounded() || st == St::Sleep || st == St::Play)) {
            deskProp = art::Prop::Nothing;
        }
        bool atDesk = deskProp != art::Prop::Nothing && (pose == art::Pose::Sit || pose == art::Pose::Stand);
        if (atDesk) prop = deskProp;
        bool closingIn = closeupOn() && closeT < CLOSE_IN, backingOff = closeupOn() && closeT >= CLOSE_IN + CLOSE_STARE;
        if (closeupOn()) {  // walks up to you, stands and stares, walks back into the screen
            pose = closingIn || backingOff ? art::Pose::WalkA : art::Pose::Stand;
            prop = art::Prop::Nothing;
            atDesk = false;
        }
        // Turn around smoothly: the pig swings through "facing you" on the way. At the desk it turns to the prop.
        bool faceYou = (bubbleLeft > 0 || happyLeft > 0) && !atDesk && (pose == art::Pose::Stand || pose == art::Pose::Sit);
        double targetYaw = closeupOn() ? (backingOff ? 270 : 90) : faceYou ? 90 : atDesk ? deskYaw : (mirror ? 150 : 30);
        double turn = std::fmod(targetYaw - yaw + 540, 360) - 180;  // shortest way round
        yaw = std::fmod(yaw + turn * 0.16 + 360, 360);
        art3d::Frame fr;
        fr.pose = pose; fr.eyes = eyes; fr.prop = prop; fr.t = tick / 30.0; fr.yaw = yaw; fr.frame = (int)(tick / 6);
        fr.speed = closeupOn() && pose == art::Pose::WalkA ? 1.6 : std::fabs(vx); fr.vy = vy; fr.thrown = thrown && st == St::Air;
        fr.approach = approach;
        fr.tint = dayTint();
        fr.talking = bubbleLeft > 0; fr.happy = happyLeft > 0; fr.hungry = s.food < 25 && !working;
        fr.progress = st == St::Eat ? 1 - clampd(stLeft / 4, 0, 1) : 0;
        fr.lookX = lookX; fr.lookY = lookY; fr.lookW = lookW; fr.lookKind = lookKind;
        fr.propX = deskX; fr.propZ = deskZ; fr.propYaw = deskPropYaw;
        fr.costume = costume;
        art3d::Rig rig = anim.update(fr);
        // In the air the pig spins about its middle: draw it a bit higher so nothing leaves the window.
        airLift += ((st == St::Air || st == St::Drag ? 30.0 : 0.0) - airLift) * 0.25;
        art3d::HeadPos hp = art3d::render(cr, fr, rig, SPR_X + SPR_W / 2.0, SPR_Y + SPR_H - 1 - GROUND_MARGIN - 14 * approach - airLift);

        if (bubX < 0) { bubX = hp.cx; bubY = hp.top; }
        bubX += (hp.cx - bubX) * 0.25;
        bubY += (hp.top - bubY) * 0.25;
        double headCx = std::round(bubX);
        double headTop = std::round(bubY);

        for (auto& p : particles) {
            double a = clampd(p.life, 0, 1);
            if (p.kind == 0) {
                art::blitPart(cr, art::HEART, headCx + p.x, headTop + p.y, 2, a);
            } else {
                cairo_select_font_face(cr, "Sans", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_BOLD);
                cairo_set_font_size(cr, 11 + (2.5 - p.life) * 4);
                cairo_set_source_rgba(cr, 0.36, 0.48, 0.84, a);
                cairo_move_to(cr, headCx + (mirror ? -p.x : p.x), headTop + p.y);
                cairo_show_text(cr, "z");
            }
        }
        // Only urgent questions (or the one you're answering right now) get a bubble; quiet ones
        // show as a small dot and wait in the right-click menu.
        bool showAsk = !asks.empty() && asks.front().urgent && !askMenu;
        if (bubbleLeft > 0) drawBubble(cr, headCx, headTop, bubble, bubbleStats ? Bub::Stats : Bub::Plain);
        else if (showAsk) drawBubble(cr, headCx, headTop, asks.front().text, Bub::Ask);
        else if (working && !workText().empty()) drawBubble(cr, headCx, headTop, workText(), Bub::Plain);
        if (!asks.empty() && !showAsk) {
            cairo_arc(cr, headCx + (mirror ? -13 : 13), headTop - 9, 4, 0, 2 * M_PI);
            cairo_set_source_rgb(cr, 1.0, 0.86, 0.38);
            cairo_fill_preserve(cr);
            cairo_set_source_rgb(cr, 0.16, 0.12, 0.10);
            cairo_set_line_width(cr, 1.5);
            cairo_stroke(cr);
        }
        cairo_destroy(cr);
        cairo_surface_flush(back);

        cairo_t* cw = cairo_create(winSurf);
        cairo_set_operator(cw, CAIRO_OPERATOR_SOURCE);
        cairo_set_source_surface(cw, back, 0, 0);
        cairo_paint(cw);
        cairo_destroy(cw);
        cairo_surface_flush(winSurf);
        updateShape(compositor);
    }

    bool menuOwns(Window w) const { return menu.owns(w); }
    bool quitRequested = false;

private:
    Display* dpy;
    Desktop& D;
    Stats& s;
    Menu menu;

    Window win = 0;
    Visual* visual = nullptr;
    cairo_surface_t* winSurf = nullptr;
    cairo_surface_t* back = nullptr;
    std::vector<char> lastMask;
    bool lastCompositor = false, hidden = false;
    int winX = INT32_MIN, winY = INT32_MIN;

    // physics
    double px = 0, py = 0, vx = 0, vy = 0, lastPx = 0, lastPy = 0;
    int dir = 1;
    double yaw = 30;  // where the pig is facing, smoothed each frame
    art3d::Animator anim;  // the smoothed rig: poses ease into each other, actions layer on top
    const art3d::Costume* costume = wardrobe::today();  // what it's wearing (wardrobe.hpp: by the calendar)
    bool costumeForced = false;                         // set over the socket ("wear"); the calendar waits
    double lookX = 0, lookY = 0, lookW = 0, prevHappy = 0, idleActionIn = 4, airLift = 0;
    int lookKind = 0;  // 0 nothing, 1 a point on the screen, 2 you
    // attention: what it looks at
    int ptrX = 0, ptrY = 0;
    double ptrMovedAt = 0, winActivityAt = 0, glanceAt = 0, glanceIn = 20;
    Window activeWin = 0;
    std::string activeTitle;
    int activeCx = 0, activeCy = 0;
    double interestUntil = 0, nextInterestAt = 0;  // it only looks sometimes: a few seconds, then loses interest
    Window visitWin = 0;  // a window it is on its way to (to sit on the one you're working in)
    double placedAt = 0, stayUntil = 0;   // put down by hand: it stays there a while
    double bubX = -1, bubY = -1;           // where the bubble hangs: eased toward the head so it doesn't jiggle
    double previewUntil = 0, nextBlink = 3, snuffleAt = 0;
    double winFocusAt = 0, lastVisit = 0;
    // the close-up: ignored while it needs you, it comes up to the glass and stares, then goes back
    double closeT = -1, attentionSince = 0, lastCloseup = 0, approach = 0;
    static constexpr double CLOSE_IN = 2.0, CLOSE_STARE = 4.5, CLOSE_OUT = 2.0;
    // the desk: the prop stands still in the world and the pig turns to it; it lingers there after the work stops
    art::Prop deskProp = art::Prop::Nothing;
    double deskYaw = 315, deskX = 0, deskZ = 0, deskPropYaw = 0, deskLeaveAt = 0, workStoppedAt = 0;
    bool thrown = false;
    Window plat = 0;
    int platX = 0, platY = 0;

    // behaviour
    St st = St::Air;
    double stLeft = 0, playLeft = 0, awayLeft = 0, jumpCooldown = 0;
    bool edgeDecided = false, walkOff = false;
    bool eatPending = false;

    // input
    bool pressed = false, dragging = false;
    double pressX = 0, pressY = 0, dragOffX = 0, dragOffY = 0, dragVx = 0, dragVy = 0;
    std::deque<double> petTimes;

    // looks
    long tick = 0;
    double blink = 0, happyLeft = 0, bubbleLeft = 0, chatLeft = 120;
    std::string bubble;
    bool bubbleStats = false;
    std::vector<Particle> particles;

    // questions from the brain, oldest first
    std::vector<Ask> asks;
    double askHop = 0;
    bool askMenu = false;  // the open menu is answering asks[askIndex], not the normal one
    size_t askIndex = 0;   // which question the panel shows; Next/Previous move it
    int menuX = 0, menuY = 0;

    // what we're acting out
    std::map<std::string, Session> sessions;
    Act shown = Act::Off;
    std::string shownSession, shownDetail;
    int shownCount = 0;
    double shownSince = 0;

    // Pick which session to show, with a minimum time on screen so bursts of quick tool calls
    // don't flicker. Anything that goes quiet for long enough falls back to normal pet life.
    void updateActivity() {
        double t = now();
        for (auto it = sessions.begin(); it != sessions.end();) {
            Session& se = it->second;
            double age = t - se.actAt;
            if (se.act == Act::Done && age > 3) se.act = Act::Off;
            if (se.act == Act::Alert && age > 180) se.act = Act::Off;  // nobody came; stop flagging it
            if (se.busy && t - se.last > 600) { se.busy = false; se.act = Act::Off; }  // hook missed / crashed
            if (!se.busy && se.act != Act::Alert && se.act != Act::Done) se.act = Act::Off;
            if (se.act == Act::Off && t - se.last > 3600) it = sessions.erase(it);
            else ++it;
        }
        const Session* best = nullptr;
        std::string bestId;
        for (auto& [id, se] : sessions) {
            if (se.act == Act::Off) continue;
            bool better = !best || (se.act == Act::Alert) > (best->act == Act::Alert) ||
                          ((se.act == Act::Alert) == (best->act == Act::Alert) && se.actAt > best->actAt);
            if (better) { best = &se; bestId = id; }
        }
        Act target = best ? best->act : Act::Off;
        bool urgent = target == Act::Alert || target == Act::Done || shown == Act::Off || target == shown;
        if (!urgent && t - shownSince < 1.5) return;
        if (target != shown) {
            shownSince = t;
            if (target == Act::Done) { happyLeft = 2; spawn(0); spawn(0); }
            if (target == Act::Alert) chatLeft = frand(90, 240);
        }
        shown = target;
        shownSession = bestId;
        shownDetail = best ? best->detail : "";
        shownCount = best ? best->count : 0;
    }

    std::string workText() const {
        bool d = g_showDetails && !shownDetail.empty();
        std::string n = std::to_string(shownCount);
        switch (shown) {
            case Act::Think: return "";  // between a prompt and a tool call: nothing to say, the pose says busy
            case Act::Read: return shownCount > 1 ? "reading " + n + " files" : d ? "reading " + shownDetail : "reading";
            case Act::Search: return "searching...";
            case Act::Write: return d ? "editing " + shownDetail : "editing";
            case Act::Run: return d ? shownDetail : "running a command";
            case Act::Web: return d ? "browsing " + shownDetail : "browsing the web";
            case Act::Agent: return d ? "helper: " + shownDetail : "sent a helper";
            case Act::Plan: return "making a plan";
            case Act::Tool: return d ? "using " + shownDetail : "using a tool";
            case Act::Alert: {  // a Claude Code session is stuck on a prompt (never Mochi's own questions)
                if (shownDetail.find("permission") != std::string::npos) return "claude wants a permission";
                return d ? "claude: " + shownDetail.substr(0, 40) : "claude needs you";
            }
            case Act::Done: return "done!";
            case Act::Compact: return "tidying up memory...";
            default: return "";
        }
    }

    void createWindow() {
        int scr = DefaultScreen(dpy);
        XVisualInfo vi;
        bool argb = XMatchVisualInfo(dpy, scr, 32, TrueColor, &vi);
        visual = argb ? vi.visual : DefaultVisual(dpy, scr);
        XSetWindowAttributes swa{};
        swa.override_redirect = True;
        swa.colormap = argb ? XCreateColormap(dpy, DefaultRootWindow(dpy), visual, AllocNone)
                            : DefaultColormap(dpy, scr);
        swa.border_pixel = 0;
        swa.background_pixel = 0;
        swa.event_mask = ButtonPressMask | ButtonReleaseMask | ButtonMotionMask;
        win = XCreateWindow(dpy, DefaultRootWindow(dpy), -WIN_W, -WIN_H, WIN_W, WIN_H, 0,
                            argb ? 32 : DefaultDepth(dpy, scr), InputOutput, visual,
                            CWOverrideRedirect | CWColormap | CWBorderPixel | CWBackPixel | CWEventMask, &swa);
        XClassHint ch{(char*)"xpet", (char*)"xpet"};
        XSetClassHint(dpy, win, &ch);
        XStoreName(dpy, win, "xpet");
        // Ask compositors not to draw a shadow behind us.
        Atom type = XInternAtom(dpy, "_NET_WM_WINDOW_TYPE", False);
        Atom dnd = XInternAtom(dpy, "_NET_WM_WINDOW_TYPE_DND", False);
        XChangeProperty(dpy, win, type, XA_ATOM, 32, PropModeReplace, (unsigned char*)&dnd, 1);

        winSurf = cairo_xlib_surface_create(dpy, win, visual, WIN_W, WIN_H);
        back = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, WIN_W, WIN_H);
        XMapRaised(dpy, win);
    }

    // Clicks only land on the drawn pixels; without a compositor the window is also cut to that shape.
    void updateShape(bool compositor) {
        unsigned char* data = cairo_image_surface_get_data(back);
        int stride = cairo_image_surface_get_stride(back);
        int bpl = (WIN_W + 7) / 8;
        std::vector<char> bits(bpl * WIN_H, 0);
        for (int y = 0; y < WIN_H; ++y)
            for (int x = 0; x < WIN_W; ++x) {
                uint32_t p = *(uint32_t*)(data + y * stride + x * 4);
                if ((p >> 24) > 40) bits[y * bpl + x / 8] |= (char)(1 << (x % 8));
            }
        if (bits == lastMask && compositor == lastCompositor) return;
        Pixmap pm = XCreateBitmapFromData(dpy, win, bits.data(), WIN_W, WIN_H);
        XShapeCombineMask(dpy, win, ShapeInput, 0, 0, pm, ShapeSet);
        XShapeCombineMask(dpy, win, ShapeBounding, 0, 0, compositor ? None : pm, ShapeSet);
        XFreePixmap(dpy, pm);
        lastMask.swap(bits);
        lastCompositor = compositor;
    }

    void raiseAll() {
        if (hidden) return;
        XRaiseWindow(dpy, win);
        menu.raise();
    }

    void pointer(int& x, int& y) {
        Window r, c;
        int wx, wy;
        unsigned mask;
        x = y = 0;
        XQueryPointer(dpy, DefaultRootWindow(dpy), &r, &c, &x, &y, &wx, &wy, &mask);
    }

    // ---- stats & mood

    void updateStats(double dt) {
        if (st == St::Work) return;  // it's busy being Claude; don't let it starve
        bool asleep = st == St::Sleep;
        s.food -= FOOD_DECAY * dt;
        s.fun -= FUN_DECAY * dt * (s.food < 25 ? 2 : 1) * (asleep ? 0.5 : 1);
        s.energy += asleep ? ENERGY_GAIN * dt : -ENERGY_DECAY * dt;
        if (st == St::Play) { s.energy -= 0.6 * dt; s.fun += 1.5 * dt; s.food -= 0.3 * dt; }
        s.clamp();
    }

    void chatter() {  // a rare little oink, nothing about its needs: it handles those itself
        chatLeft = frand(600, 1500);
        if (st == St::Sleep || st == St::Air || st == St::Drag || st == St::Work) return;
        say(pick(lines::happy), 2);
    }

    void say(const std::string& t, double secs = 3.5) {
        bubble = t;
        bubbleStats = false;
        bubbleLeft = secs;
    }

    void spawn(int kind) {
        if (kind == 0) particles.push_back({frand(-14, 6), -6, frand(-0.3, 0.3), -0.8, 1.4, 0});
        else particles.push_back({8, 0, 0.35, -0.5, 2.5, 1});
    }

    // ---- interactions

    // Light by the clock: plain by day, warm in the evening, cool and dim at night. Blends over the hour.
    art3d::RGB dayTint() const {
        time_t tt = time(nullptr);
        tm lt{};
        localtime_r(&tt, &lt);
        double h = lt.tm_hour + lt.tm_min / 60.0;
        auto mix = [](art3d::RGB a, art3d::RGB b, double k) { return art3d::RGB{a.r + (b.r - a.r) * k, a.g + (b.g - a.g) * k, a.b + (b.b - a.b) * k}; };
        const art3d::RGB day{1, 1, 1}, dusk{1.0, 0.93, 0.84}, night{0.80, 0.84, 0.98};
        if (h < 6) return night;
        if (h < 8) return mix(night, day, (h - 6) / 2);
        if (h < 18) return day;
        if (h < 20) return mix(day, dusk, (h - 18) / 2);
        if (h < 23) return mix(dusk, night, (h - 20) / 3);
        return night;
    }

    // One line about what it's up to (middle click).
    std::string statusLine() const {
        std::string where;
        if (plat && plat != FLOOR) {
            auto it = D.rects.find(plat);
            if (it != D.rects.end()) {
                std::string t = const_cast<Desktop&>(D).title(plat);
                if (t.size() > 28) t = t.substr(0, 27) + "\u2026";
                where = t.empty() ? "" : " on " + t;
            }
        }
        std::string doing;
        switch (st) {
            case St::Sleep: doing = "napping"; break;
            case St::Eat: doing = "eating"; break;
            case St::Play: doing = visitWin ? "coming over" : "playing"; break;
            case St::Work: doing = shown == Act::Off ? "waiting at my desk" : "working: " + workText(); break;
            case St::Walk: doing = "wandering"; break;
            case St::Air: doing = "falling!"; break;
            case St::Drag: doing = "being held"; break;
            default: doing = closeupOn() ? "waiting for you" : "chilling"; break;
        }
        std::string q = asks.empty() ? "" : ", " + std::to_string(asks.size()) + (asks.size() == 1 ? " question" : " questions") + " for you";
        return doing + where + q;
    }

    void onPet() {
        double t = now();
        petTimes.push_back(t);
        while (!petTimes.empty() && t - petTimes.front() > 10) petTimes.pop_front();
        if (st == St::Sleep) {
            anim.start(art3d::Action::HeadShake, 0.9);
            say(pick(lines::grumpy));
            s.fun -= 2;
            set(St::Idle, 2);
            return;
        }
        if (petTimes.size() > 8) {
            anim.start(art3d::Action::HeadShake, 0.8);
            say(pick(lines::tooMuch));
            s.fun -= 1;
            return;
        }
        s.fun += 4;
        s.clamp();
        happyLeft = 1.5;
        anim.start(chance(0.7) ? art3d::Action::Nuzzle : art3d::Action::Wiggle, 1.3);
        spawn(0);
        if (chance(0.35)) say(pick(lines::pet), 2);
    }

    // Where the pig and its bubble are, in root coordinates: the panel keeps out of this.
    Avoid pigRect() const { return {(int)px - 70, (int)py - 150, 140, 152}; }

    void openMenu(int x, int y) {
        menuX = x;
        menuY = y;
        std::vector<std::string> items;
        if (!asks.empty())
            items.push_back(asks.size() == 1 ? "1 question for you" : std::to_string(asks.size()) + " questions for you");
        for (const char* s : {"Give me a task...", "Open a chat", "Shoo for 10 min", "Quit"})
            items.push_back(s);
        menu.show(items, D.monitorNear(px, py - 60), pigRect());
    }

    // The question and its answers in one panel. With several waiting you can flip between them (Next /
    // Previous, or the mouse wheel over the panel); answering one moves on to the next.
    void openAskMenu(int, int) {
        if (asks.empty()) return;
        askMenu = true;
        if (askIndex >= asks.size()) askIndex = 0;
        const Ask& a = asks[askIndex];
        std::vector<std::string> items;
        std::stringstream ss(a.options);
        for (std::string item; std::getline(ss, item, '|');)
            if (!item.empty()) items.push_back(item);
        if (items.empty()) items = {"Ok"};
        if (asks.size() > 1) {
            items.push_back(Menu::NEXT);
            items.push_back(Menu::PREV);
        }
        items.push_back("Later");
        std::string title = "Mochi asks";
        if (asks.size() > 1) title += "  (" + std::to_string(askIndex + 1) + " of " + std::to_string(asks.size()) + ")";
        menu.show(items, D.monitorNear(px, py - 60), pigRect(), title, a.text);
    }

    // The chosen label goes back to the brain as-is; what it means is the brain's business.
    void answerAsk(const std::string& what) {
        if (asks.empty()) return;
        if (what == "Later") return;  // keeps waiting behind the dot
        if (what == Menu::NEXT || what == Menu::PREV) {
            size_t n = asks.size();
            askIndex = (askIndex + (what == Menu::NEXT ? 1 : n - 1)) % n;
            openAskMenu(0, 0);
            return;
        }
        if (askIndex >= asks.size()) askIndex = 0;
        Ask a = asks[askIndex];
        asks.erase(asks.begin() + askIndex);
        if (!ipc::send(json::object({{"event", "answer"}, {"id", a.id}, {"answer", what}}), ipc::brainPath()))
            say("my brain isn't running", 3);
        else if (what.rfind("Yes", 0) == 0) { happyLeft = 1.5; spawn(0); }
        if (!asks.empty()) openAskMenu(0, 0);  // the next one, in the same place
    }

    bool grounded() const { return st != St::Air && st != St::Drag && st != St::Away; }

    void act(const std::string& what) {
        if (what == "Quit") { quitRequested = true; return; }
        if (what.find("question") != std::string::npos) {
            if (!asks.empty()) openAskMenu(menuX, menuY);
            return;
        }
        if (what == "Open a chat") {
            if (ipc::send(json::object({{"event", "chat"}}), ipc::brainPath())) say("let's talk", 3);
            else say("my brain isn't running", 3);
            return;
        }
        if (what == "Give me a task...") {
            if (ipc::send(json::object({{"event", "ask"}}), ipc::brainPath())) say("what's up?", 3);
            else say("my brain isn't running", 3);
            return;
        }
        if (what == "Shoo for 10 min") {
            say("fine.");
            awayLeft = 600;
            set(St::Away);
            return;
        }
    }

    void startEat() {
        eatPending = false;
        set(St::Eat, 4);
    }

    void goSleep() {
        say("*yawn*", 2);
        set(St::Sleep, 20 * 60);
    }

    // Waking up on its own, rested: it gets up with a yawn, a stretch and a shake rather than just standing up.
    // (Woken by you it only grumbles: that's in onPet and the socket.)
    void wakeUp() {
        say("mrrp", 2);
        anim.start(art3d::Action::Rouse, 3.0);
        set(St::Idle, 3.2);
    }

    // ---- state machine

    void set(St n, double secs = 0) {
        if (n != St::Walk) edgeDecided = walkOff = false;
        if (n == St::Air || n == St::Drag || n == St::Away) plat = 0;
        st = n;
        stLeft = secs;
    }

    // It looks after itself: eats when hungry, naps when tired, and now and then chases your pointer for fun.
    // No menu for any of that, and no nagging.
    void decide() {
        if (eatPending || s.food < 30) { startEat(); return; }
        if (s.energy < 18) { goSleep(); return; }
        if (s.fun < 45 && s.energy > 30 && now() - ptrMovedAt < 6 && chance(0.3)) {
            playLeft = frand(6, 12);
            set(St::Play);
            return;
        }
        if (now() < stayUntil) { set(chance(0.5) ? St::Sit : St::Idle, frand(4, 10)); return; }  // stays where you put it
        double r = frand();
        if (r < 0.40) startWalk(frand(2, 7));
        else if (r < 0.58) set(St::Sit, frand(4, 12));
        else if (r < 0.72) set(St::Idle, frand(1.5, 4));
        else if (r < 0.88) { if (!tryJump(220, 260, NAN)) startWalk(frand(2, 5)); }
        else if (s.energy < 60) goSleep();
        else set(St::Sit, frand(3, 8));
    }

    void startWalk(double secs) {
        if (chance(0.5)) dir = -dir;
        set(St::Walk, secs);
    }

    // Leap to a higher ledge. preferX steers towards a spot (NaN = anywhere).
    bool tryJump(double maxUp, double reach, double preferX) {
        std::vector<const Seg*> cand;
        for (auto& sg : D.segs) {
            if (sg.y >= py - 30 || sg.y < py - maxUp) continue;
            if (sg.x2 < px - reach || sg.x1 > px + reach || sg.x2 - sg.x1 < 50) continue;
            cand.push_back(&sg);
        }
        if (cand.empty()) return false;
        const Seg* t = cand[std::uniform_int_distribution<size_t>(0, cand.size() - 1)(rng)];
        if (!std::isnan(preferX)) {
            double best = 1e18;
            for (auto* c : cand) {
                double d = std::fabs(clampd(preferX, c->x1, c->x2) - preferX) + std::fabs(c->y - py) * 0.2;
                if (d < best) { best = d; t = c; }
            }
        }
        double aim = std::isnan(preferX) ? px + frand(-reach, reach) : preferX;
        double tx = clampd(clampd(aim, t->x1 + 20, t->x2 - 20), px - reach, px + reach);
        double h = py - t->y + 30;
        double vy0 = -std::sqrt(2 * GRAVITY * h);
        double dy = t->y - py;
        double time = (-vy0 + std::sqrt(vy0 * vy0 + 2 * GRAVITY * dy)) / GRAVITY;
        vx = (tx - px) / std::max(time, 1.0);
        vy = vy0;
        dir = vx >= 0 ? 1 : -1;
        anim.start(art3d::Action::Jump, 0.45);
        set(St::Air);
        return true;
    }

    void land(const Seg& sg) {
        py = sg.y;
        plat = sg.id;
        if (plat != FLOOR) {
            auto& r = D.rects[plat];
            platX = r.x;
            platY = r.y;
        }
        bool hard = vy > 20;
        if (now() - placedAt < 3) stayUntil = now() + 90;  // you put it here: it stays a while
        anim.start(art3d::Action::Land, hard ? 0.8 : 0.55);  // long enough for the settle after the squash
        thrown = false;
        vx = vy = 0;
        if (playLeft > 0 && (visitWin || st == St::Play || st == St::Air)) { set(St::Play); return; }
        if (hard) { say(pick(lines::oof), 2); s.fun -= 1; }
        set(St::Idle, hard ? 1.5 : frand(0.5, 1.5));
    }

    void airStep() {
        vy = std::min(vy + GRAVITY, MAX_FALL);
        if (std::fabs(vx) > 1) dir = vx > 0 ? 1 : -1;
        double nx = px + vx, ny = py + vy;
        if (!D.inMonitors(nx, py - 10) && D.inMonitors(px, py - 10)) {
            vx = -vx * 0.5;  // bounce off screen edges
            nx = px;
        }
        if (vy > 0) {
            const Seg* best = nullptr;
            for (auto& sg : D.segs)
                if (sg.y >= py && sg.y <= ny && nx >= sg.x1 && nx <= sg.x2 && (!best || sg.y < best->y)) best = &sg;
            if (best) { px = nx; land(*best); return; }
        }
        px = nx;
        py = ny;
        vx *= 0.995;
        if (py > D.lowestFloor() + 200) rescue();
    }

    // Fell somewhere with no floor (gap between monitors): put it back on solid ground.
    void rescue() {
        const Rect& m = D.monitorNear(px, D.lowestFloor() - 1);
        px = clampd(px, m.x + 40, m.x + m.w - 40);
        py = m.y;
        vx = vy = 0;
    }

    void groundStep(double dt) {
        // Ride along with the window we're standing on.
        if (plat != FLOOR) {
            auto it = D.rects.find(plat);
            if (it == D.rects.end()) { fall(); return; }
            px += it->second.x - platX;
            py += it->second.y - platY;
            platX = it->second.x;
            platY = it->second.y;
        }
        const Seg* sg = D.segAt(plat, px, py);
        if (!sg) { fall(); return; }
        if (menu.open || closeupOn()) return;  // a panel is up, or it's at the glass: no wandering off

        jumpCooldown -= dt;
        bool busy = shown != Act::Off;
        if (busy && st != St::Work && st != St::Play && st != St::Eat) set(St::Work);
        if (busy) workStoppedAt = 0;
        if (!busy && st == St::Work) {
            if (!workStoppedAt) workStoppedAt = now();
            if (now() - workStoppedAt > 18) set(St::Idle, frand(1, 2));  // it waited; nothing more came
        }
        if (st == St::Work) return;
        if (st == St::Play) playStep(dt, *sg);
        else if (st == St::Walk) walkStep(*sg, 1.5, false);
        if (st == St::Air) return;

        if (playLeft <= 0 && st == St::Sleep && s.energy >= 100) wakeUp();
        if (st != St::Play && (stLeft -= dt) <= 0) {
            if (st == St::Eat) {
                s.food += 35;
                s.fun += 5;
                s.clamp();
                happyLeft = 1.5;
                spawn(0);
            }
            if (st == St::Sleep) { s.clamp(); wakeUp(); }
            else decide();
        }
        // Walking may have carried us off the ledge.
        if (grounded() && !D.segAt(plat, px, py)) fall();
    }

    void fall() {
        vy = 0;
        if (st != St::Walk && st != St::Play) vx = 0;
        set(St::Air);
    }

    void walkStep(const Seg& sg, double speed, bool allowOff) {
        double nx = px + dir * speed;
        // Blocked: turn round and stand. vx must go to zero here, or a pig blocked on both sides (chasing the
        // pointer at a screen edge, a ledge shorter than its margins) keeps the walk pose and runs on the spot.
        if (!D.inMonitors(nx + dir * 20, py - 10)) { dir = -dir; vx = 0; return; }
        double margin = sg.id == FLOOR ? 20 : 4;
        bool pastEdge = dir > 0 ? nx > sg.x2 - margin : nx < sg.x1 + margin;
        if (pastEdge && sg.id != FLOOR) {
            if (!edgeDecided) { edgeDecided = true; walkOff = allowOff || chance(0.3); }
        }
        if (pastEdge && !(walkOff && sg.id != FLOOR)) {
            dir = -dir;
            edgeDecided = false;
            vx = 0;
            return;
        }
        px = nx;
        vx = dir * speed;
    }

    void playStep(double dt, const Seg& sg) {
        playLeft -= dt;
        bool visiting = visitWin != 0;
        if (visiting && (plat == visitWin || !D.rects.count(visitWin))) {  // arrived (or the window went away)
            visitWin = 0;
            set(St::Sit, frand(15, 40));
            return;
        }
        if (playLeft <= 0) {
            visitWin = 0;
            if (!visiting) happyLeft = 2;
            set(St::Sit, 4);
            return;
        }
        int rx, ry;
        if (visiting) {
            const Rect& r = D.rects[visitWin];
            rx = r.x + r.w / 2;
            ry = r.y;
        } else pointer(rx, ry);
        if (ry < py - 70 && std::fabs(rx - px) < 260 && jumpCooldown <= 0) {
            jumpCooldown = 1.2;
            if (tryJump(visiting ? 600 : 300, 260, rx)) return;
        }
        if (std::fabs(rx - px) > 12) {
            dir = rx > px ? 1 : -1;
            walkStep(sg, visiting ? 2.2 : 3.8, ry > py);  // chase off ledges if the target is below
        }
    }

    art::Pose currentPose() const {
        switch (st) {
            case St::Air: return art::Pose::Fall;
            case St::Drag: return art::Pose::Dangle;
            case St::Sit: return art::Pose::Sit;
            case St::Sleep: return art::Pose::Sleep;
            case St::Eat: return art::Pose::Eat;
            case St::Work: return shown == Act::Alert ? art::Pose::Stand : art::Pose::Sit;
            case St::Walk:
            case St::Play:
                return std::fabs(vx) < 0.1 ? art::Pose::Stand : art::Pose::WalkA;
            default: return art::Pose::Stand;
        }
    }

    void roundedRect(cairo_t* cr, double x, double y, double w, double h, double r) {
        cairo_new_sub_path(cr);
        cairo_arc(cr, x + w - r, y + r, r, -M_PI / 2, 0);
        cairo_arc(cr, x + w - r, y + h - r, r, 0, M_PI / 2);
        cairo_arc(cr, x + r, y + h - r, r, M_PI / 2, M_PI);
        cairo_arc(cr, x + r, y + r, r, M_PI, 3 * M_PI / 2);
        cairo_close_path(cr);
    }

    // Greedy word wrap; the last line gets an ellipsis if there was more.
    static std::vector<std::string> wrapText(cairo_t* cr, const std::string& text, double maxW, int maxLines) {
        return Menu::wrap(cr, text, maxW, maxLines);
    }

    void drawBubble(cairo_t* cr, double cx, double headTop, std::string text, Bub kind) {
        cairo_select_font_face(cr, "Sans", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_BOLD);
        cairo_set_font_size(cr, 12);
        double bw, bh;
        cairo_text_extents_t ext;
        std::vector<std::string> lines;
        if (kind == Bub::Stats) {
            bw = 170;
            bh = 82;
        } else if (kind == Bub::Ask) {
            lines = wrapText(cr, text, WIN_W - 40, 3);
            double widest = 0;
            for (auto& l : lines) {
                cairo_text_extents(cr, l.c_str(), &ext);
                widest = std::max(widest, ext.x_advance);
            }
            bw = std::min<double>(std::max(widest + 20, 96.0), WIN_W - 4);
            bh = 10 + 15 * (int)lines.size() + 14;  // text lines + the "click me" hint
        } else {
            lines = wrapText(cr, text, WIN_W - 40, 3);
            double widest = 0;
            for (auto& l : lines) {
                cairo_text_extents(cr, l.c_str(), &ext);
                widest = std::max(widest, ext.x_advance);
            }
            bw = std::min<double>(widest + 18, WIN_W - 4);
            bh = 9 + 15 * (int)std::max<size_t>(1, lines.size());
        }
        double bx = clampd(cx - bw / 2, 2, WIN_W - bw - 2);
        double by = std::max(2.0, headTop - bh - 12);

        roundedRect(cr, bx, by, bw, bh, 7);
        cairo_move_to(cr, cx - 6, by + bh - 1);
        cairo_line_to(cr, cx, by + bh + 8);
        cairo_line_to(cr, cx + 6, by + bh - 1);
        // Questions get a warmer, yellow bubble so they stand out from chatter.
        double fr = 1.0, fg = 0.98, fb = 0.94;
        if (kind == Bub::Ask) { fg = 0.93; fb = 0.72; }
        cairo_set_source_rgb(cr, fr, fg, fb);
        cairo_fill_preserve(cr);
        cairo_set_source_rgb(cr, 0.16, 0.12, 0.10);
        cairo_set_line_width(cr, 2);
        cairo_stroke(cr);
        // Cover the stroke where the tail meets the bubble.
        cairo_set_source_rgb(cr, fr, fg, fb);
        cairo_rectangle(cr, cx - 5, by + bh - 2, 10, 3);
        cairo_fill(cr);

        cairo_set_source_rgb(cr, 0.16, 0.12, 0.10);
        if (kind == Bub::Plain) {
            for (size_t i = 0; i < lines.size(); ++i) {
                cairo_move_to(cr, bx + 9, by + 16 + 15 * i);
                cairo_show_text(cr, lines[i].c_str());
            }
            return;
        }
        if (kind == Bub::Ask) {
            for (size_t i = 0; i < lines.size(); ++i) {
                cairo_move_to(cr, bx + 10, by + 17 + 15 * i);
                cairo_show_text(cr, lines[i].c_str());
            }
            cairo_select_font_face(cr, "Sans", CAIRO_FONT_SLANT_ITALIC, CAIRO_FONT_WEIGHT_NORMAL);
            cairo_set_font_size(cr, 10);
            cairo_set_source_rgb(cr, 0.45, 0.38, 0.30);
            cairo_move_to(cr, bx + 10, by + bh - 6);
            std::string more = asks.size() > 1 ? " (+" + std::to_string(asks.size() - 1) + " more)" : "";
            cairo_show_text(cr, ((askMenu ? "pick one" : "click me to answer") + more).c_str());
            return;
        }
        cairo_move_to(cr, bx + 10, by + 17);
        cairo_show_text(cr, s.name.c_str());
        cairo_set_font_size(cr, 11);
        struct { const char* label; double v; } rows[] = {{"Food", s.food}, {"Fun", s.fun}, {"Energy", s.energy}};
        for (int i = 0; i < 3; ++i) {
            double y = by + 25 + i * 18;
            cairo_set_source_rgb(cr, 0.16, 0.12, 0.10);
            cairo_move_to(cr, bx + 10, y + 11);
            cairo_show_text(cr, rows[i].label);
            cairo_rectangle(cr, bx + 62, y + 2, 96, 11);
            cairo_stroke(cr);
            double v = rows[i].v / 100;
            if (v < 0.25) cairo_set_source_rgb(cr, 0.91, 0.22, 0.31);
            else if (v < 0.5) cairo_set_source_rgb(cr, 0.95, 0.65, 0.35);
            else cairo_set_source_rgb(cr, 0.40, 0.72, 0.42);
            cairo_rectangle(cr, bx + 63, y + 3, 94 * v, 9);
            cairo_fill(cr);
        }
    }
};

// ---- misc ------------------------------------------------------------------------

void writeSheet(const char* path) {
    using namespace art;
    using art3d::Action;
    double zoom = getenv("XPET_SHEET_ZOOM") ? atof(getenv("XPET_SHEET_ZOOM")) : 1.6;
    const int cellW = (int)(100 * zoom), cellH = (int)(78 * zoom);
    Pose poses[] = {Pose::Stand, Pose::WalkA, Pose::WalkA, Pose::Sit, Pose::Sleep, Pose::Dangle, Pose::Fall, Pose::Eat};
    Action actions[] = {Action::Sniff, Action::Stretch, Action::Shake, Action::LookAround, Action::Hop, Action::Nuzzle, Action::Scratch, Action::Land};
    cairo_surface_t* sf = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, cellW * 8, cellH * 5);
    cairo_t* cr = cairo_create(sf);
    cairo_set_source_rgb(cr, 0.82, 0.85, 0.90);
    cairo_paint(cr);
    for (int e = 0; e < 5; ++e)
        for (int i = 0; i < 8; ++i) {
            art3d::Frame f;
            f.pose = e == 4 ? Pose::Stand : poses[i];
            f.eyes = (Eyes)std::min(e, 2);
            if (e < 4 && i == 3) f.prop = Prop::Laptop;
            if (e == 3) f.prop = i == 0 ? Prop::Terminal : i == 1 ? Prop::Globe : i == 3 ? Prop::Book : Prop::Nothing;
            f.t = 0.9 + i * 0.37;
            f.yaw = e == 3 ? 150.0 - i * 15 : e == 2 ? 90.0 : 30.0;
            f.frame = i;
            f.speed = i == 2 ? 3.8 : i == 1 ? 1.5 : 0;
            f.progress = 0.6;
            f.costume = wardrobe::today();
            art3d::renderStill(cr, f, i * cellW + cellW / 2.0, e * cellH + cellH - 14 * zoom, zoom, e == 4 ? actions[i] : Action::Idle, 0.5);
        }
    cairo_surface_write_to_png(sf, path);
    cairo_destroy(cr);
    cairo_surface_destroy(sf);
}

// One action played out frame by frame: eight steps through it, from the side and from the front, in today's
// costume (XPET_COSTUME / XPET_DATE as for the sheet). For looking at a new trick without a screen.
int writeStrip(const std::string& name, const char* path) {
    const art3d::ActionInfo* a = art3d::actionNamed(name);
    if (!a) {
        fprintf(stderr, "xpet: no action called '%s'; there are:", name.c_str());
        for (const auto& x : art3d::ACTIONS) fprintf(stderr, " %s", x.name);
        fputc('\n', stderr);
        return 1;
    }
    double zoom = getenv("XPET_SHEET_ZOOM") ? atof(getenv("XPET_SHEET_ZOOM")) : 1.6;
    const int cellW = (int)(100 * zoom), cellH = (int)(78 * zoom), N = 8;
    cairo_surface_t* sf = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, cellW * N, cellH * 2);
    cairo_t* cr = cairo_create(sf);
    cairo_set_source_rgb(cr, 0.82, 0.85, 0.90);
    cairo_paint(cr);
    for (int row = 0; row < 2; ++row)
        for (int i = 0; i < N; ++i) {
            art3d::Frame f;
            f.yaw = row ? 90 : 30;
            double p = (i + 0.5) / N;
            f.t = 1.0 + p * a->secs;
            f.costume = wardrobe::today();
            art3d::renderStill(cr, f, i * cellW + cellW / 2.0, row * cellH + cellH - 14 * zoom, zoom, a->action, p);
        }
    cairo_surface_write_to_png(sf, path);
    cairo_destroy(cr);
    cairo_surface_destroy(sf);
    return 0;
}

bool compositorRunning(Display* dpy) {
    char name[32];
    snprintf(name, sizeof name, "_NET_WM_CM_S%d", DefaultScreen(dpy));
    return XGetSelectionOwner(dpy, XInternAtom(dpy, name, False)) != None;
}

void usage() {
    puts("usage: xpet [--name NAME] [--reset] [--no-details] [--sheet FILE.png] [--strip ACTION FILE.png]\n"
         "       xpet --send JSON|TEXT     send a message to the running pig\n"
         "       xpet --send-hook          forward a Claude Code hook (JSON on stdin)\n"
         "       xpet --ask [TEXT]         ask the brain (mochi-brain) to do something\n"
         "  left-click: pet (or answer a question)    drag: pick up / throw    right-click: menu");
}

std::string shorten(std::string v, size_t max = 48) {
    for (char& c : v)
        if (c == '\n' || c == '\t') c = ' ';
    if (v.size() > max) {
        v.resize(max);
        while (!v.empty() && ((unsigned char)v.back() & 0xC0) == 0x80) v.pop_back();
        if (!v.empty() && (unsigned char)v.back() >= 0xC0) v.pop_back();
    }
    return v;
}

std::string baseName(const std::string& p) {
    size_t i = p.find_last_of('/');
    return i == std::string::npos ? p : p.substr(i + 1);
}

// Turns Claude Code hook input into a small message. Only short summaries leave this
// function: file names, a command's description, a host name. Never file contents.
std::string hookToMessage(const json::Flat& h) {
    auto get = [&](const std::string& k) {
        auto it = h.find(k);
        return it == h.end() ? std::string() : it->second;
    };
    std::string ev = get("hook_event_name"), tool = get("tool_name"), detail, event, error;
    if (ev == "SessionStart") event = "session_start";
    else if (ev == "UserPromptSubmit") event = "prompt";
    else if (ev == "PreToolUse") {
        event = "tool_start";
        if (tool == "Read" || tool == "Edit" || tool == "Write" || tool == "MultiEdit") detail = baseName(get("tool_input.file_path"));
        else if (tool == "NotebookEdit") detail = baseName(get("tool_input.notebook_path"));
        else if (tool == "Bash") {
            detail = get("tool_input.description");
            if (detail.empty()) {
                std::string cmd = get("tool_input.command");
                detail = "running " + baseName(cmd.substr(0, cmd.find(' ')));
            }
        } else if (tool == "WebFetch") {
            std::string url = get("tool_input.url");
            size_t a = url.find("://");
            a = a == std::string::npos ? 0 : a + 3;
            detail = url.substr(a, url.find_first_of("/:?#", a) - a);
        } else if (tool == "Agent" || tool == "Task") detail = get("tool_input.description");
    } else if (ev == "PostToolUse" || ev == "PostToolUseFailure") {
        event = "tool_end";
        if (ev == "PostToolUseFailure" || get("tool_response.is_error") == "true") error = "1";
    } else if (ev == "Notification") { event = "notify"; detail = get("message"); }
    else if (ev == "Stop") event = "stop";
    else if (ev == "SubagentStop") event = "subagent_stop";
    else if (ev == "SessionEnd") event = "session_end";
    else if (ev == "PreCompact") event = "compact";
    if (event.empty()) return "";
    const char* mochi = getenv("MOCHI_BRAIN");  // set on the runs the brain starts itself
    return json::object({{"src", "claude"},
                         {"event", event},
                         {"session", get("session_id").substr(0, 12)},
                         {"tool", tool},
                         {"detail", shorten(detail)},
                         {"error", error},
                         {"cwd", get("cwd")},
                         {"mochi", mochi ? "1" : ""},
                         {"pid", std::to_string(getppid())}});
}

// Hooks must never slow Claude down or print anything (stdout of some hooks becomes context).
// The same message goes to the pig (to act out) and to the brain (to keep track of sessions).
int sendHook() {
    std::string in;
    char buf[65536];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, stdin)) > 0 && in.size() < (8u << 20)) in.append(buf, n);
    json::Flat h;
    if (json::parse(in, h)) {
        std::string msg = hookToMessage(h);
        if (!msg.empty()) {
            ipc::send(msg);
            ipc::send(msg, ipc::brainPath());
        }
    }
    return 0;
}

int sendCommand(const std::string& arg) {
    json::Flat f;
    std::string msg = json::parse(arg, f) ? arg : json::object({{"event", "say"}, {"text", arg}});
    if (ipc::send(msg)) return 0;
    fprintf(stderr, "xpet: not running (no socket at %s)\n", ipc::socketPath().c_str());
    return 1;
}

int askBrain(const std::string& text) {
    if (ipc::send(json::object({{"event", "ask"}, {"text", text}}), ipc::brainPath())) return 0;
    fprintf(stderr, "xpet: mochi-brain isn't running (no socket at %s)\n", ipc::brainPath().c_str());
    return 1;
}

}  // namespace

int main(int argc, char** argv) {
    Stats stats;
    std::string newName;
    bool reset = false;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--sheet" && i + 1 < argc) { writeSheet(argv[++i]); return 0; }
        else if (a == "--strip" && i + 2 < argc) return writeStrip(argv[i + 1], argv[i + 2]);
        else if (a == "--send" && i + 1 < argc) return sendCommand(argv[++i]);
        else if (a == "--send-hook") return sendHook();
        else if (a == "--ask") {
            std::string text;
            for (int j = i + 1; j < argc; ++j) text += (j > i + 1 ? " " : "") + std::string(argv[j]);
            return askBrain(text);
        }
        else if (a == "--no-details") g_showDetails = false;
        else if (a == "--name" && i + 1 < argc) newName = argv[++i];
        else if (a == "--reset") reset = true;
        else { usage(); return a == "-h" || a == "--help" ? 0 : 1; }
    }

    mkdir(configDir().c_str(), 0755);
    int lock = open((configDir() + "/lock").c_str(), O_CREAT | O_RDWR, 0644);
    if (lock < 0 || flock(lock, LOCK_EX | LOCK_NB) != 0) {
        fprintf(stderr, "xpet is already running\n");
        return 1;
    }

    double away = reset ? 0 : loadStats(stats);
    if (reset) stats = Stats{};
    if (!newName.empty()) stats.name = newName;
    if (away > 0) {  // time passes while you're gone, but gently
        stats.food -= FOOD_DECAY * away * 0.5;
        stats.fun -= FUN_DECAY * away * 0.3;
        if (away > 1800) stats.energy = 100;
        stats.clamp();
    }

    Display* dpy = XOpenDisplay(nullptr);
    if (!dpy) { fprintf(stderr, "xpet: can't open display\n"); return 1; }
    int shapeEv, shapeErr;
    if (!XShapeQueryExtension(dpy, &shapeEv, &shapeErr)) { fprintf(stderr, "xpet: needs the SHAPE extension\n"); return 1; }
    XSetErrorHandler([](Display*, XErrorEvent*) { return 0; });  // windows vanish under us all the time
    g_stats = &stats;
    XSetIOErrorHandler([](Display*) -> int {  // X server gone (logout, crash): save and let systemd decide
        saveStats(*g_stats);
        _exit(1);
    });

    signal(SIGINT, [](int) { g_running = 0; });
    signal(SIGTERM, [](int) { g_running = 0; });

    int sock = ipc::listen();
    if (sock < 0) fprintf(stderr, "xpet: couldn't open %s, messages disabled\n", ipc::socketPath().c_str());
    ipc::send(json::object({{"event", "hello"}}), ipc::brainPath());  // the brain re-sends whatever is waiting for you

    Desktop desk(dpy);
    desk.slowRefresh();
    desk.fastRefresh();
    Pet pet(dpy, desk, stats);
    pet.dropInAtPointer();
    pet.greet(away);

    const double tickLen = 1.0 / 30;
    double next = now(), lastSlow = 0, lastMon = 0, lastSave = now(), lastComp = 0;
    bool needSlow = false, compositor = compositorRunning(dpy);
    long frame = 0;

    while (g_running && !pet.quitRequested) {
        while (XPending(dpy)) {
            XEvent e;
            XNextEvent(dpy, &e);
            if (e.type == PropertyNotify && e.xproperty.window == desk.root) {
                if (desk.isWatchedRootProp(e.xproperty.atom)) { needSlow = true; pet.onRootProperty(); }
                continue;
            }
            pet.handle(e);
        }

        double t = now();
        if (t >= next) {
            if (t - lastMon > 5) { desk.refreshMonitors(); lastMon = t; }
            if (needSlow || t - lastSlow > 1) { desk.slowRefresh(); lastSlow = t; needSlow = false; }
            if (t - lastComp > 2) { compositor = compositorRunning(dpy); lastComp = t; }
            if (frame % 2 == 0) desk.fastRefresh();
            pet.update(tickLen);
            pet.render(compositor);
            XFlush(dpy);
            ++frame;
            next += tickLen;
            if (t - next > 0.5) next = t;  // don't try to catch up after a stall
            if (t - lastSave > 60) { saveStats(stats); lastSave = t; }
        }
        if (sock >= 0)
            for (auto& m : ipc::receive(sock)) pet.onMessage(m);

        pollfd pfd[2] = {{ConnectionNumber(dpy), POLLIN, 0}, {sock, POLLIN, 0}};
        int ms = (int)std::max(0.0, (next - now()) * 1000);
        if (!XPending(dpy)) poll(pfd, sock >= 0 ? 2 : 1, ms);
    }

    saveStats(stats);
    if (sock >= 0) unlink(ipc::socketPath().c_str());
    return 0;
}
