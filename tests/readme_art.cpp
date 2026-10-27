// Renders the README's pictures with the pig's own renderer: frame sequences for the GIFs and a few stills. Nothing
// here comes from a real desktop (the windows are drawn), so no one's screen ends up in the repo. `make readme-art`
// builds it and turns the frames into docs/*.gif with ffmpeg.
#include <cairo.h>
#include <sys/stat.h>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <string>
#include <vector>
#include "../src/art.hpp"
#include "../src/art3d.hpp"
#include "../src/wardrobe.hpp"
using namespace art3d;

constexpr double FPS = 30, Z = 1.9;  // the pig's frame rate; scale 1 is its size on the desktop
static std::string OUT = "docs/frames";

static double turnTo(double yaw, double target) {  // the pig's own smooth turn (main.cpp), shortest way round
    double turn = std::fmod(target - yaw + 540, 360) - 180;
    return std::fmod(yaw + turn * 0.16 + 360, 360);
}

static void roundedRect(cairo_t* cr, double x, double y, double w, double h, double r) {
    cairo_new_sub_path(cr);
    cairo_arc(cr, x + w - r, y + r, r, -M_PI / 2, 0);
    cairo_arc(cr, x + w - r, y + h - r, r, 0, M_PI / 2);
    cairo_arc(cr, x + r, y + h - r, r, M_PI / 2, M_PI);
    cairo_arc(cr, x + r, y + r, r, M_PI, 3 * M_PI / 2);
    cairo_close_path(cr);
}

static void gradient(cairo_t* cr, int w, int h, RGB top, RGB bottom) {
    cairo_pattern_t* p = cairo_pattern_create_linear(0, 0, w * 0.3, h);
    cairo_pattern_add_color_stop_rgb(p, 0, top.r, top.g, top.b);
    cairo_pattern_add_color_stop_rgb(p, 1, bottom.r, bottom.g, bottom.b);
    cairo_set_source(cr, p);
    cairo_paint(cr);
    cairo_pattern_destroy(p);
}

// A made-up window: title bar with three dots, a body with grey "text" bars. The pig walks on its top edge.
static void window(cairo_t* cr, double x, double y, double w, double h, const char* title) {
    cairo_set_source_rgba(cr, 0, 0, 0, 0.18);
    roundedRect(cr, x + 4, y + 8, w, h, 10);
    cairo_fill(cr);
    roundedRect(cr, x, y, w, h, 10);
    cairo_set_source_rgb(cr, 0.99, 0.98, 0.97);
    cairo_fill(cr);
    cairo_save(cr);
    roundedRect(cr, x, y, w, h, 10);
    cairo_clip(cr);
    cairo_set_source_rgb(cr, 0.91, 0.89, 0.88);
    cairo_rectangle(cr, x, y, w, 30);
    cairo_fill(cr);
    cairo_restore(cr);
    const RGB dots[] = {{0.93, 0.42, 0.38}, {0.96, 0.75, 0.31}, {0.42, 0.78, 0.43}};
    for (int i = 0; i < 3; ++i) {
        cairo_arc(cr, x + 18 + 18 * i, y + 15, 5.5, 0, 2 * M_PI);
        cairo_set_source_rgb(cr, dots[i].r, dots[i].g, dots[i].b);
        cairo_fill(cr);
    }
    cairo_select_font_face(cr, "Sans", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_BOLD);
    cairo_set_font_size(cr, 12);
    cairo_text_extents_t e;
    cairo_text_extents(cr, title, &e);
    cairo_set_source_rgb(cr, 0.45, 0.42, 0.42);
    cairo_move_to(cr, x + (w - e.x_advance) / 2, y + 19);
    cairo_show_text(cr, title);
    // Lines of "code": fixed widths so every frame is the same.
    const double widths[] = {0.42, 0.66, 0.31, 0.55, 0.72, 0.25, 0.48, 0.6, 0.38};
    const RGB inks[] = {{0.62, 0.55, 0.85}, {0.85, 0.85, 0.85}, {0.55, 0.75, 0.62}, {0.85, 0.85, 0.85}};
    for (int i = 0; i < 9 && 46 + i * 20 < h - 12; ++i) {
        double indent = (i % 3) * 22;
        const RGB& c = inks[i % 4];
        cairo_set_source_rgb(cr, c.r, c.g, c.b);
        roundedRect(cr, x + 24 + indent, y + 46 + i * 20, (w - 80) * widths[i], 9, 4.5);
        cairo_fill(cr);
    }
}

