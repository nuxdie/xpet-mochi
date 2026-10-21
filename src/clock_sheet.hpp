// The pig for the clocks in the kitchen and the corridor (clock_v3_cpp on Raspberry Pi 2s): a Pi 2 cannot run this
// software renderer at 30 frames a second next to its own landscape, so the pig is drawn here, once a day in that
// day's costume, as a sprite sheet of short loops and one-shot tricks. The relay serves it (mochi-brain, "clocks"); the
// clock downloads it, keeps a copy, and plays the frames, tinting them with its own light.
//
// `xpet --clock-sheet DIR` writes DIR/sheet.png and DIR/sheet.json:
//   {"cell":[w,h], "cols":c, "foot":[x,y], "scale":s, "speed":px_per_frame_at_30fps, "costume":name,
//    "seqs":{"walk_r":{"start":i, "frames":n, "fps":f, "loop":true}, ...}}
// Frames are packed left to right, top to bottom, `cols` to a row (the sheet stays under the Pi's 2048-pixel texture
// limit); frame k of a sequence is cell start+k. "foot" is where the ground under the pig's middle is in each cell.
#pragma once
#include "art3d.hpp"
#include "wardrobe.hpp"

#include <cairo/cairo.h>
#include <cmath>
#include <cstdio>
#include <functional>
#include <string>
#include <vector>

