import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_JSC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jsc")
BUILD_DIR = "WebKitBuild/Fuzzilli"
MAX_LOCATIONS = 1024

SCAN_DIRS = [
    "Source/JavaScriptCore/dfg",
    "Source/JavaScriptCore/ftl",
]
FILE_EXTENSIONS = (".cpp", ".h")

MACRO_NAME = "RECORD_JSC_STRUCTURE"


@dataclass
class InsertionPoint:
    filepath: str
    match_line: int
    insert_after_line: int
    var_name: str
    mode: str              # "after_line" or "inside_brace"
    brace_pos: int = -1


def find_source_files(jsc_path):
    files = []
    for scan_dir in SCAN_DIRS:
        base = os.path.join(jsc_path, scan_dir)
        if not os.path.isdir(base):
            continue
        for root, dirs, filenames in os.walk(base):
            for f in sorted(filenames):
                if f.endswith(FILE_EXTENSIONS):
                    files.append(os.path.join(root, f))
    return sorted(files)


def filter_files_with_type(files):
    pattern = re.compile(r"\bRegisteredStructure\b")
    return [f for f in files if pattern.search(open(f).read())]


def compute_comment_mask(lines):
    mask = []
    in_block = False
    for line in lines:
        mask.append(in_block)
        j = 0
        while j < len(line):
            if in_block:
                end = line.find("*/", j)
                if end == -1:
                    break
                in_block = False
                j = end + 2
            else:
                start = line.find("/*", j)
                single = line.find("//", j)
                if single != -1 and (start == -1 or single < start):
                    break
                if start == -1:
                    break
                end = line.find("*/", start + 2)
                if end == -1:
                    in_block = True
                    break
                j = end + 2
    return mask


def strip_line_comment(line):
    in_str = None
    i = 0
    while i < len(line):
        c = line[i]
        if in_str:
            if c == "\\" and i + 1 < len(line):
                i += 2
                continue
            if c == in_str:
                in_str = None
        else:
            if c in ('"', "'"):
                in_str = c
            elif c == "/" and i + 1 < len(line) and line[i + 1] == "/":
                return line[:i]
        i += 1
    return line


def find_semicolon_line(lines, start_line):
    paren_depth = 0
    brace_depth = 0
    for i in range(start_line, min(start_line + 30, len(lines))):
        code = strip_line_comment(lines[i])
        for c in code:
            if c == "(":
                paren_depth += 1
            elif c == ")":
                paren_depth -= 1
            elif c == "{":
                brace_depth += 1
            elif c == "}":
                brace_depth -= 1
            elif c == ";" and paren_depth <= 0 and brace_depth <= 0:
                return i
    return start_line


def is_body_brace(code, pos):
    prev = code[:pos].rstrip()
    if not prev:
        return True
    if not (prev[-1].isalnum() or prev[-1] == "_"):
        return True
    for kw in ("const", "override", "final", "noexcept", "default", "delete"):
        if prev.endswith(kw):
            before_kw = prev[: -len(kw)].rstrip()
            if not before_kw or not (before_kw[-1].isalnum() or before_kw[-1] == "_"):
                return True
    return False


def find_brace_line(lines, start_line):
    paren_depth = 0
    brace_depth = 0
    for i in range(start_line, min(start_line + 15, len(lines))):
        code = strip_line_comment(lines[i])
        for ci, c in enumerate(code):
            if c == "(":
                paren_depth += 1
            elif c == ")":
                paren_depth -= 1
            elif c == "{":
                if brace_depth > 0:
                    brace_depth += 1
                elif paren_depth <= 0:
                    if is_body_brace(code, ci):
                        return i, ci
                    else:
                        brace_depth += 1
            elif c == "}":
                if brace_depth > 0:
                    brace_depth -= 1
    return None, None


# Regex for variable declarations: RegisteredStructure varname =
DECL_RE = re.compile(
    r"^\s*(?:auto\s+)?(?:DFG::)?RegisteredStructure\s+(\w+)\s*="
)

# Regex for range-for: for (RegisteredStructure varname :
FOR_RE = re.compile(
    r"^\s*for\s*\(\s*(?:const\s+)?(?:DFG::)?RegisteredStructure\s+(\w+)\s*:"
)

# Regex for function parameters with RegisteredStructure
FUNC_PARAM_RE = re.compile(
    r"(?:DFG::)?RegisteredStructure\s+(\w+)"
)


