#!/usr/bin/env python3
"""Assert that every plugin actually satisfies the nine requirements in contract/README.md.

This is not a lint. Each check either reads a declaration and verifies it against the filesystem, or
executes the piece's own scripts and asserts on what they do. A piece can lie in its README; it
cannot lie to this file.

Usage:
    python3 scripts/contract_test.py [--piece=<name>] [--output=json] [--quiet]
Exit codes: 0 = every piece satisfies the contract, 1 = at least one failure, 2 = usage / setup error.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jsonschema_mini import unsupported_keywords, validate  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"
FIXTURE_REPO = ROOT / "tests" / "fixture-repo"

# Networking APIs a collector must not touch. Requirement 2: collectors are offline, full stop.
# Anything that needs Noru is fetched by the MCP client and handed over as a local file.
NETWORK_TOKENS = [
    "node:http", "node:https", "node:net", "node:tls", "node:dgram", "node:dns",
    "fetch(", "XMLHttpRequest", "WebSocket", "require('http", 'require("http',
]

# Every module a validator is allowed to import. Requirement 3: Python standard library only.
ALLOWED_PY_IMPORTS = {
    "argparse", "base64", "collections", "csv", "datetime", "difflib", "functools", "hashlib",
    "io", "itertools", "json", "math", "os", "pathlib", "re", "shutil", "string", "subprocess",
    "sys", "tempfile", "textwrap", "time", "typing", "unicodedata", "urllib", "uuid",
    # yaml is imported inside a try/except ImportError and is never required.
    "yaml",
}

# Catalogue-shaped identifiers. Requirement 9 and the licensing non-goal: no framework control text,
# guidance or evidence list is vendored into this repository.
EVIDENCE_ID_RE = re.compile(r"\bE-[A-Z]{2,4}-\d{2,3}\b")
CONTROL_ID_RE = re.compile(r"\b[a-z]{2}-\d{2}\b")
DISPLAY_CONTROL_ID_RE = re.compile(r"\b[A-Z]{2}-\d{2}\b")
# Reserved synthetic namespaces, allowed only inside fixtures/ and tests/.
RESERVED_EVIDENCE_PREFIX = "E-ZZ-"
RESERVED_CONTROL_PREFIX = "zz-"

SCANNED_SUFFIXES = {".mjs", ".js", ".py", ".json", ".md", ".yml", ".yaml"}


class Failures:
    def __init__(self):
        self.rows = []

    def add(self, piece, item, message):
        self.rows.append({"piece": piece, "requirement": item, "message": message})

    def __len__(self):
        return len(self.rows)


def run(cmd, cwd=None, env=None):
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(
        cmd, cwd=cwd, env=merged, capture_output=True, text=True, timeout=180, check=False
    )


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def piece_dirs(only=None):
    if not PLUGINS.is_dir():
        return []
    out = []
    for d in sorted(PLUGINS.iterdir()):
        if not (d / "piece.json").is_file():
            continue
        if only and d.name != only:
            continue
        out.append(d)
    return out


# --------------------------------------------------------------------------------------------- #
def check_declaration(piece, decl, fail):
    """piece.json must validate against contract/piece.schema.json before anything else."""
    schema_path = ROOT / "contract" / "piece.schema.json"
    schema = read_json(schema_path)
    gaps = unsupported_keywords(schema)
    if gaps:
        fail.add(piece.name, 0, f"piece.schema.json uses unsupported keywords: {', '.join(gaps)}")
        return False
    errors = validate(decl, schema, schema)
    for path, message in errors:
        fail.add(piece.name, 0, f"piece.json {path}: {message}")
    if decl.get("piece") != piece.name:
        fail.add(
            piece.name, 0,
            f"piece.json declares piece '{decl.get('piece')}' but lives in plugins/{piece.name}",
        )
    return not errors


def check_item_1(piece, decl, fail):
    """One plugin manifest, one skill, three commands."""
    manifest = piece / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        fail.add(piece.name, 1, "missing .claude-plugin/plugin.json")
    else:
        data = read_json(manifest)
        if data.get("name") != piece.name:
            fail.add(
                piece.name, 1,
                f".claude-plugin/plugin.json name is '{data.get('name')}', expected '{piece.name}'",
            )
        if not data.get("version"):
            fail.add(piece.name, 1, ".claude-plugin/plugin.json has no version")

    codex = piece / ".codex-plugin" / "plugin.json"
    if not codex.is_file():
        fail.add(piece.name, 1, "missing .codex-plugin/plugin.json (the Codex mirror)")

    skill = decl.get("skill")
    if skill:
        if not (piece / skill).is_file():
            fail.add(piece.name, 1, f"declared skill {skill} does not exist")
        else:
            text = (piece / skill).read_text(encoding="utf-8")
            if not text.startswith("---"):
                fail.add(piece.name, 1, f"{skill} has no YAML frontmatter")
            elif f"name: {piece.name}" not in text.split("---")[1]:
                fail.add(piece.name, 1, f"{skill} frontmatter name does not match the piece name")
    elif piece.name != "noru":
        fail.add(piece.name, 1, "piece.json declares no skill")

    for command, rel in (decl.get("commands") or {}).items():
        path = piece / rel
        if not path.is_file():
            fail.add(piece.name, 1, f"declared command {command} -> {rel} does not exist")
            continue
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            fail.add(piece.name, 1, f"{rel} has no YAML frontmatter")
        elif f"name: {command}" not in text.split("---")[1]:
            fail.add(piece.name, 1, f"{rel} frontmatter name is not '{command}'")


def check_item_2(piece, decl, fail, workdir):
    """Deterministic, offline collector writing a committable manifest under .noru/."""
    collector = piece / decl["collector"]["entrypoint"]
    if not collector.is_file():
        fail.add(piece.name, 2, f"collector {decl['collector']['entrypoint']} does not exist")
        return

    source = collector.read_text(encoding="utf-8")
    for token in NETWORK_TOKENS:
        if token in source:
            fail.add(
                piece.name, 2,
                f"collector uses '{token}' — collectors must be offline; anything that needs Noru "
                "is fetched by the MCP client and handed over as a local file",
            )

    # Determinism: same repository state must produce byte-identical derived output. Two copies of
    # the same fixture repo, so nothing either run writes can influence the other.
    digests = []
    targets = []
    for i in (0, 1):
        target = workdir / f"{piece.name}-determinism-{i}"
        shutil.copytree(FIXTURE_REPO, target)
        targets.append(target)
        result = run(["node", str(collector), f"--repo={target}", "--output=json", "--quiet"])
        if result.returncode not in (0, 1):
            fail.add(
                piece.name, 2,
                f"collector exited {result.returncode} on the fixture repo: "
                f"{(result.stderr or result.stdout).strip()[:300]}",
            )
            return
        derived = target / ".noru" / ".cache" / f"{piece.name}.derived.json"
        if not derived.is_file():
            fail.add(piece.name, 2, f"collector wrote no derived facts at {derived.name}")
            return
        digests.append(derived.read_bytes())

    if digests[0] != digests[1]:
        fail.add(
            piece.name, 2,
            "collector is not deterministic: two runs over identical repository state produced "
            "different derived output",
        )

    reconcile_decl = decl.get("reconciler")
    if reconcile_decl:
        reconciler = piece / reconcile_decl["entrypoint"]
        if not reconciler.is_file():
            fail.add(piece.name, 2, f"reconciler {reconcile_decl['entrypoint']} does not exist")
        else:
            reconcile_source = reconciler.read_text(encoding="utf-8")
            for token in NETWORK_TOKENS:
                if token in reconcile_source:
                    fail.add(
                        piece.name,
                        2,
                        f"reconciler uses '{token}' — reconciliation must be offline",
                    )
            executable = "node" if reconcile_decl.get("runtime") == "node" else "python3"
            outputs = []
            for target in targets:
                result = run(
                    [
                        executable,
                        str(reconciler),
                        f"--repo={target}",
                        "--output=json",
                        "--quiet",
                    ]
                )
                if result.returncode != 0:
                    fail.add(
                        piece.name,
                        2,
                        f"reconciler exited {result.returncode} on the fixture repo: "
                        f"{(result.stderr or result.stdout).strip()[:300]}",
                    )
                    break
                outputs.append(result.stdout)
            if len(outputs) == 2 and outputs[0] != outputs[1]:
                fail.add(
                    piece.name,
                    2,
                    "reconciler is not deterministic: identical observations produced different "
                    "actions",
                )

    # A declared output is a deliverable a human is handed, so the one thing that can be checked
    # from here is that the declaration and the documentation agree. An output nobody documents is
    # an output nobody knows to look for, and a path that moves without the README moving with it
    # sends someone to a directory that is no longer there.
    outputs = decl.get("outputs") or []
    if outputs:
        readme = piece / "README.md"
        if not readme.is_file():
            fail.add(piece.name, 2, "piece declares outputs but has no README.md to document them in")
            return
        text = readme.read_text(encoding="utf-8")
        for output in outputs:
            path = output["path"]
            if path not in text:
                fail.add(
                    piece.name, 2,
                    f"output {path} is declared in piece.json but absent from README.md — the "
                    "deliverable has to be documented where the person who has to hand it over "
                    "will look",
                )
            if path == decl["artifact"]:
                fail.add(
                    piece.name, 2,
                    f"output {path} is the manifest itself; outputs[] names what a piece renders "
                    "*besides* the manifest",
                )


def check_item_3(piece, decl, fail):
    """Stdlib-only validator with did-you-mean hints and 0/1/2 exit codes, proven on fixtures."""
    validator = piece / decl["validator"]["entrypoint"]
    if not validator.is_file():
        fail.add(piece.name, 3, f"validator {decl['validator']['entrypoint']} does not exist")
        return

    source = validator.read_text(encoding="utf-8")
    for match in re.finditer(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", source, re.M):
        module = match.group(1).split(".")[0]
        if module not in ALLOWED_PY_IMPORTS:
            fail.add(
                piece.name, 3,
                f"validator imports '{module}', which is not in the allowed standard-library set",
            )
    if "difflib" not in source or "get_close_matches" not in source:
        fail.add(piece.name, 3, "validator has no difflib 'did you mean ...?' hint")
    for vocab in decl["validator"].get("vocabulary", []):
        if not (piece / vocab).is_file():
            fail.add(piece.name, 3, f"declared vocabulary {vocab} does not exist")

    # Exit code 2: no argument at all.
    result = run(["python3", str(validator)])
    if result.returncode != 2:
        fail.add(piece.name, 3, f"validator with no argument exited {result.returncode}, expected 2")

    # Exit code 2: a file that is not there.
    result = run(["python3", str(validator), str(piece / "does-not-exist.yml")])
    if result.returncode != 2:
        fail.add(piece.name, 3, f"validator on a missing file exited {result.returncode}, expected 2")

    fixtures = decl["validator"].get("fixtures")
    if not fixtures:
        fail.add(piece.name, 3, "piece.json declares no validator fixtures")
        return

    for rel in fixtures["valid"]:
        path = piece / rel
        if not path.is_file():
            fail.add(piece.name, 3, f"valid fixture {rel} does not exist")
            continue
        result = run(["python3", str(validator), str(path)])
        if result.returncode != 0:
            fail.add(
                piece.name, 3,
                f"valid fixture {rel} exited {result.returncode}: {result.stdout.strip()[:300]}",
            )

    for entry in fixtures["invalid"]:
        path = piece / entry["path"]
        if not path.is_file():
            fail.add(piece.name, 3, f"invalid fixture {entry['path']} does not exist")
            continue
        result = run(["python3", str(validator), str(path)])
        if result.returncode != 1:
            fail.add(
                piece.name, 3,
                f"invalid fixture {entry['path']} exited {result.returncode}, expected 1",
            )
        combined = result.stdout + result.stderr
        if entry["expect_message"] not in combined:
            fail.add(
                piece.name, 3,
                f"invalid fixture {entry['path']} did not produce a useful message: expected "
                f"{entry['expect_message']!r} in the output",
            )


def check_item_4(piece, decl, fail):
    """One idempotent push carrying slug + commitSha + branch; every write has a key."""
    push = decl["push"]
    if not (piece / push["entrypoint"]).is_file():
        fail.add(piece.name, 4, f"push entrypoint {push['entrypoint']} does not exist")

    if push["mode"] == "keyed_upsert" and not push.get("collapses_to"):
        fail.add(
            piece.name, 4,
            "mode is keyed_upsert but collapses_to is missing — a transitional fan-out must name "
            "the server-side tool that will replace it",
        )

    for op in push["operations"]:
        idem = op["idempotency"]
        if not idem.get("verified_at"):
            fail.add(piece.name, 4, f"{op['name']} idempotency has no verified_at")
        if idem["kind"] == "client_probe" and not idem.get("gap"):
            fail.add(
                piece.name, 4,
                f"{op['name']} relies on a client probe but records no server-side gap",
            )

    for field in ("slug", "commit_sha", "branch"):
        if push["provenance"].get(field) is not True:
            fail.add(piece.name, 4, f"push provenance does not carry {field}")

    # The manifest schema must require the same provenance fields, or the declaration is decorative.
    schema_rel = decl.get("manifest_schema")
    if schema_rel:
        schema = read_json(ROOT / schema_rel)
        source_schema = schema.get("properties", {}).get("source", {})
        if "$ref" in source_schema:
            ref = source_schema["$ref"].split("/")[-1]
            source_schema = schema.get("$defs", {}).get(ref, {})
        required = set(source_schema.get("required", []))
        for field in ("slug", "commit_sha", "branch"):
            if field not in required:
                fail.add(
                    piece.name, 4,
                    f"{schema_rel} does not require source.{field}, so a manifest could be pushed "
                    "with no provenance",
                )


def check_item_5(piece, decl, fail, workdir):
    """:diff before :push, and push refuses a stale plan. Executed, not asserted from docs."""
    push = piece / decl["push"]["entrypoint"]
    if not push.is_file():
        return

    repo = workdir / f"{piece.name}-gate"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".noru" / ".cache").mkdir(parents=True, exist_ok=True)
    manifest = repo / decl["artifact"]
    manifest.write_text("piece: test\n", encoding="utf-8")

    # No plan at all -> refuse with 1.
    result = run(["node", str(push), f"--repo={repo}", "--confirm"])
    if result.returncode != 1:
        fail.add(
            piece.name, 5,
            f"push with no plan exited {result.returncode}, expected 1",
        )

    # A plan bound to different manifest bytes -> refuse with 1, even with --confirm.
    plan = {
        "plan_version": 2,
        "created_at": "fixture",
        "generated_at": "2026-01-01T00:00:00.000Z",
        "expires_at": "2999-01-01T00:00:00.000Z",
        "piece": piece.name,
        "piece_version": read_json(piece / ".codex-plugin" / "plugin.json")["version"],
        "manifest": decl["artifact"],
        "manifest_sha256": "0" * 64,
        "target": {
            "organization_id": "org_fixture",
            "organization_name": "Fixture Organization",
            "mcp_endpoint": "https://api.noru.tech/v1/mcp",
        },
        "repository": {
            "root": str(repo.resolve()),
            "remote": "s",
            "branch": "main",
            "commit_sha": "c" * 40,
        },
        "required_scopes": [],
        "provenance": {"slug": "s", "commit_sha": "c" * 40, "branch": "main"},
        "operations": [],
        "summary": {"create": 0, "update": 0, "skip": 0, "total": 0},
    }
    plan_path = repo / ".noru" / ".cache" / f"{piece.name}.plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    result = run(["node", str(push), f"--repo={repo}", "--confirm"])
    if result.returncode != 1:
        fail.add(
            piece.name, 5,
            f"push with a stale plan exited {result.returncode}, expected 1 — the freshness check "
            "is the control that makes reviewing a diff mean anything",
        )

    # A fresh plan but no --confirm -> refuse with 2.
    import hashlib

    plan["manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    (repo / ".noru" / ".cache" / "noru-state.json").write_text(
        json.dumps(
            {
                "connection": {
                    "organization": {"id": "org_fixture", "name": "Fixture Organization"},
                    "endpoint": "https://api.noru.tech/v1/mcp",
                    "scopes": ["*"],
                }
            }
        ),
        encoding="utf-8",
    )
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    result = run(["node", str(push), f"--repo={repo}"])
    if result.returncode != 2:
        fail.add(
            piece.name, 5,
            f"push without --confirm exited {result.returncode}, expected 2",
        )

    if decl["push"].get("diff_required") is not True:
        fail.add(piece.name, 5, "piece.json does not declare diff_required")
    if decl["push"].get("requires_confirmation") is not True:
        fail.add(piece.name, 5, "piece.json does not declare requires_confirmation")


def check_item_6(piece, decl, fail):
    """Least-privilege scopes, declared and documented."""
    readme = piece / "README.md"
    if not readme.is_file():
        fail.add(piece.name, 6, "piece has no README.md to document its scopes in")
        return
    text = readme.read_text(encoding="utf-8")
    if "## Scopes" not in text:
        fail.add(piece.name, 6, "README.md has no '## Scopes' section")
    for scope in decl["scopes"]["read"] + decl["scopes"]["write"]:
        if scope not in text:
            fail.add(piece.name, 6, f"scope {scope} is declared in piece.json but absent from README.md")
    for scope in decl["scopes"]["write"]:
        if not scope.startswith("write:"):
            fail.add(piece.name, 6, f"'{scope}' is listed as a write scope but is not a write scope")


def check_item_7(piece, decl, fail, workdir):
    """Headless: every entrypoint accepts --output=json and --quiet and emits JSON or nothing."""
    entrypoints = [
        decl["collector"]["entrypoint"],
        decl["push"]["entrypoint"],
        decl["validator"]["entrypoint"],
    ]
    diff = piece / "scripts" / "diff.mjs"
    if diff.is_file():
        entrypoints.append("scripts/diff.mjs")

    empty = workdir / f"{piece.name}-headless"
    empty.mkdir(parents=True, exist_ok=True)

    for rel in entrypoints:
        path = piece / rel
        runner = "python3" if path.suffix == ".py" else "node"
        args = [runner, str(path), "--output=json", "--quiet"]
        if runner == "node":
            args.append(f"--repo={empty}")
        result = run(args, env={"NORU_API_KEY": ""})
        combined = result.stdout + result.stderr
        if "unknown option" in combined:
            fail.add(piece.name, 7, f"{rel} rejects --output=json or --quiet: {combined.strip()[:200]}")
        if result.stdout.strip():
            try:
                json.loads(result.stdout)
            except json.JSONDecodeError:
                fail.add(
                    piece.name, 7,
                    f"{rel} with --output=json wrote non-JSON to stdout: {result.stdout.strip()[:200]}",
                )
        # --help must exit 0 and never hang waiting for a TTY.
        result = run([runner, str(path), "--help"])
        if result.returncode != 0:
            fail.add(piece.name, 7, f"{rel} --help exited {result.returncode}, expected 0")

    for code in ("0", "1", "2"):
        if not decl["ci"]["exit_codes"].get(code):
            fail.add(piece.name, 7, f"exit code {code} is not documented in piece.json")

    ci = decl["ci"]
    expected = {
        "validate": decl["validator"]["entrypoint"],
        "drift_check": decl["collector"]["entrypoint"],
    }
    for name, expected_entrypoint in expected.items():
        invocation = ci.get(name) or {}
        entrypoint = invocation.get("entrypoint")
        if entrypoint != expected_entrypoint:
            fail.add(
                piece.name,
                7,
                f"ci.{name}.entrypoint is {entrypoint}, expected trusted {expected_entrypoint}",
            )
        if entrypoint and not (piece / entrypoint).is_file():
            fail.add(piece.name, 7, f"ci.{name} entrypoint {entrypoint} does not exist")
        arguments = invocation.get("arguments") or []
        if "--output=json" not in arguments or "--quiet" not in arguments:
            fail.add(piece.name, 7, f"ci.{name} is not declared as JSON/no-TTY")
    if "{manifest}" not in (ci.get("validate") or {}).get("arguments", []):
        fail.add(piece.name, 7, "ci.validate does not receive the committed manifest")
    if not any(
        "{repo}" in argument
        for argument in (ci.get("drift_check") or {}).get("arguments", [])
    ):
        fail.add(piece.name, 7, "ci.drift_check does not receive the target repository")
    if not ci.get("watch_paths"):
        fail.add(piece.name, 7, "ci.watch_paths is empty")


def check_item_8(piece, decl, fail):
    """Unattributed claims are an ERROR. Proven by an invalid fixture, not by reading the code."""
    interp = decl["interpretation"]
    if interp.get("unattributed") != "error":
        fail.add(piece.name, 8, "piece.json does not declare unattributed claims as an error")
    if interp.get("required") is not True:
        fail.add(piece.name, 8, "piece.json does not declare interpretation as required")

    fixtures = decl["validator"].get("fixtures", {})
    entries = [e for e in fixtures.get("invalid", []) if "interpretation" in e["expect_message"]]
    if not entries:
        fail.add(
            piece.name, 8,
            "no invalid fixture proves that a missing interpretation block fails validation",
        )
        return

    validator = piece / decl["validator"]["entrypoint"]
    for entry in entries:
        path = piece / entry["path"]
        if not path.is_file():
            continue
        result = run(["python3", str(validator), str(path)])
        if result.returncode != 1:
            fail.add(
                piece.name, 8,
                f"{entry['path']} strips an interpretation block but the validator exited "
                f"{result.returncode} instead of 1",
            )
        if "WARN" in result.stdout and "ERROR" not in result.stdout:
            fail.add(
                piece.name, 8,
                f"{entry['path']} produced only warnings — an unattributed claim must be an error",
            )

    # The manifest schema must require the interpretation fields too.
    schema_rel = decl.get("manifest_schema")
    if schema_rel:
        schema = read_json(ROOT / schema_rel)
        block = schema.get("$defs", {}).get("interpretation", {})
        required = set(block.get("required", []))
        for field in ("owner", "decided_at", "rationale"):
            if field not in required:
                fail.add(piece.name, 8, f"{schema_rel} $defs.interpretation does not require {field}")
        if "expires_at" not in set(block.get("properties", {})):
            fail.add(piece.name, 8, f"{schema_rel} $defs.interpretation has no expires_at")


def check_item_9(piece, decl, fail):
    """The queue comes from Noru. No catalogue is vendored, and fixtures cannot smuggle one in."""
    queue = decl["queue"]
    if queue.get("hardcoded_expectations") is not False:
        fail.add(piece.name, 9, "piece.json does not declare hardcoded_expectations: false")
    if not queue.get("source"):
        fail.add(piece.name, 9, "piece.json declares no queue source")

    for path in sorted(piece.rglob("*")):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        rel = path.relative_to(piece).as_posix()
        in_fixtures = rel.startswith("fixtures/")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        for match in set(EVIDENCE_ID_RE.findall(text)):
            if in_fixtures and match.startswith(RESERVED_EVIDENCE_PREFIX):
                continue
            fail.add(
                piece.name, 9,
                f"{rel} contains the catalogue-shaped evidence item id '{match}'. A piece asks Noru "
                "what is needed; it never ships a catalogue"
                + (
                    f" (fixtures may only use the reserved {RESERVED_EVIDENCE_PREFIX}* namespace)"
                    if in_fixtures
                    else ""
                ),
            )

        for regex, label in ((CONTROL_ID_RE, "control id"), (DISPLAY_CONTROL_ID_RE, "display control id")):
            for match in set(regex.findall(text)):
                if match.lower().startswith(RESERVED_CONTROL_PREFIX):
                    if in_fixtures:
                        continue
                    fail.add(
                        piece.name, 9,
                        f"{rel} uses the reserved fixture namespace '{match}' outside fixtures/",
                    )
                    continue
                fail.add(
                    piece.name, 9,
                    f"{rel} contains the catalogue-shaped {label} '{match}'. Control ids come from "
                    "getOrganizationControls at run time, never from this repository",
                )


def check_piece(piece, fail, workdir):
    decl = read_json(piece / "piece.json")
    if not check_declaration(piece, decl, fail):
        return
    check_item_1(piece, decl, fail)
    check_item_2(piece, decl, fail, workdir)
    check_item_3(piece, decl, fail)
    check_item_4(piece, decl, fail)
    check_item_5(piece, decl, fail, workdir)
    check_item_6(piece, decl, fail)
    check_item_7(piece, decl, fail, workdir)
    check_item_8(piece, decl, fail)
    check_item_9(piece, decl, fail)


def main(argv):
    only = None
    output_json = False
    quiet = False
    for arg in argv:
        if arg.startswith("--piece="):
            only = arg.split("=", 1)[1]
        elif arg == "--output=json":
            output_json = True
        elif arg == "--output=text":
            output_json = False
        elif arg == "--quiet":
            quiet = True
        elif arg in ("-h", "--help"):
            sys.stdout.write(
                "usage: contract_test.py [--piece=<name>] [--output=json] [--quiet]\n"
            )
            return 0
        else:
            sys.stderr.write(f"error: unknown option '{arg}'\n")
            return 2

    if not FIXTURE_REPO.is_dir():
        sys.stderr.write(f"error: fixture repository missing at {FIXTURE_REPO}\n")
        return 2

    pieces = piece_dirs(only)
    if not pieces:
        sys.stderr.write("error: no pieces found (a piece is a plugin directory with a piece.json)\n")
        return 2

    fail = Failures()
    with tempfile.TemporaryDirectory(prefix="noru-contract-") as tmp:
        workdir = pathlib.Path(tmp)
        for piece in pieces:
            check_piece(piece, fail, workdir)

    ok = len(fail) == 0
    if output_json:
        sys.stdout.write(
            json.dumps(
                {
                    "ok": ok,
                    "pieces": [p.name for p in pieces],
                    "failures": fail.rows,
                },
                indent=None if quiet else 2,
                sort_keys=True,
            )
            + "\n"
        )
        return 0 if ok else 1

    for row in fail.rows:
        print(f"  FAIL  [{row['piece']}] requirement {row['requirement']}: {row['message']}")
    if ok:
        if not quiet:
            print(f"OK: {len(pieces)} piece(s) satisfy contract requirements 1-9.")
        return 0
    print(f"\nFAILED: {len(fail)} contract violation(s) across {len(pieces)} piece(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
