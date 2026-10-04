CXX      ?= g++
CXXFLAGS ?= -O2 -Wall -Wextra
PKGS     := x11 xext xrandr cairo
PREFIX   ?= $(HOME)/.local

xpet: src/main.cpp src/art.hpp src/art3d.hpp src/ipc.hpp
	$(CXX) -std=c++17 $(CXXFLAGS) $(shell pkg-config --cflags $(PKGS)) src/main.cpp -o $@ $(shell pkg-config --libs $(PKGS))

MOCHI_HOME ?= $(HOME)/.local/share/mochi

install: xpet
	install -Dm755 xpet $(PREFIX)/bin/xpet
	install -Dm755 brain/mochi_brain.py $(PREFIX)/bin/mochi-brain
	install -Dm755 brain/mochi_sense.py $(PREFIX)/bin/mochi-sense
	install -Dm755 brain/mochi_mail.py $(PREFIX)/bin/mochi-mail
	install -Dm644 brain/CLAUDE.md $(MOCHI_HOME)/CLAUDE.md

# Start at login and keep running (see README).
install-autostart: install
	install -Dm644 dist/xpet.service dist/mochi-brain.service -t $(HOME)/.config/systemd/user
	install -Dm644 dist/xpet.desktop -t $(HOME)/.config/autostart
	systemctl --user daemon-reload

# Renders every pose, action and prop at window size and checks the frames (no X needed).
test: tests/render_test.cpp src/art3d.hpp src/art.hpp
	$(CXX) -std=c++17 -O2 -Wall -Wno-missing-field-initializers $(shell pkg-config --cflags cairo) tests/render_test.cpp -o tests/render_test $(shell pkg-config --libs cairo)
	./tests/render_test

clean:
	rm -f xpet tests/render_test

.PHONY: install install-autostart clean test