def find_insertion_points(filepath, lines, comment_mask=None):
    if comment_mask is None:
        comment_mask = compute_comment_mask(lines)
    points = []
    matched_lines = set()

    for i, raw_line in enumerate(lines):
        if comment_mask[i]:
            continue

        line = strip_line_comment(raw_line)
        stripped = line.strip()

        if stripped.startswith("#") or stripped.endswith("\\") or MACRO_NAME in line:
            continue

        # Pattern 1: Range-for loop
        m = FOR_RE.search(line)
        if m:
            var_name = m.group(1)
            # Find the closing ) of the for(...) first
            paren_depth = 0
            close_paren_line = i
            for j in range(i, min(i + 5, len(lines))):
                for c in strip_line_comment(lines[j]):
                    if c == "(":
                        paren_depth += 1
                    elif c == ")":
                        paren_depth -= 1
                        if paren_depth == 0:
                            close_paren_line = j
                            break
                if paren_depth == 0:
                    break
            # Check if next non-empty line after ) has a {
            brace_line, brace_col = find_brace_line(lines, close_paren_line)
            if brace_line is not None and brace_line - i <= 3:
                points.append(InsertionPoint(
                    filepath=filepath, match_line=i,
                    insert_after_line=brace_line,
                    var_name=var_name, mode="inside_brace",
                    brace_pos=brace_col,
                ))
                matched_lines.add(i)
            else:
                # Braceless for-loop: skip (variable out of scope after loop)
                pass
            continue

        # Pattern 2: Variable declaration
        m = DECL_RE.search(line)
        if m and i not in matched_lines:
            if re.match(r"^\s*for\s*\(", line):
                continue
            paren_check = 0
            for c in line:
                if c == "(":
                    paren_check += 1
                elif c == ")":
                    paren_check -= 1
            if paren_check < 0:
                continue
            var_name = m.group(1)
            semi_line = find_semicolon_line(lines, i)
            points.append(InsertionPoint(
                filepath=filepath, match_line=i,
                insert_after_line=semi_line,
                var_name=var_name, mode="after_line",
            ))
            matched_lines.add(i)

    return points


def find_func_param_points(filepath, lines, comment_mask=None):
    if comment_mask is None:
        comment_mask = compute_comment_mask(lines)
    points = []

    i = 0
    while i < len(lines):
        if comment_mask[i]:
            i += 1
            continue

        line = strip_line_comment(lines[i])

        m = FUNC_PARAM_RE.search(line)
        if not m:
            i += 1
            continue

        # Skip lambda parameters
        if re.search(r'\]\s*\(', line):
            i += 1
            continue

        sig_start = i
        match_pos = m.start()

        paren_depth = 0
        found_open = False
        for j in range(i, max(i - 10, -1), -1):
            text = strip_line_comment(lines[j])
            scan_text = text[:match_pos] if j == i else text
            for c in reversed(scan_text):
                if c == ")":
                    paren_depth += 1
                elif c == "(":
                    paren_depth -= 1
                    if paren_depth < 0:
                        found_open = True
                        sig_start = j
                        break
            if found_open:
                break

        if not found_open:
            i += 1
            continue

        paren_depth = 0
        found_body = False
        body_brace_line = None
        body_brace_col = 0
        found_semi = False
        for j in range(sig_start, min(sig_start + 20, len(lines))):
            text = strip_line_comment(lines[j])
            for ci, c in enumerate(text):
                if c == "(":
                    paren_depth += 1
                elif c == ")":
                    paren_depth -= 1
                elif c == "{":
                    if paren_depth <= 0:
                        if is_body_brace(text, ci):
                            found_body = True
                            body_brace_line = j
                            body_brace_col = ci
                            break
                elif c == ";" and paren_depth <= 0:
                    found_semi = True
                    break
            if found_body or found_semi:
                break

        if not found_body or body_brace_line is None:
            i += 1
            continue

        sig_text = " ".join(
            strip_line_comment(lines[j])
            for j in range(sig_start, body_brace_line + 1)
        )

        sig_before_paren = sig_text[:sig_text.find("(")].strip()
        if re.search(r"\b(for|if|while|switch|catch)\s*$", sig_before_paren):
            i += 1
            continue

        paren_start = sig_text.find("(")
        paren_end = sig_text.rfind(")")
        if paren_start == -1 or paren_end == -1:
            i += 1
            continue
        param_text = sig_text[paren_start + 1 : paren_end]

        for pm in FUNC_PARAM_RE.finditer(param_text):
            var_name = pm.group(1)
            if not var_name or var_name in ("const",):
                continue
            points.append(InsertionPoint(
                filepath=filepath, match_line=sig_start,
                insert_after_line=body_brace_line,
                var_name=var_name, mode="inside_brace",
                brace_pos=body_brace_col,
            ))

        i = body_brace_line + 1 if body_brace_line else i + 1

    return points


