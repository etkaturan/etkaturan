#!/usr/bin/env python3
"""Validate the profile repository before a change is merged.

Every check here corresponds to something that actually broke on the live
profile. This is a regression suite for the repo itself, not a style linter.

Run locally with:

    python .github/scripts/validate_profile.py

Exits non-zero if any check fails.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: pip install pyyaml")


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKFLOW_GLOB = ".github/workflows/*.yml"

failures: list[str] = []
notes: list[str] = []


def fail(check: str, message: str) -> None:
    failures.append(f"{check}: {message}")
    print(f"  FAIL  {message}")


def ok(message: str) -> None:
    print(f"  ok    {message}")


def posix(path: str) -> str:
    """Print paths with forward slashes on every platform.

    Keeps Windows output comparable to CI output and to the README, which
    always uses forward slashes.
    """
    return path.replace(os.sep, "/")


def load_workflows() -> dict[str, dict]:
    """Parse every workflow once and reuse it across checks."""
    parsed = {}
    for path in sorted(glob.glob(WORKFLOW_GLOB)):
        try:
            parsed[path] = yaml.safe_load(open(path, encoding="utf-8"))
        except yaml.YAMLError as exc:
            fail("workflow-yaml", f"{posix(path)} is not valid YAML: {exc}")
    return parsed


def check_readme_assets() -> None:
    """Every local image the README points at must exist.

    This is the check that would have caught the five broken images on the
    live profile: the README referenced generated SVGs before the workflows
    that produce them had ever run.
    """
    print("\nREADME asset references")
    readme = open("README.md", encoding="utf-8").read()
    refs = sorted(set(re.findall(r'(?:src|srcset)="(assets/[^"]+)"', readme)))

    if not refs:
        fail("readme-assets", "no asset references found — did the pattern break?")
        return

    for ref in refs:
        if os.path.isfile(ref):
            ok(ref)
        else:
            fail("readme-assets", f"{ref} is referenced by README.md but does not exist")


def check_svgs_parse() -> None:
    """Every SVG must be well-formed XML.

    A truncated or half-written SVG renders as a broken image on GitHub with
    no error anywhere, so this is worth catching mechanically.
    """
    print("\nSVG well-formedness")
    paths = sorted(glob.glob("assets/**/*.svg", recursive=True))
    if not paths:
        fail("svg-parse", "no SVGs found under assets/")
        return
    for path in paths:
        try:
            ET.parse(path)
            ok(posix(path))
        except ET.ParseError as exc:
            fail("svg-parse", f"{posix(path)} is not well-formed XML: {exc}")


def check_workflows_parse(workflows: dict[str, dict]) -> None:
    print("\nWorkflow YAML")
    if not workflows:
        fail("workflow-yaml", "no workflows found")
        return
    for path in workflows:
        ok(posix(path))


def check_cron_collisions(workflows: dict[str, dict]) -> None:
    """No two workflows may share a cron schedule.

    Both of our scheduled workflows commit to main. When snake.yml and
    pacman.yml shared `0 3 * * *` they raced, and whichever pushed second was
    rejected — silently discarding minutes of generated output.

    Note: PyYAML parses the `on:` key as the boolean True, because YAML 1.1
    treats `on` as a boolean literal. Hence the d.get(True) below.
    """
    print("\nCron collisions")
    schedules: dict[str, list[str]] = {}
    for path, doc in workflows.items():
        triggers = doc.get(True) or doc.get("on") or {}
        for entry in (triggers.get("schedule") or []):
            schedules.setdefault(entry["cron"], []).append(os.path.basename(path))

    if not schedules:
        notes.append("no scheduled workflows found")
        print("  note  no scheduled workflows")
        return

    for cron, files in sorted(schedules.items()):
        if len(files) > 1:
            fail("cron-collision", f"{cron} is shared by {', '.join(files)}")
        else:
            ok(f"{cron} -> {files[0]}")


def bash_is_usable() -> bool:
    """Probe whether a working bash exists.

    shutil.which() is not enough on Windows: it happily finds a WSL shim that
    then fails at exec time with

        CreateProcessCommon:818: execvpe(/bin/bash) failed

    So actually run something trivial and see whether it works. On the Ubuntu
    CI runner this always succeeds, which is the environment that matters.
    """
    if shutil.which("bash") is None:
        return False
    try:
        probe = subprocess.run(
            ["bash", "-c", "exit 0"], capture_output=True, text=True, timeout=10
        )
        return probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def check_embedded_shell(workflows: dict[str, dict]) -> None:
    """Every `run:` block must be syntactically valid bash.

    Workflow shell only fails at runtime, which on a scheduled job means
    finding out a day later.
    """
    print("\nEmbedded shell syntax")

    if not bash_is_usable():
        notes.append("bash unavailable — shell syntax check skipped (runs in CI)")
        print("  skip  no usable bash on this machine; this check runs in CI")
        return

    checked = 0
    for path, doc in workflows.items():
        for job_name, job in (doc.get("jobs") or {}).items():
            for index, step in enumerate(job.get("steps") or []):
                script = step.get("run")
                if not script:
                    continue
                label = step.get("name") or f"{job_name} step {index}"
                # encoding must be explicit: run: blocks contain non-ASCII
                # (e.g. the ✓ in stats-mirror.yml) and Python would otherwise
                # use the locale codec, which blows up on non-UTF-8 systems.
                with tempfile.NamedTemporaryFile(
                    "w", suffix=".sh", delete=False, encoding="utf-8"
                ) as handle:
                    handle.write(script)
                    tmp = handle.name
                try:
                    result = subprocess.run(
                        ["bash", "-n", tmp], capture_output=True, text=True
                    )
                    if result.returncode == 0:
                        ok(f"{os.path.basename(path)} :: {label}")
                    else:
                        fail(
                            "shell-syntax",
                            f"{posix(path)} :: {label}\n{result.stderr.strip()}",
                        )
                finally:
                    os.unlink(tmp)
                checked += 1
    if checked == 0:
        notes.append("no run: blocks found")


def main() -> int:
    os.chdir(REPO_ROOT)
    print(f"Validating {REPO_ROOT}")

    workflows = load_workflows()

    check_readme_assets()
    check_svgs_parse()
    check_workflows_parse(workflows)
    check_cron_collisions(workflows)
    check_embedded_shell(workflows)

    print("\n" + "=" * 60)
    if failures:
        print(f"FAILED — {len(failures)} problem(s):\n")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("PASSED — all checks green")
    for note in notes:
        print(f"  note: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