// The pig's speech bubble, as main.cpp draws it (yellow for a question, with the "click me" hint).
static void bubble(cairo_t* cr, double cx, double headTop, const std::vector<std::string>& lines, bool ask) {
    cairo_select_font_face(cr, "Sans", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_BOLD);
    cairo_set_font_size(cr, 12 * Z / 1.6);
    double lh = 15 * Z / 1.6, widest = 0;
    cairo_text_extents_t e;
    for (auto& l : lines) { cairo_text_extents(cr, l.c_str(), &e); widest = std::max(widest, e.x_advance); }
    double bw = widest + 26, bh = 14 + lh * lines.size() + (ask ? 18 : 0);
    double bx = cx - bw / 2, by = headTop - bh - 14;
    roundedRect(cr, bx, by, bw, bh, 9);
    cairo_move_to(cr, cx - 8, by + bh - 1);
    cairo_line_to(cr, cx, by + bh + 11);
    cairo_line_to(cr, cx + 8, by + bh - 1);
    double fg = ask ? 0.93 : 0.98, fb = ask ? 0.72 : 0.94;
    cairo_set_source_rgb(cr, 1, fg, fb);
    cairo_fill_preserve(cr);
    cairo_set_source_rgb(cr, 0.16, 0.12, 0.10);
    cairo_set_line_width(cr, 2.5);
    cairo_stroke(cr);
    cairo_set_source_rgb(cr, 1, fg, fb);
    cairo_rectangle(cr, cx - 6.5, by + bh - 3, 13, 4);
    cairo_fill(cr);
    cairo_set_source_rgb(cr, 0.16, 0.12, 0.10);
    for (size_t i = 0; i < lines.size(); ++i) {
        cairo_move_to(cr, bx + 13, by + 7 + lh * (i + 0.85));
        cairo_show_text(cr, lines[i].c_str());
    }
    if (ask) {
        cairo_select_font_face(cr, "Sans", CAIRO_FONT_SLANT_ITALIC, CAIRO_FONT_WEIGHT_NORMAL);
        cairo_set_font_size(cr, 10 * Z / 1.6);
        cairo_set_source_rgb(cr, 0.45, 0.38, 0.30);
        cairo_move_to(cr, bx + 13, by + bh - 9);
        cairo_show_text(cr, "click me to answer");
    }
}

static void label(cairo_t* cr, double cx, double y, const char* text, RGB c = {0.36, 0.27, 0.27}, double size = 15) {
    cairo_select_font_face(cr, "Sans", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_BOLD);
    cairo_set_font_size(cr, size);
    cairo_text_extents_t e;
    cairo_text_extents(cr, text, &e);
    cairo_set_source_rgb(cr, c.r, c.g, c.b);
    cairo_move_to(cr, cx - e.x_advance / 2, y);
    cairo_show_text(cr, text);
}

static void save(cairo_surface_t* sf, const std::string& scene, int i) {
    std::string dir = OUT + "/" + scene;
    mkdir(OUT.c_str(), 0755);
    mkdir(dir.c_str(), 0755);
    char name[32];
    snprintf(name, sizeof name, "/%04d.png", i);
    cairo_surface_write_to_png(sf, (dir + name).c_str());
}

static const RGB BLUSH_T{0.99, 0.93, 0.90}, BLUSH_B{0.95, 0.85, 0.86};

// One pig in a cell, living by a script: given its time t (s) it fills in the frame and may start an action.
struct Actor {
    Animator anim;
    Frame f;
    double x = 0, baseY = 0;
    int blinkAt = 50, blink = 0;
    const char* bubbleText = nullptr;
};

static Eyes eyesFor(const Animator& a, Eyes e) {
    if (a.action == Action::Rouse && a.actionT < a.actionLen * ROUSE_YAWN) return Eyes::Closed;
    if (a.action == Action::Perk && a.actionT > a.actionLen * PERK_GLAD) return Eyes::Happy;
    if (a.action == Action::Cheer && a.actionT > a.actionLen * CHEER_TURN) return Eyes::Happy;
    double q = a.actionT / a.actionLen;  // the rest as main.cpp has them
    if (a.action == Action::Chase && q > CHASE_SPIN) return Eyes::Happy;
    if (a.action == Action::Roll && q > 0.3 && q < 0.9) return Eyes::Happy;
    if (a.action == Action::Sneeze && q > SNEEZE_SHUT && q < SNEEZE_OPEN) return Eyes::Closed;
    if (a.action == Action::Yawn && yawnEyesShut(q)) return Eyes::Closed;
    if (a.action == Action::Rub && q > RUB_FROM && q < RUB_TO) return q > RUB_BLISS && q < RUB_BLISS_END ? Eyes::Closed : Eyes::Happy;
    return e;
}

