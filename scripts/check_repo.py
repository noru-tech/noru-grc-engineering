#!/usr/bin/env python3
"""Repository-level checks: marketplace manifests, MCP config, schema/vocabulary sync, secret hygiene.

Standard library only, no network, no install step.

What it covers, and why each one is here rather than left to review:

  * **Marketplace manifests** — the Claude Code and Codex marketplaces must agree on the same set of
    plugins at the same paths. They are two files nobody edits together, so they drift.
  * **Plugin manifests** — every declared source directory really contains a plugin whose name
    matches, for both clients.
  * **MCP config** — points at Noru's hosted endpoint and carries no credential.
  * **Schema / vocabulary sync** — a piece's bundled vocabulary and the contract schema describe the
    same enums. Two sources of truth is one too many, so this makes them one in effect.
  * **Schema evaluability** — no contract schema uses a JSON Schema keyword scripts/jsonschema_mini.py
    cannot evaluate, so the schemas can never quietly outgrow the checker that enforces them.
  * **Secret hygiene** — this repository is public.

Usage:
    python3 scripts/check_repo.py [--output=json] [--quiet]
Exit codes: 0 = clean, 1 = problems found, 2 = usage / IO error.
"""
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jsonschema_mini import unsupported_keywords  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
MCP_URL = "https://api.noru.tech/v1/mcp"

# (piece, vocabulary key, dotted path into the schema, comparison)
#
# "set" compares membership. "keys" compares the vocabulary list against the *declaration order* of
# a schema object's properties — used for the findings categories, where the order is the substance
# and not a formatting preference: what is enforceable today comes before what is enforceable later.
VOCAB_SYNC = [
    ("ai-inventory", "deployment", "$defs.aiSystem.properties.deployment.enum", "set"),
    ("ai-inventory", "autonomy", "$defs.aiSystem.properties.autonomy.enum", "set"),
    ("ai-inventory", "oversight_type", "$defs.oversightPoint.properties.type.enum", "set"),
    ("ai-inventory", "retrieval_kind", "$defs.retrievalSource.properties.kind.enum", "set"),
    ("ai-inventory", "provider_category", "$defs.provider.properties.category.enum", "set"),
    ("ai-inventory", "claim_kind", "$defs.providerClaim.properties.kind.enum", "set"),
    ("ai-inventory", "claim_source_type", "$defs.claimSource.properties.type.enum", "set"),
    ("ai-inventory", "finding_categories", "$defs.findings.properties", "keys"),
    ("ai-inventory", "finding_status", "$defs.findingStatus.enum", "set"),
    ("ai-inventory", "prohibited_practice",
     "$defs.prohibitedPracticeFinding.properties.practice.enum", "set"),
    ("ai-inventory", "prohibited_determination",
     "$defs.prohibitedPracticeFinding.properties.determination.enum", "set"),
    ("ai-inventory", "transparency_trigger",
     "$defs.transparencyFinding.properties.trigger.enum", "set"),
    ("ai-inventory", "required_action",
     "$defs.transparencyFinding.properties.required_action.enum", "set"),
    ("ai-inventory", "disclosure_state", "$defs.disclosureCheck.properties.state.enum", "set"),
    ("ai-inventory", "eu_ai_act_role", "$defs.roleAndRiskFinding.properties.role.enum", "set"),
    ("ai-inventory", "eu_ai_act_tier", "$defs.roleAndRiskFinding.properties.tier.enum", "set"),
    ("ai-inventory", "annex_iii_area",
     "$defs.roleAndRiskFinding.properties.annex_iii_area.enum", "set"),
    ("ai-inventory", "art_6_3_ground", "$defs.notHighRiskAssessment.properties.ground.enum", "set"),
    ("ai-inventory", "standards_scheme", "$defs.standardsFinding.properties.scheme.enum", "set"),
    ("evidence-push", "mime_types", "$defs.upload.properties.mime_type.enum", "set"),
    ("evidence-push", "max_file_bytes", "$defs.upload.properties.size_bytes.maximum", "value"),
    ("evidence-push", "queue_tools",
     "properties.queue_snapshot.properties.via.items.enum", "set"),
    ("governance-records", "record_kind", "$defs.record.properties.kind.enum", "set"),
    ("governance-records", "attendance", "$defs.participant.properties.attendance.enum", "set"),
    ("governance-records", "action_status", "$defs.action.properties.status.enum", "set"),
    ("governance-records", "queue_tools",
     "properties.queue_snapshot.properties.via.items.enum", "set"),
    ("audit-pack", "conclusion", "$defs.workpaper.properties.conclusion.enum", "set"),
    ("audit-pack", "inspected_kind", "$defs.inspected.properties.kind.enum", "set"),
    ("audit-pack", "sampling_method", "$defs.sample.properties.method.enum", "set"),
    ("audit-pack", "disposition", "$defs.exception.properties.disposition.enum", "set"),
    ("audit-pack", "queue_tools",
     "properties.queue_snapshot.properties.via.items.enum", "set"),
    ("iac-scan", "technology", "$defs.finding.properties.technology.enum", "set"),
    ("iac-scan", "severity", "$defs.finding.properties.severity.enum", "set"),
    ("iac-scan", "finding_status", "$defs.finding.properties.status.enum", "set"),
    ("iac-scan", "category", "$defs.finding.properties.category.enum", "set"),
    ("iac-scan", "queue_tools",
     "properties.queue_snapshot.properties.via.items.enum", "set"),
    ("review-signoff", "review_kind", "$defs.review.properties.kind.enum", "set"),
    ("review-signoff", "cadence", "$defs.review.properties.cadence.enum", "set"),
    ("review-signoff", "disposition", "$defs.exception.properties.disposition.enum", "set"),
    ("review-signoff", "queue_tools",
     "properties.queue_snapshot.properties.via.items.enum", "set"),
]

