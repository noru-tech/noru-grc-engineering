#!/usr/bin/env python3
"""Repository-level checks: marketplace manifests, MCP config, schema/vocabulary sync, secret hygiene.

Standard library only, no network, no install step.

What it covers, and why each one is here rather than left to review:

  * **Marketplace manifests** — the Claude Code and Codex marketplaces must agree on the same set of
    plugins at the same paths. They are two files nobody edits together, so they drift.
  * **Plugin manifests** — every declared source directory really contains a plugin whose name
    matches, for both clients, and no public metadata contains an unfinished placeholder.
  * **MCP config** — when a plugin declares Noru access, it points at the hosted endpoint and
    carries no credential. Local-only utility plugins do not acquire an unnecessary connection.
  * **Supported workflows** — the copyable PR review stays fork-safe and structurally read-only.
  * **Hub routing** — every declared piece appears exactly once in the hub's routing catalogue.
  * **Published examples** — copyable `noru-ci` examples pin the current marketplace version.
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
from generate_enforcement_registry import rendered as rendered_enforcement_registry  # noqa: E402

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
PUBLIC_METADATA_PLACEHOLDER = re.compile(r"\b(?:TODO|TBD|CHANGE_ME)\b", re.IGNORECASE)
UNSUPPORTED_CODEX_MANIFEST_FIELDS = {"commands", "hooks"}
SCANNED_SUFFIXES = {".mjs", ".js", ".py", ".json", ".md", ".yml", ".yaml", ".txt", ".ts"}
SKIP_DIRS = {".git", "node_modules", ".noru"}


def dotted(node, path):
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def marketplace_plugin_directories():
    """Return the plugin names and source directories declared by Claude's marketplace."""
    marketplace = ROOT / ".claude-plugin" / "marketplace.json"
    if not marketplace.is_file():
        return []
    data = json.loads(marketplace.read_text(encoding="utf-8"))
    return [
        (entry["name"], (ROOT / entry["source"]).resolve())
        for entry in data.get("plugins", [])
        if isinstance(entry.get("source"), str)
    ]


def check_public_metadata(problems):
    """Reject unfinished copy in either marketplace or either client's plugin manifests."""
    paths = [
        ROOT / ".claude-plugin" / "marketplace.json",
        ROOT / ".agents" / "plugins" / "marketplace.json",
    ]
    for _name, directory in marketplace_plugin_directories():
        paths.extend(
            directory / rel / "plugin.json"
            for rel in (".claude-plugin", ".codex-plugin")
        )

    for path in paths:
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = PUBLIC_METADATA_PLACEHOLDER.search(line)
            if match:
                problems.append(
                    f"{path.relative_to(ROOT)}:{number}: contains unfinished placeholder text "
                    f"'{match.group()}'"
                )