def build_macro_text(point, location_id, indent="    "):
    return f"{indent}{MACRO_NAME}({point.var_name}, {location_id});"


def get_indent(line):
    m = re.match(r"^(\s*)", line)
    return m.group(1) if m else ""


def apply_insertions(filepath, lines, points, location_start):
    sorted_points = sorted(points, key=lambda p: p.insert_after_line, reverse=True)

    loc_id = location_start + len(points) - 1
    for point in sorted_points:
        target_line = lines[point.insert_after_line]
        indent = get_indent(target_line)
        if not indent:
            indent = "    "

        macro_text = build_macro_text(point, loc_id, indent)
        loc_id -= 1

        if point.mode == "inside_brace":
            brace_pos = point.brace_pos
            if brace_pos >= len(target_line) or target_line[brace_pos] != "{":
                brace_pos = -1
                for ci, c in enumerate(target_line):
                    if c == "{" and is_body_brace(target_line, ci):
                        brace_pos = ci
                        break
            if brace_pos != -1:
                after_brace = target_line[brace_pos + 1:]
                lines[point.insert_after_line] = (
                    target_line[: brace_pos + 1]
                    + "\n"
                    + macro_text
                    + after_brace.rstrip()
                )
                if not after_brace.strip():
                    lines[point.insert_after_line] = (
                        target_line[: brace_pos + 1] + "\n" + macro_text
                    )
        else:
            lines.insert(point.insert_after_line + 1, macro_text)

    return lines, len(points)


def add_include(lines, filepath):
    include_line = '#include "FuzzilliTypeCov.h"'
    for line in lines:
        if include_line in line:
            return lines

    last_include = -1
    for i, line in enumerate(lines):
        if line.strip().startswith("#include"):
            last_include = i

    if last_include >= 0:
        lines.insert(last_include + 1, include_line)
    else:
        lines.insert(0, include_line)

    return lines


def write_type_cov_header(jsc_path):
    header_path = os.path.join(
        jsc_path, "Source/JavaScriptCore/fuzzilli/FuzzilliTypeCov.h"
    )
    content = """\
#pragma once

#include <cstdint>

#if ENABLE(FUZZILLI)

#define TYPE_COV_MAX_LOCATIONS 1024
#define TYPE_COV_MAX_TYPES     4096
#define TYPE_COV_BITMAP_SIZE   ((TYPE_COV_MAX_LOCATIONS * TYPE_COV_MAX_TYPES) / 8)

void record_jsc_type(uint16_t location_id, uint16_t type_id);

// Encode JSType (8 bits) + IndexingType (4 bits) = 12 bits, max 4095
#define RECORD_JSC_STRUCTURE(structure, location) \\
  do { \\
    auto* _s = (structure).get(); \\
    if (_s) { \\
      uint16_t _tid = static_cast<uint16_t>(_s->typeInfo().type()) \\
          | (static_cast<uint16_t>(_s->indexingType() & 0xF) << 8); \\
      record_jsc_type((location), _tid); \\
    } \\
  } while (0)

#else

#define RECORD_JSC_STRUCTURE(structure, location) ((void)0)

#endif
"""
    with open(header_path, "w") as f:
        f.write(content)
    print(f"  Wrote {header_path}")


