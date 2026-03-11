import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass

DEFAULT_V8_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v8")
BUILD_DIR = "out/fuzzbuild"
MAX_LOCATIONS = 1024

SCAN_DIRS = ["src/compiler", "src/maglev"]
EXCLUDE_DIRS = ["src/fuzzilli", "src/compiler/turboshaft"]
FILE_EXTENSIONS = (".cc", ".cpp")

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
COV_H_SOURCE = os.path.join(PROJECT_ROOT, "cov.ours.h")
COV_CC_SOURCE = os.path.join(PROJECT_ROOT, "cov.ours.cc")

# Older V8 versions are missing the case 8 (DEBUG crash) branch in
# fuzzilli.cc that some of our hooks rely on. Inserted before the first
# default: case if absent.
CASE8_SNIPPET = """      case 8: {
#ifdef DEBUG
        IMMEDIATE_CRASH();
#endif
        break;
      }
"""

# (macro_name, needs_broker) — single source of truth for all type mappings.
# Optional variants are derived automatically.
_BASE_TYPES = {
    "MapRef":            ("RECORD_MAPREF",            False),
    "HeapObjectRef":     ("RECORD_HEAPOBJECTREF",     True),
    "JSObjectRef":       ("RECORD_JSOBJECTREF",       True),
    "JSReceiverRef":     ("RECORD_JSRECEIVERREF",     True),
    "NameRef":           ("RECORD_NAMEREF",            True),
    "StringRef":         ("RECORD_STRINGREF",          True),
    "JSFunctionRef":     ("RECORD_JSFUNCTIONREF",      True),
    "FixedArrayBaseRef": ("RECORD_FIXEDARRAYBASEREF",  True),
    "FixedArrayRef":     ("RECORD_FIXEDARRAYREF",      True),
    "ContextRef":        ("RECORD_CONTEXTREF",         True),
}

# type name -> (macro, needs_broker)
TYPE_INFO = {}
for _tn, (_macro, _broker) in _BASE_TYPES.items():
    TYPE_INFO[_tn] = (_macro, _broker)
    TYPE_INFO[f"Optional{_tn}"] = (f"RECORD_OPTIONAL_{_macro.split('_', 1)[1]}", _broker)


@dataclass
class InsertionPoint:
    filepath: str
    match_line: int
    insert_after_line: int
    type_name: str         # e.g. "MapRef", "OptionalHeapObjectRef"
    var_name: str
    broker: str
    mode: str              # "after_line" or "inside_brace"
    brace_pos: int = -1


def classify_type(raw_type):
    clean = raw_type.strip()
    clean = re.sub(r"^const\s+", "", clean)
    clean = re.sub(r"^compiler::", "", clean)
    return clean.strip() if clean.strip() in TYPE_INFO else None


def needs_broker(type_name):
    return TYPE_INFO[type_name][1]


def get_macro(type_name):
    return TYPE_INFO[type_name][0]


def find_source_files(v8_path):
    files = []
    for scan_dir in SCAN_DIRS:
        base = os.path.join(v8_path, scan_dir)
        if not os.path.isdir(base):
            continue
        for root, dirs, filenames in os.walk(base):
            rel = os.path.relpath(root, v8_path)
            if any(rel.startswith(ex) for ex in EXCLUDE_DIRS):
                continue
            for f in sorted(filenames):
                if f.endswith(FILE_EXTENSIONS):
                    files.append(os.path.join(root, f))
    return sorted(files)


_TYPE_NAMES_RE = (
    r"(?:MapRef|HeapObjectRef|JSObjectRef|JSReceiverRef|NameRef|StringRef"
    r"|JSFunctionRef|FixedArrayBaseRef|FixedArrayRef|ContextRef)"
)

def filter_files_with_types(files):
    pattern = re.compile(r"\b(?:Optional)?" + _TYPE_NAMES_RE + r"\b")
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
    # Body brace follows ), >, or C++ trailing keywords (const, override, final,
    # noexcept). Brace init follows an identifier: name_{...}
    if not (prev[-1].isalnum() or prev[-1] == "_"):
        return True
    # Check for trailing keywords that indicate a function body
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