def check_codex_manifests(problems):
    """Reject unsupported fields in every marketplace-listed Codex manifest."""
    for name, directory in marketplace_plugin_directories():
        manifest = directory / ".codex-plugin" / "plugin.json"
        if not manifest.is_file():
            continue
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for field in sorted(UNSUPPORTED_CODEX_MANIFEST_FIELDS.intersection(data)):
            problems.append(
                f"[{name}] .codex-plugin/plugin.json declares unsupported Codex field '{field}'"
            )
        interface = data.get("interface") or {}
        prompts = interface.get("defaultPrompt")
        prompt_text = " ".join(prompts) if isinstance(prompts, list) else str(prompts or "")
        if "write to Noru" not in prompt_text:
            problems.append(
                f"[{name}] Codex default prompt does not explicitly say not to write to Noru"
            )
        capabilities = interface.get("capabilities") or []
        for prefix in ("Local read:", "Local write:", "Noru read:", "Noru write:"):
            if not any(isinstance(value, str) and value.startswith(prefix) for value in capabilities):
                problems.append(f"[{name}] Codex capabilities do not declare '{prefix}'")


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

    shared_mcp = None
    shared_mcp_owner = None
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

        client_manifests = {}
        for client, rel in (("Claude Code", ".claude-plugin"), ("Codex", ".codex-plugin")):
            manifest = directory / rel / "plugin.json"
            if not manifest.is_file():
                problems.append(f"[{name}] missing {rel}/plugin.json ({client})")
                continue
            data = json.loads(manifest.read_text(encoding="utf-8"))
            client_manifests[client] = data
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
        if not mcp.is_file():
            if "mcpServers" in client_manifests.get("Codex", {}):
                problems.append(f"[{name}] declares MCP capability but has no .mcp.json")
            continue
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
        if shared_mcp is None:
            shared_mcp = server
            shared_mcp_owner = name
        elif server != shared_mcp:
            problems.append(
                f"[{name}] .mcp.json differs from [{shared_mcp_owner}]; independently installed "
                "plugins must declare the same logical Noru server without depending on the hub"
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


def check_hub_routing(problems):
    """The broad hub prompt must be able to route to every piece the repository ships."""
    pieces = sorted(path.parent.name for path in (ROOT / "plugins").glob("*/piece.json"))
    routing_path = ROOT / "plugins" / "noru" / "references" / "routing.json"
    skill_path = ROOT / "plugins" / "noru" / "skills" / "noru" / "SKILL.md"

    if not routing_path.is_file():
        problems.append(
            "[noru] missing references/routing.json — broad repository prompts have no "
            "maintained piece catalogue"
        )
        return

    try:
        routing = json.loads(routing_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        problems.append(f"[noru] references/routing.json is invalid JSON: {exc}")
        return

    rows = routing.get("pieces")
    if not isinstance(rows, list):
        problems.append("[noru] references/routing.json must contain a 'pieces' list")
        return

    names = []
    for index, row in enumerate(rows):
        label = f"references/routing.json pieces[{index}]"
        if not isinstance(row, dict):
            problems.append(f"[noru] {label} must be an object")
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name:
            problems.append(f"[noru] {label} has no name")
        else:
            names.append(name)
            label = f"references/routing.json [{name}]"
        for field in ("summary", "caveat"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                problems.append(f"[noru] {label} has no {field}")
        for field in ("signals", "inspect"):
            values = row.get(field)
            if not (
                isinstance(values, list)
                and values
                and all(isinstance(value, str) and value.strip() for value in values)
            ):
                problems.append(f"[noru] {label} {field} must be a non-empty list of strings")

    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        problems.append(f"[noru] routing catalogue repeats pieces: {duplicates}")

    routed = set(names)
    declared = set(pieces)
    missing = sorted(declared - routed)
    unknown = sorted(routed - declared)
    if missing:
        problems.append(
            f"[noru] routing catalogue omits declared pieces: {missing} — add routing signals "
            "when adding the piece"
        )
    if unknown:
        problems.append(
            f"[noru] routing catalogue names pieces with no piece.json: {unknown}"
        )

    utilities = routing.get("utilities") or []
    utility_names = []
    for index, row in enumerate(utilities):
        label = f"references/routing.json utilities[{index}]"
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            problems.append(f"[noru] {label} must name a utility")
            continue
        utility_names.append(row["name"])
        for field in ("summary", "caveat"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                problems.append(f"[noru] {label} has no {field}")
        for field in ("signals", "inspect"):
            if not isinstance(row.get(field), list) or not row[field]:
                problems.append(f"[noru] {label} {field} must be a non-empty list")
    expected_utilities = sorted(
        path.parent.parent.name
        for path in (ROOT / "plugins").glob("*/.codex-plugin/plugin.json")
        if not (path.parent.parent / "piece.json").is_file()
        and path.parent.parent.name != "noru"
    )
    if sorted(utility_names) != expected_utilities:
        problems.append(
            f"[noru] routed utilities are {sorted(utility_names)}, expected {expected_utilities}"
        )

    if skill_path.is_file() and "references/routing.json" not in skill_path.read_text(encoding="utf-8"):
        problems.append(
            "[noru] the hub skill does not route broad requests through references/routing.json"
        )

    review_path = ROOT / "plugins" / "noru" / "references" / "review-signals.json"
    review_script = ROOT / "plugins" / "noru" / "scripts" / "review.mjs"
    review_command = ROOT / "plugins" / "noru" / "commands" / "review.md"
    if not review_path.is_file():
        problems.append("[noru] missing references/review-signals.json")
        return
    try:
        review = json.loads(review_path.read_text(encoding="utf-8")).get("pieces")
    except (json.JSONDecodeError, AttributeError) as exc:
        problems.append(f"[noru] references/review-signals.json is invalid: {exc}")
        return
    if not isinstance(review, dict):
        problems.append("[noru] references/review-signals.json must contain a 'pieces' object")
        return
    review_names = set(review)
    if review_names != declared:
        problems.append(
            "[noru] branch-review signals do not match declared pieces: "
            f"missing {sorted(declared - review_names)}, unknown {sorted(review_names - declared)}"
        )
    for name, rules in review.items():
        if not isinstance(rules, dict):
            problems.append(f"[noru] review signals for {name} must be an object")
            continue
        count = 0
        for kind in ("paths", "content"):
            entries = rules.get(kind)
            if not isinstance(entries, list):
                problems.append(f"[noru] review signals for {name}.{kind} must be a list")
                continue
            count += len(entries)
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict) or not entry.get("pattern") or not entry.get("reason"):
                    problems.append(
                        f"[noru] review signals for {name}.{kind}[{index}] need pattern and reason"
                    )
        if count == 0:
            problems.append(f"[noru] review signals for {name} contain no rules")
    if not review_script.is_file():
        problems.append("[noru] missing scripts/review.mjs for the branch-review signals")
    if not review_command.is_file():
        problems.append("[noru] missing commands/review.md")

    orchestration_path = ROOT / "plugins" / "noru" / "references" / "orchestration.json"
    status_command = ROOT / "plugins" / "noru" / "commands" / "status.md"
    if not orchestration_path.is_file():
        problems.append("[noru] missing references/orchestration.json")
        return
    try:
        orchestration = json.loads(orchestration_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        problems.append(f"[noru] references/orchestration.json is invalid JSON: {exc}")
        return

    orchestration_pieces = orchestration.get("pieces")
    if not isinstance(orchestration_pieces, dict):
        problems.append("[noru] references/orchestration.json must contain a 'pieces' object")
    else:
        orchestrated = set(orchestration_pieces)
        if orchestrated != declared:
            problems.append(
                "[noru] orchestration pieces do not match declared pieces: "
                f"missing {sorted(declared - orchestrated)}, "
                f"unknown {sorted(orchestrated - declared)}"
            )
        for name, entry in orchestration_pieces.items():
            if not isinstance(entry, dict):
                problems.append(f"[noru] orchestration entry for {name} must be an object")
                continue
            piece_path = ROOT / "plugins" / name / "piece.json"
            if not piece_path.is_file():
                continue
            contract = json.loads(piece_path.read_text(encoding="utf-8"))
            if entry.get("manifest") != contract.get("artifact"):
                problems.append(
                    f"[noru] orchestration manifest for {name} is {entry.get('manifest')}, "
                    f"piece.json declares {contract.get('artifact')}"
                )
            generated = [output.get("path") for output in contract.get("outputs", [])]
            if entry.get("generated_files", []) != generated:
                problems.append(
                    f"[noru] orchestration generated files for {name} are "
                    f"{entry.get('generated_files', [])}, piece.json declares {generated}"
                )
            for phase in ("scan", "diff"):
                if entry.get(f"{phase}_command") != f"/{name}:{phase}":
                    problems.append(
                        f"[noru] orchestration {phase} command for {name} must be /{name}:{phase}"
                    )
                tools = entry.get(f"{phase}_read_tools")
                if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
                    problems.append(
                        f"[noru] orchestration {phase}_read_tools for {name} must be a string list"
                    )

    sections = orchestration.get("status_sections")
    if not isinstance(sections, dict) or not sections:
        problems.append("[noru] orchestration.json must declare status_sections")
    else:
        for name, section in sections.items():
            scope = section.get("scope") if isinstance(section, dict) else None
            tools = section.get("tools") if isinstance(section, dict) else None
            if not isinstance(scope, str) or not scope.startswith("read:"):
                problems.append(f"[noru] status section {name} must declare a read scope")
            if not (
                isinstance(tools, list)
                and tools
                and all(
                    isinstance(tool, str) and tool.startswith(("find", "get", "list"))
                    for tool in tools
                )
            ):
                problems.append(f"[noru] status section {name} must contain read-tool names")
    if not status_command.is_file():
        problems.append("[noru] missing commands/status.md")


def check_action_version_pins(problems):
    """Copyable action examples must not strand users on an older collector release."""
    marketplace_path = ROOT / ".claude-plugin" / "marketplace.json"
    if not marketplace_path.is_file():
        return
    marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    entries = {entry.get("name"): entry for entry in marketplace.get("plugins", [])}
    expected = (entries.get("noru") or {}).get("version")
    if not expected:
        return

    paths = [
        ROOT / "README.md",
        ROOT / "docs" / "ci-mode.md",
        ROOT / "docs" / "developer-onboarding.md",
        ROOT / ".github" / "actions" / "noru-ci" / "README.md",
        ROOT / ".github" / "actions" / "noru-review" / "README.md",
        ROOT / "actions" / "enforce" / "README.md",
        ROOT / "templates" / "github" / "noru-grc-review.yml",
    ]
    # Both the in-tree path and the Marketplace distribution form (noru-tech/noru-ci-action@v…),
    # which scripts/publish_actions.py mirrors from the same tag.
    pattern = re.compile(r"noru-(?:ci|review|enforce)(?:-action)?@v([0-9]+\.[0-9]+\.[0-9]+)")
    found = 0
    for path in paths:
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for match in pattern.finditer(line):
                found += 1
                if match.group(1) != expected:
                    problems.append(
                        f"{path.relative_to(ROOT)}:{number}: pins a Noru action at v{match.group(1)}, "
                        f"but the marketplace version is {expected}"
                    )
    if not found:
        problems.append("no copyable Noru action pinned at v<version> is documented")


def check_supported_workflows(problems):
    """The supported PR template must stay fork-safe and structurally unable to publish."""
    template = ROOT / "templates" / "github" / "noru-grc-review.yml"
    action = ROOT / ".github" / "actions" / "noru-review" / "action.yml"
    script = ROOT / "scripts" / "ci_review.py"
    for path in (template, action, script):
        if not path.is_file():
            problems.append(f"missing supported review component {path.relative_to(ROOT)}")
    if not template.is_file() or not action.is_file() or not script.is_file():
        return

    template_text = template.read_text(encoding="utf-8")
    if "pull_request_target" in template_text:
        problems.append("supported PR template must never use pull_request_target")
    if "contents: read" not in template_text:
        problems.append("supported PR template must request only read access to repository contents")
    if "NORU_API_KEY" in template_text or "secrets." in template_text:
        problems.append("supported PR template must not receive a Noru or repository secret")

    action_text = action.read_text(encoding="utf-8")
    script_text = script.read_text(encoding="utf-8")
    if "--steps=scan,validate,expiry,policy" not in script_text:
        problems.append("consolidated PR review must hard-code the local read-only CI steps")
    if 'env.pop("NORU_API_KEY", None)' not in script_text:
        problems.append("consolidated PR review must remove NORU_API_KEY from child processes")
    if "inputs:\n  steps:" in action_text:
        problems.append("noru-review must not expose a steps input that could enable push")



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

def check_yaml_11_booleans(problems):
    """No manifest may use a bare YAML 1.1 boolean word as a key or an unquoted value.

    PyYAML resolves `yes`, `no`, `on`, `off`, `y` and `n` to booleans; the bundled fallback leaves
    them as strings. Both loaders are in production — which one runs is a property of the machine —
    so a file using one of these words validates here and fails there, for reasons no message
    explains.

    docs/verification.md carried this as an open Known gap with the note "no fixture is written that
    way, so nothing fails today". One then was: `change-control` named an approval's date field
    `on`, PyYAML read the key as the boolean True, and the CI matrix caught it where every local run
    had passed. This check is that gap closed — the divergence is still real, but a file can no
    longer walk into it unnoticed.
    """
    words = {"yes", "no", "on", "off", "y", "n"}
    roots = [ROOT / "plugins", ROOT / "contract", ROOT / "tests"]
    key_re = re.compile(r"^\s*(?:-\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*:(?:\s|$)")
    value_re = re.compile(r"^\s*(?:-\s+)?[A-Za-z_][A-Za-z0-9_]*\s*:\s*([A-Za-z]+)\s*(?:#.*)?$")

    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.yml")) + sorted(root.rglob("*.yaml")):
            # A GitHub Actions workflow MUST use `on:` — GitHub defines the key and nothing here
            # parses these for meaning. The rule is about manifests this repository's own two
            # loaders read, where the divergence actually bites.
            if ".github/workflows/" in path.as_posix():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(lines, start=1):
                if line.lstrip().startswith("#"):
                    continue
                key = key_re.match(line)
                if key and key.group(1).lower() in words:
                    problems.append(
                        f"{path.relative_to(ROOT)}:{number}: the key `{key.group(1)}` is a YAML 1.1 "
                        "boolean — PyYAML reads it as true/false and the bundled loader reads it as "
                        "a string, so this file means two different things on two machines. Rename it"
                    )
                value = value_re.match(line)
                if value and value.group(1).lower() in words:
                    problems.append(
                        f"{path.relative_to(ROOT)}:{number}: the unquoted value `{value.group(1)}` is "
                        "a YAML 1.1 boolean — quote it, or spell it true/false"
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

    classification = ROOT / "plugins" / "privacy-datamap" / "references" / "classification.json"
    if not classification.is_file():
        return
    piece_keys = json.loads(classification.read_text(encoding="utf-8")).get("special_categories")
    for key in sorted(
        key
        for key in (piece_keys or [])
        if not any(key == root or key.startswith(root + ".") for root in roots)
    ):
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


def check_enforcement_registry(problems):
    path = ROOT / "actions" / "enforce" / "registry.json"
    if not path.is_file():
        problems.append("actions/enforce/registry.json is missing")
        return
    if path.read_text(encoding="utf-8") != rendered_enforcement_registry():
        problems.append(
            "actions/enforce/registry.json has drifted from piece.json declarations — run "
            "python3 scripts/generate_enforcement_registry.py"
        )


def check_enforcement_action(problems):
    """The copyable merge gate must remain whole-repository, pinned, and credential-free."""
    action = ROOT / "actions" / "enforce" / "action.yml"
    runtime = ROOT / "actions" / "enforce" / "dist" / "enforce.js"
    workflow = ROOT / "plugins" / "repo-enforcement" / "assets" / "github" / "noru-grc.yml"
    for path in (action, runtime, workflow):
        if not path.is_file():
            problems.append(f"missing repository enforcement component {path.relative_to(ROOT)}")
    if not all(path.is_file() for path in (action, runtime, workflow)):
        return
    action_text = action.read_text(encoding="utf-8")
    runtime_text = runtime.read_text(encoding="utf-8")
    workflow_text = workflow.read_text(encoding="utf-8")
    if 'using: "node24"' not in action_text:
        problems.append("actions/enforce must use the supported node24 JavaScript action runtime")
    if "pull_request:" not in workflow_text or re.search(r"^\s+paths(?:-ignore)?:", workflow_text, re.M):
        problems.append("repository enforcement workflow must run on every pull request without path filters")
    if "permissions:\n  contents: read" not in workflow_text:
        problems.append("repository enforcement workflow must request only contents: read")
    if "secrets." in workflow_text or "NORU_API_KEY" in workflow_text:
        problems.append("repository enforcement workflow must not receive Noru or repository secrets")
    if not re.search(r"actions/checkout@[0-9a-f]{40}\b", workflow_text):
        problems.append("repository enforcement workflow must pin checkout to a full commit SHA")
    if "actions/enforce@__NORU_ENFORCE_SHA__" not in workflow_text:
        problems.append("repository enforcement workflow must expose only the full action-SHA placeholder")
    if '"--steps=scan,validate,expiry"' not in (ROOT / "plugins" / "repo-enforcement" / "scripts" / "enforce.py").read_text(encoding="utf-8"):
        problems.append("repository enforcement must hard-code offline-only aggregate validation steps")
    if "TOKEN|SECRET|PASSWORD|API_KEY|AUTHORIZATION" not in runtime_text:
        problems.append("actions/enforce must remove credential-like environment variables")


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
        check_public_metadata(problems)
        check_codex_manifests(problems)
        check_pieces_registered(problems, plugin_names)
        check_hub_routing(problems)
        check_action_version_pins(problems)
        check_supported_workflows(problems)
        check_reference_files_exist(problems)
        check_yaml_11_booleans(problems)
        check_special_categories(problems)
        check_vocab_sync(problems)
        check_schemas_evaluable(problems)
        check_enforcement_registry(problems)
        check_enforcement_action(problems)
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