namespace clocksheet {

using namespace art3d;

constexpr double SCALE = 0.7;          // the pig on a 1024×600 clock: small, on the near shore of its lake
constexpr int CELL_W = 84, CELL_H = 102;  // room for a hop, a cheer and the costume's hat
constexpr int COLS = 2048 / CELL_W, MAX_SIDE = 2048;
constexpr double FOOT_X = CELL_W / 2.0, FOOT_Y = CELL_H - 16.0;  // facing you, the near hooves stand lower
// Walking: the gait advances by (6 + speed*3.2) rad/s, so at this speed one stride is exactly 20 frames at 30 fps
// and the walk loops without a hitch.
constexpr double WALK_SPEED = (2 * M_PI * 30 / 20 - 6) / 3.2;

struct Seq {
    const char* name;
    int frames;
    double fps;
    bool loop;
    std::function<void(Frame&, Animator&, int)> step;  // fills in frame i (and may start an action on the first)
};

inline Eyes eyesFor(const Animator& a, Eyes e) {  // as the pet does it: the rig can't shut or brighten the eyes
    if (a.action == Action::Rouse && a.actionT < a.actionLen * ROUSE_YAWN) return Eyes::Closed;
    if (a.action == Action::Bedtime && a.actionT > a.actionLen * BEDTIME_EYES) return Eyes::Closed;
    if (a.action == Action::Perk && a.actionT > a.actionLen * PERK_GLAD) return Eyes::Happy;
    if (a.action == Action::Cheer && a.actionT > a.actionLen * CHEER_TURN) return Eyes::Happy;
    return e;
}

inline std::vector<Seq> sequences() {
    auto walk = [](double yaw) {
        return [yaw](Frame& f, Animator&, int) { f.pose = Pose::WalkA; f.speed = WALK_SPEED; f.yaw = yaw; };
    };
    auto stand = [](double yaw) {  // idling: breathing, glancing about, one blink
        return [yaw](Frame& f, Animator&, int i) { f.yaw = yaw; f.eyes = i == 31 || i == 32 ? Eyes::Closed : Eyes::Open; };
    };
    auto trick = [](double yaw, Action act, double secs, Pose pose = Pose::Stand) {
        return [=](Frame& f, Animator& a, int i) {
            f.yaw = yaw;
            f.pose = pose;
            if (i == 0) a.start(act, secs);
        };
    };
    auto turn = [](double from, double to) {  // stepping round on the spot
        return [=](Frame& f, Animator&, int i) { f.yaw = from + (to - from) * smoothstep(std::min(1.0, i / 6.0)); };
    };
    auto secsOf = [](const char* name) { return actionNamed(name)->secs; };
    return {
        {"walk_r", 20, 30, true, walk(15)},
        {"walk_l", 20, 30, true, walk(165)},
        {"stand_r", 40, 10, true, stand(30)},
        {"stand_l", 40, 10, true, stand(150)},
        {"turn_rl", 7, 12, false, turn(30, 150)},
        {"turn_lr", 7, 12, false, turn(150, 30)},
        {"sniff_r", 21, 12, false, trick(30, Action::Sniff, secsOf("sniff"))},
        {"sniff_l", 21, 12, false, trick(150, Action::Sniff, secsOf("sniff"))},
        {"look_r", 29, 12, false, trick(30, Action::LookAround, secsOf("lookaround"))},
        {"look_l", 29, 12, false, trick(150, Action::LookAround, secsOf("lookaround"))},
        {"cheer", 26, 12, false, trick(90, Action::Cheer, CHEER_SECS)},            // tapped
        {"perk", 27, 12, false, trick(60, Action::Perk, secsOf("perk"))},           // something for you
        {"hop", 14, 15, false, trick(90, Action::Hop, secsOf("hop") * 1.6)},        // urgent: look here
        {"talk", 24, 12, true, [](Frame& f, Animator&, int) { f.yaw = 80; f.talking = true; }},
        {"sit", 26, 10, true, [](Frame& f, Animator&, int) { f.yaw = 70; f.pose = Pose::Sit; }},
        {"busy", 24, 12, true, [](Frame& f, Animator&, int) { f.yaw = 315; f.pose = Pose::Sit; f.prop = Prop::Laptop; }},
        {"bedtime", 39, 12, false, trick(60, Action::Bedtime, secsOf("bedtime"))},
        {"sleep", 30, 10, true, [](Frame& f, Animator&, int) { f.yaw = 60; f.pose = Pose::Sleep; f.eyes = Eyes::Closed; }},
        {"rouse", 36, 12, false, trick(60, Action::Rouse, secsOf("rouse"), Pose::Stand)},
    };
}

// Renders every sequence into DIR/sheet.png and describes it in DIR/sheet.json.
inline int write(const std::string& dir) {
    std::vector<Seq> seqs = sequences();
    int total = 0;
    for (const Seq& s : seqs) total += s.frames;
    const int W = COLS * CELL_W, H = (total + COLS - 1) / COLS * CELL_H;
    if (H > MAX_SIDE) {
        fprintf(stderr, "xpet: clock sheet %dx%d is too big for the Pi\n", W, H);
        return 1;
    }
    cairo_surface_t* sf = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, W, H);
    cairo_t* cr = cairo_create(sf);
    const Costume* costume = wardrobe::today();
    std::string json = "{\"cell\":[" + std::to_string(CELL_W) + "," + std::to_string(CELL_H) + "],\"cols\":" + std::to_string(COLS) + ",\"foot\":[" +
                       std::to_string((int)FOOT_X) + "," + std::to_string((int)FOOT_Y) + "],\"scale\":" +
                       std::to_string(SCALE) + ",\"speed\":" + std::to_string(WALK_SPEED * SCALE) +
                       ",\"costume\":\"" + (costume ? costume->name : "") + "\",\"seqs\":{";
    int cell = 0;
    for (size_t n = 0; n < seqs.size(); ++n) {
        const Seq& s = seqs[n];
        const int start = cell;
        Animator anim;
        Frame f;
        f.costume = costume;
        // Settle into the first frame's pose before anything is drawn, so a loop doesn't start mid-ease.
        auto fill = [&](Frame& g, int i) {
            Frame fresh;
            fresh.costume = costume;
            g = fresh;
            s.step(g, anim, i);
        };
        double t = 10;
        Animator settle;
        for (int k = 0; k < 90; ++k) {
            Frame g;
            g.costume = costume;
            Animator dummy;
            s.step(g, dummy, 1);  // the pose without starting the trick
            g.t = t - (90 - k) / 30.0;
            settle.update(g);
        }
        anim = settle;
        for (int i = 0; i < s.frames; ++i) {
            fill(f, i);
            // Sub-steps at 30 Hz between drawn frames keep the eases and the gait what they are on the desktop.
            const int sub = std::max(1, (int)std::lround(30 / s.fps));
            Rig r;
            for (int k = 0; k < sub; ++k) {
                t += 1.0 / 30;
                f.t = t;
                f.frame = (int)(t * 5);
                r = anim.update(f);
            }
            f.eyes = eyesFor(anim, f.eyes);
            const int x = cell % COLS * CELL_W, y = cell / COLS * CELL_H;
            ++cell;
            cairo_save(cr);
            cairo_rectangle(cr, x, y, CELL_W, CELL_H);
            cairo_clip(cr);
            render(cr, f, r, x + FOOT_X, y + FOOT_Y, SCALE);
            cairo_restore(cr);
        }
        char buf[160];
        snprintf(buf, sizeof buf, "%s\"%s\":{\"start\":%d,\"frames\":%d,\"fps\":%g,\"loop\":%s}", n ? "," : "", s.name,
                 start, s.frames, s.fps, s.loop ? "true" : "false");
        json += buf;
    }
    json += "}}\n";
    cairo_surface_flush(sf);
    const std::string png = dir + "/sheet.png", meta = dir + "/sheet.json";
    const bool ok = cairo_surface_write_to_png(sf, png.c_str()) == CAIRO_STATUS_SUCCESS;
    cairo_destroy(cr);
    cairo_surface_destroy(sf);
    FILE* out = ok ? fopen(meta.c_str(), "w") : nullptr;
    if (!out) {
        fprintf(stderr, "xpet: can't write the clock sheet to %s\n", dir.c_str());
        return 1;
    }
    fputs(json.c_str(), out);
    fclose(out);
    return 0;
}

}  // namespace clocksheet