def _search_broker_in_text(text):
    if re.search(r'(?<![\w.>])broker\(\)', text):
        return "broker()"
    # Prefixed broker() - e.g. t->broker(), typer->broker()
    m = re.search(r'(\w+)->broker\(\)', text)
    if m:
        return f"{m.group(1)}->broker()"
    m = re.search(r'(\w+)\.broker\(\)', text)
    if m:
        return f"{m.group(1)}.broker()"
    # broker_ - member variable (word boundary: preceded by non-alnum)
    if re.search(r'(?<![a-zA-Z0-9])broker_(?![a-zA-Z0-9])', text):
        return "broker_"
    # broker - parameter name (word boundary: not part of JSHeapBroker etc.)
    if re.search(r'(?<![a-zA-Z0-9_])broker(?![a-zA-Z0-9_(])', text):
        return "broker"
    return None


def find_broker_in_scope(lines, line_idx):
    brace_depth = 0
    func_start = 0

    for i in range(line_idx, -1, -1):
        line = lines[i]
        brace_depth += line.count("}") - line.count("{")
        if brace_depth < 0:
            func_start = i
            break

    brace_depth = 0
    func_end = len(lines) - 1
    for i in range(func_start, len(lines)):
        line = lines[i]
        brace_depth += line.count("{") - line.count("}")
        if brace_depth == 0 and i > func_start:
            func_end = i
            break

    func_text = "\n".join(lines[func_start : func_end + 1])
    return _search_broker_in_text(func_text)


def find_broker_in_func(lines, sig_start, body_brace_line):
    brace_depth = 0
    body_end = body_brace_line
    for i in range(body_brace_line, min(body_brace_line + 500, len(lines))):
        code = strip_line_comment(lines[i])
        for c in code:
            if c == "{":
                brace_depth += 1
            elif c == "}":
                brace_depth -= 1
        if brace_depth <= 0:
            body_end = i
            break

    func_text = "\n".join(lines[sig_start : body_end + 1])
    return _search_broker_in_text(func_text)


_OPT_TYPE_PATTERN = r"(?:Optional" + _TYPE_NAMES_RE + r"|" + _TYPE_NAMES_RE + r")"

# Regex for variable declarations: [const] [compiler::]Type varname =
DECL_RE = re.compile(
    r"^\s*(const\s+)?(?:compiler::)?(" + _OPT_TYPE_PATTERN + r")\s+(\w+)\s*="
)

# Regex for range-for loops: for ([const] [compiler::]Type varname :
FOR_RE = re.compile(
    r"^\s*for\s*\(\s*(const\s+)?(?:compiler::)?(" + _OPT_TYPE_PATTERN + r")\s+(\w+)\s*:"
)

