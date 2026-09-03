#!/usr/bin/env python3
"""Run the read-only, branch-level GRC review in CI.

This is the deterministic counterpart to `/noru:review`: route the branch diff to the relevant
bundled pieces, run only their local scan/validation/expiry/policy checks, and combine the reports.
It deliberately has no diff or push option and removes NORU_API_KEY from every child process.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_PLUGINS = ROOT / "plugins"
SELECTOR = DEFAULT_PLUGINS / "noru" / "scripts" / "review.mjs"
PIECE_CHECK = ROOT / "scripts" / "ci_check.py"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_TOOLING = 6

USAGE = (
    "usage: ci_review.py [--repo=<path>] [--plugins=<path>] [--base-ref=<ref>]\n"
    "                    [--pieces=a,b] [--mode=warn|gate] [--fail-on=<kinds>|none]\n"
    "                    [--baseline=<path>] [--max-age-days=N] [--warn-within-days=N]\n"
    "                    [--gate-on-new] [--report=<path.json>]\n"
    "                    [--output=json|text] [--quiet]\n"
)


def parse_args(argv):
    opts = {
        "repo": pathlib.Path.cwd(),
        "plugins": DEFAULT_PLUGINS,
        "base_ref": "origin/main",
        "pieces": None,
        "mode": "warn",
        "fail_on": None,
        "baseline": None,
        "max_age_days": 0,
        "warn_within_days": 30,
        "gate_on_new": False,
        "report": None,
        "json": False,
        "quiet": False,
        "help": False,
    }
    for arg in argv:
        if arg.startswith("--repo="):
            opts["repo"] = pathlib.Path(arg.split("=", 1)[1])
        elif arg.startswith("--plugins="):
            opts["plugins"] = pathlib.Path(arg.split("=", 1)[1])
        elif arg.startswith("--base-ref="):
            opts["base_ref"] = arg.split("=", 1)[1]
        elif arg.startswith("--pieces="):
            opts["pieces"] = [value.strip() for value in arg.split("=", 1)[1].split(",") if value.strip()]
        elif arg.startswith("--mode="):
            opts["mode"] = arg.split("=", 1)[1]
        elif arg.startswith("--fail-on="):
            opts["fail_on"] = arg.split("=", 1)[1]
        elif arg.startswith("--baseline="):
            opts["baseline"] = arg.split("=", 1)[1]
        elif arg.startswith("--max-age-days="):
            try:
                opts["max_age_days"] = int(arg.split("=", 1)[1])
            except ValueError:
                return None, f"{arg} is not an integer"
        elif arg.startswith("--warn-within-days="):
            try:
                opts["warn_within_days"] = int(arg.split("=", 1)[1])
            except ValueError:
                return None, f"{arg} is not an integer"
        elif arg == "--gate-on-new":
            opts["gate_on_new"] = True
        elif arg.startswith("--report="):
            opts["report"] = pathlib.Path(arg.split("=", 1)[1])
        elif arg == "--output=json":
            opts["json"] = True
        elif arg == "--output=text":
            opts["json"] = False
        elif arg == "--quiet":
            opts["quiet"] = True
        elif arg in ("-h", "--help"):
            opts["help"] = True
        else:
            return None, f"unknown option '{arg}'"
    if opts["mode"] not in ("warn", "gate"):
        return None, "--mode must be warn or gate"
    if opts["max_age_days"] < 0 or opts["warn_within_days"] < 0:
        return None, "day counts cannot be negative"
    return opts, None


def run(command, cwd):
    env = dict(os.environ)
    # This workflow is structurally read-only. Even an accidentally configured repository secret
    # cannot widen it into a publisher.
    env.pop("NORU_API_KEY", None)
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return exc


def available_pieces(plugins):
    return sorted(
        path.parent.name
        for path in plugins.glob("*/piece.json")
        if path.parent.name != "noru"
    )


def select(opts):
    selector = opts["plugins"] / "noru" / "scripts" / "review.mjs"
    if not selector.is_file():
        selector = SELECTOR
    available = available_pieces(opts["plugins"])
    command = [
        "node",
        str(selector),
        f"--repo={opts['repo']}",
        f"--base-ref={opts['base_ref']}",
        f"--available-pieces={','.join(available)}",
        "--output=json",
        "--quiet",
    ]
    if opts["pieces"] is not None:
        command.append(f"--pieces={','.join(opts['pieces'])}")
    completed = run(command, opts["repo"])
    if isinstance(completed, Exception):
        return None, f"could not run branch selector ({completed})"
    if completed.returncode != 0:
        return None, (completed.stderr or "branch selector failed").strip().splitlines()[0]
    try:
        return json.loads(completed.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"branch selector did not return JSON ({exc})"


def run_piece(opts, piece, report_path):
    command = [
        sys.executable or "python3",
        str(PIECE_CHECK),
        f"--piece={piece}",
        f"--repo={opts['repo']}",
        f"--plugins={opts['plugins']}",
        f"--mode={opts['mode']}",
        "--steps=scan,validate,expiry,policy",
        "--on-missing-prerequisite=skip",
        f"--base-ref={opts['base_ref']}",
        f"--max-age-days={opts['max_age_days']}",
        f"--warn-within-days={opts['warn_within_days']}",
        f"--report={report_path}",
        "--output=text",
        "--quiet",
    ]
    if opts["fail_on"] is not None:
        command.append(f"--fail-on={opts['fail_on']}")
    if opts["baseline"] is not None:
        command.append(f"--baseline={opts['baseline']}")
    if opts["gate_on_new"]:
        command.append("--gate-on-new")
    completed = run(command, opts["repo"])
    if isinstance(completed, Exception):
        return None, EXIT_TOOLING, str(completed)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        detail = (completed.stderr or completed.stdout or str(exc)).strip().splitlines()
        return None, EXIT_TOOLING, detail[0] if detail else str(exc)
    return report, completed.returncode, None


def overall_status(mode, reports, codes):
    if any(code == EXIT_TOOLING or report.get("status") == "error" for report, code in zip(reports, codes)):
        return "error", EXIT_TOOLING
    if mode == "gate" and any(code != 0 for code in codes):
        return "fail", EXIT_FAILED
    statuses = {report.get("status") for report in reports}
    if not reports:
        return "pass", EXIT_OK
    if statuses <= {"skipped"}:
        return "skipped", EXIT_OK
    if statuses & {"warn", "pass-with-warnings"}:
        return "warn", EXIT_OK
    return "pass", EXIT_OK


def render(payload):
    lines = [
        f"Noru GRC pull-request review ({payload['mode']} mode)",
        f"  base: {payload['base_ref']}",
        f"  selected: {', '.join(payload['selected_pieces']) or '(none)'}",
        "",
    ]
    for result in payload["results"]:
        lines.append(f"  {result['piece']}: {result['status']}")
        for step in result.get("steps", []):
            lines.append(f"    {step['step']}: {step['status']}")
    if not payload["selected_pieces"]:
        lines.append("  No branch-change signal selected a GRC piece.")
    lines.extend(["", "No diff or push step ran. Nothing was written to Noru."])
    return "\n".join(lines)


def main(argv):
    opts, error = parse_args(argv)
    if error:
        sys.stderr.write(f"error: {error}\n{USAGE}")
        return EXIT_USAGE
    if opts["help"]:
        sys.stdout.write(USAGE)
        return EXIT_OK
    if not opts["repo"].is_dir() or not opts["plugins"].is_dir():
        sys.stderr.write("error: --repo and --plugins must identify directories\n")
        return EXIT_USAGE
    opts["repo"] = opts["repo"].resolve()
    opts["plugins"] = opts["plugins"].resolve()

    selection, error = select(opts)
    if error:
        sys.stderr.write(f"error: {error}\n")
        return EXIT_TOOLING
    selected = [
        piece["name"]
        for piece in selection["pieces"]
        if piece["disposition"] == "selected" and piece["run_state"] == "ready"
    ]

    reports = []
    codes = []
    with tempfile.TemporaryDirectory(prefix="noru-ci-review-") as tmp:
        for piece in selected:
            report, code, failure = run_piece(opts, piece, pathlib.Path(tmp) / f"{piece}.json")
            if failure:
                report = {"piece": piece, "status": "error", "steps": [], "detail": failure}
            reports.append(report)
            codes.append(code)

    status, code = overall_status(opts["mode"], reports, codes)
    payload = {
        "check": "ci-review",
        "mode": opts["mode"],
        "base_ref": opts["base_ref"],
        "repository": str(opts["repo"]),
        "head": selection["head"],
        "merge_base": selection["merge_base"],
        "changed_files": selection["changed_files"],
        "selection": selection["pieces"],
        "selected_pieces": selected,
        "status": status,
        "results": reports,
        "exit_code": code,
        "write_boundary": "scan, validate, expiry and policy only; diff and push unavailable",
    }
    if opts["report"] is not None:
        try:
            opts["report"].parent.mkdir(parents=True, exist_ok=True)
            opts["report"].write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError as exc:
            sys.stderr.write(f"error: could not write {opts['report']} ({exc})\n")
            return EXIT_TOOLING

    if opts["json"]:
        sys.stdout.write(json.dumps(payload, indent=None if opts["quiet"] else 2, sort_keys=True) + "\n")
    elif not opts["quiet"]:
        sys.stdout.write(render(payload) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