# Real credentials look like these. Documentation placeholders do not.
SECRET_PATTERNS = [
    (re.compile(r"\bnoru_[A-Za-z0-9]{12,}"), "a Noru API key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "an OpenAI-style secret key"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}"), "a bearer token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "a GitHub token"),
]
PLACEHOLDER_TOKENS = ("<NORU_API_KEY>", "${NORU_API_KEY}", "…", "<your", "<redacted>", "example")
SCANNED_SUFFIXES = {".mjs", ".js", ".py", ".json", ".md", ".yml", ".yaml", ".txt", ".ts"}
SKIP_DIRS = {".git", "node_modules", ".noru"}


def dotted(node, path):
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def check_marketplaces(problems):
    claude_path = ROOT / ".claude-plugin" / "marketplace.json"
    codex_path = ROOT / ".agents" / "plugins" / "marketplace.json"
    if not claude_path.is_file():
        problems.append("missing .claude-plugin/marketplace.json")
        return []
    if not codex_path.is_file():
        problems.append("missing .agents/plugins/marketplace.json (the Codex mirror)")
        return []

    claude = json.loads(claude_path.read_text(encoding="utf-8"))
    codex = json.loads(codex_path.read_text(encoding="utf-8"))

    if claude.get("name") != codex.get("name"):
        problems.append(
            f"marketplace name differs: Claude '{claude.get('name')}' vs Codex '{codex.get('name')}'"
        )
    if not claude.get("owner", {}).get("email"):
        problems.append(".claude-plugin/marketplace.json has no owner email")

    claude_entries = {p["name"]: p for p in claude.get("plugins", [])}
    codex_entries = {p["name"]: p for p in codex.get("plugins", [])}
    if set(claude_entries) != set(codex_entries):
        problems.append(
            "the two marketplaces list different plugins: "
            f"Claude {sorted(claude_entries)} vs Codex {sorted(codex_entries)}"
        )

    for name, entry in sorted(claude_entries.items()):
        source = entry.get("source")
        if not isinstance(source, str):
            problems.append(f"[{name}] Claude marketplace source must be a path string")
            continue
        directory = (ROOT / source).resolve()
        if not directory.is_dir():
            problems.append(f"[{name}] Claude marketplace source {source} is not a directory")
            continue
        for field in ("version", "license", "description"):
            if not entry.get(field):
                problems.append(f"[{name}] Claude marketplace entry has no {field}")

        codex_entry = codex_entries.get(name)
        if codex_entry:
            codex_path_value = (codex_entry.get("source") or {}).get("path")
            if codex_path_value != source:
                problems.append(
                    f"[{name}] Codex marketplace points at {codex_path_value}, Claude at {source}"
                )

        for client, rel in (("Claude Code", ".claude-plugin"), ("Codex", ".codex-plugin")):
            manifest = directory / rel / "plugin.json"
            if not manifest.is_file():
                problems.append(f"[{name}] missing {rel}/plugin.json ({client})")
                continue
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if data.get("name") != name:
                problems.append(
                    f"[{name}] {rel}/plugin.json declares name '{data.get('name')}'"
                )
            if data.get("version") != entry.get("version"):
                problems.append(
                    f"[{name}] {rel}/plugin.json version {data.get('version')} does not match the "
                    f"marketplace entry {entry.get('version')}"
                )
            if data.get("license") != "MIT":
                problems.append(f"[{name}] {rel}/plugin.json license is not MIT")

        mcp = directory / ".mcp.json"
        if mcp.is_file():
            config = json.loads(mcp.read_text(encoding="utf-8"))
            server = (config.get("mcpServers") or {}).get("noru", {})
            if server.get("url") != MCP_URL:
                problems.append(f"[{name}] .mcp.json url is {server.get('url')}, expected {MCP_URL}")
            for key in ("headers", "env", "token", "apiKey", "authorization"):
                if key in server:
                    problems.append(
                        f"[{name}] .mcp.json declares '{key}' — authentication belongs to the MCP "
                        "client, never to a committed config"
                    )

    return sorted(claude_entries)