# Regex for function definitions with target type parameters
FUNC_PARAM_RE = re.compile(
    r"(?:const\s+)?(?:compiler::)?(" + _OPT_TYPE_PATTERN + r")\s+(\w+)"
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

        # Skip preprocessor, macro continuations, already instrumented
        if stripped.startswith("#") or stripped.endswith("\\") or "RECORD_" in line:
            continue

        # Pattern 1: Range-for loop
        m = FOR_RE.search(line)
        if m:
            type_str = m.group(2)
            var_name = m.group(3)
            cat = classify_type(type_str)
            if cat:
                brace_line, brace_col = find_brace_line(lines, i)
                if brace_line is not None:
                    broker = None
                    if needs_broker(cat):
                        broker = find_broker_in_scope(lines, i)
                        if broker is None:
                            continue
                    points.append(InsertionPoint(
                        filepath=filepath, match_line=i,
                        insert_after_line=brace_line,
                        type_name=cat, var_name=var_name,
                        broker=broker, mode="inside_brace",
                        brace_pos=brace_col,
                    ))
                    matched_lines.add(i)
            continue

        # Pattern 2: Variable declaration
        m = DECL_RE.search(line)
        if m and i not in matched_lines:
            # Skip for-loops (handled above) and function parameter defaults.
            # A default parameter value like `Type param = default)` has net
            # negative paren depth on the line (the trailing ) closes the
            # enclosing function's parameter list).
            if re.match(r"^\s*for\s*\(", line):
                continue
            paren_check = 0
            for c in line:
                if c == "(": paren_check += 1
                elif c == ")": paren_check -= 1
            if paren_check < 0:
                continue
            type_str = m.group(2)
            var_name = m.group(3)
            cat = classify_type(type_str)
            if cat:
                semi_line = find_semicolon_line(lines, i)
                broker = None
                if needs_broker(cat):
                    broker = find_broker_in_scope(lines, i)
                    if broker is None:
                        continue
                points.append(InsertionPoint(
                    filepath=filepath, match_line=i,
                    insert_after_line=semi_line,
                    type_name=cat, var_name=var_name,
                    broker=broker, mode="after_line",
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

        # Look for function-like patterns: ) { or ) const {
        # We search for lines with target types that look like parameter lists
        m = FUNC_PARAM_RE.search(line)
        if not m:
            i += 1
            continue

        # Skip lambda parameters: [capture](Type param) { ... }
        # The ](  pattern on the same line as the type match indicates a lambda.
        if re.search(r'\]\s*\(', line):
            i += 1
            continue

        # Collect the full signature by looking backward and forward for ( and {
        # Heuristic: if we find TYPE name followed eventually by ) and {,
        # it's likely a function parameter
        sig_start = i
        sig_lines = []
        match_pos = m.start()

        # Look backwards to find the opening paren.
        # On the match line, only scan characters before the match to avoid
        # counting a closing ) that appears after the type match.
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

        # Now collect forward from sig_start to find closing ) and then {
        paren_depth = 0
        brace_depth = 0
        found_body = False
        found_semi = False
        body_brace_line = None
        for j in range(sig_start, min(sig_start + 20, len(lines))):
            text = strip_line_comment(lines[j])
            for ci, c in enumerate(text):
                if c == "(":
                    paren_depth += 1
                elif c == ")":
                    paren_depth -= 1
                elif c == "{":
                    if brace_depth > 0:
                        brace_depth += 1
                    elif paren_depth <= 0:
                        if is_body_brace(text, ci):
                            found_body = True
                            body_brace_line = j
                            body_brace_col = ci
                            break
                        else:
                            brace_depth += 1
                elif c == "}":
                    if brace_depth > 0:
                        brace_depth -= 1
                elif c == ";" and paren_depth <= 0 and brace_depth == 0:
                    found_semi = True
                    break
            if found_body or found_semi:
                break
            sig_lines.append(text)

        if not found_body or body_brace_line is None:
            i += 1
            continue

        # Extract the full signature text from sig_start to body_brace_line
        sig_text = " ".join(
            strip_line_comment(lines[j])
            for j in range(sig_start, body_brace_line + 1)
        )

        # Check if this is actually a function definition (not a for-loop, if, etc.)
        sig_before_paren = sig_text[:sig_text.find("(")].strip()
        # Function defs typically have a return type and function name before (
        # Skip control flow: for, if, while, switch
        if re.search(r"\b(for|if|while|switch|catch)\s*$", sig_before_paren):
            i += 1
            continue

        # Find all target type parameters in the signature
        # Extract just the parameter list
        paren_start = sig_text.find("(")
        paren_end = sig_text.rfind(")")
        if paren_start == -1 or paren_end == -1:
            i += 1
            continue
        param_text = sig_text[paren_start + 1 : paren_end]

        for pm in FUNC_PARAM_RE.finditer(param_text):
            type_str = pm.group(1)
            var_name = pm.group(2)
            cat = classify_type(type_str)
            if not cat:
                continue

            # Skip unnamed parameters
            if not var_name or var_name in ("const",):
                continue

            broker = None
            if needs_broker(cat):
                broker = find_broker_in_func(lines, sig_start, body_brace_line)
                if broker is None:
                    continue

            points.append(InsertionPoint(
                filepath=filepath, match_line=sig_start,
                insert_after_line=body_brace_line,
                type_name=cat, var_name=var_name,
                broker=broker, mode="inside_brace",
                brace_pos=body_brace_col,
            ))

        i = body_brace_line + 1 if body_brace_line else i + 1

    return points


def build_macro_text(point, location_id, indent="  "):
    macro = get_macro(point.type_name)
    if needs_broker(point.type_name):
        return f"{indent}{macro}({point.var_name}, {point.broker}, {location_id});"
    else:
        return f"{indent}{macro}({point.var_name}, {location_id});"


def get_indent(line):
    m = re.match(r"^(\s*)", line)
    return m.group(1) if m else ""


def apply_insertions(filepath, lines, points, location_start):
    # Sort by insert_after_line descending so we can insert without shifting
    sorted_points = sorted(points, key=lambda p: p.insert_after_line, reverse=True)

    loc_id = location_start + len(points) - 1
    for point in sorted_points:
        target_line = lines[point.insert_after_line]
        indent = get_indent(target_line)
        if not indent:
            indent = "  "

        macro_text = build_macro_text(point, loc_id, indent)
        loc_id -= 1

        if point.mode == "inside_brace":
            # Insert after the body { on the target line, using stored position.
            # Re-find the brace because previous insertions may have shifted text.
            brace_pos = point.brace_pos
            # If the line was modified by a prior insertion on the same line,
            # find the correct body brace by scanning with is_body_brace.
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
                # If after_brace was just whitespace/empty, avoid trailing space
                if not after_brace.strip():
                    lines[point.insert_after_line] = (
                        target_line[: brace_pos + 1] + "\n" + macro_text
                    )
        else:
            # Insert after the line
            lines.insert(point.insert_after_line + 1, macro_text)

    return lines, len(points)


def add_include(lines, filepath):
    include_line = '#include "src/fuzzilli/cov.h"'
    for line in lines:
        if include_line in line:
            return lines

    # Insert after the last #include block
    last_include = -1
    for i, line in enumerate(lines):
        if line.strip().startswith("#include"):
            last_include = i

    if last_include >= 0:
        lines.insert(last_include + 1, include_line)
    else:
        lines.insert(0, include_line)

    return lines


def instrument(v8_path, build_dir, jobs):
    print("Installing cov.h, cov.cc and patching fuzzilli.cc...")
    install_cov_files(v8_path)
    patch_fuzzilli_case8(v8_path)

    print(f"Scanning {v8_path} for instrumentation points...")

    files = find_source_files(v8_path)
    relevant_files = filter_files_with_types(files)
    print(f"Found {len(relevant_files)} files containing target types")

    # Phase 1: Collect all insertion points across all files
    all_points = {}
    total = 0
    for filepath in relevant_files:
        with open(filepath) as f:
            lines = f.read().split("\n")

        comment_mask = compute_comment_mask(lines)
        points = find_insertion_points(filepath, lines, comment_mask)
        func_points = find_func_param_points(filepath, lines, comment_mask)

        # Deduplicate: don't add function parameter points for lines
        # already covered by declaration/for-loop points
        covered_lines = {(p.insert_after_line, p.var_name) for p in points}
        for fp in func_points:
            key = (fp.insert_after_line, fp.var_name)
            if key not in covered_lines:
                points.append(fp)
                covered_lines.add(key)

        if points:
            all_points[filepath] = points
            total += len(points)

    if total > MAX_LOCATIONS:
        print(f"Warning: {total} insertion points exceeds MAX_LOCATIONS ({MAX_LOCATIONS})")
        print("Truncating to first {MAX_LOCATIONS} points")

    print(f"Found {total} instrumentation points across {len(all_points)} files")

    # Phase 2: Assign location IDs and apply insertions
    location_id = 0
    modified_files = []
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
        modified_files.append(filepath)

        with open(filepath, "w") as f:
            f.write("\n".join(lines))

        rel = os.path.relpath(filepath, v8_path)
        stats[rel] = count
        print(f"  {rel}: {count} macro(s) inserted")

    print(f"\nTotal: {location_id} instrumentation points (location IDs 0..{location_id - 1})")

    print("\nBreakdown by type:")
    type_counts = {}
    for filepath, points in all_points.items():
        for p in points:
            type_counts[p.type_name] = type_counts.get(p.type_name, 0) + 1
    for tn, count in sorted(type_counts.items()):
        print(f"  {get_macro(tn)}: {count}")

    # Phase 4: Build
    if jobs > 0:
        build_v8(v8_path, build_dir, jobs)

    return location_id


def find_ninja(v8_path):
    candidates = [
        os.path.join(v8_path, "third_party", "ninja", "ninja"),
        os.path.join(v8_path, "third_party", "depot_tools", "ninja"),
        "ninja",
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    # Fall back to PATH
    return "ninja"


def build_v8(v8_path, build_dir, jobs):
    build_path = os.path.join(v8_path, build_dir)
    if not os.path.isdir(build_path):
        print(f"Build directory {build_path} does not exist.")
        print("Run gn gen first with appropriate args.")
        sys.exit(1)

    ninja = find_ninja(v8_path)
    print(f"\nBuilding V8 with {ninja} -j {jobs}...")
    ret = subprocess.run(
        [ninja, "-C", build_path, "-j", str(jobs), "d8"],
        cwd=v8_path,
    )
    if ret.returncode != 0:
        print("Build failed!")
        sys.exit(1)
    print("Build succeeded.")


def install_cov_files(v8_path):
    fuzzilli_dir = os.path.join(v8_path, "src", "fuzzilli")
    os.makedirs(fuzzilli_dir, exist_ok=True)
    with open(COV_H_SOURCE) as f:
        cov_h = f.read()
    with open(COV_CC_SOURCE) as f:
        cov_cc = f.read()
    with open(os.path.join(fuzzilli_dir, "cov.h"), "w") as f:
        f.write(cov_h)
    with open(os.path.join(fuzzilli_dir, "cov.cc"), "w") as f:
        f.write(cov_cc)
    print(f"  Installed cov.h, cov.cc in {fuzzilli_dir}")


def patch_fuzzilli_case8(v8_path):
    target = os.path.join(v8_path, "src", "fuzzilli", "fuzzilli.cc")
    if not os.path.isfile(target):
        return
    with open(target) as f:
        text = f.read()
    if "case 8:" in text:
        return
    new_text = text.replace("      default:", CASE8_SNIPPET + "      default:", 1)
    if new_text == text:
        print(f"  Warning: could not find 'default:' in {target}, skipping case 8 patch")
        return
    with open(target, "w") as f:
        f.write(new_text)
    print(f"  Patched {target}")


def revert_fuzzilli_files(v8_path):
    paths = ["src/fuzzilli/cov.h", "src/fuzzilli/cov.cc", "src/fuzzilli/fuzzilli.cc"]
    subprocess.run(["git", "checkout", "--"] + paths, cwd=v8_path, check=False)


def deinstrument(v8_path, build_dir, jobs):
    print("Reverting instrumentation...")

    for scan_dir in SCAN_DIRS:
        full_dir = os.path.join(v8_path, scan_dir)
        if os.path.isdir(full_dir):
            ret = subprocess.run(
                ["git", "checkout", "--", scan_dir],
                cwd=v8_path,
            )
            if ret.returncode != 0:
                print(f"Warning: git checkout failed for {scan_dir}")

    revert_fuzzilli_files(v8_path)

    print("Source files reverted.")
    build_v8(v8_path, build_dir, jobs)
    print("Deinstrument complete.")


def validate(v8_path):
    print("Validating instrumentation...")

    files = find_source_files(v8_path)
    relevant_files = filter_files_with_types(files)

    # Match RECORD_* macros. The last argument (before closing paren) is the
    # location ID. We allow nested parens (e.g., broker()) by matching greedily.
    macro_re = re.compile(
        r"\bRECORD_(?:OPTIONAL_)?"
        r"(?:MAPREF|HEAPOBJECTREF|JSOBJECTREF|JSRECEIVERREF|NAMEREF|STRINGREF"
        r"|JSFUNCTIONREF|FIXEDARRAYBASEREF|FIXEDARRAYREF|CONTEXTREF)"
        r"\s*\(.*,\s*(\d+)\s*\)"
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
                            f"Duplicate location ID {loc_id} in "
                            f"{os.path.relpath(filepath, v8_path)}:{line_no}"
                        )
                    location_ids.add(loc_id)
                    if loc_id >= MAX_LOCATIONS:
                        issues.append(
                            f"Location ID {loc_id} >= MAX_LOCATIONS in "
                            f"{os.path.relpath(filepath, v8_path)}:{line_no}"
                        )

    print(f"Found {total_macros} RECORD_* macros")
    if location_ids:
        print(f"Location ID range: {min(location_ids)}..{max(location_ids)}")
        expected = set(range(min(location_ids), max(location_ids) + 1))
        missing = expected - location_ids
        if missing:
            issues.append(f"Missing location IDs: {sorted(missing)[:10]}...")

    if issues:
        print(f"\n{len(issues)} issue(s) found:")
        for issue in issues[:20]:
            print(f"  - {issue}")
    else:
        print("Validation passed: no issues found")

    return len(issues) == 0


def main():
    parser = argparse.ArgumentParser(
        description="Instrument V8 source with type coverage recording"
    )
    parser.add_argument(
        "v8_path", nargs="?", default=DEFAULT_V8_PATH,
        help=f"Path to V8 source (default: {DEFAULT_V8_PATH})",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--instrument", action="store_true",
                       help="Instrument V8 and build")
    group.add_argument("--deinstrument", action="store_true",
                       help="Revert instrumentation and rebuild")
    group.add_argument("--validate", action="store_true",
                       help="Validate current instrumentation")
    group.add_argument("--dry-run", action="store_true",
                       help="Show what would be instrumented without modifying files")
    parser.add_argument("--build-dir", default=BUILD_DIR,
                       help=f"V8 build directory (default: {BUILD_DIR})")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count(),
                       help="Number of build jobs (default: all CPUs)")
    parser.add_argument("--no-build", action="store_true",
                       help="Skip the build step")
    args = parser.parse_args()

    v8_path = os.path.abspath(args.v8_path)
    if not os.path.isdir(v8_path):
        print(f"Error: {v8_path} is not a directory")
        sys.exit(1)

    if args.instrument:
        # Check that source is clean before instrumenting
        ret = subprocess.run(
            ["git", "diff", "--stat", "--"] + SCAN_DIRS,
            cwd=v8_path, capture_output=True, text=True,
        )
        if ret.stdout.strip():
            print("Error: V8 source has uncommitted changes in compiler dirs.")
            print("Run --deinstrument first or commit your changes.")
            print(ret.stdout)
            sys.exit(1)

        count = instrument(v8_path, args.build_dir,
                          args.jobs if not args.no_build else 0)
        validate(v8_path)

    elif args.deinstrument:
        if args.no_build:
            for scan_dir in SCAN_DIRS:
                subprocess.run(["git", "checkout", "--", scan_dir], cwd=v8_path)
            revert_fuzzilli_files(v8_path)
            print("Source files reverted.")
        else:
            deinstrument(v8_path, args.build_dir, args.jobs)

    elif args.validate:
        validate(v8_path)

    elif args.dry_run:
        files = find_source_files(v8_path)
        relevant_files = filter_files_with_types(files)
        print(f"Found {len(relevant_files)} files containing target types\n")

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
                rel = os.path.relpath(filepath, v8_path)
                print(f"{rel}: {len(points)} point(s)")
                for p in points:
                    macro = get_macro(p.type_name)
                    broker_str = f", {p.broker}" if p.broker else ""
                    print(f"  L{p.match_line + 1}: {macro}({p.var_name}{broker_str}, <id>)")
                total += len(points)

        print(f"\nTotal: {total} instrumentation points")


if __name__ == "__main__":
    main()
