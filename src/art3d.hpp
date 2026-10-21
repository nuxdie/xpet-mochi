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
// The Fresh Animations skin's accents (FreshLX's resource pack): the snout is the face's own pink with a darker middle
// row and the nostrils at its outer edges, two dark toes on each hoof, a red ridge down the back.
inline const RGB SNOUT_FACE = rgb(0xF19E98), SNOUT_MID = rgb(0xE67E7B), NOSTRIL = rgb(0x894746),
                 RIDGE = rgb(0xC6615A), RIDGE_DOT = rgb(0xBE504D);

// ---- what the pet tells the animator each frame -------------------------------------------------------

enum class Action { Idle, Sniff, Stretch, Shake, LookAround, Hop, Struggle, Nuzzle, HeadShake, Land, Jump, Wiggle, Scratch, Sulk, Cheer, Snuffle, Rouse, Bedtime, Perk, Paw, Dream, Stir, Chase, Sneeze, Ride, Leap };

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
    {"jump", Action::Jump, 0.75},     {"wiggle", Action::Wiggle, 0.9},       {"scratch", Action::Scratch, 1.7},
    {"sulk", Action::Sulk, 3.5},       {"cheer", Action::Cheer, 2.0},         {"snuffle", Action::Snuffle, 1.4},
    {"rouse", Action::Rouse, 3.0},     {"bedtime", Action::Bedtime, 3.2},   {"perk", Action::Perk, 2.2},
    {"paw", Action::Paw, 2.0},         {"dream", Action::Dream, 3.4},         {"stir", Action::Stir, 2.8},
    {"chase", Action::Chase, 3.0},     {"sneeze", Action::Sneeze, 2.0},       {"ride", Action::Ride, 2.0},
    {"leap", Action::Leap, 1.3},
};
// Sneeze: two breaths in, the sneeze at SNEEZE_CHOO, then a shake of the head. The eyes squeeze shut from SNEEZE_SHUT
// to SNEEZE_OPEN (the pet closes them, the rig can't).
constexpr double SNEEZE_CHOO = 0.52, SNEEZE_SHUT = 0.38, SNEEZE_OPEN = 0.66, SNEEZE_SECS = 2.0;
// Chase: round and round after its own tail. It spins from CHASE_SPIN to CHASE_STOP of the way through (the pet keeps
// its head from tracking anything meanwhile); before is the moment it spots the tail, after is the dizzy wobble.
constexpr double CHASE_SPIN = 0.2, CHASE_STOP = 0.72, CHASE_SECS = 3.0;
// Dream plays only while it sleeps (the Sleep pose under it): how long one dream lasts.
constexpr double DREAM_SECS = 3.4;
// Stir, asleep too: you came back while it napped, and it half-hears you.
constexpr double STIR_SECS = 2.8;
// Rouse keeps its eyes shut through the yawn: the first this much of it (the pet closes them, the rig can't).
constexpr double ROUSE_YAWN = 0.4;
// Bedtime shuts them once the head goes down, from this far in.
constexpr double BEDTIME_EYES = 0.74;
// Perk: the head is up and it has turned to look by here (the pet points the eyes at you from then on), and from
// PERK_GLAD it has recognised you (happy eyes, the wag starts in the rig at the same moment).
constexpr double PERK_LOOK = 0.1, PERK_GLAD = 0.3;
// Cheer: the first this much is a crouch while the pet turns it round to face you (at the desk it faces the laptop).
constexpr double CHEER_TURN = 0.15, CHEER_SECS = 2.0;
// Jump starts with a crouch on the ground: it leaves the floor this far in (0.3 s of the 0.75).
constexpr double JUMP_CROUCH = 0.4, JUMP_SECS = 0.75;
// Leap: the same for a big climb (LEAP_RISE px up or more): 0.65 s of crouch and pounce wiggle before it goes.
constexpr double LEAP_CROUCH = 0.5, LEAP_SECS = 1.3, LEAP_RISE = 200;
inline double takeoffAt(Action a) { return a == Action::Leap ? LEAP_CROUCH : JUMP_CROUCH; }
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
    double ear = 0;          // both ears flung up and out from where they hang (deg; < 0 lies them flat)
    double earSwing = 0;     // both ears swung forward (deg, > 0) or back
    double earFlick = 0;     // one ear alone up: > 0 the +z one, < 0 the -z one (a twitch, a scratch)
    double legLift[4] = {0, 0, 0, 0};  // a hip raised (units): that foot picked up off the ground
    double pupilX = 0, pupilY = 0;     // the pupils in the eye whites, -1..1: > 0 toward +z, and up
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
    a.ear += b.ear; a.earSwing += b.earSwing; a.earFlick += b.earFlick;
    for (int i = 0; i < 4; ++i) a.legLift[i] += b.legLift[i];
    a.pupilX += b.pupilX; a.pupilY += b.pupilY;
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
    ease(r.ear, to.ear, k); ease(r.earSwing, to.earSwing, k); ease(r.earFlick, to.earFlick, k);
    for (int i = 0; i < 4; ++i) ease(r.legLift[i], to.legLift[i], k);
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
            r.ear = -12;  // lying flat against the head
            break;
        case Pose::Dangle:  // held up by the scruff: nose up, legs hang
            r.bodyPitch = 12;
            for (double& l : r.leg) l = -22;
            r.leg[0] += 8; r.leg[3] += 8;
            r.bodyY = 1.5;
            r.shadow = 0;
            r.centred = 1;
            r.ear = 25;
            break;
        case Pose::Fall:
            r.centred = 1;
            for (int i = 0; i < 4; ++i) r.splay[i] = (i % 2 ? 1 : -1) * 32;
            r.leg[0] = r.leg[1] = -30;
            r.leg[2] = r.leg[3] = 30;
            r.bodyPitch = std::clamp(f.vy * 1.6, -28.0, 28.0) * (f.thrown ? 0 : 1);
            r.shadow = 0;
            r.ear = 70;  // the air flings them up
            break;
    }
    return r;
}