def check_pieces_registered(problems, plugin_names):
    """A piece that is not in the marketplace is a piece nobody can install."""
    plugins_dir = ROOT / "plugins"
    if not plugins_dir.is_dir():
        return
    listed = set(plugin_names)
    for directory in sorted(plugins_dir.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        if directory.name not in listed:
            problems.append(
                f"plugins/{directory.name} exists but is in neither marketplace — add it to "
                ".claude-plugin/marketplace.json and .agents/plugins/marketplace.json"
            )



def check_reference_files_exist(problems):
    """A piece's prose may only point at reference files that are actually there.

    The skill and the commands are instructions an agent follows literally. "Read
    references/classification-guide.md" against a file that does not exist sends it looking, and
    what it does next is anybody's guess — most likely classify without the guidance and never
    mention that it could not find it. Cheap to check, and invisible in review otherwise.
    """
    plugins = ROOT / "plugins"
    if not plugins.is_dir():
        return
    pattern = re.compile(r"references/([A-Za-z0-9._/-]+\.(?:md|json|ya?ml|txt))")
    for piece in sorted(plugins.iterdir()):
        if not piece.is_dir() or piece.name.startswith("."):
            continue
        for prose in sorted(piece.rglob("*.md")) + sorted(piece.glob("references/*.json")):
            if "/references/taxonomy/" in prose.as_posix():
                continue
            try:
                text = prose.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for rel in sorted(set(pattern.findall(text))):
                if not (piece / "references" / rel).is_file():
                    problems.append(
                        f"[{piece.name}] {prose.relative_to(ROOT)} points at "
                        f"references/{rel}, which does not exist"
                    )

def check_special_categories(problems):
    """The canonical Article 9 / Article 10 list must cover every key a piece treats as special.

    `contract/lib/taxonomy/special_categories.json` is what `scripts/check_policy.py` reads, and
    `plugins/privacy-datamap/references/classification.json` is what the collector reads. Two lists
    of what the law calls special is one list too many: if they drift, the piece flags a field the
    policy gate then says nothing about, or the other way round.

    The comparison is coverage rather than equality, because the canonical file is written as
    prefix roots — `user.biometric` standing for the whole subtree — while a piece may enumerate
    the leaves it actually classifies. Every canonical root must also be a real Fideslang key, or
    the roots silently cover nothing.
    """
    canonical_path = ROOT / "contract" / "lib" / "taxonomy" / "special_categories.json"
    categories_path = ROOT / "contract" / "lib" / "taxonomy" / "data_categories.json"
    if not canonical_path.is_file():
        problems.append(
            "contract/lib/taxonomy/special_categories.json is missing — check_policy.py cannot "
            "report special-category data without it"
        )
        return
    roots = json.loads(canonical_path.read_text(encoding="utf-8")).get("fides_keys") or []

    if categories_path.is_file():
        known = {row["fides_key"] for row in json.loads(categories_path.read_text(encoding="utf-8"))}
        for root in sorted(set(roots) - known):
            problems.append(
                f"special_categories.json lists '{root}', which is not a Fideslang data category — "
                "a root that matches nothing silently covers nothing"
            )

    def covered(key):
        return any(key == root or key.startswith(root + ".") for root in roots)

    classification = ROOT / "plugins" / "privacy-datamap" / "references" / "classification.json"
    if not classification.is_file():
        return
    piece_keys = json.loads(classification.read_text(encoding="utf-8")).get("special_categories")
    for key in sorted(k for k in (piece_keys or []) if not covered(k)):
        problems.append(
            f"[privacy-datamap] classification.json treats '{key}' as a special category but no "
            "root in contract/lib/taxonomy/special_categories.json covers it — the collector would "
            "flag it and scripts/check_policy.py would not"
        )


def check_vocab_sync(problems):
    for piece, key, path, mode in VOCAB_SYNC:
        vocab_path = ROOT / "plugins" / piece / "references" / "vocabulary.json"
        piece_json = ROOT / "plugins" / piece / "piece.json"
        if not vocab_path.is_file() or not piece_json.is_file():
            continue
        schema_rel = json.loads(piece_json.read_text(encoding="utf-8")).get("manifest_schema")
        if not schema_rel:
            continue
        schema = json.loads((ROOT / schema_rel).read_text(encoding="utf-8"))
        vocab = json.loads(vocab_path.read_text(encoding="utf-8"))

        expected = dotted(schema, path)
        actual = vocab.get(key)
        if expected is None:
            problems.append(f"[{piece}] {schema_rel} has nothing at {path} to compare with '{key}'")
            continue
        if mode == "keys":
            declared = list(expected) if isinstance(expected, dict) else None
            if declared != list(actual or []):
                problems.append(
                    f"[{piece}] vocabulary '{key}' is {actual} but {schema_rel} {path} declares "
                    f"{declared} — these are ordered, and the order is what says which obligation "
                    "is enforceable now and which is not"
                )
        elif mode == "set":
            if set(expected) != set(actual or []):
                problems.append(
                    f"[{piece}] vocabulary '{key}' and {schema_rel} {path} have drifted: "
                    f"only in schema {sorted(set(expected) - set(actual or []))}, "
                    f"only in vocabulary {sorted(set(actual or []) - set(expected))}"
                )
        elif expected != actual:
            problems.append(
                f"[{piece}] vocabulary '{key}' is {actual} but {schema_rel} {path} is {expected}"
            )


def check_schemas_evaluable(problems):
    for schema_path in sorted((ROOT / "contract").glob("*.schema.json")):
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        gaps = unsupported_keywords(schema)
        if gaps:
            problems.append(
                f"{schema_path.relative_to(ROOT)} uses JSON Schema keywords "
                f"scripts/jsonschema_mini.py cannot evaluate: {', '.join(gaps)}"
            )


def check_secrets(problems):
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern, label in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                snippet = match.group(0)
                if any(token in snippet for token in PLACEHOLDER_TOKENS):
                    continue
                problems.append(
                    f"{path.relative_to(ROOT)}:{line} looks like {label} — this repository is public"
                )


def check_skills(problems, plugin_names):
    for name in plugin_names:
        skills = ROOT / "plugins" / name / "skills"
        if not skills.is_dir():
            problems.append(f"[{name}] has no skills/ directory")
            continue
        found = list(skills.glob("*/SKILL.md"))
        if not found:
            problems.append(f"[{name}] has no skills/<name>/SKILL.md")
        for skill in found:
            text = skill.read_text(encoding="utf-8")
            if not text.startswith("---"):
                problems.append(f"{skill.relative_to(ROOT)} has no YAML frontmatter")
                continue
            frontmatter = text.split("---")[1]
            for field in ("name:", "description:"):
                if field not in frontmatter:
                    problems.append(f"{skill.relative_to(ROOT)} frontmatter has no {field}")


def main(argv):
    output_json = False
    quiet = False
    for arg in argv:
        if arg == "--output=json":
            output_json = True
        elif arg == "--output=text":
            output_json = False
        elif arg == "--quiet":
            quiet = True
        elif arg in ("-h", "--help"):
            sys.stdout.write("usage: check_repo.py [--output=json] [--quiet]\n")
            return 0
        else:
            sys.stderr.write(f"error: unknown option '{arg}'\n")
            return 2

    problems = []
    try:
        plugin_names = check_marketplaces(problems)
        check_pieces_registered(problems, plugin_names)
        check_reference_files_exist(problems)
        check_special_categories(problems)
        check_vocab_sync(problems)
        check_schemas_evaluable(problems)
        check_skills(problems, plugin_names)
        check_secrets(problems)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"error: {exc}\n")
        return 2

    ok = not problems
    if output_json:
        sys.stdout.write(
            json.dumps({"ok": ok, "problems": problems}, indent=None if quiet else 2)
            + "\n"
        )
        return 0 if ok else 1

    for problem in problems:
        print(f"  FAIL  {problem}")
    if ok:
        if not quiet:
            print(f"OK: marketplaces, manifests, schemas and secret hygiene are clean "
                  f"({len(plugin_names)} plugin(s)).")
        return 0
    print(f"\nFAILED: {len(problems)} problem(s).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