// Hero: it walks along the top of a window, turns to you, cheers that the tests passed, walks back. Loops cleanly.
static void hero() {
    const int W = 880, H = 370;
    cairo_surface_t* sf = cairo_image_surface_create(CAIRO_FORMAT_RGB24, W, H);
    Actor a;
    a.x = 200;
    double yaw = 30;
    const double walk = 1.5, legLen = 3.3;  // px/frame; seconds per leg of the walk
    const double tTurn = legLen, tCheer = tTurn + 0.7, tBack = tCheer + CHEER_SECS + 1.6, tHome = tBack + legLen, tEnd = tHome + 0.8;
    int n = (int)(tEnd * FPS);
    for (int i = 0; i < n; ++i) {
        double t = i / FPS;
        Frame& f = a.f;
        f.t = 10 + t;
        f.frame = i / 6;
        double target = 30;
        f.pose = Pose::Stand;
        f.speed = 0;
        f.lookKind = 0; f.lookW = 0;
        if (t < tTurn) { f.pose = Pose::WalkA; f.speed = walk; a.x += walk; }
        else if (t < tBack) { target = 90; f.lookKind = 2; f.lookW = 1; }
        else if (t < tHome) { target = 150; f.pose = Pose::WalkA; f.speed = walk; a.x -= walk; }
        if (std::fabs(t - tCheer) < 0.5 / FPS) a.anim.start(Action::Cheer, CHEER_SECS);
        yaw = turnTo(yaw, target);
        f.yaw = yaw;
        f.eyes = Eyes::Open;
        if (i % 75 == 40) a.blink = 4;
        if (a.blink > 0) { f.eyes = Eyes::Closed; --a.blink; }
        f.eyes = eyesFor(a.anim, f.eyes);
        bool talking = t > tCheer + 0.3 && t < tBack - 0.1;
        f.talking = talking;
        Rig r = a.anim.update(f);

        cairo_t* cr = cairo_create(sf);
        gradient(cr, W, H, {0.99, 0.91, 0.87}, {0.80, 0.80, 0.95});
        window(cr, 540, 240, 310, 240, "~/projects/garden");
        window(cr, 60, 278, 600, 200, "mochi \xe2\x80\x94 make test");
        HeadPos hp = render(cr, f, r, a.x, 278 + 1, Z);
        if (talking) bubble(cr, hp.cx, hp.top, {"312 tests passing!"}, false);
        cairo_destroy(cr);
        save(sf, "hero", i);
    }
    cairo_surface_destroy(sf);
}

// A grid of cells, each a pig looping its own little script over the same length. Labels under each.
struct Cell {
    const char* name;
    std::function<void(Actor&, double t, int i)> step;  // sets a.f for time t; may start actions
    double yaw = 30;
};

static void grid(const char* scene, std::vector<Cell> cells, int cols, double secs, int cw = 230, int ch = 210, double groundAt = 0.78) {
    int rows = (cells.size() + cols - 1) / cols;
    int W = cw * cols, H = ch * rows;
    cairo_surface_t* sf = cairo_image_surface_create(CAIRO_FORMAT_RGB24, W, H);
    std::vector<Actor> actors(cells.size());
    int n = (int)(secs * FPS);
    // Run one loop first unrecorded, so the recorded one starts settled and ends where it began.
    for (int pass = 0; pass < 2; ++pass)
        for (int i = 0; i < n; ++i) {
            double t = i / FPS;
            cairo_t* cr = cairo_create(sf);
            if (pass) gradient(cr, W, H, BLUSH_T, BLUSH_B);
            for (size_t k = 0; k < cells.size(); ++k) {
                Actor& a = actors[k];
                a.f = Frame{};
                a.f.t = 10 + pass * secs + t;
                a.f.frame = i / 6;
                a.f.yaw = cells[k].yaw;
                a.f.eyes = (i + 13 * k) % 90 < 4 ? Eyes::Closed : Eyes::Open;
                cells[k].step(a, t, i);
                a.f.eyes = eyesFor(a.anim, a.f.eyes);
                Rig r = a.anim.update(a.f);
                if (!pass) continue;
                double cx = (k % cols) * cw + cw / 2.0, top = (k / cols) * ch;
                cairo_set_source_rgba(cr, 1, 1, 1, 0.35);
                roundedRect(cr, (k % cols) * cw + 8, top + 8, cw - 16, ch - 16, 14);
                cairo_fill(cr);
                render(cr, a.f, r, cx + a.x, top + ch * groundAt, Z * 0.8);
                label(cr, cx, top + ch - 22, cells[k].name);
            }
            cairo_destroy(cr);
            if (pass) save(sf, scene, i);
        }
    cairo_surface_destroy(sf);
}

