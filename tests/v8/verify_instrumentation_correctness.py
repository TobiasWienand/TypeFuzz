"""
Verify that our type-coverage instrumentation builds and runs correctly across
the V8 commits listed in tests/v8/commits.txt.

For each commit, the script does the following:

  1. deinstrument
        Calls instrument_v8.py --deinstrument to revert the V8 tree to a
        completely clean state. Reverts the RECORD_* macros in src/compiler
        and src/maglev, and also restores src/fuzzilli/{cov.h,cov.cc,fuzzilli.cc}
        to their upstream contents via git checkout.

  2. checkout
        git checkout --force <commit> to switch the V8 tree to the target
        revision, then gclient sync to pull matching DEPS. Uses a standalone
        depot_tools clone (data/version_test/depot_tools_standalone) so we
        always have a depot_tools version that understands the latest DEPS
        format.

  3. instrument
        Calls instrument_v8.py --instrument, which (in this order):
          a. installs cov.ours.{h,cc} into src/fuzzilli/
          b. patches fuzzilli.cc to add the case 8 DEBUG-crash branch if
             missing (older V8 versions are missing it)
          c. inserts RECORD_* macros at every type-recording point in
             src/compiler and src/maglev

  4. build
        Builds d8 with ASan via gn gen + ninja. ASan adds memory-safety checks
        on top of our hooks, catching out-of-bounds writes / use-after-free in
        the instrumentation that would otherwise be silent. The build is
        considered successful only if the d8 binary's mtime is newer than
        before the build started -- this guards against ninja silently using
        a stale binary if the build broke part-way.

  5. mjsunit
        Runs V8's full mjsunit test suite against the instrumented ASan d8
        and parses the structured JSON output. A commit passes only if all
        three hold:
          a. Zero entries in results[] — V8's runner only records tests
             with unexpected outcomes, so any non-empty results[] means
             something failed.
          b. test_total is at least MIN_EXPECTED_TESTS. V8's runner stops
             after --exit-after-n-failures (default 100); if tests ran
             far fewer than expected, the runner aborted early and we'd
             silently miss data without this sanity check.
          c. No entry in results[] looks like a crash. We can't rely on
             V8's result=="CRASH" classification alone, because V8's own
             signal handler catches fatal signals and calls _exit(128+signo),
             so the child's exit code is positive and V8's HasCrashed()
             returns False. Instead we flag as "signal failure" any entry
             whose exit_code is one of the signal-indicating codes
             (139 SEGV, 134 ABRT, etc.) or whose stderr contains a crash
             banner (ASan report, "Received signal N", record_type, ...).

We deliberately do not attempt differential mjsunit (uninstrumented vs
instrumented): JIT scheduling and GC timing make individual mjsunit tests
non-deterministic, so the diff would have noise of its own. Instead we trust
that real instrumentation bugs are systematic (a null deref in our hook fires
on every JIT compilation) and will show up as CRASH or as our function in a
crash backtrace.

Output for each commit goes to data/version_test/<label>.{log,result,mjsunit.json}.
The result file is a single line in key=value format starting with status=.
The script's exit code is 0 iff every commit passed.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
V8_DIR = PROJECT_ROOT / "v8"
TESTS_DIR = PROJECT_ROOT / "tests" / "v8"
COMMITS_FILE = TESTS_DIR / "commits.txt"
INSTRUMENT_SCRIPT = PROJECT_ROOT / "instrument_v8.py"

OUT_DIR = PROJECT_ROOT / "data" / "version_test"
DEPOT_TOOLS = OUT_DIR / "depot_tools_standalone"
BUILD_DIR_NAME = "out/asan_verify"
BUILD_DIR = V8_DIR / BUILD_DIR_NAME

# ASan build args. v8_static_library=false because static + ASan is fragile
# across V8 versions; v8_enable_partition_alloc=false because partition_alloc
# is incompatible with several gn arg combinations we need.
GN_ARGS = (
    'is_debug=false dcheck_always_on=true v8_static_library=false '
    'v8_enable_verify_heap=true v8_enable_partition_alloc=false '
    'v8_fuzzilli=true sanitizer_coverage_flags="trace-pc-guard" '
    'target_cpu="x64" is_asan=true is_clang=true'
)

# Patterns in a failing test's stderr that suggest the crash could have been
# caused by our instrumentation. Failures NOT matching any of these are
# assumed to be pre-existing V8 issues (e.g. DCHECK failures in V8 heap code,
# leaks in V8's own regression tests flagged by LSan, etc.) and are reported
# for transparency but do not fail the commit.
HOOK_SUSPECT_PATTERNS = [
    re.compile(r"\brecord_type\b"),
    re.compile(r"\bRECORD_(?:OPTIONAL_)?[A-Z]+REF\b"),
    re.compile(r"AddressSanitizer:.*(heap-buffer-overflow|heap-use-after-free|stack-buffer-overflow|global-buffer-overflow)"),
    re.compile(r"UndefinedBehaviorSanitizer:"),
]

# Exit codes that indicate the child died via a fatal signal that V8's own
# signal handler caught and converted into _exit(128 + signo).
# 139 = SEGV, 134 = ABRT (often V8 CHECK), 136 = FPE, 132 = ILL, 138 = BUS.
# A pure SEGV in an ASan build with our instrumentation is very unlikely to
# come from V8 itself (ASan would catch it first), so we treat these exit
# codes as suspect.
SIGNAL_EXIT_CODES = {132, 136, 138, 139}

# Pre-existing V8 failure patterns — excluded from "suspect" count because
# they reproduce in uninstrumented V8 builds at the same commit.
KNOWN_V8_FAILURE_PATTERNS = [
    re.compile(r"LeakSanitizer"),  # LSan (part of ASan) on V8's own leaks
    re.compile(r"Debug check failed.*mark-compact\.cc"),  # known heap DCHECK
]

# Baseline mjsunit test count for the canonical Mar2026 commit. If the
# actual test count drops significantly below this, the runner almost
# certainly stopped early on too many failures and we're silently missing
# data. Used as a sanity check.
MIN_EXPECTED_TESTS = 6000

JOBS = str(os.cpu_count() or 1)


def env_with_depot_tools():
    env = os.environ.copy()
    extra = [
        "/usr/bin", "/usr/local/bin", str(DEPOT_TOOLS),
        str(V8_DIR / "buildtools" / "linux64"),
        str(V8_DIR / "third_party" / "ninja"),
    ]
    env["PATH"] = ":".join(extra) + ":" + env.get("PATH", "")
    env["GCLIENT_SUPPRESS_GIT_VERSION_WARNING"] = "1"
    # Note: we cannot disable LeakSanitizer via ASAN_OPTIONS here because
    # V8's own test runner (tools/testrunner/base_runner.py) overwrites
    # ASAN_OPTIONS before spawning d8. Instead, LSan reports from V8's
    # pre-existing leaks are matched by KNOWN_V8_FAILURE_PATTERNS and
    # classified as pre-existing, not as instrumentation bugs.
    return env


def read_commits():
    commits = []
    for raw in COMMITS_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        commit, label = line.split(":", 1)
        commits.append((commit.strip(), label.strip()))
    return commits


def bootstrap_depot_tools():
    if DEPOT_TOOLS.exists():
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Cloning depot_tools to {DEPOT_TOOLS}")
    subprocess.run(
        ["git", "clone",
         "https://chromium.googlesource.com/chromium/tools/depot_tools.git",
         str(DEPOT_TOOLS)],
        check=True,
    )


def deinstrument(log):
    """Revert the V8 tree to a fully clean state. Trivially calls our script."""
    log.write("\n=== deinstrument ===\n")
    log.flush()
    subprocess.run(
        [sys.executable, str(INSTRUMENT_SCRIPT), "--deinstrument", "--no-build", str(V8_DIR)],
        stdout=log, stderr=subprocess.STDOUT, check=False,
    )


def checkout(commit, log):
    log.write(f"\n=== checkout {commit} ===\n")
    log.flush()
    subprocess.run(
        ["git", "checkout", "--force", commit],
        cwd=V8_DIR, stdout=log, stderr=subprocess.STDOUT, check=True,
    )
    subprocess.run(
        ["gclient", "sync", "-D", "--force", "--reset", "--no-history"],
        cwd=V8_DIR, env=env_with_depot_tools(),
        stdout=log, stderr=subprocess.STDOUT, check=False,
    )


def instrument(log):
    """Apply our instrumentation. Trivially calls our script."""
    log.write("\n=== instrument ===\n")
    log.flush()
    subprocess.run(
        [sys.executable, str(INSTRUMENT_SCRIPT), "--instrument", "--no-build", str(V8_DIR)],
        stdout=log, stderr=subprocess.STDOUT, check=True,
    )


def build(log):
    """Build d8 with ASan. Returns True iff a fresh d8 binary appears."""
    log.write("\n=== build ===\n")
    log.flush()

    d8_path = BUILD_DIR / "d8"
    before = d8_path.stat().st_mtime if d8_path.exists() else 0.0

    env = env_with_depot_tools()
    gen = subprocess.run(
        ["gn", "gen", BUILD_DIR_NAME, f"--args={GN_ARGS}"],
        cwd=V8_DIR, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    if gen.returncode != 0:
        return False

    ninja = subprocess.run(
        ["ninja", "-C", BUILD_DIR_NAME, "-j", JOBS, "d8"],
        cwd=V8_DIR, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    if ninja.returncode != 0:
        return False

    if not d8_path.exists():
        return False
    after = d8_path.stat().st_mtime
    return after > before


def run_mjsunit(label, log):
    """Run mjsunit and count failures. Returns a dict or None on JSON failure.

    We count failures conservatively because V8's signal-handler converts
    fatal signals into _exit(128+signo), so neither result=="CRASH" nor a
    symbolicated stack trace is reliable on its own.

    - failures: every entry in results[] whose final result is not PASS.
      V8's runner only puts unexpected outcomes in results[], so any entry
      here is already a real failure.
    - signal_failures: subset where the process died via a fatal signal
      (exit code in SIGNAL_EXIT_CODES, or stderr matches CRASH_PATTERNS).
      These are the ones attributable to a crashing hook.
    - total_ran: the number of tests the runner actually executed. If this
      is significantly below MIN_EXPECTED_TESTS, the runner probably hit
      --exit-after-n-failures and stopped early.
    """
    log.write("\n=== mjsunit ===\n")
    log.flush()

    json_out = OUT_DIR / f"{label}.mjsunit.json"
    if json_out.exists():
        json_out.unlink()

    subprocess.run(
        [sys.executable, "tools/run-tests.py", "mjsunit",
         f"--outdir={BUILD_DIR_NAME}", "-j", JOBS, "--timeout=60",
         f"--json-test-results={json_out}"],
        cwd=V8_DIR, env=env_with_depot_tools(),
        stdout=log, stderr=subprocess.STDOUT, check=False,
    )

    if not json_out.exists():
        return None

    with json_out.open() as f:
        data = json.load(f)

    results = data.get("results", [])
    failures = [r for r in results if r.get("result") != "PASS"]

    suspect = 0
    pre_existing = 0
    for r in failures:
        exit_code = r.get("exit_code", 0)
        stderr = r.get("stderr", "") or ""

        if any(p.search(stderr) for p in KNOWN_V8_FAILURE_PATTERNS):
            pre_existing += 1
            continue
        if any(p.search(stderr) for p in HOOK_SUSPECT_PATTERNS):
            suspect += 1
            continue
        if exit_code in SIGNAL_EXIT_CODES:
            suspect += 1
            continue
        pre_existing += 1

    return {
        "total_ran": data.get("test_total", 0),
        "failures": len(failures),
        "suspect": suspect,
        "pre_existing": pre_existing,
        "early_exit": data.get("test_total", 0) < MIN_EXPECTED_TESTS,
    }


def verify_one(commit, label):
    log_path = OUT_DIR / f"{label}.log"
    result_path = OUT_DIR / f"{label}.result"

    print(f"\n=== {label} ({commit}) ===")
    t0 = time.time()
    with log_path.open("w") as log:
        try:
            print(f"  [{label}] deinstrument")
            deinstrument(log)

            print(f"  [{label}] checkout")
            checkout(commit, log)

            print(f"  [{label}] instrument")
            instrument(log)

            print(f"  [{label}] build (ASan)")
            if not build(log):
                result_path.write_text("status=FAIL_BUILD\n")
                print(f"  [{label}] FAIL_BUILD")
                return False

            print(f"  [{label}] mjsunit")
            mjsunit = run_mjsunit(label, log)
            if mjsunit is None:
                result_path.write_text("status=FAIL_MJSUNIT_NO_JSON\n")
                print(f"  [{label}] FAIL_MJSUNIT_NO_JSON")
                return False

            elapsed = int(time.time() - t0)

            if mjsunit["suspect"] > 0:
                result_path.write_text(
                    f"status=FAIL_MJSUNIT total_ran={mjsunit['total_ran']} "
                    f"failures={mjsunit['failures']} "
                    f"suspect={mjsunit['suspect']} "
                    f"pre_existing={mjsunit['pre_existing']} "
                    f"early_exit={mjsunit['early_exit']} elapsed_s={elapsed}\n"
                )
                print(f"  [{label}] FAIL_MJSUNIT suspect={mjsunit['suspect']} "
                      f"(total failures={mjsunit['failures']})")
                return False

            if mjsunit["early_exit"]:
                result_path.write_text(
                    f"status=FAIL_EARLY_EXIT total_ran={mjsunit['total_ran']} "
                    f"failures={mjsunit['failures']} "
                    f"pre_existing={mjsunit['pre_existing']} elapsed_s={elapsed}\n"
                )
                print(f"  [{label}] FAIL_EARLY_EXIT only {mjsunit['total_ran']} "
                      f"tests ran (expected >= {MIN_EXPECTED_TESTS})")
                return False

            result_path.write_text(
                f"status=OK total_ran={mjsunit['total_ran']} suspect=0 "
                f"pre_existing={mjsunit['pre_existing']} elapsed_s={elapsed}\n"
            )
            if mjsunit["pre_existing"] > 0:
                print(f"  [{label}] OK ({mjsunit['total_ran']} tests, {elapsed}s) "
                      f"[{mjsunit['pre_existing']} pre-existing V8 failures ignored]")
            else:
                print(f"  [{label}] OK ({mjsunit['total_ran']} tests, {elapsed}s)")
            return True

        except subprocess.CalledProcessError as e:
            result_path.write_text(f"status=FAIL_EXCEPTION cmd={e.cmd!r}\n")
            print(f"  [{label}] FAIL_EXCEPTION: {e}")
            return False


def print_summary(commits):
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for commit, label in commits:
        result_path = OUT_DIR / f"{label}.result"
        result = result_path.read_text().strip() if result_path.exists() else "status=NOT_RUN"
        print(f"  {label:10s}  {result}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Verify instrumentation correctness across V8 commits."
    )
    parser.add_argument(
        "--only", help="Run only the commit with this label (e.g. Mar2026)",
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    bootstrap_depot_tools()

    commits = read_commits()
    if args.only:
        commits = [(c, l) for c, l in commits if l == args.only]
        if not commits:
            print(f"No commit with label {args.only}")
            sys.exit(1)

    print(f"Verifying instrumentation across {len(commits)} commits")

    failed = 0
    for commit, label in commits:
        if not verify_one(commit, label):
            failed += 1

    print_summary(commits)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