// Now and then one ear flicks while it stands about: a quick up-and-down every ~7 s, the two ears in turn.
inline double earTwitch(double t) {
    constexpr double every = 6.7, len = 0.32;
    double q = std::fmod(t, every);
    if (q > len) return 0;
    double side = (long)(t / every) % 2 ? 1 : -1;
    return side * 28 * std::sin(q / len * M_PI);
}

// The things that keep moving while a pose holds. Added on top of the eased base every frame.
struct Motion {
    double gait = 0;  // walking phase, advanced by ground speed so the hooves match the ground
    double spin = 0;  // tumble angle when thrown
    double prevYaw = 1e9, turnPh = 0, turning = 0;  // stepping round on the spot while it turns
    Rig at(const Frame& f, double dt) {
        Rig m;
        double t = f.t;
        // Turning where it stands, it steps round on its feet instead of sliding (Fresh Animations' pigs do): a step
        // per ~40 degrees of turn, diagonal pairs in turn, each foot lifted as it goes.
        double dyaw = prevYaw > 1e8 ? 0 : std::remainder(f.yaw - prevYaw, 360.0);
        prevYaw = f.yaw;
        double rate = dt > 0 ? std::fabs(dyaw) / dt : 0;
        ease(turning, f.pose == Pose::Stand ? std::clamp(rate / 90, 0.0, 1.0) : 0.0, 0.25);
        turnPh += std::fabs(dyaw) * DEG * 4.5;
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
                auto swing = [&](double ph) { return std::sin(ph + 0.35 * std::sin(ph)) * (trot ? 42 : 34); };
                double a = swing(gait), b = swing(gait + M_PI);
                m.leg[0] = a; m.leg[3] = a; m.leg[1] = b; m.leg[2] = b;
                // Each foot comes up off the ground while it swings forward and is planted for the push back.
                double la = std::max(0.0, std::cos(gait)) * (trot ? 1.6 : 1.0), lb = std::max(0.0, -std::cos(gait)) * (trot ? 1.6 : 1.0);
                m.legLift[0] = m.legLift[3] = la; m.legLift[1] = m.legLift[2] = lb;
                m.yaw = std::sin(gait - M_PI / 4) * 1.5;  // the body swings a little from side to side with the stride
                m.bodyY = std::fabs(std::sin(gait)) * (trot ? 1.0 : 0.45);
                m.bodyRoll = std::sin(gait) * (trot ? 3.5 : 2.5);
                m.bodyPitch = trot ? -3 : 0;  // leaning into it
                m.headPitch = std::sin(gait * 2 + 0.6) * (trot ? 6 : 4) + (trot ? -5 : 1) - m.bodyPitch;  // held level
                m.headYaw = std::sin(gait) * 3;  // sways with the shoulders
                m.headRoll = std::sin(gait) * -2;
                m.tail = std::sin(gait) * 25 + std::sin(gait * 2) * 8;
                m.ear = std::fabs(std::sin(gait)) * (trot ? 14 : 6);  // they bounce with each step
                m.earSwing = std::cos(gait * 2) * (trot ? 10 : 6) - (trot ? 8 : 0);
                break;
            }
            case Pose::Stand:
                m.scaleY = 1 + 0.012 * std::sin(t * 2.2);
                m.bodyRoll = 1.2 * std::sin(t * 0.37);  // slow shift of weight from one side to the other
                m.bodyX = 0.3 * std::sin(t * 0.37 + 1);
                m.headYaw = 14 * std::sin(t * 0.45) * std::sin(t * 0.17) * (1 - f.lookW);  // glances this way and that
                m.headPitch = 4 * std::sin(t * 0.31 + 2 * std::sin(t * 0.13));  // and the head drifts, never quite still
                m.headRoll = 3 * std::sin(t * 0.21 + 1);
                m.tail = std::sin(t * 1.3) * 14;
                m.earFlick = earTwitch(t);
                break;
            case Pose::Eat:
                m.headPitch = 6 + std::sin(t * 9) * 7;
                m.snout = 1 + 0.14 * std::fabs(std::sin(t * 18));
                m.tail = std::sin(t * 7) * 28;
                m.earSwing = 8 + 4 * std::sin(t * 9);  // flopping forward over the bowl with each bite
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
                    m.earFlick = earTwitch(t);
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
                m.ear = 8 * std::sin(t * 2.3 + 1);
                break;
            case Pose::Fall:
                if (f.thrown) {
                    m.bodyPitch = spin;
                    for (int i = 0; i < 4; ++i) m.leg[i] = std::sin(t * 25 + i) * 25;
                    m.ear = 20 * std::sin(t * 25);
                } else {
                    m.bodyPitch = std::sin(t * 8) * 6;
                    for (int i = 0; i < 4; ++i) m.leg[i] = std::sin(t * 12 + i * 1.5) * 8;
                    m.ear = 8 * std::sin(t * 13);  // fluttering in the wind
                }
                break;
        }
        if (turning > 0.01) {
            double s = std::sin(turnPh) * 16 * turning, l = std::fabs(std::sin(turnPh)) * 1.1 * turning;
            m.leg[0] += s; m.leg[3] += s; m.leg[1] -= s; m.leg[2] -= s;
            if (std::cos(turnPh) > 0) { m.legLift[0] += l; m.legLift[3] += l; } else { m.legLift[1] += l; m.legLift[2] += l; }
            m.bodyY += 0.25 * std::fabs(std::sin(turnPh)) * turning;
        }
        if (f.talking) { m.headPitch += std::sin(t * 11) * 2.5; m.snout += 0.06 * std::fabs(std::sin(t * 11)); }
        if (f.happy) { m.tail += std::sin(t * 14) * 30; m.bodyY += 0.3 * std::fabs(std::sin(t * 7)); }
        return m;
    }
};