static Cell trick(const char* name, Action act, double secs, double at = 0.4) {
    return {name, [=](Actor& a, double t, int) {
        if (std::fabs(t - at) < 0.5 / FPS) a.anim.start(act, secs);
    }};
}

static void tricks() {
    std::vector<Cell> cells = {
        trick("sniff", Action::Sniff, 1.7), trick("stretch", Action::Stretch, 2.2), trick("shake", Action::Shake, 0.9, 0.8),
        trick("hop", Action::Hop, 0.55, 0.9), trick("scratch", Action::Scratch, 1.7), trick("wiggle", Action::Wiggle, 0.9, 0.8),
        trick("look around", Action::LookAround, 2.4), trick("paw", Action::Paw, 2.0), trick("land", Action::Land, 0.7, 0.9),
        trick("cheer", Action::Cheer, CHEER_SECS), trick("chases its tail", Action::Chase, CHASE_SECS),
        trick("sneeze", Action::Sneeze, SNEEZE_SECS), trick("yawn", Action::Yawn, YAWN_SECS),
        trick("scratches its rump", Action::Rub, RUB_SECS, 0.2), trick("whoops", Action::Whoops, WHOOPS_SECS, 0.9),
    };
    cells[9].yaw = 90;
    grid("tricks", cells, 5, 4.0);
}

static void life() {
    std::vector<Cell> cells = {
        {"walks", [](Actor& a, double, int) { a.f.pose = Pose::WalkA; a.f.speed = 1.5; }},
        {"chases you", [](Actor& a, double, int) { a.f.pose = Pose::WalkA; a.f.speed = 3.8; }},
        {"eats", [](Actor& a, double t, int i) {
            a.f.pose = Pose::Eat; a.f.progress = std::fmod(t / 3.0, 1.0);
            if ((i / 8) % 2) a.f.eyes = Eyes::Closed;
        }},
        {"naps", [](Actor& a, double, int) { a.f.pose = Pose::Sleep; }},
        {"dreams", [](Actor& a, double t, int) {
            a.f.pose = Pose::Sleep;
            if (std::fabs(t - 0.2) < 0.5 / FPS) a.anim.start(Action::Dream, DREAM_SECS);
        }},
        {"works", [](Actor& a, double, int) { a.f.pose = Pose::Sit; a.f.prop = Prop::Laptop; }, 315},
        {"reads", [](Actor& a, double, int) { a.f.pose = Pose::Sit; a.f.prop = Prop::Book; }, 315},
        {"runs things", [](Actor& a, double, int) { a.f.pose = Pose::Sit; a.f.prop = Prop::Terminal; }, 315},
        {"browses", [](Actor& a, double, int) { a.f.pose = Pose::Sit; a.f.prop = Prop::Globe; }, 315},
        {"half-hears you", [](Actor& a, double t, int) {
            a.f.pose = Pose::Sleep;
            if (std::fabs(t - 0.4) < 0.5 / FPS) a.anim.start(Action::Stir, STIR_SECS);
        }},
    };
    for (Cell& c : cells) {  // props stand where the pig faces
        auto inner = c.step;
        c.step = [inner](Actor& a, double t, int i) { inner(a, t, i); propPlacement(a.f.prop, a.f.yaw, a.f.propX, a.f.propZ, a.f.propYaw); };
    }
    grid("life", cells, 5, 4.0);
}

