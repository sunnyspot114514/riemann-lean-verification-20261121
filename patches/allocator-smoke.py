#!/usr/bin/env python3
"""Standalone smoke checks for the exact C++ allocator helper text, not Lean proof tests."""
from pathlib import Path
import subprocess
import tempfile
import json
import sys

base = Path(__file__).resolve().parent
source = (base / "source/src/library/module.cpp").read_text()
helpers = source[source.index("namespace lean {"):source.index("/** On-disk format")]
prefix = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <fstream>
#include <iostream>
#include <limits>
#include <memory>
#include <mutex>
#include <new>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>
#include <vector>
#ifdef LOCAL_ASAN
#include <sanitizer/lsan_interface.h>
#endif
#define lean_always_assert(x) assert(x)
#define LEAN_MIMALLOC
static void *mi_malloc(size_t size) { return malloc(size); }
static void mi_free(void *p) { free(p); }
namespace lean {
using exception = std::runtime_error;
struct sstream {
  std::ostringstream stream;
  template<class T> sstream &operator<<(T const &t) { stream << t; return *this; }
  std::string str() const { return stream.str(); }
};
}
static bool fail_reservation = false;
static bool fail_mapping = false;
extern "C" int __real_posix_fallocate(int, off_t, off_t);
extern "C" int __wrap_posix_fallocate(int fd, off_t start, off_t size) {
  if (fail_reservation) { fail_reservation = false; return ENOSPC; }
  return __real_posix_fallocate(fd, start, size);
}
extern "C" void *__real_mmap(void *, size_t, int, int, int, off_t);
extern "C" void *__wrap_mmap(void *p, size_t s, int prot, int flags, int fd, off_t o) {
  if (fail_mapping) { fail_mapping = false; errno = ENOMEM; return MAP_FAILED; }
  return __real_mmap(p, s, prot, flags, fd, o);
}
'''
test = r'''
}
static size_t arena_maps() {
  std::ifstream file("/proc/self/maps");
  std::string line;
  size_t n = 0;
  while (std::getline(file, line))
    if (line.find("lean-olean-arena-") != std::string::npos) ++n;
  return n;
}
static size_t fd_count() {
  DIR *d = opendir("/proc/self/fd"); assert(d);
  size_t n = 0; while (readdir(d)) ++n;
  closedir(d); return n;
}
static void *allocate(size_t s, bool file = true) {
  void *p = lean::alloc_compacted_region_buffer(s, file); assert(p);
  assert(reinterpret_cast<uintptr_t>(p) % alignof(std::max_align_t) == 0);
  static_cast<char *>(p)[0] = 19;
  static_cast<char *>(p)[s - 1] = 71;
  return p;
}
static void release(void *p, size_t s) {
  lean::free_compacted_region_buffer(static_cast<char *>(p), s, false);
}
int main(int argc, char **argv) {
  assert(argc == 2);
  setenv("LEAN_OLEAN_ARENA_DIR", argv[1], 1);
  size_t fds = fd_count();
  assert(arena_maps() == 0);
  auto *heap = allocate(47, false); assert(arena_maps() == 0); release(heap, 47);
  auto *a = allocate(47); auto *b = allocate(73);
  assert(arena_maps() == 1 && fd_count() == fds);
  release(a, 47); assert(arena_maps() == 1);
  release(b, 73); assert(arena_maps() == 0);
  // Reusing the allocator after its current segment was freed must be safe.
  a = allocate(47); release(a, 47); assert(arena_maps() == 0);
  // Oversized allocations get a page-rounded dedicated-size segment.
  a = allocate(47);
  constexpr size_t huge = size_t{256} * 1024 * 1024 + 123;
  b = allocate(huge); assert(arena_maps() == 2);
  release(b, huge); assert(arena_maps() == 1);
  auto *c = allocate(111); assert(arena_maps() == 2);
  release(a, 47); release(c, 111); assert(arena_maps() == 0);
  // Reservation and mmap failures close descriptors and leave no live segment.
  fail_reservation = true;
  try { allocate(47); assert(false); }
  catch (lean::exception const &e) { assert(std::string(e.what()).find("No space") != std::string::npos); }
  assert(arena_maps() == 0 && fd_count() == fds);
  fail_mapping = true;
  try { allocate(47); assert(false); }
  catch (lean::exception const &e) { assert(std::string(e.what()).find("could not map") != std::string::npos); }
  assert(arena_maps() == 0 && fd_count() == fds);
  try { lean::alloc_compacted_region_buffer(std::numeric_limits<size_t>::max(), true); assert(false); }
  catch (lean::exception const &) {}
  // The same ownership guard used by the loader releases on exceptions.
  try {
    std::unique_ptr<char, lean::compacted_region_buffer_deleter> guard(
      static_cast<char *>(allocate(91)), {91, false});
    throw lean::exception("simulated reader failure");
  } catch (lean::exception const &) {}
  assert(arena_maps() == 0);
  // Concurrent allocation/free shares state and releases the final segment.
  std::atomic<unsigned> ready{0};
  std::vector<std::thread> workers;
  for (unsigned t = 0; t < 8; ++t) workers.emplace_back([&]() {
    std::vector<void *> buffers;
    for (unsigned i = 0; i < 100; ++i) buffers.push_back(allocate(101 + i));
    ready.fetch_add(1);
    while (ready.load() != 8) std::this_thread::yield();
    for (unsigned i = 0; i < 100; ++i) release(buffers[i], 101 + i);
  });
  for (auto &worker : workers) worker.join();
  assert(arena_maps() == 0 && fd_count() == fds);
  // PID reset covers allocation bookkeeping, not arbitrary fork/COW semantics.
  a = allocate(47);
  pid_t child = fork(); assert(child >= 0);
  if (child == 0) {
    auto *child_buffer = allocate(47);
    assert(arena_maps() == 2);
    release(child_buffer, 47); release(a, 47);
    assert(arena_maps() == 0);
    _exit(0);
  }
  int status; assert(waitpid(child, &status, 0) == child);
  assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
  assert(static_cast<char *>(a)[0] == 19 && arena_maps() == 1);
  release(a, 47); assert(arena_maps() == 0);
  std::cout << "allocator smoke passed: align/heap/last-free/oversize/ENOSPC/mmap-failure/RAII/threads/PID-reset\n";
}
'''
with tempfile.TemporaryDirectory(prefix="olean-allocator-smoke-") as directory:
    directory = Path(directory)
    cpp = directory / "allocator.cpp"
    cpp.write_text(prefix + helpers + test)
    binary = directory / "allocator"
    args = ["g++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-pthread", str(cpp),
            "-Wl,--wrap=posix_fallocate", "-Wl,--wrap=mmap", "-o", str(binary)]
    if "--asan" in sys.argv[1:]:
        args.extend(["-O1", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                     "-DLOCAL_ASAN", "-D__has_feature(x)=1"])
    build = subprocess.run(args, text=True, capture_output=True)
    print(build.stdout, end="")
    print(build.stderr, end="")
    build.check_returncode()
    run = subprocess.run([str(binary), str(directory)], text=True, capture_output=True)
    print(run.stdout, end="")
    print(run.stderr, end="")
    print(json.dumps({"build_exit": build.returncode, "run_exit": run.returncode,
                      "scope": "exact helper text; Lean types and mimalloc calls stubbed"}))
    run.check_returncode()