def patch_fuzzilli_cpp(jsc_path):
    cpp_path = os.path.join(
        jsc_path, "Source/JavaScriptCore/fuzzilli/Fuzzilli.cpp"
    )
    with open(cpp_path) as f:
        content = f.read()

    if "record_jsc_type" in content:
        print("  Fuzzilli.cpp already patched")
        return

    # Bump SHM_SIZE to 0x200000 to make room for type bitmap
    if "#define SHM_SIZE 0x100000" in content:
        content = content.replace(
            "#define SHM_SIZE 0x100000",
            "#define SHM_SIZE 0x200000"
        )
        content = content.replace(
            "#define MAX_EDGES ((SHM_SIZE - 4) * 8)",
            "#define TYPE_COV_OFFSET (SHM_SIZE - TYPE_COV_BITMAP_SIZE)\n"
            "#define MAX_EDGES ((TYPE_COV_OFFSET - 4) * 8)"
        )

    # Add include for the type coverage header
    content = content.replace(
        '#include "Fuzzilli.h"',
        '#include "Fuzzilli.h"\n#include "FuzzilliTypeCov.h"'
    )

    # Add record_jsc_type function before the #endif
    type_func = """
void record_jsc_type(uint16_t location_id, uint16_t type_id) {
    if (!Fuzzilli::sharedData) return;
    if (location_id >= TYPE_COV_MAX_LOCATIONS ||
        type_id >= TYPE_COV_MAX_TYPES) return;

    uint8_t* type_bits = reinterpret_cast<uint8_t*>(Fuzzilli::sharedData) + TYPE_COV_OFFSET;
    uint32_t idx =
        static_cast<uint32_t>(location_id) * TYPE_COV_MAX_TYPES + type_id;
    type_bits[idx >> 3] |= (1 << (idx & 7));
}
"""

    # Insert before the final #endif
    last_endif = content.rfind("#endif")
    if last_endif != -1:
        content = content[:last_endif] + type_func + "\n" + content[last_endif:]

    with open(cpp_path, "w") as f:
        f.write(content)
    print(f"  Patched {cpp_path}")


def instrument(jsc_path, build_dir, jobs):
    print(f"Scanning {jsc_path} for instrumentation points...")

    files = find_source_files(jsc_path)
    relevant_files = filter_files_with_type(files)
    print(f"Found {len(relevant_files)} files containing RegisteredStructure")

    all_points = {}
    total = 0
    for filepath in relevant_files:
        with open(filepath) as f:
            lines = f.read().split("\n")

        comment_mask = compute_comment_mask(lines)
        points = find_insertion_points(filepath, lines, comment_mask)
        func_points = find_func_param_points(filepath, lines, comment_mask)

        covered = {(p.insert_after_line, p.var_name) for p in points}
        for fp in func_points:
            key = (fp.insert_after_line, fp.var_name)
            if key not in covered:
                points.append(fp)
                covered.add(key)

        if points:
            all_points[filepath] = points
            total += len(points)

    if total > MAX_LOCATIONS:
        print(f"Warning: {total} points exceeds MAX_LOCATIONS ({MAX_LOCATIONS})")

    print(f"Found {total} instrumentation points across {len(all_points)} files")

    # Write type coverage header
    write_type_cov_header(jsc_path)

    # Patch Fuzzilli.cpp
    patch_fuzzilli_cpp(jsc_path)

    # Apply insertions
    location_id = 0
    stats = {}
    for filepath in sorted(all_points.keys()):
        points = all_points[filepath]
        remaining = MAX_LOCATIONS - location_id
        if remaining <= 0:
            break
        if len(points) > remaining:
            points = points[:remaining]

        with open(filepath) as f:
            lines = f.read().split("\n")

        lines, count = apply_insertions(filepath, lines, points, location_id)
        location_id += count

        lines = add_include(lines, filepath)

        with open(filepath, "w") as f:
            f.write("\n".join(lines))

        rel = os.path.relpath(filepath, jsc_path)
        stats[rel] = count
        print(f"  {rel}: {count} hook(s) inserted")

    print(f"\nTotal: {location_id} instrumentation points (IDs 0..{location_id - 1})")

    if jobs > 0:
        build_jsc(jsc_path, build_dir, jobs)

    return location_id


def build_jsc(jsc_path, build_dir, jobs):
    build_path = os.path.join(jsc_path, build_dir)
    if not os.path.isdir(build_path):
        print(f"Build directory {build_path} does not exist. Run fuzzbuild.sh first.")
        sys.exit(1)

    print(f"\nRebuilding JSC with ninja -j {jobs}...")
    ret = subprocess.run(
        ["ninja", "-C", build_path, "-j", str(jobs), "bin/jsc"],
    )
    if ret.returncode != 0:
        print("Build failed!")
        sys.exit(1)
    print("Build succeeded.")


def deinstrument(jsc_path, build_dir, jobs):
    print("Reverting instrumentation...")
    for scan_dir in SCAN_DIRS:
        full_dir = os.path.join(jsc_path, scan_dir)
        if os.path.isdir(full_dir):
            subprocess.run(["git", "checkout", "--", scan_dir], cwd=jsc_path)

    # Also revert Fuzzilli.cpp
    subprocess.run(
        ["git", "checkout", "--", "Source/JavaScriptCore/fuzzilli/Fuzzilli.cpp"],
        cwd=jsc_path,
    )

    # Remove generated header
    header = os.path.join(
        jsc_path, "Source/JavaScriptCore/fuzzilli/FuzzilliTypeCov.h"
    )
    if os.path.exists(header):
        os.remove(header)

    print("Source files reverted.")
    if jobs > 0:
        build_jsc(jsc_path, build_dir, jobs)


