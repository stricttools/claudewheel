/* LD_PRELOAD interposer logging file syscalls (open/write/fsync/rename/chmod)
 * for machines without strace. Used to verify the atomic-write commit
 * sequence in claudewheel/effects.py; keep it for the pending fsync-durability
 * work (todo/residual-decisions-and-housekeeping.md).
 *
 * Build:  gcc -shared -fPIC -o libiotrace.so scripts/iotrace.c -ldl
 * Run:    IOTRACE_OUT=/path/trace.txt IOTRACE_FILTER=settings \
 *         LD_PRELOAD=./libiotrace.so python3 ...
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdarg.h>

static FILE *out;
static const char *filt;
static int inited;

static void init(void) {
    if (inited) return;
    inited = 1;
    const char *p = getenv("IOTRACE_OUT");
    out = p ? fopen(p, "a") : stderr;
    filt = getenv("IOTRACE_FILTER");
    if (!filt) filt = "";
}
static int match(const char *s) { return s && filt[0] && strstr(s, filt); }
static void emit(const char *fmt, ...) {
    init();
    if (!out) return;
    va_list ap; va_start(ap, fmt);
    vfprintf(out, fmt, ap);
    va_end(ap);
    fputc('\n', out);
    fflush(out);
}
static void flagstr(int f, char *b, size_t n) {
    b[0] = 0;
    if ((f & O_ACCMODE) == O_RDONLY) strncat(b, "O_RDONLY", n-strlen(b)-1);
    if ((f & O_ACCMODE) == O_WRONLY) strncat(b, "O_WRONLY", n-strlen(b)-1);
    if ((f & O_ACCMODE) == O_RDWR)   strncat(b, "O_RDWR",   n-strlen(b)-1);
    if (f & O_CREAT)     strncat(b, "|O_CREAT", n-strlen(b)-1);
    if (f & O_EXCL)      strncat(b, "|O_EXCL", n-strlen(b)-1);
    if (f & O_TRUNC)     strncat(b, "|O_TRUNC", n-strlen(b)-1);
    if (f & O_APPEND)    strncat(b, "|O_APPEND", n-strlen(b)-1);
    if (f & O_DIRECTORY) strncat(b, "|O_DIRECTORY", n-strlen(b)-1);
    if (f & O_CLOEXEC)   strncat(b, "|O_CLOEXEC", n-strlen(b)-1);
}

#define TRACKED 4096
static char *paths[TRACKED];

int openat(int dirfd, const char *path, int flags, ...) {
    static int (*real)(int, const char *, int, ...);
    if (!real) real = dlsym(RTLD_NEXT, "openat");
    mode_t m = 0;
    if (flags & (O_CREAT | O_TMPFILE)) {
        va_list ap; va_start(ap, flags); m = va_arg(ap, mode_t); va_end(ap);
    }
    int r = real(dirfd, path, flags, m);
    init();
    if (match(path)) {
        char fb[128]; flagstr(flags, fb, sizeof fb);
        if (flags & O_CREAT) emit("openat(AT_FDCWD, \"%s\", %s, 0%03o) = %d", path, fb, m, r);
        else                 emit("openat(AT_FDCWD, \"%s\", %s) = %d", path, fb, r);
        if (r >= 0 && r < TRACKED) { free(paths[r]); paths[r] = strdup(path); }
    } else if (r >= 0 && r < TRACKED) { free(paths[r]); paths[r] = NULL; }
    return r;
}
int open64(const char *path, int flags, ...) {
    mode_t m = 0;
    if (flags & (O_CREAT | O_TMPFILE)) { va_list ap; va_start(ap, flags); m = va_arg(ap, mode_t); va_end(ap); }
    return openat(AT_FDCWD, path, flags, m);
}
int open(const char *path, int flags, ...) {
    mode_t m = 0;
    if (flags & (O_CREAT | O_TMPFILE)) { va_list ap; va_start(ap, flags); m = va_arg(ap, mode_t); va_end(ap); }
    return openat(AT_FDCWD, path, flags, m);
}
ssize_t write(int fd, const void *buf, size_t n) {
    static ssize_t (*real)(int, const void *, size_t);
    if (!real) real = dlsym(RTLD_NEXT, "write");
    ssize_t r = real(fd, buf, n);
    if (fd >= 0 && fd < TRACKED && paths[fd]) emit("write(%d<%s>, %zu) = %zd", fd, paths[fd], n, r);
    return r;
}
int fsync(int fd) {
    static int (*real)(int);
    if (!real) real = dlsym(RTLD_NEXT, "fsync");
    int r = real(fd);
    if (fd >= 0 && fd < TRACKED && paths[fd]) emit("fsync(%d<%s>) = %d", fd, paths[fd], r);
    else emit("fsync(%d) = %d", fd, r);
    return r;
}
int fdatasync(int fd) {
    static int (*real)(int);
    if (!real) real = dlsym(RTLD_NEXT, "fdatasync");
    int r = real(fd);
    emit("fdatasync(%d<%s>) = %d", fd, (fd<TRACKED&&paths[fd])?paths[fd]:"?", r);
    return r;
}
int close(int fd) {
    static int (*real)(int);
    if (!real) real = dlsym(RTLD_NEXT, "close");
    if (fd >= 0 && fd < TRACKED && paths[fd]) { emit("close(%d<%s>)", fd, paths[fd]); free(paths[fd]); paths[fd] = NULL; }
    return real(fd);
}
int rename(const char *a, const char *b) {
    static int (*real)(const char *, const char *);
    if (!real) real = dlsym(RTLD_NEXT, "rename");
    int r = real(a, b);
    init();
    if (match(a) || match(b)) emit("rename(\"%s\", \"%s\") = %d%s", a, b, r, r?" ERR":"");
    return r;
}
int renameat2(int ad, const char *a, int bd, const char *b, unsigned int fl) {
    static int (*real)(int, const char *, int, const char *, unsigned int);
    if (!real) real = dlsym(RTLD_NEXT, "renameat2");
    int r = real(ad, a, bd, b, fl);
    init();
    if (match(a) || match(b)) emit("renameat2(AT_FDCWD, \"%s\", AT_FDCWD, \"%s\", %u) = %d", a, b, fl, r);
    return r;
}
int chmod(const char *p, mode_t m) {
    static int (*real)(const char *, mode_t);
    if (!real) real = dlsym(RTLD_NEXT, "chmod");
    int r = real(p, m);
    init();
    if (match(p)) emit("chmod(\"%s\", 0%03o) = %d", p, m, r);
    return r;
}
