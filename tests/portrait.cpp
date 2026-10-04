// Renders Mochi's portrait (for the Telegram bot's profile picture) with the pig's own renderer.
#include <cairo.h>
#include <cstdio>
#include <cstdlib>
#include <string>
#include "../src/art.hpp"
#include "../src/art3d.hpp"
using namespace art3d;
int main(int argc, char** argv) {
    const char* out = argc > 1 ? argv[1] : "mochi.png";
    double zoom = argc > 2 ? atof(argv[2]) : 5.6;
    double baseY = argc > 3 ? atof(argv[3]) : 500;
    double yaw = argc > 4 ? atof(argv[4]) : 30;
    const int S = 640;
    cairo_surface_t* sf = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, S, S);
    cairo_t* cr = cairo_create(sf);
    cairo_set_source_rgb(cr, 0.98, 0.90, 0.86);  // warm blush, a pig's colour family
    cairo_paint(cr);
    Frame f;
    f.pose = Pose::Sit;
    f.eyes = argc > 5 && argv[5][0] == 'o' ? Eyes::Open : Eyes::Happy;
    f.yaw = yaw;
    f.t = 1.0;
    f.progress = 0.6;
    renderStill(cr, f, S / 2.0, baseY, zoom, Action::Idle, 0.5);
    cairo_surface_write_to_png(sf, out);
    cairo_destroy(cr);
    cairo_surface_destroy(sf);
    return 0;
}
