// Shared vocabulary for the pig's looks (poses, eyes, props) plus the few flat pixel bits that are still
// drawn in 2D over the 3D render: the heart particle. The pig itself lives in art3d.hpp.
#pragma once
#include <cairo/cairo.h>
#include <cstdint>
#include <string>
#include <vector>

namespace art {

constexpr int CW = 28, CH = 20;  // nominal sprite box in "pixels"; the 3D pig is framed to fit it

enum class Pose { Stand, WalkA, WalkB, Sit, Sleep, Dangle, Fall, Eat };
enum class Eyes { Open, Closed, Happy };
enum class Prop { Nothing, Book, Laptop, Terminal, Globe };

using Part = std::vector<std::string>;

inline uint32_t colorOf(char c) {
    switch (c) {
        case 'r': return 0xFFE8384F;  // heart red
        case 'k': return 0xFF2A1E1A;  // outline
        default: return 0;
    }
}

inline const Part HEART = {
    "rr.rr",
    "rrrrr",
    ".rrr.",
    "..r..",
};

inline void blitPart(cairo_t* cr, const Part& p, double x, double y, int scale, double alpha = 1) {
    for (int r = 0; r < (int)p.size(); ++r)
        for (int c = 0; c < (int)p[r].size(); ++c) {
            uint32_t col = colorOf(p[r][c]);
            if (!col) continue;
            cairo_set_source_rgba(cr, ((col >> 16) & 255) / 255.0, ((col >> 8) & 255) / 255.0,
                                  (col & 255) / 255.0, alpha);
            cairo_rectangle(cr, x + c * scale, y + r * scale, scale, scale);
            cairo_fill(cr);
        }
}

}  // namespace art
