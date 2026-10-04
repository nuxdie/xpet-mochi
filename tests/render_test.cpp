// Renders every pose, action and prop the pig has, at window size, and checks that each frame draws
// something, stays inside the window, and puts the head somewhere sensible. `make test` builds and runs it.
#include "../src/art3d.hpp"

#include <cstdio>
#include <cstdlib>

using namespace art3d;

constexpr int WIN_W = 300, WIN_H = 220;

static int failures = 0;

static void check(const char* what, bool ok) {
    if (!ok) { fprintf(stderr, "FAIL: %s\n", what); ++failures; }
}

// Bounding box of drawn pixels (alpha > 40) on a WIN_W x WIN_H surface.
static bool bbox(cairo_surface_t* sf, int& x0, int& y0, int& x1, int& y1) {
    cairo_surface_flush(sf);
    unsigned char* d = cairo_image_surface_get_data(sf);
    int stride = cairo_image_surface_get_stride(sf);
    x0 = y0 = 1 << 30; x1 = y1 = -1;
    for (int y = 0; y < WIN_H; ++y)
        for (int x = 0; x < WIN_W; ++x)
            if (d[y * stride + x * 4 + 3] > 60) { x0 = std::min(x0, x); x1 = std::max(x1, x); y0 = std::min(y0, y); y1 = std::max(y1, y); }
    return x1 >= 0;
}

static void frame(const char* name, Frame f, Action act = Action::Idle, double actP = 0.5) {
    cairo_surface_t* sf = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, WIN_W, WIN_H);
    cairo_t* cr = cairo_create(sf);
    double lift = f.pose == Pose::Fall || f.pose == Pose::Dangle ? 30 : 0;
    HeadPos hp = renderStill(cr, f, WIN_W / 2.0, WIN_H - 1 - 24 - 14 * f.approach - lift, 1.0, act, actP);  // as main.cpp: GROUND_MARGIN 24
    int x0, y0, x1, y1;
    char buf[160];
    bool drawn = bbox(sf, x0, y0, x1, y1);
    snprintf(buf, sizeof buf, "%s: draws something", name);
    check(buf, drawn);
    if (drawn) {
        snprintf(buf, sizeof buf, "%s: inside the window (x %d..%d, y %d..%d)", name, x0, x1, y0, y1);
        check(buf, x0 > 0 && y0 > 0 && x1 < WIN_W - 1 && y1 < WIN_H - 1);
        snprintf(buf, sizeof buf, "%s: a sensible size (%dx%d)", name, x1 - x0, y1 - y0);
        check(buf, x1 - x0 > 30 && y1 - y0 > 30 && x1 - x0 < WIN_W - 10);
        snprintf(buf, sizeof buf, "%s: head over the body (%.0f,%.0f)", name, hp.cx, hp.top);
        check(buf, hp.cx > x0 - 20 && hp.cx < x1 + 20 && hp.top > y0 - 30 && hp.top < y1);
    }
    if (const char* dir = getenv("RENDER_TEST_DUMP")) {  // RENDER_TEST_DUMP=/some/dir saves every frame as a PNG
        char path[512];
        snprintf(path, sizeof path, "%s/%s.png", dir, name);
        cairo_surface_write_to_png(sf, path);
    }
    cairo_destroy(cr);
    cairo_surface_destroy(sf);
}

int main() {
    Pose poses[] = {Pose::Stand, Pose::WalkA, Pose::Sit, Pose::Sleep, Pose::Dangle, Pose::Fall, Pose::Eat};
    const char* poseNames[] = {"stand", "walk", "sit", "sleep", "dangle", "fall", "eat"};
    double yaws[] = {30, 90, 150, 225, 270, 315};
    for (int p = 0; p < 7; ++p)
        for (double yaw : yaws) {
            Frame f; f.pose = poses[p]; f.yaw = yaw; f.speed = p == 1 ? 3.8 : 0; f.t = 1.3; f.thrown = p == 5;
            char name[64]; snprintf(name, sizeof name, "%s@%.0f", poseNames[p], yaw);
            frame(name, f);
        }
    Prop props[] = {Prop::Laptop, Prop::Terminal, Prop::Book, Prop::Globe};
    const char* propNames[] = {"laptop", "terminal", "book", "globe"};
    for (int i = 0; i < 4; ++i)
        for (double yaw : {225.0, 315.0}) {
            Frame f; f.pose = Pose::Sit; f.prop = props[i]; f.yaw = yaw; f.t = 2.0;
            char name[64]; snprintf(name, sizeof name, "desk-%s@%.0f", propNames[i], yaw);
            frame(name, f);
        }
    Action actions[] = {Action::Sniff, Action::Stretch, Action::Shake, Action::LookAround, Action::Hop, Action::Struggle,
                        Action::Nuzzle, Action::HeadShake, Action::Land, Action::Jump, Action::Wiggle, Action::Scratch, Action::Sulk, Action::Cheer, Action::Snuffle};
    const char* actionNames[] = {"sniff", "stretch", "shake", "lookaround", "hop", "struggle", "nuzzle", "headshake", "land", "jump", "wiggle", "scratch", "sulk", "cheer", "snuffle"};
    for (int i = 0; i < 15; ++i)
        for (double p : {0.25, 0.5, 0.8}) {
            Frame f; f.yaw = 30; f.t = 1.1;
            char name[64]; snprintf(name, sizeof name, "action-%s@%.2f", actionNames[i], p);
            frame(name, f, actions[i], p);
        }
    for (double a : {0.5, 1.0}) {  // the close-up, at its biggest
        Frame f; f.yaw = 90; f.approach = a; f.lookKind = 2; f.lookW = 1; f.t = 1;
        char name[64]; snprintf(name, sizeof name, "closeup@%.1f", a);
        frame(name, f);
    }
    if (failures) { fprintf(stderr, "%d check(s) failed\n", failures); return 1; }
    puts("render test: all frames draw, fit the window and keep their heads on");
    return 0;
}
