// How other processes talk to the cat: one flat JSON object per datagram on a
// user-only Unix socket. Senders never block, and fail instantly if the cat isn't running.
#pragma once
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <string>
#include <utility>
#include <vector>

namespace json {

// Nested objects/arrays are flattened into dotted keys: {"a":{"b":[1]}} -> "a.b.0" = "1".
using Flat = std::map<std::string, std::string>;

namespace detail {

inline void utf8(std::string& out, unsigned cp) {
    if (cp < 0x80) out += (char)cp;
    else if (cp < 0x800) { out += (char)(0xC0 | cp >> 6); out += (char)(0x80 | (cp & 63)); }
    else if (cp < 0x10000) {
        out += (char)(0xE0 | cp >> 12); out += (char)(0x80 | ((cp >> 6) & 63)); out += (char)(0x80 | (cp & 63));
    } else {
        out += (char)(0xF0 | cp >> 18); out += (char)(0x80 | ((cp >> 12) & 63));
        out += (char)(0x80 | ((cp >> 6) & 63)); out += (char)(0x80 | (cp & 63));
    }
}

struct Parser {
    const char* s;
    const char* e;

    void ws() { while (s < e && isspace((unsigned char)*s)) ++s; }

    unsigned hex4() {
        if (e - s < 4) return 0xFFFD;
        unsigned v = (unsigned)strtoul(std::string(s, 4).c_str(), nullptr, 16);
        s += 4;
        return v;
    }

    bool str(std::string& out) {
        if (s >= e || *s != '"') return false;
        ++s;
        while (s < e && *s != '"') {
            char c = *s++;
            if (c != '\\' || s >= e) { out += c; continue; }
            switch (char x = *s++) {
                case 'n': out += '\n'; break;
                case 't': out += '\t'; break;
                case 'r': out += '\r'; break;
                case 'b': out += '\b'; break;
                case 'f': out += '\f'; break;
                case 'u': {
                    unsigned cp = hex4();
                    if (cp >= 0xD800 && cp < 0xDC00 && e - s >= 6 && s[0] == '\\' && s[1] == 'u') {
                        s += 2;
                        unsigned lo = hex4();
                        cp = (lo >= 0xDC00 && lo < 0xE000) ? 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00) : 0xFFFD;
                    }
                    utf8(out, cp);
                    break;
                }
                default: out += x;
            }
        }
        if (s >= e) return false;
        ++s;
        return true;
    }

    bool value(const std::string& key, Flat& f, int depth) {
        ws();
        if (s >= e || depth > 16) return false;
        if (*s == '{' || *s == '[') {
            bool obj = *s++ == '{';
            char close = obj ? '}' : ']';
            ws();
            if (s < e && *s == close) { ++s; return true; }
            for (int i = 0;; ++i) {
                std::string k = std::to_string(i);
                if (obj) {
                    ws();
                    k.clear();
                    if (!str(k)) return false;
                    ws();
                    if (s >= e || *s++ != ':') return false;
                }
                if (!value(key.empty() ? k : key + "." + k, f, depth + 1)) return false;
                ws();
                if (s < e && *s == ',') { ++s; continue; }
                if (s < e && *s == close) { ++s; return true; }
                return false;
            }
        }
        if (*s == '"') {
            std::string v;
            if (!str(v)) return false;
            f[key] = std::move(v);
            return true;
        }
        const char* start = s;
        while (s < e && (isalnum((unsigned char)*s) || *s == '-' || *s == '+' || *s == '.')) ++s;
        if (s == start) return false;
        f[key] = std::string(start, s);
        return true;
    }
};

}  // namespace detail

inline bool parse(const std::string& in, Flat& out) {
    detail::Parser p{in.data(), in.data() + in.size()};
    if (!p.value("", out, 0)) return false;
    p.ws();
    return p.s == p.e;
}

inline std::string quote(const std::string& v) {
    std::string o = "\"";
    for (unsigned char c : v) {
        if (c == '"' || c == '\\') { o += '\\'; o += (char)c; }
        else if (c == '\n') o += "\\n";
        else if (c < 0x20) { char b[8]; snprintf(b, sizeof b, "\\u%04x", c); o += b; }
        else o += (char)c;
    }
    return o + "\"";
}

inline std::string object(const std::vector<std::pair<std::string, std::string>>& kv) {
    std::string o = "{";
    for (auto& [k, v] : kv) {
        if (v.empty()) continue;
        if (o.size() > 1) o += ",";
        o += quote(k) + ":" + quote(v);
    }
    return o + "}";
}

}  // namespace json

namespace ipc {

// "<runtime dir>/<name>.sock", or a per-user file in /tmp when there is no runtime dir.
inline std::string sockPath(const std::string& name) {
    const char* r = getenv("XDG_RUNTIME_DIR");
    if (r && *r) return std::string(r) + "/" + name + ".sock";
    std::string run = "/run/user/" + std::to_string(getuid());
    struct stat st;
    if (stat(run.c_str(), &st) == 0) return run + "/" + name + ".sock";
    return "/tmp/" + name + "-" + std::to_string(getuid()) + ".sock";
}

inline std::string socketPath() { return sockPath("xpet"); }
inline std::string brainPath() { return sockPath("mochi-brain"); }  // where the cat's clicks go

inline bool address(sockaddr_un& a, const std::string& p = socketPath()) {
    if (p.size() >= sizeof a.sun_path) return false;
    memset(&a, 0, sizeof a);
    a.sun_family = AF_UNIX;
    memcpy(a.sun_path, p.c_str(), p.size() + 1);
    return true;
}

// Only call while holding the single-instance lock, since it replaces any stale socket.
inline int listen() {
    sockaddr_un a;
    if (!address(a)) return -1;
    int fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0) return -1;
    unlink(a.sun_path);
    mode_t old = umask(077);
    int r = bind(fd, (sockaddr*)&a, sizeof a);
    umask(old);
    if (r != 0) { close(fd); return -1; }
    return fd;
}

inline bool send(const std::string& msg, const std::string& path = socketPath()) {
    sockaddr_un a;
    if (!address(a, path)) return false;
    int fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return false;
    bool ok = sendto(fd, msg.data(), msg.size(), MSG_DONTWAIT | MSG_NOSIGNAL, (sockaddr*)&a, sizeof a) ==
              (ssize_t)msg.size();
    close(fd);
    return ok;
}

// Drains whatever is queued; returns parsed messages (malformed ones are dropped).
inline std::vector<json::Flat> receive(int fd, int max = 64) {
    std::vector<json::Flat> out;
    char buf[8192];
    for (int i = 0; i < max; ++i) {
        ssize_t n = recv(fd, buf, sizeof buf, 0);
        if (n <= 0) break;
        json::Flat f;
        if (json::parse(std::string(buf, n), f)) out.push_back(std::move(f));
    }
    return out;
}

}  // namespace ipc