def validate(jsc_path):
    print("Validating instrumentation...")

    files = find_source_files(jsc_path)
    relevant_files = filter_files_with_type(files)

    macro_re = re.compile(
        r"\b" + MACRO_NAME + r"\s*\(\s*\w+\s*,\s*(\d+)\s*\)"
    )

    total_macros = 0
    location_ids = set()
    issues = []

    for filepath in relevant_files:
        with open(filepath) as f:
            for line_no, line in enumerate(f, 1):
                for m in macro_re.finditer(line):
                    total_macros += 1
                    loc_id = int(m.group(1))
                    if loc_id in location_ids:
                        issues.append(
                            f"Duplicate ID {loc_id} in "
                            f"{os.path.relpath(filepath, jsc_path)}:{line_no}"
                        )
                    location_ids.add(loc_id)
                    if loc_id >= MAX_LOCATIONS:
                        issues.append(
                            f"ID {loc_id} >= MAX_LOCATIONS in "
                            f"{os.path.relpath(filepath, jsc_path)}:{line_no}"
                        )

    print(f"Found {total_macros} {MACRO_NAME} macros")
    if location_ids:
        print(f"Location ID range: {min(location_ids)}..{max(location_ids)}")
        expected = set(range(min(location_ids), max(location_ids) + 1))
        missing = expected - location_ids
        if missing:
            issues.append(f"Missing IDs: {sorted(missing)[:10]}...")

    if issues:
        print(f"\n{len(issues)} issue(s):")
        for issue in issues[:20]:
            print(f"  - {issue}")
    else:
        print("Validation passed")

    return len(issues) == 0


def main():
    parser = argparse.ArgumentParser(
        description="Instrument JavaScriptCore with type coverage recording"
    )
    parser.add_argument(
        "jsc_path", nargs="?", default=DEFAULT_JSC_PATH,
        help=f"Path to WebKit source (default: {DEFAULT_JSC_PATH})",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--instrument", action="store_true")
    group.add_argument("--deinstrument", action="store_true")
    group.add_argument("--validate", action="store_true")
    group.add_argument("--dry-run", action="store_true")
    parser.add_argument("--build-dir", default=BUILD_DIR)
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count())
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()

    jsc_path = os.path.abspath(args.jsc_path)
    if not os.path.isdir(jsc_path):
        print(f"Error: {jsc_path} is not a directory")
        sys.exit(1)

    if args.instrument:
        ret = subprocess.run(
            ["git", "diff", "--stat", "--"] + SCAN_DIRS,
            cwd=jsc_path, capture_output=True, text=True,
        )
        if ret.stdout.strip():
            print("Error: source has uncommitted changes in scan dirs.")
            print("Run --deinstrument first.")
            print(ret.stdout)
            sys.exit(1)

        instrument(jsc_path, args.build_dir,
                   args.jobs if not args.no_build else 0)
        validate(jsc_path)

    elif args.deinstrument:
        deinstrument(jsc_path, args.build_dir,
                     args.jobs if not args.no_build else 0)

    elif args.validate:
        validate(jsc_path)

    elif args.dry_run:
        files = find_source_files(jsc_path)
        relevant_files = filter_files_with_type(files)
        print(f"Found {len(relevant_files)} files containing RegisteredStructure\n")

        total = 0
        for filepath in sorted(relevant_files):
            with open(filepath) as f:
                lines = f.read().split("\n")
            comment_mask = compute_comment_mask(lines)
            points = find_insertion_points(filepath, lines, comment_mask)
            func_points = find_func_param_points(filepath, lines, comment_mask)
            covered = {(p.insert_after_line, p.var_name) for p in points}
            for fp in func_points:
                if (fp.insert_after_line, fp.var_name) not in covered:
                    points.append(fp)
                    covered.add((fp.insert_after_line, fp.var_name))

            if points:
                rel = os.path.relpath(filepath, jsc_path)
                print(f"{rel}: {len(points)} point(s)")
                for p in points:
                    print(f"  L{p.match_line + 1}: {MACRO_NAME}({p.var_name}, <id>)")
                total += len(points)

        print(f"\nTotal: {total} instrumentation points")


if __name__ == "__main__":
    main()
