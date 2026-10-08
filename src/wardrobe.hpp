// What Mochi wears, and when. Each costume is a few boxes on the pig's parts (see Costume in art3d.hpp for the
// anchors and their spaces); SEASONS says which one is worn on which days. Mochi adds to this file in its night
// workshop (brain/CLAUDE.md, "Your workshop"); the render test dresses the pig in every costume here.
//
// XPET_COSTUME=name (or "none") forces a costume; XPET_DATE=YYYY-MM-DD pretends it's another day. Both are for
// previews (`xpet --sheet`, `mochi-workshop preview`); the running pig also takes {"event":"wear","costume":...}.
#pragma once
#include "art3d.hpp"

#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <string>
#include <vector>

namespace wardrobe {

using art3d::Anchor;
using art3d::Box;
using art3d::Costume;
using art3d::Decal;
using art3d::rgb;

inline const art3d::RGB RUST = rgb(0xC8552B), CREAM = rgb(0xF3E3C3), WITCH = rgb(0x3B2650), PUMPKIN = rgb(0xF08A24),
                        BUCKLE = rgb(0xF2D04B), TEAL = rgb(0x2A7F86), DEEP_TEAL = rgb(0x1F6168);

// Autumn: a chunky knitted scarf round the neck, one end hanging down the side.
inline Costume autumnScarf() {
    Costume c{"autumn-scarf", {}, 0};
    // The collar rides on the head, round its back half, so it turns with the head. Thicker than the head, so it
    // shows all round; its back is buried in the body like the head's.
    Box ring{{2.0, -0.7, 0}, {3.4, 10.0, 10.4}, RUST, 0, {}};
    for (double k : {-1.2, 1.2}) {  // cream stripes across the knit: on each side and over the top
        ring.decals.push_back({4, k, 0, 0.3, 5.0, CREAM});  // +z face: u is x, v is y
        ring.decals.push_back({5, 0, k, 5.0, 0.3, CREAM});  // -z face: u is y, v is x
        ring.decals.push_back({2, 0, k, 5.2, 0.3, CREAM});  // top: u is z, v is x
    }
    c.pieces.push_back({Anchor::Head, ring});
    // The loose end hangs from the collar down the near side, with a stripe and a cream fringe.
    Box end{{2.6, -6.6, 4.6}, {2.4, 4.4, 1.2}, RUST, 0, {}};
    end.decals.push_back({4, 0, 0.4, 1.2, 0.3, CREAM});
    end.decals.push_back({5, 0.4, 0, 0.3, 1.2, CREAM});
    c.pieces.push_back({Anchor::Head, end});
    c.pieces.push_back({Anchor::Head, {{2.6, -9.2, 4.6}, {2.2, 0.8, 1.0}, CREAM, 0, {}}});
    return c;
}

// Halloween: a crooked witch's hat with an orange band and a buckle.
inline Costume witchHat() {
    Costume c{"witch-hat", {}, 7.6};
    c.pieces.push_back({Anchor::Head, {{4, 4.4, 0}, {10.5, 0.8, 10.5}, WITCH, 0, {}}});  // the brim
    Box band{{4, 5.3, 0}, {6.3, 1.0, 6.3}, PUMPKIN, 0, {}};
    band.decals.push_back({0, 0, 0, 0.42, 0.75, BUCKLE});  // the buckle, on the side facing forward
    c.pieces.push_back({Anchor::Head, band});
    c.pieces.push_back({Anchor::Head, {{4, 6.6, 0}, {6, 1.8, 6}, WITCH, 0, {}}});
    c.pieces.push_back({Anchor::Head, {{3.5, 8.3, 0}, {4.2, 1.8, 4.2}, WITCH, 0, {}}});
    c.pieces.push_back({Anchor::Head, {{2.8, 9.8, 0.2}, {2.6, 1.6, 2.6}, WITCH, 0, {}}});
    c.pieces.push_back({Anchor::Head, {{1.9, 11.0, 0.5}, {1.4, 1.2, 1.4}, WITCH, 0, {}}});  // the tip, bent back
    return c;
}

// Winter: a knitted bobble hat, pulled down to just above the eyes, with a ribbed cuff and a cream pom-pom.
inline Costume bobbleHat() {
    Costume c{"bobble-hat", {}, 6.2};
    // The cuff: a little wider than the head all round, its bottom edge (y 3.3) clear of the eyes (they end at 3).
    Box cuff{{4, 4.1, 0}, {8.8, 1.6, 8.8}, TEAL, 0, {}};
    for (double k : {-3.0, -1.5, 0.0, 1.5, 3.0}) {  // darker ribs running up the knit
        cuff.decals.push_back({0, 0, k, 0.8, 0.22, DEEP_TEAL});  // front: u is y, v is z
        cuff.decals.push_back({1, k, 0, 0.22, 0.8, DEEP_TEAL});  // back: u is z, v is y
        cuff.decals.push_back({4, k, 0, 0.22, 0.8, DEEP_TEAL});  // +z: u is x, v is y
        cuff.decals.push_back({5, 0, k, 0.8, 0.22, DEEP_TEAL});  // -z: u is y, v is x
    }
    c.pieces.push_back({Anchor::Head, cuff});
    // The crown, with one cream stripe round it.
    Box crown{{4, 5.7, 0}, {8.0, 1.6, 8.0}, TEAL, 0, {}};
    crown.decals.push_back({0, 0, 0, 0.25, 4.0, CREAM});
    crown.decals.push_back({1, 0, 0, 4.0, 0.25, CREAM});
    crown.decals.push_back({4, 0, 0, 4.0, 0.25, CREAM});
    crown.decals.push_back({5, 0, 0, 0.25, 4.0, CREAM});
    c.pieces.push_back({Anchor::Head, crown});
    c.pieces.push_back({Anchor::Head, {{3.9, 6.9, 0}, {6.4, 0.8, 6.4}, TEAL, 0, {}}});  // gathered at the top
    c.pieces.push_back({Anchor::Head, {{3.8, 7.5, 0}, {4.2, 0.6, 4.2}, TEAL, 0, {}}});
    c.pieces.push_back({Anchor::Head, {{3.6, 8.9, 0}, {3.2, 2.6, 3.2}, CREAM, 0, {}}});  // the pom-pom
    return c;
}

// Every costume there is (the render test goes through all of them).
inline const std::vector<Costume>& all() {
    static const std::vector<Costume> v = {autumnScarf(), witchHat(), bobbleHat()};
    return v;
}

inline const Costume* named(const std::string& name) {
    for (const Costume& c : all())
        if (c.name == name) return &c;
    return nullptr;
}

// When each costume is worn: month*100 + day, inclusive, and a range may wrap the new year (1220 to 110).
// The first match wins, so put the short special days above the long seasons they fall in.
struct Season {
    const char* costume;
    int from, to;
};
inline const Season SEASONS[] = {
    {"witch-hat", 1024, 1101},
    {"autumn-scarf", 922, 1120},
    {"bobble-hat", 1121, 228},  // all winter, through the new year
};

inline const Costume* forDate(int month, int day) {
    int md = month * 100 + day;
    for (const Season& s : SEASONS) {
        bool in = s.from <= s.to ? md >= s.from && md <= s.to : md >= s.from || md <= s.to;
        if (in) return named(s.costume);
    }
    return nullptr;
}

// Today's costume, honouring XPET_COSTUME and XPET_DATE.
inline const Costume* today() {
    if (const char* c = getenv("XPET_COSTUME"); c && *c) return named(c);  // "none" (or any unknown name): nothing
    int y = 0, m = 0, d = 0;
    if (const char* fake = getenv("XPET_DATE"); fake && sscanf(fake, "%d-%d-%d", &y, &m, &d) == 3) return forDate(m, d);
    time_t t = time(nullptr);
    tm lt{};
    localtime_r(&t, &lt);
    return forDate(lt.tm_mon + 1, lt.tm_mday);
}

}  // namespace wardrobe