// Riding a window that is being dragged: how a jolt looks. surge > 0 is the pig thrown toward its nose, side > 0 toward
// its +z flank (degrees of lean), heave > 0 thrown upward (< 0 pressed down). The pet runs the spring behind these
// (main.cpp rideStep), so the start of a drag rocks it back and the stop pitches it forward, then it finds its feet.
inline Rig rideRig(double surge, double side, double heave) {
    Rig r;
    double fwd = std::max(0.0, surge), back = std::max(0.0, -surge);
    // Thrown forward it tips onto its front hooves, rump up: pitched about the front feet (the body rises by what the
    // nose would sink), hind legs left reaching back. Thrown back it rocks onto its haunches, front hooves up.
    r.bodyPitch = -surge;
    r.bodyY = 16 * std::sin(fwd * DEG) + 5 * std::sin(std::fabs(side) * DEG);  // the low side stays on the floor
    r.leg[0] = r.leg[1] = 0.5 * fwd + 0.9 * back;
    r.leg[2] = r.leg[3] = -0.9 * fwd - 0.3 * back;
    r.bodyRoll = side;
    for (double& s : r.splay) s = 0.7 * std::fabs(side) + 0.6 * std::max(0.0, -heave);  // braced wide
    r.headPitch = 0.7 * surge;  // the head goes on a little further than the body, a beat behind
    r.headRoll = 0.5 * side;
    r.scaleY = 1 + 0.012 * heave;
    r.scaleXZ = 1 - 0.005 * heave;
    r.ear = 0.9 * std::fabs(surge) + 0.9 * std::fabs(side) + 1.5 * heave;
    r.earSwing = 1.6 * surge;
    r.tail = -2.2 * surge;
    return r;
}
// The spring behind rideRig: a stiff, well-damped one (one clear lean, a small swing back, still), as he likes his
// bounces. RIDE_SECS is the "ride" trick's length (the strip and the socket play a canned ride with it).
constexpr double RIDE_W = 8, RIDE_Z = 0.45, RIDE_SECS = 2.0;
// One jolt's lean t seconds after it, scaled so it peaks at `peak` degrees.
inline double rideJolt(double t, double peak) {
    if (t <= 0) return 0;
    double wd = RIDE_W * std::sqrt(1 - RIDE_Z * RIDE_Z), tp = std::atan2(wd, RIDE_Z * RIDE_W) / wd;
    double top = std::exp(-RIDE_Z * RIDE_W * tp) * std::sin(wd * tp);
    return peak * std::exp(-RIDE_Z * RIDE_W * t) * std::sin(wd * t) / top;
}