// Picked up, kicking, let go: dangles, falls, lands with a squash.
static void handled() {
    std::vector<Cell> cells = {
        {"picked up", [](Actor& a, double t, int) {
            a.f.pose = Pose::Dangle;
            if (std::fabs(std::fmod(t, 1.5) - 0.2) < 0.5 / FPS) a.anim.start(Action::Struggle, 1.1);
        }},
        {"petted", [](Actor& a, double t, int) {
            a.f.yaw = 90; a.f.lookKind = 2; a.f.lookW = 1;
            if (std::fabs(t - 0.2) < 0.5 / FPS) a.anim.start(Action::Nuzzle, 1.3);
            if (t > 0.2 && t < 1.6) a.f.eyes = Eyes::Happy;
            if (std::fabs(t - 1.8) < 0.5 / FPS) a.anim.start(Action::HeadShake, 0.9);
        }},
        {"belly rub", [](Actor& a, double t, int) {
            a.f.yaw = 30; a.f.flank = 1;
            if (std::fabs(t - 0.1) < 0.5 / FPS) a.anim.start(Action::Roll, ROLL_SECS);
        }},
        {"you're back!", [](Actor& a, double t, int) {
            a.f.lookKind = 2; a.f.lookW = t > 0.3 ? 1 : 0;
            if (std::fabs(t - 0.2) < 0.5 / FPS) a.anim.start(Action::Perk, 2.2);
        }},
        {"sulks", [](Actor& a, double t, int) {
            if (std::fabs(t - 0.1) < 0.5 / FPS) a.anim.start(Action::Sulk, 2.8);
        }},
    };
    grid("handled", cells, 5, 4.4);
}

// An arrow pointer, tip at (x, y).
static void cursor(cairo_t* cr, double x, double y) {
    const double pts[][2] = {{0, 0}, {0, 22}, {6, 17}, {10, 26}, {14, 24}, {10, 16}, {17, 16}};
    cairo_move_to(cr, x, y);
    for (auto& q : pts) cairo_line_to(cr, x + q[0], y + q[1]);
    cairo_close_path(cr);
    cairo_set_source_rgb(cr, 1, 1, 1);
    cairo_fill_preserve(cr);
    cairo_set_source_rgb(cr, 0.1, 0.1, 0.1);
    cairo_set_line_width(cr, 1.5);
    cairo_stroke(cr);
}

// Up close: the eyes get to your pointer before the head does, an ear twitches, it blinks. A loop of one circle.
static void face() {
    const int W = 880, H = 360;
    const double secs = 5.0, zoom = Z * 1.7;
    cairo_surface_t* sf = cairo_image_surface_create(CAIRO_FORMAT_RGB24, W, H);
    Actor a;
    HeadPos hp{W / 2.0, 120};
    int n = (int)(secs * FPS);
    for (int pass = 0; pass < 2; ++pass)
        for (int i = 0; i < n; ++i) {
            double t = i / FPS, ang = t / secs * 2 * M_PI;
            // The pointer goes round the pig: out to the right, over the top, down the left side, back below.
            double px = W / 2.0 + 330 * std::cos(ang), py = 165 - 120 * std::sin(ang);
            Frame& f = a.f;
            f = Frame{};
            f.t = 10 + pass * secs + t;  // the ear twitch falls into the loop (every 6.7 s from t = 13.4)
            f.frame = i / 6;
            f.yaw = 80;
            f.lookKind = 1;
            f.lookX = px - hp.cx;
            f.lookY = py - (hp.top + 50);
            f.lookW = 1;
            f.eyes = i >= 96 && i < 100 ? Eyes::Closed : Eyes::Open;
            Rig r = a.anim.update(f);
            if (!pass) continue;
            cairo_t* cr = cairo_create(sf);
            gradient(cr, W, H, BLUSH_T, BLUSH_B);
            hp = render(cr, f, r, W / 2.0, H - 48, zoom);
            cursor(cr, px, py);
            cairo_destroy(cr);
            save(sf, "face", i);
        }
    cairo_surface_destroy(sf);
}

// Stills ----------------------------------------------------------------------------------------------------------

static Rig settle(Animator& a, Frame& f, double t0 = 10) {
    for (int i = 0; i < 90; ++i) { f.t = t0 - (90 - i) / FPS; a.update(f); }
    f.t = t0;
    return a.update(f);
}

