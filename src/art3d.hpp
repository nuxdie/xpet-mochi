// A real 3D pig, Minecraft proportions, software rendered: boxes → flat-shaded quads → perspective →
// painter's sort → cairo polygons. Eight boxes make the pig (head, snout, body, four legs, a tail nub);
// the face is decals on the head. Props (laptop, terminal, book, globe, bowl) are boxes too. No GL.
//
// Animation is a rig of plain numbers (body offset/tilt, leg swings, head angles, squash, tail, snout).
// An Animator owns the smoothed rig: a slow "base" layer that eases between poses, a "motion" layer of
// oscillations for the current pose (gait, breathing, chewing, typing), one-shot "actions" layered on top
// (sniff, stretch, shake, hop, nuzzle, struggle, landing squash...), and head tracking of the pointer.
//
// Model space: units are Minecraft pixels, y up, the pig faces +x, the ground is y = 0, origin under the
// middle of the body. World space adds the pig's yaw; the camera sits in front (+z), a little above.
#pragma once
#include "art.hpp"

#include <algorithm>
#include <cstdint>
#include <cmath>
#include <string>
#include <vector>

namespace art3d {

using art::Eyes;
using art::Pose;
using art::Prop;

constexpr double DEG = M_PI / 180;

struct V3 {
    double x = 0, y = 0, z = 0;
};
inline V3 operator+(V3 a, V3 b) { return {a.x + b.x, a.y + b.y, a.z + b.z}; }
inline V3 operator-(V3 a, V3 b) { return {a.x - b.x, a.y - b.y, a.z - b.z}; }
inline V3 operator*(V3 a, double k) { return {a.x * k, a.y * k, a.z * k}; }
inline double dot(V3 a, V3 b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
inline V3 norm(V3 a) {
    double l = std::sqrt(dot(a, a));
    return l > 1e-9 ? a * (1 / l) : a;
}

struct RGB {
    double r, g, b;
};
inline RGB rgb(unsigned hex) { return {((hex >> 16) & 255) / 255.0, ((hex >> 8) & 255) / 255.0, (hex & 255) / 255.0}; }

// Rigid transform: p' = R p + t. (A * B) applies B first, then A.
struct Xf {
    double m[9] = {1, 0, 0, 0, 1, 0, 0, 0, 1};
    V3 t;
    V3 dir(V3 p) const { return {m[0] * p.x + m[1] * p.y + m[2] * p.z, m[3] * p.x + m[4] * p.y + m[5] * p.z, m[6] * p.x + m[7] * p.y + m[8] * p.z}; }
    V3 pt(V3 p) const { return dir(p) + t; }
    Xf operator*(const Xf& b) const {
        Xf r;
        for (int i = 0; i < 3; ++i)
            for (int j = 0; j < 3; ++j) r.m[i * 3 + j] = m[i * 3] * b.m[j] + m[i * 3 + 1] * b.m[3 + j] + m[i * 3 + 2] * b.m[6 + j];
        r.t = pt(b.t);
        return r;
    }
    static Xf trans(V3 t) {
        Xf r;
        r.t = t;
        return r;
    }
    static Xf rotX(double deg) {
        double c = std::cos(deg * DEG), s = std::sin(deg * DEG);
        Xf r;
        r.m[4] = c; r.m[5] = -s; r.m[7] = s; r.m[8] = c;
        return r;
    }
    static Xf rotY(double deg) {
        double c = std::cos(deg * DEG), s = std::sin(deg * DEG);
        Xf r;
        r.m[0] = c; r.m[2] = s; r.m[6] = -s; r.m[8] = c;
        return r;
    }
    static Xf rotZ(double deg) {
        double c = std::cos(deg * DEG), s = std::sin(deg * DEG);
        Xf r;
        r.m[0] = c; r.m[1] = -s; r.m[3] = s; r.m[4] = c;
        return r;
    }
};

// A rectangle painted on one face of a box, in that face's (u, v) coordinates from the face centre.
struct Decal {
    int face;  // 0:+x 1:-x 2:+y 3:-y 4:+z 5:-z
    double cu, cv, hu, hv;
    RGB col;
};

struct Box {
    V3 c, s;  // centre and full size, in part space
    RGB col;
    unsigned hidden = 0;  // bit per face: faces buried inside another part (never drawn)
    std::vector<Decal> decals;
};

struct Sphere {
    V3 c;
    double r;
    RGB col;
    double spin = 0;  // where the continents are
};

struct Part {
    Xf xf;
    std::vector<Box> boxes;
    std::vector<Sphere> spheres;
};

// Something the pig wears (a scarf, a hat): extra boxes riding on its parts, so they move with them. Each piece is
// in its anchor's own space: Body has its origin under the middle of the body on its bottom face (the body box
// spans x -8..8, y 0..8, z -5..5); Head has its origin at the neck (the head spans x 0..8, y -4..4, z -4..4,
// the face is +x); Tail at the root of the tail (it sticks out along -x); legs at the hip (a leg spans y -6..0).
// The designs and the dates they're worn on live in wardrobe.hpp.
enum class Anchor { Body, Head, Tail, LegFL, LegFR, LegBL, LegBR };
struct Piece {
    Anchor at;
    Box box;
};
struct Costume {
    std::string name;
    std::vector<Piece> pieces;
    double hatHeight = 0;  // model units it adds above the head, so bubbles and hearts clear it
};

// Per face: outward normal n and tangent axes (u, v) with n = u × v, so corners (±hu, ±hv) wind CCW from outside.
struct FaceAxes {
    V3 n, u, v;
};
inline const FaceAxes FACES[6] = {
    {{1, 0, 0}, {0, 1, 0}, {0, 0, 1}},  {{-1, 0, 0}, {0, 0, 1}, {0, 1, 0}}, {{0, 1, 0}, {0, 0, 1}, {1, 0, 0}},
    {{0, -1, 0}, {1, 0, 0}, {0, 0, 1}}, {{0, 0, 1}, {1, 0, 0}, {0, 1, 0}},  {{0, 0, -1}, {0, 1, 0}, {1, 0, 0}},
};
inline double half(const Box& b, V3 axis) { return std::fabs(axis.x) * b.s.x / 2 + std::fabs(axis.y) * b.s.y / 2 + std::fabs(axis.z) * b.s.z / 2; }

// ---- palette ---------------------------------------------------------------------------------------

inline const RGB PINK = rgb(0xF0A5A2), SNOUT = rgb(0xD97F7D), DARK = rgb(0x4A2626), WHITE = rgb(0xF6F6F6),
                 BLACK = rgb(0x1A1A1A), BLUSH = rgb(0xE86E8A), SILVER = rgb(0xB8C0CC), SCREEN = rgb(0xA8E6F2),
                 INK = rgb(0x3A4A6A), TERM = rgb(0x141A1F), TERMTXT = rgb(0x5BE37A), PAGE = rgb(0xFFF7EA),
                 COVER = rgb(0x4A78D6), SEA = rgb(0x3F74D8), LAND = rgb(0x5DBB63), BOWL = rgb(0x6C8EBF),
                 FOOD = rgb(0x8A5A2B), STAND = rgb(0x5A4A3A);

// ---- what the pet tells the animator each frame -------------------------------------------------------

enum class Action { Idle, Sniff, Stretch, Shake, LookAround, Hop, Struggle, Nuzzle, HeadShake, Land, Jump, Wiggle, Scratch, Sulk, Cheer, Snuffle, Rouse, Bedtime };

// Every action by name, with how long it normally plays. The render test, the sheet's --strip and the socket's
// "trick" event all go through this table, so a new action belongs here too.
struct ActionInfo {
    const char* name;
    Action action;
    double secs;
};
inline const ActionInfo ACTIONS[] = {
    {"sniff", Action::Sniff, 1.7},     {"stretch", Action::Stretch, 2.2},     {"shake", Action::Shake, 0.9},
    {"lookaround", Action::LookAround, 2.4}, {"hop", Action::Hop, 0.55},   {"struggle", Action::Struggle, 1.1},
    {"nuzzle", Action::Nuzzle, 1.3},   {"headshake", Action::HeadShake, 0.9}, {"land", Action::Land, 0.7},
    {"jump", Action::Jump, 0.45},      {"wiggle", Action::Wiggle, 0.9},       {"scratch", Action::Scratch, 1.7},
    {"sulk", Action::Sulk, 3.5},       {"cheer", Action::Cheer, 1.6},         {"snuffle", Action::Snuffle, 1.4},
    {"rouse", Action::Rouse, 3.0},     {"bedtime", Action::Bedtime, 3.2},
};
// Rouse keeps its eyes shut through the yawn: the first this much of it (the pet closes them, the rig can't).
constexpr double ROUSE_YAWN = 0.4;
// Bedtime shuts them once the head goes down, from this far in.
constexpr double BEDTIME_EYES = 0.74;
inline const ActionInfo* actionNamed(const std::string& name) {
    for (const ActionInfo& a : ACTIONS)
        if (name == a.name) return &a;
    return nullptr;
}

struct Frame {
    Pose pose = Pose::Stand;
    Eyes eyes = Eyes::Open;
    Prop prop = Prop::Nothing;
    double t = 0;         // seconds, for animation
    double yaw = 30;      // degrees: 30 = facing right (a little toward you), 150 = facing left
    int frame = 0;        // for flickering screens
    double speed = 0;     // ground speed in screen px/frame (drives the gait)
    double vy = 0;        // vertical speed in the air (px/frame, + is down)
    bool thrown = false;  // flying fast: tumble
    bool talking = false, happy = false, hungry = false;
    double progress = 0;  // 0..1 through a timed state (eating: how much of the bowl is gone)
    double propX = 0, propZ = 0, propYaw = 0;  // where the prop stands, in world units on the ground (see propPlacement)
    double approach = 0;  // 0..1: how far it has come up to the glass (the close-up when you ignore it)
    RGB tint{1, 1, 1};    // time-of-day light: warm in the evening, cool and dim at night
    double lookX = 0, lookY = 0, lookW = 0;  // a point to look at, relative to the head (screen px), and how much to care
    int lookKind = 0;                         // 0 nothing, 1 that point, 2 you (out of the screen)
    const Costume* costume = nullptr;         // what it's wearing today (wardrobe.hpp), if anything
};

// Every number that moves. Base + motion + actions add up into one of these each frame.
struct Rig {
    double bodyX = 0, bodyY = 0, bodyPitch = 0, bodyRoll = 0;  // body offset/tilt about its rear-bottom pivot
    double scaleY = 1, scaleXZ = 1;                            // squash and stretch
    double leg[4] = {0, 0, 0, 0};                              // swing about the sideways axis: FL FR BL BR
    double splay[4] = {0, 0, 0, 0};                            // sideways splay
    double headPitch = 0, headYaw = 0, headRoll = 0;
    double snout = 1;        // snout scale (wiggles when sniffing/chewing)
    double tail = 0;         // tail nub yaw
    double shadow = 1;       // ground shadow opacity (0 in the air)
    double lift = 0;         // whole pig raised off the ground (hops), on top of bodyY
    double neck = 0;         // head pushed forward along the body (the close-up stare)
    double centred = 0;      // 0: the body tilts about its rear-bottom corner (sitting, stretching);
                             // 1: about its centre (in the air), so a tumble stays put
    double yaw = 0;          // the whole pig turned on the spot, on top of the frame's yaw (only actions use it)
};

inline Rig operator+(Rig a, const Rig& b) {
    a.bodyX += b.bodyX; a.bodyY += b.bodyY; a.bodyPitch += b.bodyPitch; a.bodyRoll += b.bodyRoll;
    a.scaleY += b.scaleY - 1; a.scaleXZ += b.scaleXZ - 1;
    for (int i = 0; i < 4; ++i) { a.leg[i] += b.leg[i]; a.splay[i] += b.splay[i]; }
    a.headPitch += b.headPitch; a.headYaw += b.headYaw; a.headRoll += b.headRoll;
    a.snout += b.snout - 1; a.tail += b.tail; a.shadow = std::min(a.shadow, b.shadow); a.lift += b.lift;
    a.centred = std::max(a.centred, b.centred);
    a.neck += b.neck;
    a.yaw += b.yaw;
    return a;
}
inline void ease(double& v, double target, double k) { v += (target - v) * k; }
inline void ease(Rig& r, const Rig& to, double k) {
    ease(r.bodyX, to.bodyX, k); ease(r.bodyY, to.bodyY, k); ease(r.bodyPitch, to.bodyPitch, k); ease(r.bodyRoll, to.bodyRoll, k);
    ease(r.scaleY, to.scaleY, k); ease(r.scaleXZ, to.scaleXZ, k);
    for (int i = 0; i < 4; ++i) { ease(r.leg[i], to.leg[i], k); ease(r.splay[i], to.splay[i], k); }
    ease(r.headPitch, to.headPitch, k); ease(r.headYaw, to.headYaw, k); ease(r.headRoll, to.headRoll, k);
    ease(r.snout, to.snout, k); ease(r.tail, to.tail, k); ease(r.shadow, to.shadow, k); ease(r.lift, to.lift, k);
    ease(r.centred, to.centred, k);
    ease(r.neck, to.neck, k);
}

inline double env(double p) { return std::sin(std::clamp(p, 0.0, 1.0) * M_PI); }  // 0 → 1 → 0 over an action
inline double smoothstep(double x) {
    x = std::clamp(x, 0.0, 1.0);
    return x * x * (3 - 2 * x);
}

// The resting shape of each pose (no oscillation). Eased into over a few frames.
inline Rig baseFor(const Frame& f) {
    Rig r;
    switch (f.pose) {
        case Pose::Stand:
        case Pose::WalkA:
        case Pose::WalkB:
            if (f.hungry) { r.headPitch = 14; r.bodyY = -0.4; }
            break;
        case Pose::Eat:
            r.headPitch = 30;
            break;
        case Pose::Sit:  // like a dog: hind legs folded forward, rump on the ground, nose up
            r.bodyPitch = f.prop == Prop::Nothing ? 16 : 7;  // at the desk: lower, leaning over the work
            r.bodyY = -1;
            r.leg[2] = r.leg[3] = 85;
            r.headPitch = f.prop == Prop::Book || f.prop == Prop::Laptop || f.prop == Prop::Terminal ? 18 : 4;
            if (f.prop == Prop::Laptop || f.prop == Prop::Terminal) { r.leg[0] = r.leg[1] = 35; }  // hooves up on the keys
            if (f.prop == Prop::Globe) r.headYaw = 10;
            break;
        case Pose::Sleep:  // flat on the belly, legs folded away, head on the ground
            r.bodyY = -4;
            r.leg[0] = r.leg[1] = 90;
            r.leg[2] = r.leg[3] = -90;
            r.headPitch = 14;
            r.headRoll = 6;
            break;
        case Pose::Dangle:  // held up by the scruff: nose up, legs hang
            r.bodyPitch = 12;
            for (double& l : r.leg) l = -22;
            r.leg[0] += 8; r.leg[3] += 8;
            r.bodyY = 1.5;
            r.shadow = 0;
            r.centred = 1;
            break;
        case Pose::Fall:
            r.centred = 1;
            for (int i = 0; i < 4; ++i) r.splay[i] = (i % 2 ? 1 : -1) * 32;
            r.leg[0] = r.leg[1] = -30;
            r.leg[2] = r.leg[3] = 30;
            r.bodyPitch = std::clamp(f.vy * 1.6, -28.0, 28.0) * (f.thrown ? 0 : 1);
            r.shadow = 0;
            break;
    }
    return r;
}

// The things that keep moving while a pose holds. Added on top of the eased base every frame.
struct Motion {
    double gait = 0;  // walking phase, advanced by ground speed so the hooves match the ground
    double spin = 0;  // tumble angle when thrown
    Rig at(const Frame& f, double dt) {
        Rig m;
        double t = f.t;
        bool walking = f.pose == Pose::WalkA || f.pose == Pose::WalkB;
        if (walking) gait += dt * (6 + f.speed * 3.2);
        else gait = std::fmod(gait, 2 * M_PI) * 0.9;
        if (f.thrown) spin += dt * 420; else spin = 0;
        switch (f.pose) {
            case Pose::WalkA:
            case Pose::WalkB: {
                bool trot = f.speed > 2.5;
                // Diagonal pairs swing together; the forward swing is quicker than the push back (skewed sine),
                // so the step reads as a step and not a pendulum.
                auto swing = [&](double ph) { return std::sin(ph + 0.35 * std::sin(ph)) * (trot ? 42 : 30); };
                double a = swing(gait), b = swing(gait + M_PI);
                m.leg[0] = a; m.leg[3] = a; m.leg[1] = b; m.leg[2] = b;
                m.bodyY = std::fabs(std::sin(gait)) * (trot ? 1.0 : 0.45);
                m.bodyRoll = std::sin(gait) * (trot ? 3.5 : 2.5);
                m.bodyPitch = trot ? -3 : 0;  // leaning into it
                m.headPitch = std::sin(gait * 2 + 0.6) * (trot ? 6 : 4) + (trot ? -5 : 1);
                m.headYaw = std::sin(gait) * 3;  // sways with the shoulders
                m.headRoll = std::sin(gait) * -2;
                m.tail = std::sin(gait) * 25 + std::sin(gait * 2) * 8;
                break;
            }
            case Pose::Stand:
                m.scaleY = 1 + 0.012 * std::sin(t * 2.2);
                m.bodyRoll = 1.2 * std::sin(t * 0.37);  // slow shift of weight from one side to the other
                m.bodyX = 0.3 * std::sin(t * 0.37 + 1);
                m.headYaw = 14 * std::sin(t * 0.45) * std::sin(t * 0.17) * (1 - f.lookW);  // glances this way and that
                m.tail = std::sin(t * 1.3) * 14;
                break;
            case Pose::Eat:
                m.headPitch = 6 + std::sin(t * 9) * 7;
                m.snout = 1 + 0.14 * std::fabs(std::sin(t * 18));
                m.tail = std::sin(t * 7) * 28;
                break;
            case Pose::Sit:
                m.scaleY = 1 + 0.01 * std::sin(t * 2.2);
                if (f.prop == Prop::Laptop || f.prop == Prop::Terminal) {  // typing: hooves tap the keys in turn
                    m.leg[0] = -10 * std::max(0.0, std::sin(t * 13));
                    m.leg[1] = -10 * std::max(0.0, std::sin(t * 13 + M_PI));
                    m.headPitch = std::sin(t * 2.6) * 2;
                    m.headYaw = 8 * std::sin(t * 0.9);
                } else if (f.prop == Prop::Book) {  // reading: eyes run along the lines
                    m.headYaw = 9 * std::sin(t * 1.6) - 6;
                    m.headPitch = 2 * std::sin(t * 3.2);
                } else if (f.prop == Prop::Globe) {
                    m.headYaw = 10 * std::sin(t * 0.8);
                    m.headPitch = 4 * std::sin(t * 0.6);
                } else {
                    m.headYaw = 12 * std::sin(t * 0.4) * std::sin(t * 0.15) * (1 - f.lookW);
                    m.tail = std::sin(t * 1.1) * 12;
                }
                break;
            case Pose::Sleep: {
                m.scaleY = 1 + 0.035 * std::sin(t * 1.5);
                double twitch = std::fmod(t, 7.0);  // a dreaming leg, every seven seconds
                if (twitch < 0.6) m.leg[1] = 8 * std::sin(twitch * 10);
                break;
            }
            case Pose::Dangle:
                for (double& l : m.leg) l = std::sin(t * 5) * 9;
                m.bodyPitch = std::sin(t * 3) * 3;
                m.bodyRoll = std::sin(t * 2.3) * 6;
                m.tail = std::sin(t * 4) * 15;
                break;
            case Pose::Fall:
                if (f.thrown) {
                    m.bodyPitch = spin;
                    for (int i = 0; i < 4; ++i) m.leg[i] = std::sin(t * 25 + i) * 25;
                } else {
                    m.bodyPitch = std::sin(t * 8) * 6;
                    for (int i = 0; i < 4; ++i) m.leg[i] = std::sin(t * 12 + i * 1.5) * 8;
                }
                break;
        }
        if (f.talking) { m.headPitch += std::sin(t * 11) * 2.5; m.snout += 0.06 * std::fabs(std::sin(t * 11)); }
        if (f.happy) { m.tail += std::sin(t * 14) * 30; m.bodyY += 0.3 * std::fabs(std::sin(t * 7)); }
        return m;
    }
};

// One-shot overlays. p runs 0 → 1 over the action's length.
inline Rig actionRig(Action a, double p, double t) {
    Rig r;
    double e = env(p);
    switch (a) {
        case Action::Sniff:  // nose to the ground, snout working
            r.headPitch = 38 * e;
            r.bodyY = -0.6 * e;
            r.leg[0] = r.leg[1] = 6 * e;
            r.snout = 1 + 0.22 * std::fabs(std::sin(t * 22)) * e;
            break;
        case Action::Stretch:  // front legs out, rump up, nose up: a big cat-like stretch
            r.leg[0] = r.leg[1] = 55 * e;
            r.bodyPitch = -16 * e;
            r.bodyY = 2.4 * e;
            r.headPitch = -22 * e;
            r.scaleXZ = 1 + 0.04 * e;
            r.tail = 20 * e;
            break;
        case Action::Shake:  // a wet-dog shake
            r.bodyRoll = 13 * std::sin(t * 32) * e;
            r.headRoll = 22 * std::sin(t * 32 + 1.2) * e;
            r.headYaw = 8 * std::sin(t * 16) * e;
            r.tail = 40 * std::sin(t * 30) * e;
            break;
        case Action::LookAround:  // left, then right, then back
            r.headYaw = 42 * std::sin(p * 2 * M_PI) * smoothstep(std::min(p, 1 - p) * 6);
            r.headPitch = -5 * e;
            break;
        case Action::Hop:
            r.lift = 7 * e;
            for (double& l : r.leg) l = 22 * e;
            r.scaleY = 1 + 0.12 * std::sin(p * 2 * M_PI);
            r.scaleXZ = 1 - 0.06 * std::sin(p * 2 * M_PI);
            r.shadow = 1 - 0.5 * e;
            break;
        case Action::Struggle:  // kicking in your hand
            for (int i = 0; i < 4; ++i) r.leg[i] = 28 * std::sin(t * 26 + i * 1.6) * e;
            r.bodyRoll = 9 * std::sin(t * 20) * e;
            r.headYaw = 12 * std::sin(t * 18) * e;
            break;
        case Action::Nuzzle:  // leans into the hand
            r.headPitch = -26 * e;
            r.headYaw = 28 * e;
            r.headRoll = 10 * e;
            r.bodyY = 0.5 * e;
            r.tail = 25 * std::sin(t * 12) * e;
            break;
        case Action::HeadShake:
            r.headYaw = 32 * std::sin(t * 19) * e;
            break;
        case Action::Land: {  // squash on impact, one bounce off the floor, a soft touch-down, rest
            // One spring cycle and no more (he asked for a single bounce): g > 0 is squash, g < 0 the rebound. After
            // the rebound only the touch-down squash is kept, never a second lift. The last fifth fades to nothing so
            // the hand-off to the pose doesn't pop.
            auto spring = [](double x) {
                if (x < 0 || x > 3 * M_PI) return 0.0;
                return x < 2 * M_PI ? std::sin(x) : std::max(0.0, std::sin(x));
            };
            double fade = smoothstep((1 - p) * 5), decay = std::exp(-2.4 * p);
            double g = spring(p * 9.75) * decay * fade;
            double lag = spring(p * 9.75 - 0.9) * decay * fade;  // the head follows late
            double up = std::max(0.0, -g);
            r.scaleY = 1 - (g > 0 ? 0.3 : 0.4) * g;  // the stretch reads stronger than the squash
            r.scaleXZ = 1 + (g > 0 ? 0.15 : 0.18) * g;
            r.bodyY = -0.6 * std::max(0.0, g);  // sinks into its legs
            r.lift = 9 * up;                    // the rebound leaves the floor, about a third of a hop
            r.shadow = 1 - 0.8 * up;
            for (double& s : r.splay) s = 10 * std::max(0.0, g);
            for (double& l : r.leg) l = 14 * up;  // legs tuck a little in the air, like a small hop
            r.headPitch = 16 * lag;  // a nod after the body has already stopped
            r.bodyRoll = 3.5 * std::sin(p * 19) * smoothstep((p - 0.1) * 6) * std::exp(-2.5 * p) * fade;  // finds its balance
            r.tail = 30 * std::sin(p * 22) * std::exp(-3 * p) * fade;
            break;
        }
        case Action::Jump:  // stretch on take-off
            r.scaleY = 1 + 0.16 * e;
            r.scaleXZ = 1 - 0.07 * e;
            r.leg[2] = r.leg[3] = 25 * e;
            r.leg[0] = r.leg[1] = -20 * e;
            r.headPitch = -10 * e;
            break;
        case Action::Wiggle:  // a happy butt wiggle
            r.bodyRoll = 7 * std::sin(t * 18) * e;
            r.bodyX = 0.4 * std::sin(t * 18) * e;
            r.tail = 45 * std::sin(t * 18) * e;
            r.headRoll = -5 * std::sin(t * 18) * e;
            break;
        case Action::Sulk:  // head hangs, body sags, a slow unhappy sway (a job that keeps failing)
            r.headPitch = 28 * e;
            r.bodyY = -1.2 * e;
            r.headRoll = 6 * std::sin(t * 1.7) * e;
            r.bodyRoll = 2 * std::sin(t * 1.7) * e;
            r.leg[0] = r.leg[1] = 6 * e;
            break;
        case Action::Cheer: {  // two jumps for joy with a wiggle in between (a big job finished)
            double hop = std::fabs(std::sin(p * 2 * M_PI)) * e;
            r.lift = 9 * hop;
            for (double& l : r.leg) l = 25 * hop;
            r.scaleY = 1 + 0.12 * std::sin(p * 4 * M_PI) * e;
            r.headPitch = -15 * e;
            r.tail = 45 * std::sin(t * 16) * e;
            r.bodyRoll = 6 * std::sin(t * 16) * e;
            r.shadow = 1 - 0.5 * hop;
            break;
        }
        case Action::Snuffle:  // sniffing at your finger: snout working, head pushed a little forward
            r.snout = 1 + 0.25 * std::fabs(std::sin(t * 24)) * e;
            r.neck = 1.2 * e;
            r.headPitch = -6 * e;
            r.headRoll = 5 * std::sin(t * 9) * e;
            break;
        case Action::Rouse: {  // waking up from a nap: a big yawn, a long play-bow stretch, then shake the sleep off
            // Each stage rises, holds and lets go (a plateau, not a sine), and they overlap a little so it flows.
            auto stage = [&](double a, double b) {
                double q = (p - a) / (b - a);
                return smoothstep(q * 3.5) * smoothstep((1 - q) * 3.5);
            };
            double yawn = stage(0.0, ROUSE_YAWN), bow = stage(0.36, 0.8), shake = env((p - 0.78) / 0.22);
            double dip = env(p / 0.08);  // anticipation: the head ducks a touch before it tips back
            r.headPitch = 6 * dip - 34 * yawn - 20 * bow;
            r.headRoll = 7 * yawn + 2 * std::sin(t * 21) * yawn + 22 * std::sin(t * 32 + 1.2) * shake;  // the yawn's shiver
            r.snout = 1 + 0.3 * yawn;  // mouth open wide
            r.scaleY = 1 + 0.04 * yawn;  // the big breath in
            r.leg[0] = r.leg[1] = 55 * bow;
            r.bodyPitch = -16 * bow;
            r.bodyY = 2.4 * bow;
            r.scaleXZ = 1 + 0.05 * bow;
            r.bodyRoll = 13 * std::sin(t * 32) * shake;
            r.headYaw = 8 * std::sin(t * 16) * shake;
            r.tail = 25 * yawn + 20 * bow + 40 * std::sin(t * 30) * shake;
            break;
        }
        case Action::Bedtime: {  // lying down for a nap like a dog: sniff the spot, turn once round on it, flop
            // Unlike the others it doesn't come back to zero: it ends in exactly the Sleep pose's shape (and a full
            // turn), and the animator hands that to the base layer, so the pet can switch to Sleep without a pop.
            auto stage = [&](double a, double b) {
                double q = (p - a) / (b - a);
                return smoothstep(q * 3.5) * smoothstep((1 - q) * 3.5);
            };
            auto fall = [&](double a, double b) {  // accelerating, like dropping: slow to let go, quick at the floor
                double q = std::clamp((p - a) / (b - a), 0.0, 1.0);
                return q * q;
            };
            double sniff = stage(0.0, 0.2);
            double q = std::clamp((p - 0.14) / 0.42, 0.0, 1.0), turning = env(q);
            double step = smoothstep(q) * 2 * M_PI * 3;  // three steps round, in time with the turn
            double back = fall(0.56, 0.68), front = fall(0.6, 0.72);  // the rump goes down first, then the front
            double plop = env((p - 0.72) / 0.1), sigh = env((p - 0.8) / 0.18);
            double head = smoothstep((p - 0.7) / 0.14);  // the head comes down last, onto its chin
            r.yaw = 360 * smoothstep(q);
            double a = 26 * std::sin(step) * turning;
            r.leg[0] = r.leg[3] = a;
            r.leg[1] = r.leg[2] = -a;
            r.leg[0] += 90 * front; r.leg[1] += 90 * front;
            r.leg[2] += -90 * back; r.leg[3] += -90 * back;
            r.bodyY = -0.4 * sniff + 0.5 * std::fabs(std::sin(step)) * turning - 2 * back - 2 * front;
            r.bodyPitch = 14 * (back - front);
            r.bodyRoll = -5 * turning;  // leaning into the turn
            r.headPitch = 30 * sniff + 12 * turning + 14 * head + 6 * env((p - 0.74) / 0.14);  // the chin sinks, settles
            r.headYaw = 18 * turning;  // nose following its tail round
            r.headRoll = 6 * head;
            r.snout = 1 + 0.22 * std::fabs(std::sin(t * 22)) * sniff;
            r.scaleY = 1 - 0.12 * plop + 0.07 * sigh;  // lands heavy, then one long breath out
            r.scaleXZ = 1 + 0.06 * plop;
            r.tail = 25 * std::sin(step) * turning + 30 * std::sin(p * 40) * env((p - 0.8) / 0.15);
            break;
        }
        case Action::Scratch:  // hind leg scratching behind the "ear"
            r.leg[3] = -70 * e;
            r.splay[3] = 35 * e + 12 * std::sin(t * 24) * e;
            r.headRoll = -14 * e;
            r.headYaw = 10 * e;
            r.bodyRoll = 6 * e;
            break;
        default: break;
    }
    return r;
}

// Owns the smoothed rig. One per pet; call update() every frame, then render() with the rig it hands back.
struct Animator {
    Rig base;    // eased resting shape
    Motion motion;
    Action action = Action::Idle;
    double actionT = 0, actionLen = 1;
    double lastT = -1;
    double lookYaw = 0, lookPitch = 0;

    bool busy() const { return action != Action::Idle; }
    void start(Action a, double secs) {
        action = a;
        actionT = 0;
        actionLen = std::max(0.05, secs);
    }

    Rig update(const Frame& f) {
        double dt = lastT < 0 ? 1.0 / 30 : std::clamp(f.t - lastT, 0.0, 0.1);
        lastT = f.t;
        // Base layer: ease toward the pose's resting shape. Faster for the air (reactions), slower on the ground.
        double k = f.pose == Pose::Fall || f.pose == Pose::Dangle ? 0.3 : 0.14;
        ease(base, baseFor(f), k);
        Rig r = base + motion.at(f, dt);
        // Head tracking: turn toward the pointer when it's near. Screen offset → angles around the pig's yaw.
        double tx = 0, ty = 0;
        if (f.lookW > 0 && f.lookKind) {
            // Direction to the target in the screen plane: angle psi, where 0 = screen right, 90 = out at you,
            // 180 = screen left. The pig's nose points at angle yaw, so the head turns by (psi - yaw).
            double psi, pitch;
            if (f.lookKind == 2) { psi = 90; pitch = -8; }
            else {
                psi = std::atan2(200.0, f.lookX) / DEG;  // a little depth so a point right above doesn't flip the head
                pitch = std::atan2(f.lookY, std::hypot(f.lookX, 200.0)) / DEG;
            }
            double rel = std::fmod(psi - f.yaw + 540, 360) - 180;
            tx = std::clamp(rel, -75.0, 75.0);
            ty = std::clamp(pitch, -35.0, 40.0);
        }
        ease(lookYaw, tx * f.lookW, 0.12);
        ease(lookPitch, ty * f.lookW, 0.12);
        bool awake = f.pose == Pose::Stand || f.pose == Pose::Sit || f.pose == Pose::WalkA || f.pose == Pose::WalkB || f.pose == Pose::Eat;
        bool mayLook = awake && (f.prop == Prop::Nothing || f.lookKind == 2);  // at the desk it only looks up for you
        if (mayLook) { r.headYaw += lookYaw; r.headPitch += lookPitch; }
        if (f.approach > 0) {  // up close: head pushed forward and tilted up at you
            r.neck += 3.0 * f.approach;
            r.headPitch += -14 * f.approach;
            r.bodyY += 0.3 * std::sin(f.t * 1.8) * f.approach;
        }
        if (action != Action::Idle) {
            actionT += dt;
            r = r + actionRig(action, actionT / actionLen, f.t);
            if (actionT >= actionLen) {
                // Where it ended becomes the base, which eases on to the pose from there: most actions end at zero,
                // but one that finishes in another shape (Bedtime, lying down) then hands over without a pop.
                Rig end = actionRig(action, 1.0, f.t);
                end.yaw = 0;  // a whole turn is no turn
                base = base + end;
                action = Action::Idle;
            }
        }
        return r;
    }
};

// ---- building the pig ---------------------------------------------------------------------------------

inline Box legBox() { return {{0, -3, 0}, {4, 6, 4}, PINK, 0, {}}; }  // hangs from its hip; the top shows when it swings out

inline Part headPart(Eyes eyes, double snout) {
    Part p;
    Box head{{4, 0, 0}, {8, 8, 8}, PINK, 1u << 1, {}};  // pivot at the neck (back-centre); back face sits in the body
    // Eyes on the front face (+x): u is up, v is sideways. Each eye: white outer column, black inner.
    if (eyes == Eyes::Open) {
        for (double side : {-1.0, 1.0}) {
            head.decals.push_back({0, 2.0, side * 3.0, 1.0, 0.5, WHITE});
            head.decals.push_back({0, 2.0, side * 2.0, 1.0, 0.5, BLACK});
        }
    } else {
        for (double side : {-1.0, 1.0}) head.decals.push_back({0, 2.0, side * 2.5, 0.3, 1.0, DARK});
        if (eyes == Eyes::Happy)
            for (double side : {-1.0, 1.0}) head.decals.push_back({0, 0.6, side * 2.6, 0.5, 0.9, BLUSH});
    }
    p.boxes.push_back(head);
    // The head's back face is buried in the body except for the strip above the body line: a thin cap box
    // flush with the back carries that strip, so from behind the head is not a hollow box.
    p.boxes.push_back({{0.3, 3, 0}, {0.6, 2, 8}, PINK, (1u << 0) | (1u << 3), {}});
    Box sn{{8 + 0.5 * snout, -1.5, 0}, {1 * snout, 3 * snout, 4 * snout}, SNOUT, 1u << 1, {}};
    sn.decals.push_back({0, 0.5 * snout, -1.2 * snout, 0.45, 0.45, DARK});
    sn.decals.push_back({0, 0.5 * snout, 1.2 * snout, 0.45, 0.45, DARK});
    p.boxes.push_back(sn);
    return p;
}

// Where a prop stands for a pig that will face `deskYaw`: a little ahead and to the side the camera sees. The
// pet fixes this in the world when work starts, so the prop stays put while the pig turns toward it.
inline void propPlacement(Prop prop, double deskYaw, double& x, double& z, double& yaw) {
    double side = std::cos(deskYaw * DEG) >= 0 ? 1 : -1;
    double lx = prop == Prop::Book ? 13 : 14, lz = prop == Prop::Book ? 4 * side : 4.5 * side;
    double turn = prop == Prop::Laptop || prop == Prop::Terminal ? 25 * side : prop == Prop::Book ? 15 * side : 0;
    V3 w = Xf::rotY(-deskYaw).dir({lx, 0, lz});
    x = w.x;
    z = w.z;
    yaw = deskYaw + turn;
}

// Assemble the pig and its prop in world space (yaw applied), ground at y = 0.
inline std::vector<Part> assemble(const Frame& f, const Rig& r) {
    std::vector<Part> parts;
    Xf world = Xf::rotY(-(f.yaw + r.yaw)) * Xf::trans({0, r.lift, 0});
    // Body: 16 long, 8 tall, 10 wide, bottom at y = 6 (legs are 6). Pitch about the rear-bottom edge.
    // Squash keeps the feet on the ground: the body scales about its bottom, hips move with it.
    double sy = r.scaleY, sxz = r.scaleXZ;
    // Pivot: the rear-bottom corner on the ground, the body's centre in the air (blended while easing).
    V3 pivot{-8 * sxz * (1 - r.centred), 4 * sy * r.centred, 0};
    Xf body = world * Xf::trans({pivot.x + r.bodyX, 6 + r.bodyY + pivot.y, 0}) * Xf::rotZ(r.bodyPitch) * Xf::rotX(r.bodyRoll) *
              Xf::trans({-pivot.x, -pivot.y, 0});
    // The costume's pieces ride on their parts; on the body they squash and stretch with it.
    auto dress = [&](Part& p, Anchor at) {
        if (!f.costume) return;
        for (const Piece& pc : f.costume->pieces) {
            if (pc.at != at) continue;
            Box bx = pc.box;
            if (at == Anchor::Body) { bx.c = {bx.c.x * sxz, bx.c.y * sy, bx.c.z * sxz}; bx.s = {bx.s.x * sxz, bx.s.y * sy, bx.s.z * sxz}; }
            p.boxes.push_back(bx);
        }
    };
    Part b;
    b.xf = body;
    b.boxes.push_back({{0, 4 * sy, 0}, {16 * sxz, 8 * sy, 10 * sxz}, PINK, 0, {}});
    dress(b, Anchor::Body);
    parts.push_back(b);
    // Legs hang from hips at the body's bottom corners.
    const V3 hips[4] = {{6, 0, -3}, {6, 0, 3}, {-6, 0, -3}, {-6, 0, 3}};
    for (int i = 0; i < 4; ++i) {
        Part l;
        l.xf = body * Xf::trans(hips[i] * sxz) * Xf::rotZ(r.leg[i] - r.bodyPitch) * Xf::rotX(r.splay[i]);
        Box lb = legBox();
        // A roughly vertical leg has its top inside the body; the painter's sort would still paint that top over
        // the body's side, so it is hidden until the leg swings out far enough for the top to actually show.
        if (std::fabs(r.leg[i]) < 28 && std::fabs(r.splay[i]) < 14) lb.hidden |= 1u << 2;
        l.boxes.push_back(lb);
        dress(l, (Anchor)((int)Anchor::LegFL + i));
        parts.push_back(l);
    }
    // Head pivots at the neck: front-top of the body, slightly inside it.
    Part h = headPart(f.pose == Pose::Sleep ? Eyes::Closed : f.eyes, r.snout);  // asleep is asleep
    h.xf = body * Xf::trans({6 * sxz + r.neck, 6 * sy, 0}) * Xf::rotY(-r.headYaw) * Xf::rotZ(-r.headPitch) * Xf::rotX(r.headRoll);
    dress(h, Anchor::Head);
    parts.push_back(h);
    // Tail nub at the rear top, wagging.
    Part tl;
    tl.xf = body * Xf::trans({-8 * sxz, 6.5 * sy, 0}) * Xf::rotY(r.tail);
    tl.boxes.push_back({{-1, 0, 0}, {2, 1.2, 1.2}, PINK, 1u << 0, {}});
    tl.boxes.push_back({{-2.2, 0.5, 0.5}, {1.1, 1.1, 1.1}, SNOUT, 0, {}});
    dress(tl, Anchor::Tail);
    parts.push_back(tl);

    // Props sit on the ground in front of the pig, a little to the side the camera sees (the pig faces into the
    // screen at its desk, so straight ahead would be hidden behind it).
    Part p;
    int fl = f.frame & 1;
    Xf propXf = Xf::trans({f.propX, 0, f.propZ}) * Xf::rotY(-f.propYaw);
    switch (f.prop) {
        case Prop::Laptop:
        case Prop::Terminal: {
            bool term = f.prop == Prop::Terminal;
            // The pig faces into the screen at its desk, so the laptop sits straight ahead, screen toward the pig
            // (which, from behind the pig, means toward you).
            p.xf = propXf;
            p.boxes.push_back({{0, 0.5, 0}, {7, 1.0, 9}, SILVER, 0, {}});
            Box screen{{0, 3.5, 0}, {0.6, 7, 9}, SILVER, 0, {}};
            // On the lid's -x face u runs sideways (z) and v runs up (y): decals are (centre u, centre v, half w, half h).
            screen.decals.push_back({1, 0, 0, 4.0, 3.0, term ? TERM : SCREEN});
            for (int i = 0; i < 4; ++i)
                screen.decals.push_back({1, -1.0 + ((i + fl) % 2) * 0.7, 2.0 - i * 1.2, 2.4 - i * 0.4, 0.3, term ? TERMTXT : INK});
            Part s;  // the lid hinges at the far edge and leans away from the pig; its screen faces the pig
            s.xf = p.xf * Xf::trans({3.2, 1.0, 0}) * Xf::rotZ(-16);
            s.boxes.push_back(screen);
            parts.push_back(p);
            parts.push_back(s);
            break;
        }
        case Prop::Book: {
            p.xf = propXf;
            Box book{{0, 1.0, 0}, {8, 2.0, 10}, PAGE, 0, {}};
            book.decals.push_back({2, 0, 0, 4.8, 3.9, COVER});
            book.decals.push_back({2, 0, 0, 3.6, 2.7, fl ? PAGE : COVER});
            book.decals.push_back({5, 0, 0, 0.9, 4, COVER});
            p.boxes.push_back(book);
            parts.push_back(p);
            break;
        }
        case Prop::Globe: {
            p.xf = propXf;
            p.boxes.push_back({{0, 0.7, 0}, {4, 1.4, 4}, STAND, 0, {}});
            p.spheres.push_back({{0, 5.8 + 0.15 * std::sin(f.t * 3), 0}, 4.4, SEA, f.t * 0.9});
            parts.push_back(p);
            break;
        }
        default: break;
    }
    if (f.pose == Pose::Eat) {
        Part bowl;
        bowl.xf = world * Xf::trans({17, 0, 0});
        Box bb{{0, 1, 0}, {6, 2, 6}, BOWL, 0, {}};
        double left = 2.3 * (1 - 0.85 * std::clamp(f.progress, 0.0, 1.0));  // the kibble goes down as it eats
        bb.decals.push_back({2, 0, 0, left, left, FOOD});
        bowl.boxes.push_back(bb);
        parts.push_back(bowl);
    }
    return parts;
}

// ---- camera and drawing --------------------------------------------------------------------------------
//
// Faces are rasterized into a small supersampled buffer with a per-pixel depth test, then composited onto the
// cairo target. Boxes that cut through each other (a turned head in the body, legs in the belly) resolve
// correctly per pixel, which a painter's sort of whole faces cannot do. Edges are darkened in the rasterizer
// so the blocks keep their crisp outlines.

constexpr double CAM_DIST = 110, CAM_ELEV = 16, UNIT = 3.6;  // elevation in degrees, UNIT = screen px per model unit
constexpr int SS = 2;                                          // supersampling factor
inline const V3 LIGHT = norm({-0.35, 0.9, 0.55});

struct Cam {
    Xf view;  // world → camera-aligned (camera looks down -z from z = CAM_DIST)
    double cx, baseY, unit;
    V3 to(V3 w) const { return view.pt(w); }
    void project(V3 c, double& sx, double& sy) const {
        double k = CAM_DIST / (CAM_DIST - c.z);
        sx = cx + c.x * k * unit;
        sy = baseY - c.y * k * unit;
    }
};

struct Poly {
    double x[4], y[4], z[4];  // screen position and camera depth per corner (bigger z = closer)
    RGB col;
    bool decal;
    int cellsU = 0, cellsV = 0, seed = 0;  // texel grid of the face (one cell per model pixel) for the skin noise
    double sphereR = 0, sphereZ = 0, spin = 0;  // sphereR > 0: a globe centred at (x[0], y[0])
};

inline RGB shade(RGB c, V3 n, RGB tint = {1, 1, 1}) {
    double l = 0.55 + 0.45 * std::max(0.0, dot(n, LIGHT));
    return {c.r * l * tint.r, c.g * l * tint.g, c.b * l * tint.b};
}

inline void emitBox(const Box& b, const Xf& xf, const Cam& cam, std::vector<Poly>& out, RGB tint = {1, 1, 1}) {
    for (int fi = 0; fi < 6; ++fi) {
        if (b.hidden & (1u << fi)) continue;
        const FaceAxes& f = FACES[fi];
        double hn = half(b, f.n), hu = half(b, f.u), hv = half(b, f.v);
        V3 nW = xf.dir(f.n);
        V3 centreC = cam.to(xf.pt(b.c + f.n * hn));
        if (dot(cam.view.dir(nW), V3{0, 0, CAM_DIST} - centreC) <= 0) continue;  // back face
        RGB col = shade(b.col, nW, tint);
        auto quad = [&](double cu, double cv, double qu, double qv, RGB c, bool decal) {
            Poly p{};
            p.cellsU = std::max(1, (int)std::lround(qu * 2));
            p.cellsV = std::max(1, (int)std::lround(qv * 2));
            p.seed = (int)(b.c.x * 3 + b.c.y * 7 + b.c.z * 5 + b.s.x * 11 + b.s.y * 13 + fi * 17) & 0x7fff;
            const double su[4] = {-1, 1, 1, -1}, sv[4] = {-1, -1, 1, 1};
            for (int i = 0; i < 4; ++i) {
                V3 local = b.c + f.n * (hn + (decal ? 0.05 : 0)) + f.u * (cu + su[i] * qu) + f.v * (cv + sv[i] * qv);
                V3 c3 = cam.to(xf.pt(local));
                cam.project(c3, p.x[i], p.y[i]);
                p.z[i] = c3.z + (decal ? 0.08 : 0);  // decals always win against the face they sit on
            }
            p.col = c;
            p.decal = decal;
            out.push_back(p);
        };
        quad(0, 0, hu, hv, col, false);
        for (const Decal& d : b.decals)
            if (d.face == fi) quad(d.cu, d.cv, d.hu, d.hv, shade(d.col, nW, tint), true);
    }
}

inline void emitSphere(const Sphere& s, const Xf& xf, const Cam& cam, std::vector<Poly>& out) {
    V3 c = cam.to(xf.pt(s.c));
    Poly p{};
    cam.project(c, p.x[0], p.y[0]);
    p.sphereR = s.r * CAM_DIST / (CAM_DIST - c.z) * cam.unit;
    p.sphereZ = c.z;
    p.spin = s.spin;
    p.col = s.col;
    out.push_back(p);
}

// The pixel buffer: premultiplied ARGB for cairo, plus a depth per pixel. Reused between frames.
struct Raster {
    int w = 0, h = 0;
    std::vector<uint32_t> px;
    std::vector<float> depth;
    void reset(int W, int H) {
        if (W != w || H != h) { w = W; h = H; px.assign(w * h, 0); depth.assign(w * h, -1e9f); }
        else { std::fill(px.begin(), px.end(), 0); std::fill(depth.begin(), depth.end(), -1e9f); }
    }
    void put(int x, int y, float z, RGB c) {
        if (x < 0 || y < 0 || x >= w || y >= h) return;
        int i = y * w + x;
        if (z <= depth[i]) return;
        depth[i] = z;
        px[i] = 0xFF000000u | ((uint32_t)std::clamp(c.r * 255 + 0.5, 0.0, 255.0) << 16) |
                ((uint32_t)std::clamp(c.g * 255 + 0.5, 0.0, 255.0) << 8) | (uint32_t)std::clamp(c.b * 255 + 0.5, 0.0, 255.0);
    }
};

inline double edgeDist(double px, double py, double ax, double ay, double bx, double by) {
    double dx = bx - ax, dy = by - ay, l2 = dx * dx + dy * dy;
    double t = l2 > 0 ? std::clamp(((px - ax) * dx + (py - ay) * dy) / l2, 0.0, 1.0) : 0;
    return std::hypot(px - (ax + t * dx), py - (ay + t * dy));
}

inline void rasterQuad(Raster& R, const Poly& p, double sx, double sy) {
    double X[4], Y[4];
    for (int i = 0; i < 4; ++i) { X[i] = p.x[i] * SS - sx; Y[i] = p.y[i] * SS - sy; }
    int x0 = (int)std::floor(std::min({X[0], X[1], X[2], X[3]})), x1 = (int)std::ceil(std::max({X[0], X[1], X[2], X[3]}));
    int y0 = (int)std::floor(std::min({Y[0], Y[1], Y[2], Y[3]})), y1 = (int)std::ceil(std::max({Y[0], Y[1], Y[2], Y[3]}));
    x0 = std::max(x0, 0); y0 = std::max(y0, 0); x1 = std::min(x1, R.w - 1); y1 = std::min(y1, R.h - 1);
    if (x0 > x1 || y0 > y1) return;
    const double edgeW = 0.9 * SS;
    RGB dark{p.col.r * 0.55, p.col.g * 0.55, p.col.b * 0.55};
    for (int tri = 0; tri < 2; ++tri) {
        int ia = 0, ib = tri ? 2 : 1, ic = tri ? 3 : 2;
        double ax = X[ia], ay = Y[ia], bx = X[ib], by = Y[ib], cx = X[ic], cy = Y[ic];
        double det = (bx - ax) * (cy - ay) - (cx - ax) * (by - ay);
        if (std::fabs(det) < 1e-9) continue;
        for (int y = y0; y <= y1; ++y)
            for (int x = x0; x <= x1; ++x) {
                double qx = x + 0.5, qy = y + 0.5;
                double l1 = ((bx - qx) * (cy - qy) - (cx - qx) * (by - qy)) / det;
                double l2 = ((cx - qx) * (ay - qy) - (ax - qx) * (cy - qy)) / det;
                double l3 = 1 - l1 - l2;
                if (l1 < 0 || l2 < 0 || l3 < 0) continue;
                double z = l1 * p.z[ia] + l2 * p.z[ib] + l3 * p.z[ic];
                RGB c = p.col;
                if (!p.decal) {
                    // A faint per-texel tone, fixed to the surface (Minecraft skins are 1 px per model unit).
                    const double U[4] = {0, 1, 1, 0}, V[4] = {0, 0, 1, 1};
                    double u = l1 * U[ia] + l2 * U[ib] + l3 * U[ic], v = l1 * V[ia] + l2 * V[ib] + l3 * V[ic];
                    int cu = std::clamp((int)(u * p.cellsU), 0, p.cellsU - 1), cv = std::clamp((int)(v * p.cellsV), 0, p.cellsV - 1);
                    unsigned h = (unsigned)(cu * 73856093) ^ (unsigned)(cv * 19349663) ^ (unsigned)(p.seed * 83492791);
                    h ^= h >> 13; h *= 0x5bd1e995u; h ^= h >> 15;
                    double tone = 1 + ((h & 255) / 255.0 - 0.5) * 0.07;
                    c = {c.r * tone, c.g * tone, c.b * tone};
                    double d = 1e9;
                    for (int e = 0; e < 4; ++e) d = std::min(d, edgeDist(qx, qy, X[e], Y[e], X[(e + 1) % 4], Y[(e + 1) % 4]));
                    if (d < edgeW) c = dark;
                }
                R.put(x, y, (float)z, c);
            }
    }
}

inline void rasterSphere(Raster& R, const Poly& p, double sx, double sy) {
    double cx = p.x[0] * SS - sx, cy = p.y[0] * SS - sy, r = p.sphereR * SS;
    int x0 = std::max(0, (int)std::floor(cx - r)), x1 = std::min(R.w - 1, (int)std::ceil(cx + r));
    int y0 = std::max(0, (int)std::floor(cy - r)), y1 = std::min(R.h - 1, (int)std::ceil(cy + r));
    for (int y = y0; y <= y1; ++y)
        for (int x = x0; x <= x1; ++x) {
            double dx = (x + 0.5 - cx) / r, dy = (y + 0.5 - cy) / r, d2 = dx * dx + dy * dy;
            if (d2 > 1) continue;
            double nz = std::sqrt(1 - d2);
            V3 n{dx, -dy, nz};  // surface normal, camera space
            double lon = std::atan2(dx, nz) + p.spin, lat = -dy;
            bool land = std::sin(lon * 2.0 + lat * 3.0) > 0.55 || std::cos(lon * 3.1 - lat * 2.0) > 0.8;
            RGB base = land ? LAND : p.col;
            RGB c = shade(base, norm(n));
            double rim = 1 - nz;
            c = {c.r * (1 - 0.25 * rim), c.g * (1 - 0.25 * rim), c.b * (1 - 0.25 * rim)};
            R.put(x, y, (float)(p.sphereZ + nz * p.sphereR / 3.6 * 0.3), c);
        }
}

struct HeadPos {
    double cx, top;  // screen: centre x of the head and the y of its top, for bubbles and particles
};

// Draw the pig with its feet on baseY, centred on cx. Returns where the head ended up on screen.
inline HeadPos render(cairo_t* cr, const Frame& f, const Rig& rig, double cx, double baseY, double scale = 1.0) {
    Cam cam;
    cam.view = Xf::rotX(CAM_ELEV);
    cam.cx = cx;
    cam.baseY = baseY;
    cam.unit = UNIT * scale * (1 + 0.95 * f.approach);  // coming up to the glass: bigger, same feet
    std::vector<Part> parts = assemble(f, rig);
    std::vector<Poly> polys;
    polys.reserve(parts.size() * 8);
    for (const Part& p : parts) {
        for (const Box& b : p.boxes) emitBox(b, p.xf, cam, polys, f.tint);
        for (const Sphere& s : p.spheres) emitSphere(s, p.xf, cam, polys);
    }

    if (rig.shadow > 0.02) {  // soft ground shadow, under everything; shrinks as the pig lifts off
        cairo_new_path(cr);
        Xf w = Xf::rotY(-(f.yaw + rig.yaw));
        double k = 1 / (1 + rig.lift * 0.12);
        for (int i = 0; i < 16; ++i) {
            double a = i * 2 * M_PI / 16, sx, sy;
            cam.project(cam.to(w.pt({std::cos(a) * 10.5 * k + rig.bodyX, 0, std::sin(a) * 7 * k})), sx, sy);
            if (i == 0) cairo_move_to(cr, sx, sy);
            else cairo_line_to(cr, sx, sy);
        }
        cairo_close_path(cr);
        cairo_set_source_rgba(cr, 0, 0, 0, 0.16 * rig.shadow);
        cairo_fill(cr);
    }

    // The drawn region, in target pixels; the buffer covers just that.
    double minx = 1e9, miny = 1e9, maxx = -1e9, maxy = -1e9;
    for (const Poly& p : polys) {
        if (p.sphereR > 0) {
            minx = std::min(minx, p.x[0] - p.sphereR); maxx = std::max(maxx, p.x[0] + p.sphereR);
            miny = std::min(miny, p.y[0] - p.sphereR); maxy = std::max(maxy, p.y[0] + p.sphereR);
            continue;
        }
        for (int i = 0; i < 4; ++i) {
            minx = std::min(minx, p.x[i]); maxx = std::max(maxx, p.x[i]);
            miny = std::min(miny, p.y[i]); maxy = std::max(maxy, p.y[i]);
        }
    }
    if (polys.empty()) return {cx, baseY};
    int ox = (int)std::floor(minx) - 2, oy = (int)std::floor(miny) - 2;
    int W = (int)std::ceil(maxx) + 2 - ox, H = (int)std::ceil(maxy) + 2 - oy;
    static Raster R;
    R.reset(W * SS, H * SS);
    for (const Poly& p : polys) {
        if (p.sphereR > 0) rasterSphere(R, p, ox * SS, oy * SS);
        else rasterQuad(R, p, ox * SS, oy * SS);
    }
    cairo_surface_t* img = cairo_image_surface_create_for_data((unsigned char*)R.px.data(), CAIRO_FORMAT_ARGB32, R.w, R.h, R.w * 4);
    cairo_save(cr);
    cairo_translate(cr, ox, oy);
    cairo_scale(cr, 1.0 / SS, 1.0 / SS);
    cairo_set_source_surface(cr, img, 0, 0);
    cairo_pattern_set_filter(cairo_get_source(cr), CAIRO_FILTER_GOOD);
    cairo_paint(cr);
    cairo_restore(cr);
    cairo_surface_destroy(img);

    // The head part is the sixth (body, four legs, head); its top-centre in world space, hat included.
    const Part& head = parts[5];
    double hx, hy;
    cam.project(cam.to(head.xf.pt({4, 4 + (f.costume ? f.costume->hatHeight : 0), 0})), hx, hy);
    return {hx, hy};
}

// Convenience for still renders (the sprite sheet): a fresh animator settled into the pose.
inline HeadPos renderStill(cairo_t* cr, const Frame& f, double cx, double baseY, double scale = 1.0, Action act = Action::Idle, double actP = 0.5) {
    Animator a;
    Frame g = f;
    propPlacement(g.prop, g.yaw, g.propX, g.propZ, g.propYaw);
    for (int i = 0; i < 60; ++i) { g.t = f.t - (60 - i) / 30.0; a.update(g); }
    g.t = f.t;
    Rig r = a.update(g);
    if (act != Action::Idle) r = r + actionRig(act, actP, g.t);
    return render(cr, g, r, cx, baseY, scale);
}

}  // namespace art3d