// Wind up, then spring: a crouch on the ground, a stretch on take-off. Up to c of the way through it is still on the
// ground (the pet holds the take-off till then): it sinks low, eyes up at where it's going, and a little rump wiggle
// while it gathers. Then the crouch lets go at once and the stretch snaps in as it leaves, held through the climb and
// eased out in the air. `big` 0 is a hop up to the next ledge (Jump); 1 is a leap up most of a window (Leap): it looks
// up first, sinks a good deal lower and longer, and the gathering turns into a proper pounce wiggle, rump swinging and
// hind hooves treading, before a bigger spring. `secs` keeps the snap of the release the same length in both.
inline Rig jumpRig(double p, double t, double c, double secs, double big) {
    Rig r;
    const double w = secs / JUMP_SECS;  // p-windows below are tuned for the Jump's length
    double crouch = smoothstep(p / (0.75 * c)) * (1 - smoothstep((p - c) / (0.07 / w)));
    double gather = smoothstep((p - 0.45 * c) / (0.3 * c)) * (1 - smoothstep((p - c) / (0.05 / w)));
    double look = smoothstep(p / (0.25 * c)) * (1 - smoothstep((p - c) / (0.07 / w)));  // eyes on the ledge first
    double s = smoothstep((p - c) / (0.06 / w)) * smoothstep((1 - p) / (0.5 * (1 - c)));
    double wig = std::sin(t * 26) * gather;
    r.scaleY = 1 - (0.22 + 0.1 * big) * crouch + (0.2 + 0.08 * big) * s;
    r.scaleXZ = 1 + (0.1 + 0.05 * big) * crouch - (0.08 + 0.03 * big) * s;
    r.bodyY = -(0.9 + 0.6 * big) * crouch;
    for (double& sp : r.splay) sp = (10 + 5 * big) * crouch;
    r.bodyRoll = (3 + 4 * big) * wig;
    r.bodyX = 0.35 * big * wig;  // the rump swings side to side
    r.leg[2] = (25 + 10 * big) * s + 12 * big * wig;  // treading: hind hooves in turn
    r.leg[3] = (25 + 10 * big) * s - 12 * big * wig;
    r.leg[0] = r.leg[1] = -(20 + 12 * big) * s;  // reaching for the ledge
    r.headPitch = -8 * crouch - 14 * big * look - (12 + 6 * big) * s;
    r.tail = 25 * crouch + (15 + 15 * big) * wig;
    r.earSwing = -10 * crouch - 14 * s;  // swept back by the take-off
    r.ear = -10 * s - 6 * big * crouch;  // and laid flat while it aims
    return r;
}

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
            r.earSwing = 12 * e;  // flopping forward over its nose
            break;
        case Action::Stretch:  // front legs out, rump up, nose up: a big cat-like stretch
            r.leg[0] = r.leg[1] = 55 * e;
            r.bodyPitch = -16 * e;
            r.bodyY = 2.4 * e;
            r.headPitch = -22 * e;
            r.scaleXZ = 1 + 0.04 * e;
            r.tail = 20 * e;
            r.ear = -8 * e;
            r.earSwing = -18 * e;  // pressed back
            break;
        case Action::Shake:  // a wet-dog shake
            r.bodyRoll = 13 * std::sin(t * 32) * e;
            r.headRoll = 22 * std::sin(t * 32 + 1.2) * e;
            r.headYaw = 8 * std::sin(t * 16) * e;
            r.tail = 40 * std::sin(t * 30) * e;
            r.ear = (25 + 30 * std::sin(t * 32 + 2.2)) * e;  // flapping, a beat behind the head
            r.earSwing = 14 * std::sin(t * 16 + 1) * e;
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
            r.ear = -18 * std::sin(p * 2 * M_PI);  // pressed down going up, flung up coming down
            break;
        case Action::Struggle:  // kicking in your hand
            for (int i = 0; i < 4; ++i) r.leg[i] = 28 * std::sin(t * 26 + i * 1.6) * e;
            r.bodyRoll = 9 * std::sin(t * 20) * e;
            r.headYaw = 12 * std::sin(t * 18) * e;
            r.ear = (15 + 20 * std::sin(t * 20 + 1)) * e;
            break;
        case Action::Nuzzle:  // leans into the hand
            r.headPitch = -26 * e;
            r.headYaw = 28 * e;
            r.headRoll = 10 * e;
            r.bodyY = 0.5 * e;
            r.tail = 25 * std::sin(t * 12) * e;
            r.ear = -10 * e;
            r.earSwing = -14 * e;  // laid back, content
            break;
        case Action::HeadShake:
            r.headYaw = 32 * std::sin(t * 19) * e;
            r.ear = 22 * std::fabs(std::sin(t * 19 + 0.8)) * e;  // flung out at each turn
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
            r.ear = -22 * g + 10 * up;  // they slap down on impact and bounce up with the rebound
            break;
        }
        case Action::Jump: return jumpRig(p, t, JUMP_CROUCH, JUMP_SECS, 0);
        case Action::Leap: return jumpRig(p, t, LEAP_CROUCH, LEAP_SECS, 1);
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
            r.ear = -14 * e;  // drooping
            r.earSwing = 10 * e;
            break;
        case Action::Cheer: {  // "yay!": rears back, both front hooves up and pumping, two bounces (a big job finished)
            // It mostly plays at the desk, sitting at the laptop, where the pet first turns it round to face you;
            // the first CHEER_TURN of it is a small crouch while it turns, so the rear-up reads as one beat. Standing,
            // the same numbers make it rear up on its hind legs. Everything lets go over the last fifth.
            double dip = env(p / CHEER_TURN);
            double up = smoothstep((p - 0.7 * CHEER_TURN) / 0.14) * smoothstep((1 - p) / 0.2);
            double q = std::clamp((p - 0.28) / 0.52, 0.0, 1.0);  // the two bounces
            double hop = std::fabs(std::sin(q * 2 * M_PI));
            double pump = std::sin(t * 15) * smoothstep((p - 0.25) / 0.1) * up;  // hooves waving in turn
            r.bodyPitch = 24 * up;                       // back onto its rump, chest up
            r.bodyY = -0.5 * dip;
            r.scaleY = 1 - 0.1 * dip + 0.1 * std::sin(q * 4 * M_PI) * up;  // squash before, stretch going up
            r.scaleXZ = 1 + 0.05 * dip;
            r.leg[0] = 105 * up + 20 * pump;             // hooves high, left and right in turn
            r.leg[1] = 105 * up - 20 * pump;
            r.leg[2] = r.leg[3] = 10 * hop;
            r.lift = 6 * hop * up;
            r.headPitch = 8 * dip - 24 * up;             // nose to the sky
            r.headRoll = 7 * std::sin(t * 7.5) * up;     // a happy head-bob from side to side
            r.tail = 45 * std::sin(t * 16) * (dip + up);
            r.shadow = 1 - 0.4 * hop * up;
            r.ear = (20 + 12 * std::sin(t * 15)) * up;
            break;
        }
        case Action::Snuffle:  // sniffing at your finger: snout working, head pushed a little forward
            r.snout = 1 + 0.25 * std::fabs(std::sin(t * 24)) * e;
            r.neck = 1.2 * e;
            r.headPitch = -6 * e;
            r.headRoll = 5 * std::sin(t * 9) * e;
            r.earSwing = 8 * e;  // pricked forward, curious
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
            r.ear = -8 * bow + (25 + 30 * std::sin(t * 32 + 2.2)) * shake;  // up off the pillow
            r.earSwing = -18 * bow;
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
            r.ear = -12 * head;  // ends lying flat, as Sleep has them
            break;
        }
        case Action::Perk: {  // "oh, you're back": startled up out of a doze, a look, then glad to see you
            // Three beats: a quick dip (anticipation) and the head snaps up past level, the pig standing tall; a hold
            // while it looks (the pet turns the eyes to you); then recognition: a little head tilt, the tail going.
            // The turn toward you isn't here: it depends on which way the pig faces, so the pet does it (lookKind 2).
            double dip = env(p / 0.1);
            double up = smoothstep((p - 0.08) / 0.12) * smoothstep((1 - p) / 0.25);  // up fast, held, let go slowly
            double over = env((p - 0.14) / 0.16);                                      // past level, then settles
            double glad = smoothstep((p - PERK_GLAD) / 0.1) * smoothstep((1 - p) / 0.2);
            r.headPitch = 6 * dip - 22 * up - 8 * over;
            r.bodyY = -0.3 * dip + 0.3 * std::fabs(std::sin(t * 7)) * glad;  // the small happy bounce
            r.scaleY = 1 + 0.06 * up;    // up on its toes
            r.scaleXZ = 1 - 0.02 * up;
            r.bodyPitch = -4 * up;       // leaning forward, eager
            r.headRoll = 9 * env((p - PERK_GLAD) / 0.45);  // the quizzical tilt, then straight again
            r.tail = 42 * std::sin(t * 17) * glad;
            r.ear = 26 * up + 6 * std::sin(t * 7) * glad;  // pricked up
            r.earSwing = 10 * up;
            break;
        }
        case Action::Paw: {  // pawing at the ground with a front hoof, a couple of scrapes (Fresh Animations' idle)
            double e2 = smoothstep(p / 0.15) * smoothstep((1 - p) / 0.15);
            double scrape = std::sin(t * 7.5);
            r.leg[0] = (24 + 22 * scrape) * e2;
            r.legLift[0] = (2.0 + 1.0 * std::max(0.0, scrape)) * e2;
            r.bodyRoll = 3 * e2;  // the weight onto the other three
            r.bodyY = -0.2 * e2;
            r.headPitch = 14 * e2;  // watching the hoof
            r.headRoll = -6 * e2;
            r.headYaw = -8 * e2;
            r.earSwing = 8 * e2;
            r.tail = 12 * std::sin(t * 7.5) * e2;
            break;
        }
        case Action::Dream: {  // asleep and running after something in a dream: legs paddling, ears twitching
            // Made for the Sleep pose (legs stretched out flat, ears lying back). A sniff in its sleep first, then a
            // run that swells and fades, one muffled "woof" (the head jerks, the snout puffs) in the middle of it, and
            // a long breath out as it settles again. The legs swing mostly upward from where they lie, so the hooves
            // don't go through the floor. Eyes stay shut: the pose keeps them so.
            double sniff = env(p / 0.18);
            double run = smoothstep((p - 0.12) / 0.15) * smoothstep((0.8 - p) / 0.2);
            double woof = env((p - 0.5) / 0.09);
            double sigh = env((p - 0.76) / 0.24);
            double ph = t * 11;  // a little gallop, under 2 steps a second
            auto paddle = [&](double off, double amp) { return (0.35 * amp + amp * std::sin(ph + off)) * run; };
            r.leg[0] = paddle(0, 22);  r.leg[1] = paddle(M_PI, 22);  // the front pair in turn (mostly under the chin),
            r.leg[3] = -paddle(0.4, 32); r.leg[2] = -paddle(M_PI + 0.4, 32);  // the hind pair, the ones you see, a beat late
            for (int i = 0; i < 4; ++i)  // and each kicks out sideways as it comes forward
                r.splay[i] = (i % 2 ? 1 : -1) * 14 * std::max(0.0, std::sin(ph + (i == 1 || i == 2 ? M_PI : 0))) * run;
            r.bodyRoll = 2.5 * std::sin(ph) * run;
            r.headPitch = -5 * run - 10 * woof;  // the head lifts a little off the ground, jerks up once
            r.headRoll = 3 * std::sin(ph / 2) * run;
            r.snout = 1 + 0.2 * std::fabs(std::sin(t * 22)) * sniff + 0.25 * woof;
            r.ear = 12 * std::fabs(std::sin(ph)) * run + 16 * woof;  // bouncing with the run
            r.earFlick = 34 * std::sin(t * 8) * (sniff + 0.6 * run);  // one, then the other
            r.tail = 35 * std::sin(t * 17) * run;
            r.scaleY = 1 + 0.06 * sigh;
            r.scaleXZ = 1 + 0.02 * sigh;
            break;
        }
        case Action::Stir: {  // asleep, it half-hears you come back: one ear up, head up off its chin, then back down
            // Made for the Sleep pose, eyes shut throughout: it doesn't wake, it only listens. The ear goes first and
            // comes down last; between, the head lifts with a listening tilt, a sniff (it's you), a sleepy wag, and it
            // settles with the chin sinking a touch lower than before and one long breath out.
            double ear = smoothstep(p / 0.08) * smoothstep((0.9 - p) / 0.12);
            double lift = smoothstep((p - 0.12) / 0.14) * smoothstep((0.78 - p) / 0.16);
            double sniff = env((p - 0.38) / 0.24), wag = env((p - 0.35) / 0.4);
            double settle = env((p - 0.74) / 0.2), sigh = env((p - 0.72) / 0.28);
            r.earFlick = 50 * ear + 8 * std::sin(t * 14) * env((p - 0.42) / 0.16);  // up, a little twitch while it listens
            r.ear = 10 * lift;  // the other one stirs too
            r.earSwing = 10 * lift;
            r.headPitch = -20 * lift + 5 * settle;
            r.headRoll = -9 * lift;
            r.snout = 1 + 0.2 * std::fabs(std::sin(t * 22)) * sniff;
            r.tail = 24 * std::sin(t * 10) * wag;
            r.scaleY = 1 + 0.06 * sigh;
            r.scaleXZ = 1 + 0.02 * sigh;
            break;
        }
        case Action::Chase: {  // spots its own tail, crouches, spins twice round after it, stops a little dizzy
            // Three beats: the tail twitches and the head whips round to look at it; a quick crouch (anticipation); two
            // whole turns on the spot with the nose leading, stepping fast, leaning in, ears flung out by the spin; it
            // stops a touch past where it started, swings back, and the head wobbles while the world stops turning.
            // Ends at zero (two whole turns are no turn), so it hands back to standing without a pop.
            double notice = smoothstep(p / 0.08) * smoothstep((0.26 - p) / 0.08);
            double crouch = env((p - 0.11) / 0.11);
            double q = std::clamp((p - CHASE_SPIN) / (CHASE_STOP - CHASE_SPIN), 0.0, 1.0);
            double spinning = smoothstep(q * 4) * smoothstep((1 - q) * 4);
            double dizzy = smoothstep((p - CHASE_STOP) / 0.05) * smoothstep((1 - p) / 0.16);
            double step = smoothstep(q) * 2 * M_PI * 8;  // eight steps for the two turns
            r.yaw = 720 * smoothstep(q) + 16 * env((p - 0.66) / 0.18);  // carried a little past, then back
            double a = 24 * std::sin(step) * spinning, l = 1.4 * std::fabs(std::sin(step)) * spinning;
            r.leg[0] = r.leg[3] = a;
            r.leg[1] = r.leg[2] = -a;
            if (std::cos(step) > 0) { r.legLift[0] = r.legLift[3] = l; } else { r.legLift[1] = r.legLift[2] = l; }
            r.scaleY = 1 - 0.12 * crouch + 0.03 * spinning;
            r.scaleXZ = 1 + 0.06 * crouch;
            r.bodyY = -0.6 * crouch + 0.5 * std::fabs(std::sin(step)) * spinning;
            r.bodyRoll = -8 * spinning + 6 * std::sin(t * 7 + 1) * dizzy;  // leaning into the turn, then swaying
            for (int i = 0; i < 4; ++i) r.splay[i] = (i % 2 ? 1 : -1) * 14 * dizzy;  // legs braced wide: steady now
            r.headYaw = 65 * notice + 38 * spinning + 14 * std::sin(t * 3.5) * dizzy;  // looking back at the tail
            r.headPitch = 10 * notice + 6 * spinning + 6 * dizzy;
            r.headRoll = 19 * std::sin(t * 7) * dizzy;  // the wobble
            r.tail = 30 * std::sin(t * 20) * notice + 40 * std::sin(t * 18) * spinning + 25 * std::sin(t * 12) * dizzy;
            r.ear = 15 * notice + 32 * spinning + 8 * std::sin(t * 7 + 2) * dizzy;
            r.earSwing = 10 * notice - 14 * spinning;
            break;
        }
        case Action::Sneeze: {  // ah... ah... CHOO, and a shake of the head
            // A small breath in that comes to nothing, then a bigger one held at the top (nose up, snout flared, up on
            // its toes, ears back), and it all goes at once: the head snaps down past level, the body squashes and
            // jolts back off its feet, ears and tail flung out. Then it shakes its head and has a last sniff.
            double first = env((p - 0.05) / 0.24);
            double release = smoothstep((p - SNEEZE_CHOO + 0.03) / 0.03);
            double held = smoothstep((p - 0.27) / 0.2) * (1 - release);
            double in = std::max(0.55 * first, held);
            double choo = release * (1 - smoothstep((p - SNEEZE_CHOO - 0.04) / 0.13));
            double jolt = env((p - SNEEZE_CHOO + 0.01) / 0.13);
            double shakeoff = env((p - 0.6) / 0.26);
            double sniff = env((p - 0.8) / 0.2);
            r.headPitch = -28 * in + 20 * choo + 5 * shakeoff;  // more than ~20 down buries the face in the chest
            r.headYaw = 26 * std::sin(t * 19) * shakeoff;
            r.bodyPitch = 6 * in - 3 * choo;
            r.bodyX = -0.4 * in - 1.2 * choo;
            r.scaleY = 1 + 0.09 * in - 0.13 * choo;
            r.scaleXZ = 1 - 0.03 * in + 0.08 * choo;
            r.lift = 1.8 * jolt;
            r.shadow = 1 - 0.3 * jolt;
            r.leg[0] = r.leg[1] = -6 * in + 10 * choo;  // braced, then thrown forward
            r.snout = 1 + 0.3 * in + 0.06 * std::sin(t * 30) * held - 0.25 * choo + 0.15 * std::fabs(std::sin(t * 22)) * sniff;
            r.ear = 18 * in + 40 * choo + 20 * std::fabs(std::sin(t * 19 + 0.8)) * shakeoff;
            r.earSwing = -12 * in + 35 * choo;
            r.tail = 45 * choo;
            break;
        }
        case Action::Ride: {  // a window ride played out on the spot: it starts (rocked back), then stops (thrown forward)
            double s = p * RIDE_SECS;
            r = rideRig((rideJolt(s - 0.1, -12) + rideJolt(s - 0.9, 20)) * smoothstep((1 - p) * 8), 0, 0);
            break;
        }
        case Action::Scratch:  // hind leg scratching behind the "ear"
            r.leg[3] = -70 * e;
            r.splay[3] = 35 * e + 12 * std::sin(t * 24) * e;
            r.headRoll = -14 * e;
            r.headYaw = 10 * e;
            r.bodyRoll = 6 * e;
            r.earFlick = (35 + 12 * std::sin(t * 24)) * e;  // the ear being scratched (the +z side, as leg 3)
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
        // The pupils lead the head: they get to what it looks at first and settle back as the head catches up, and when
        // nothing holds its eye they drift now and then (Fresh Animations' eyes). They look even where the head can't.
        double drift = std::clamp(std::sin(f.t / 2.7) + std::sin(f.t / 1.6), -0.35, 0.35) * smoothstep(std::sin(f.t / 12.5) * 4 - 1);
        r.pupilX += std::clamp(lookYaw / 50 + (tx * f.lookW - lookYaw) / 15 + drift * (1 - f.lookW), -1.0, 1.0);
        r.pupilY += std::clamp(-lookPitch / 35 - (ty * f.lookW - lookPitch) / 12, -1.0, 1.0);
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