static void costumes() {
    const char* names[] = {"none", "autumn-scarf", "witch-hat", "bobble-hat"};
    const char* labels[] = {"every day", "autumn", "halloween", "winter"};
    const int cw = 240, ch = 250, W = cw * 4, H = ch;
    cairo_surface_t* sf = cairo_image_surface_create(CAIRO_FORMAT_RGB24, W, H);
    cairo_t* cr = cairo_create(sf);
    gradient(cr, W, H, BLUSH_T, BLUSH_B);
    for (int k = 0; k < 4; ++k) {
        Animator a;
        Frame f;
        f.yaw = 55;
        f.pose = Pose::Sit;
        f.costume = wardrobe::named(names[k]);
        f.lookKind = 2; f.lookW = 0.6;
        Rig r = settle(a, f);
        cairo_set_source_rgba(cr, 1, 1, 1, 0.35);
        roundedRect(cr, k * cw + 8, 8, cw - 16, ch - 16, 14);
        cairo_fill(cr);
        render(cr, f, r, k * cw + cw / 2.0, ch * 0.8, Z * 0.95);
        label(cr, k * cw + cw / 2.0, ch - 22, labels[k]);
    }
    cairo_surface_write_to_png(sf, "docs/costumes.png");
    cairo_destroy(cr);
    cairo_surface_destroy(sf);
}

// The same pig through the day: the light warms in the evening and goes cool and dim at night (dayTint in main.cpp).
static void lightOfDay() {
    struct { const char* name; RGB tint, top, bottom; } hours[] = {
        {"morning", {0.90, 0.92, 0.99}, {0.86, 0.90, 0.98}, {0.99, 0.93, 0.88}},
        {"day", {1, 1, 1}, {0.80, 0.90, 0.99}, {0.95, 0.97, 0.99}},
        {"evening", {1.0, 0.93, 0.84}, {0.99, 0.80, 0.62}, {0.80, 0.55, 0.62}},
        {"night", {0.80, 0.84, 0.98}, {0.16, 0.18, 0.32}, {0.30, 0.26, 0.45}},
    };
    const int cw = 240, ch = 230, W = cw * 4, H = ch;
    cairo_surface_t* sf = cairo_image_surface_create(CAIRO_FORMAT_RGB24, W, H);
    cairo_t* cr = cairo_create(sf);
    for (int k = 0; k < 4; ++k) {
        cairo_save(cr);
        cairo_rectangle(cr, k * cw, 0, cw, ch);
        cairo_clip(cr);
        cairo_pattern_t* p = cairo_pattern_create_linear(0, 0, 0, ch);
        cairo_pattern_add_color_stop_rgb(p, 0, hours[k].top.r, hours[k].top.g, hours[k].top.b);
        cairo_pattern_add_color_stop_rgb(p, 1, hours[k].bottom.r, hours[k].bottom.g, hours[k].bottom.b);
        cairo_set_source(cr, p);
        cairo_paint(cr);
        cairo_pattern_destroy(p);
        Animator a;
        Frame f;
        f.yaw = 40;
        f.tint = hours[k].tint;
        f.pose = k == 3 ? Pose::Sleep : Pose::Stand;
        Rig r = settle(a, f);
        render(cr, f, r, k * cw + cw / 2.0, ch * 0.8, Z * 0.85);
        label(cr, k * cw + cw / 2.0, ch - 18, hours[k].name, k >= 2 ? RGB{1, 0.97, 0.95} : RGB{0.30, 0.30, 0.40});
        cairo_restore(cr);
    }
    cairo_surface_write_to_png(sf, "docs/daylight.png");
    cairo_destroy(cr);
    cairo_surface_destroy(sf);
}

// A question waiting: the yellow bubble over a window, the way an ask looks when the pig shows it.
static void ask() {
    const int W = 880, H = 330;
    cairo_surface_t* sf = cairo_image_surface_create(CAIRO_FORMAT_RGB24, W, H);
    cairo_t* cr = cairo_create(sf);
    gradient(cr, W, H, {0.86, 0.90, 0.98}, {0.99, 0.90, 0.86});
    window(cr, 90, 230, 700, 200, "inbox");
    Animator a;
    Frame f;
    f.yaw = 70;
    f.talking = true;
    f.lookKind = 2; f.lookW = 1;
    Rig r = settle(a, f);
    HeadPos hp = render(cr, f, r, 440, 231, Z);
    bubble(cr, hp.cx, hp.top, {"the dentist moved to thu 15:00.", "put it in your calendar?"}, true);
    cairo_surface_write_to_png(sf, "docs/ask.png");
    cairo_destroy(cr);
    cairo_surface_destroy(sf);
}

int main(int argc, char** argv) {
    if (argc > 1) OUT = argv[1];
    mkdir("docs", 0755);
    hero();
    face();
    tricks();
    life();
    handled();
    costumes();
    lightOfDay();
    ask();
    return 0;
}
