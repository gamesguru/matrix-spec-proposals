#!/usr/bin/env python3
"""Regenerate the MSC PR-update status table as CSV.

Reads tools/pr_update_status.config.json (edit that file to add/remove MSCs,
branches, or impl repos -- no code changes needed for routine upkeep) and
re-derives everything else straight from git, each time it's run:

  * current PR-branch head SHA / date / subject
  * diff of the MSC's file(s) between the PR branch and the local working ref
  * whether the file set itself changed (a doc got split/renamed/added)
  * dangling references to placeholder MSC numbers (MSC0501, MSC00E4, ...)
  * head of each linked reference-implementation branch, in sibling repos

Usage:
    tools/pr_update_status.py                    # writes CSV to stdout
    tools/pr_update_status.py -o status.csv       # writes CSV to a file
    tools/pr_update_status.py --config other.json

Sibling repo paths in the config ("../../rezzy" etc.) are resolved relative
to this proposals repo's toplevel, matching the ../rezzy / ../continuwuity
layout this was written against. Missing sibling repos or branches are
reported inline in the row rather than aborting the whole run.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path


def run_git(repo: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def head_info(repo: Path, ref: str) -> str:
    log = run_git(repo, "log", "-1", "--format=%h %as %s", ref)
    return log if log else f"<{ref}: not found>"


def diff_stat(repo: Path, ref_a: str, ref_b: str, files: list[str]) -> tuple[int, int]:
    out = run_git(repo, "diff", "--numstat", ref_a, ref_b, "--", *files)
    ins = dele = 0
    if out:
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                ins += int(parts[0])
                dele += int(parts[1])
    return ins, dele


def files_at_ref(repo: Path, ref: str, files: list[str]) -> set[str]:
    """Subset of `files` that actually exist at `ref` (handles splits/renames)."""
    present = set()
    for f in files:
        if run_git(repo, "cat-file", "-e", f"{ref}:{f}") is not None:
            present.add(f)
    return present


def placeholder_refs(repo: Path, local_ref: str, files: list[str], pattern: str) -> str:
    rx = re.compile(pattern)
    counts: dict[str, int] = {}
    for f in files:
        content = run_git(repo, "show", f"{local_ref}:{f}")
        if content is None:
            continue
        for m in rx.finditer(content):
            key = m.group(0).replace(" ", "")
            counts[key] = counts.get(key, 0) + 1
    return "; ".join(f"{k}x{v}" for k, v in sorted(counts.items())) or "-"


def local_line_count(repo: Path, local_ref: str, files: list[str]) -> int:
    total = 0
    for f in files:
        content = run_git(repo, "show", f"{local_ref}:{f}")
        if content is not None:
            total += content.count("\n") + 1
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="path to config JSON (default: tools/pr_update_status.config.json next to this script)")
    ap.add_argument("-o", "--out", default=None, help="CSV output path (default: stdout)")
    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    config_path = Path(args.config) if args.config else script_dir / "pr_update_status.config.json"
    config = json.loads(config_path.read_text())

    repo_root = Path(run_git(script_dir, "rev-parse", "--show-toplevel") or script_dir)
    local_ref = config["local_ref"]
    placeholder_pattern = config["placeholder_pattern"]

    rows = []
    for entry in config["mscs"]:
        msc = entry["msc"]
        pr_branch = entry["pr_branch"]
        files = entry["files"]

        pr_head = head_info(repo_root, pr_branch)
        local_head = head_info(repo_root, local_ref)

        local_files = files_at_ref(repo_root, local_ref, files)
        pr_files = files_at_ref(repo_root, pr_branch, files)
        only_local = local_files - pr_files
        only_pr = pr_files - local_files
        structural = bool(only_local or only_pr)

        ins, dele = diff_stat(repo_root, pr_branch, local_ref, files)
        local_lines = local_line_count(repo_root, local_ref, files)
        refs = placeholder_refs(repo_root, local_ref, files, placeholder_pattern)

        impl_bits = []
        for impl in entry.get("impl_branches", []):
            impl_repo = (repo_root / impl["repo"]).resolve()
            info = head_info(impl_repo, impl["branch"]) if impl_repo.exists() else f"<repo not found: {impl['repo']}>"
            impl_bits.append(f"{impl_repo.name}/{impl['branch']} @ {info}")

        rows.append({
            "msc": msc,
            "pr_number": entry.get("pr_number", ""),
            "pr_branch": pr_branch,
            "pr_head": pr_head,
            "local_ref": local_ref,
            "local_head": local_head,
            "files": "; ".join(files),
            "insertions": ins,
            "deletions": dele,
            "local_total_lines": local_lines,
            "structural_change": "yes" if structural else "no",
            "files_only_in_local": "; ".join(sorted(only_local)) or "-",
            "files_only_in_pr": "; ".join(sorted(only_pr)) or "-",
            "placeholder_msc_refs": refs,
            "impl_branches": " | ".join(impl_bits) or "-",
        })

    fieldnames = list(rows[0].keys()) if rows else []
    out_fh = open(args.out, "w", newline="") if args.out else sys.stdout
    try:
        writer = csv.DictWriter(out_fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if args.out:
            out_fh.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