// A rectangle on a box face, by its edges in the face's (u, v) (decals take a centre and half sizes).
inline void rect(Box& b, int face, double u0, double u1, double v0, double v1, RGB c) {
    if (v0 > v1) std::swap(v0, v1);
    if (u1 - u0 > 0.02 && v1 - v0 > 0.02) b.decals.push_back({face, (u0 + u1) / 2, (v0 + v1) / 2, (u1 - u0) / 2, (v1 - v0) / 2, c});
}

// The eyes are the pig's own, not Fresh Animations' (he liked these better, 2026-10-15): a white outer column and a
// black inner one, no brow, no moving pupil. The rig still computes pupilX/Y; the face just doesn't use them.
inline Part headPart(Eyes eyes, double snout, double /*px*/ = 0, double /*py*/ = 0) {
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
    Box sn{{8 + 0.5 * snout, -1.5, 0}, {1 * snout, 3 * snout, 4 * snout}, SNOUT_FACE, 1u << 1, {}};
    rect(sn, 0, -0.5 * snout, 0.5 * snout, -1 * snout, 1 * snout, SNOUT_MID);
    for (double side : {-1.0, 1.0}) rect(sn, 0, -0.5 * snout, 0.5 * snout, side > 0 ? 1 * snout : -2 * snout, side > 0 ? 2 * snout : -1 * snout, NOSTRIL);
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
    Box bodyBox{{0, 4 * sy, 0}, {16 * sxz, 8 * sy, 10 * sxz}, PINK, 0, {}};
    // The ridge down the back, with a knot on each side every few steps (the top face: u is z, v is x).
    rect(bodyBox, 2, -1 * sxz, 1 * sxz, -5.5 * sxz, 5.5 * sxz, RIDGE);
    for (double x : {-3.0, 0.0, 3.0})
        for (double z : {-1.5, 1.5}) rect(bodyBox, 2, (z - 0.5) * sxz, (z + 0.5) * sxz, (x - 0.5) * sxz, (x + 0.5) * sxz, RIDGE_DOT);
    b.boxes.push_back(bodyBox);
    dress(b, Anchor::Body);
    parts.push_back(b);
    // Legs hang from hips at the body's bottom corners.
    const V3 hips[4] = {{6, 0, -3}, {6, 0, 3}, {-6, 0, -3}, {-6, 0, 3}};
    for (int i = 0; i < 4; ++i) {
        Part l;
        l.xf = body * Xf::trans(hips[i] * sxz + V3{0, r.legLift[i], 0}) * Xf::rotZ(r.leg[i] - r.bodyPitch) * Xf::rotX(r.splay[i]);
        Box lb = legBox();
        double zs = hips[i].z > 0 ? 1 : -1;  // two dark toes at the bottom of the front, mirrored on the far side
        for (double v : {-1.5, 0.5}) rect(lb, 0, -3, -2, (v - 0.5) * zs, (v + 0.5) * zs, NOSTRIL);
        // A roughly vertical leg has its top inside the body; the painter's sort would still paint that top over
        // the body's side, so it is hidden until the leg swings out far enough for the top to actually show.
        if (std::fabs(r.leg[i]) < 28 && std::fabs(r.splay[i]) < 14) lb.hidden |= 1u << 2;
        l.boxes.push_back(lb);
        dress(l, (Anchor)((int)Anchor::LegFL + i));
        parts.push_back(l);
    }
    // Head pivots at the neck: front-top of the body, slightly inside it.
    Part h = headPart(f.pose == Pose::Sleep ? Eyes::Closed : f.eyes, r.snout, r.pupilX, r.pupilY);  // asleep is asleep
    h.xf = body * Xf::trans({6 * sxz + r.neck, 6 * sy, 0}) * Xf::rotY(-r.headYaw) * Xf::rotZ(-r.headPitch) * Xf::rotX(r.headRoll);
    dress(h, Anchor::Head);
    parts.push_back(h);
    // Ears: the floppy side ears of the Fresh Animations pig (FreshLX's resource pack, its Extensions add-on:
    // modrinth.com/resourcepack/fresh-animations), rebuilt here. A flap hinged high on each side of the head near the
    // front, hanging down and flared out a little; they hang with gravity (half the head's nod is undone) and the rig
    // flings them up, sweeps them back, lays them flat or flicks one. Under a hat they hang out from beneath it, so
    // with one on they don't rise past level and through the brim.
    for (double side : {-1.0, 1.0}) {
        double flick = side > 0 ? std::max(0.0, r.earFlick) : std::max(0.0, -r.earFlick);
        double up = std::clamp(15 + r.ear + flick, 2.0, f.costume && f.costume->hatHeight > 0 ? 80.0 : 120.0);
        Part e;
        e.xf = h.xf * Xf::trans({5.5, 3, side * 4}) * Xf::rotY(-side * 18 * std::clamp(up / 20, 0.3, 1.0)) *
               Xf::rotX(-side * up) * Xf::rotZ(r.headPitch / 2 + r.earSwing - 6);
        Box ear{{-0.5, -2, side * 0.3}, {3, 4, 1}, PINK, 0, {}};
        if (side > 0) ear.decals.push_back({5, 0, 0, 1.6, 1.0, SNOUT});  // the inside, against the head (-z face: u is y)
        else ear.decals.push_back({4, 0, 0, 1.0, 1.6, SNOUT});            // (+z face: u is x)
        e.boxes.push_back(ear);
        parts.push_back(e);
    }
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
// `extra` is laid on top like the pet's own overlays (a window ride's lean), for the render test.
inline HeadPos renderStill(cairo_t* cr, const Frame& f, double cx, double baseY, double scale = 1.0, Action act = Action::Idle, double actP = 0.5,
                           const Rig& extra = Rig{}) {
    Animator a;
    Frame g = f;
    propPlacement(g.prop, g.yaw, g.propX, g.propZ, g.propYaw);
    for (int i = 0; i < 60; ++i) { g.t = f.t - (60 - i) / 30.0; a.update(g); }
    g.t = f.t;
    Rig r = a.update(g);
    if (act != Action::Idle) r = r + actionRig(act, actP, g.t);
    return render(cr, g, r + extra, cx, baseY, scale);
}

}  // namespace art3d
