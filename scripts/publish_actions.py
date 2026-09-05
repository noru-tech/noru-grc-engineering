#!/usr/bin/env python3
"""Build and publish the single-action repositories the GitHub Marketplace requires.

The Marketplace lists one action per public repository, and only when `action.yml` sits at that
repository's root. This repository ships three actions in subdirectories, and every one of them
runs the toolkit that sits beside it: `scripts/ci_check.py`, `plugins/`, `contract/`. Neither fact
is going to change, so each release is mirrored into a dedicated distribution repository whose
root *is* the action and whose subtree is a verbatim copy of the toolkit it needs.

    build    --out=<dir> [--action=<name>]
        Render every mirror into <dir>/<repository-name>/ and stop.
    --check
        Build into a temporary directory and run each action from *that* layout: the CI check on
        the fixture repository, the review on this repository, the enforcement runtime on a freshly
        configured repository. This is what proves the mirror is complete.
    publish  --version=X.Y.Z [--action=<name>] [--dry-run]
        Sync each mirror repository with the build, commit, tag vX.Y.Z, move the vX tag, and create
        the GitHub release. Needs a git credential and `gh` that can write to the mirrors.

`publish` is idempotent so the release workflow can be re-run. A mirror whose tree already matches
the build gets no new commit; a version tag that already exists is left alone when it points at the
same tree and is a hard failure when it does not, because a released tag is immutable — bump the
version instead. The GitHub release is created only when it is missing.

What this script cannot do is the very first Marketplace listing. That is a one-time click in the
mirror repository's release page ("Publish this Action to the GitHub Marketplace") which GitHub
gates behind two-factor confirmation and exposes through no API. After that, releases created here
are listed automatically. CONTRIBUTING.md has the runbook.

Stdlib only, like everything else under scripts/.
"""

import json
import os
import pathlib
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE_REPO = "noru-tech/noru-grc-engineering"
SOURCE_URL = f"https://github.com/{SOURCE_REPO}"
# Where mirror repositories live. --check points this at local bare repositories to exercise the
# publish path offline; anything that is not github.com also skips the `gh release` step.
REMOTE_BASE = os.environ.get("NORU_ACTIONS_REMOTE_BASE", "https://github.com/")

# One row per published action. `source` is the directory whose contents become the mirror root;
# `repo` is the distribution repository. Every mirror also receives the shared toolkit below.
ACTIONS = {
    "noru-ci": {
        "source": ".github/actions/noru-ci",
        "repo": "noru-tech/noru-ci-action",
    },
    "noru-review": {
        "source": ".github/actions/noru-review",
        "repo": "noru-tech/noru-review-action",
    },
    "enforce": {
        "source": "actions/enforce",
        "repo": "noru-tech/noru-enforce-action",
    },
}

# The scripts an action executes at runtime. Their sibling imports are followed automatically, so
# a helper module added to ci_check.py tomorrow ships without anyone editing this list.
RUNTIME_ENTRYPOINTS = ("ci_check.py", "ci_review.py")
TOOLKIT_DIRS = ("plugins", "contract")
ROOT_FILES = ("LICENSE", "NOTICE")
COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "node_modules", ".noru")
COMMITTER = ("Noru", "support@noru.tech")


def run(args, cwd=None, check=True, env=None, timeout=600):
    return subprocess.run(
        args, cwd=cwd, text=True, capture_output=True, check=check, env=env, timeout=timeout
    )


def marketplace_version():
    """Every plugin shares one version; the release workflow already asserts that."""
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    for entry in marketplace["plugins"]:
        if entry["name"] == "noru":
            return entry["version"]
    raise ValueError("no 'noru' entry in .claude-plugin/marketplace.json")


def source_commit():
    try:
        return run(["git", "rev-parse", "HEAD"], cwd=ROOT).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def runtime_scripts():
    """Transitive closure of sibling imports from the runtime entrypoints."""
    scripts = ROOT / "scripts"
    wanted = list(RUNTIME_ENTRYPOINTS)
    seen = set()
    pattern = re.compile(r"^\s*(?:from\s+(\w+)\s+import|import\s+(\w+))", re.M)
    while wanted:
        name = wanted.pop()
        if name in seen:
            continue
        seen.add(name)
        text = (scripts / name).read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            module = match.group(1) or match.group(2)
            if (scripts / f"{module}.py").is_file():
                wanted.append(f"{module}.py")
    return sorted(seen)


def rewrite_readme(text, action, version):
    """Turn the in-tree README into the mirror's README: banner, short `uses:` form, absolute links."""
    spec = ACTIONS[action]
    source = spec["source"]
    repo = spec["repo"]

    text = text.replace(f"{SOURCE_REPO}/{source}@", f"{repo}@")

    def absolute(match):
        target = posixpath.normpath(posixpath.join(source, match.group(1)))
        return f"]({SOURCE_URL}/blob/v{version}/{target})"

    text = re.sub(r"\]\((\.\.?/[^)\s]+)\)", absolute, text)

    lines = text.splitlines()
    title = lines[0] if lines and lines[0].startswith("# ") else f"# {action}"
    body = "\n".join(lines[1:] if lines and lines[0].startswith("# ") else lines).lstrip("\n")
    banner = [
        title,
        "",
        f"> **Distribution repository.** This is the GitHub Marketplace listing of the `{action}`",
        f"> action from [{SOURCE_REPO}]({SOURCE_URL}). It is generated by",
        "> `scripts/publish_actions.py` on every release: do not edit it here, changes land upstream",
        f"> and the next release overwrites this tree. Issues: {SOURCE_URL}/issues",
        ">",
        f"> `uses: {repo}@v{version}` and",
        f"> `uses: {SOURCE_REPO}/{source}@v{version}`",
        "> are the same code at the same version. The toolkit the action runs (`scripts/`,",
        "> `plugins/`, `contract/`) is copied verbatim from that tag.",
    ]
    if action == "enforce":
        banner += [
            ">",
            "> The workflow that `repo-enforcement` installs pins the *upstream* path at a full commit",
            "> SHA, and `/repo-enforcement:verify` recognises only that form. Use this repository for",
            "> hand-written workflows; leave the managed one on the upstream pin.",
        ]
    return "\n".join(banner) + "\n\n" + body.rstrip("\n") + "\n"


def build_one(action, out_dir, version, commit):
    spec = ACTIONS[action]
    source = ROOT / spec["source"]
    mirror = out_dir / spec["repo"].split("/", 1)[1]
    if mirror.exists():
        shutil.rmtree(mirror)
    shutil.copytree(source, mirror, ignore=COPY_IGNORE)

    readme = mirror / "README.md"
    if readme.is_file():
        readme.write_text(
            rewrite_readme(readme.read_text(encoding="utf-8"), action, version), encoding="utf-8"
        )

    (mirror / "scripts").mkdir()
    for name in runtime_scripts():
        shutil.copy2(ROOT / "scripts" / name, mirror / "scripts" / name)
    for name in TOOLKIT_DIRS:
        shutil.copytree(ROOT / name, mirror / name, ignore=COPY_IGNORE)
    for name in ROOT_FILES:
        if (ROOT / name).is_file():
            shutil.copy2(ROOT / name, mirror / name)

    provenance = {
        "action": action,
        "version": version,
        "source_repository": SOURCE_REPO,
        "source_path": spec["source"],
        "source_commit": commit,
        "generated_by": "scripts/publish_actions.py",
        "note": "Generated on release. Edit upstream, never here.",
    }
    (mirror / "DISTRIBUTION.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return mirror


def build(out_dir, actions, version=None, commit=None):
    version = version or marketplace_version()
    commit = commit or source_commit()
    out_dir.mkdir(parents=True, exist_ok=True)
    return {action: build_one(action, out_dir, version, commit) for action in actions}


# --- check ---------------------------------------------------------------------------------------


class Results:
    def __init__(self):
        self.rows = []

    def check(self, name, ok, detail=""):
        self.rows.append({"test": name, "ok": bool(ok), "detail": str(detail)[:1000]})

    def finish(self, output_json, quiet):
        ok = all(row["ok"] for row in self.rows)
        if output_json:
            print(json.dumps({"ok": ok, "total": len(self.rows), "results": self.rows}))
        elif not quiet:
            for row in self.rows:
                print(f"  {'ok' if row['ok'] else 'FAIL':4}  {row['test']}")
                if not row["ok"] and row["detail"]:
                    print(f"        {row['detail'][:300]}")
            print(f"\n{'OK' if ok else 'FAILED'}: {len(self.rows)} marketplace distribution checks.")
        return 0 if ok else 1


def check_metadata(results):
    """What the Marketplace validates at listing time, checked before a release rather than after."""
    names = {}
    for action, spec in ACTIONS.items():
        source = ROOT / spec["source"]
        text = (source / "action.yml").read_text(encoding="utf-8") if (source / "action.yml").is_file() else ""
        results.check(f"[{action}] has action.yml and README.md", text and (source / "README.md").is_file(), spec["source"])
        results.check(f"[{action}] declares branding (Marketplace requires it)", "branding:" in text)
        match = re.search(r'^name:\s*"?([^"\n]+)"?\s*$', text, re.M)
        name = match.group(1).strip() if match else ""
        names.setdefault(name, []).append(action)
        results.check(f"[{action}] declares a name", bool(name))
    for name, owners in names.items():
        results.check(f"action name '{name}' is unique across the mirrors", len(owners) == 1, owners)


def check_ci(results, mirror, tmp):
    # ci_check.py caches derived facts under <repo>/.noru/.cache, so it runs on a copy of the
    # fixture rather than leaving an untracked file in the tree the check is meant to protect.
    fixture = tmp / "fixture-repo"
    shutil.copytree(ROOT / "tests" / "fixture-repo", fixture)
    report = tmp / "ci.json"
    completed = run(
        [
            sys.executable, str(mirror / "scripts" / "ci_check.py"),
            "--piece=ai-inventory", f"--repo={fixture}",
            "--mode=warn", "--as-of=2026-08-27", f"--report={report}", "--output=json", "--quiet",
        ],
        check=False,
    )
    payload = {}
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        pass
    results.check(
        "[noru-ci] ci_check.py runs from the mirror against the fixture repository",
        completed.returncode == 0 and payload.get("check") == "ci" and report.is_file(),
        completed.stderr or completed.stdout,
    )


def check_review(results, mirror, tmp):
    report = tmp / "review.json"
    completed = run(
        [
            sys.executable, str(mirror / "scripts" / "ci_review.py"),
            f"--repo={ROOT}", "--base-ref=HEAD", "--mode=warn",
            f"--report={report}", "--output=json", "--quiet",
        ],
        check=False,
    )
    payload = {}
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        pass
    results.check(
        "[noru-review] ci_review.py runs from the mirror and stays read-only",
        completed.returncode == 0 and "push" in str(payload.get("write_boundary", "")) and report.is_file(),
        completed.stderr or completed.stdout,
    )


def check_enforce(results, mirror, tmp):
    """The same assertion scripts/test_repo_enforcement.py makes, from the mirror's layout."""
    repo = tmp / "enforced"
    repo.mkdir()
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.name", "Fixture User"],
        ["git", "config", "user.email", "fixture@example.com"],
    ):
        run(args, cwd=repo)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    run(["git", "add", "README.md"], cwd=repo)
    run(["git", "commit", "-q", "-m", "fixture"], cwd=repo)
    (repo / ".github").mkdir()
    (repo / ".github" / "CODEOWNERS").write_text("/docs/ @example/docs\n", encoding="utf-8")

    configure = mirror / "plugins" / "repo-enforcement" / "scripts" / "configure.mjs"
    common = [
        f"--repo={repo}", "--action-sha=" + "a" * 40,
        "--grc-reviewers=@example/grc-reviewers", "--privacy-reviewers=@example/privacy-reviewers",
        "--security-reviewers=@example/security-reviewers", "--break-glass=@example/security-admins",
        "--mode=ratchet", "--output=json", "--quiet",
    ]
    planned = run(["node", str(configure), "plan", *common], check=False)
    applied = run(["node", str(configure), "apply", f"--repo={repo}", "--confirm", "--output=json", "--quiet"], check=False)
    results.check(
        "[enforce] the mirrored repo-enforcement plugin configures a repository",
        planned.returncode == 0 and applied.returncode == 0,
        planned.stderr or applied.stderr,
    )

    env = {
        key: value
        for key, value in os.environ.items()
        if not re.search(r"(?:TOKEN|SECRET|PASSWORD|API_KEY|AUTHORIZATION)", key, re.I)
    }
    env.update({
        "GITHUB_WORKSPACE": str(repo), "INPUT_AS-OF": "2026-09-04", "INPUT_REPO": ".",
        "INPUT_POLICY": ".noru/enforcement.yml", "RUNNER_TEMP": str(tmp / "runner"),
    })
    action = run(["node", str(mirror / "dist" / "enforce.js")], cwd=repo, env=env, check=False)
    report = tmp / "runner" / "noru-grc-enforcement.json"
    payload = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
    results.check(
        "[enforce] dist/enforce.js finds the toolkit at the mirror root and fails closed",
        action.returncode == 1 and payload and not payload.get("ok", True),
        action.stderr or action.stdout,
    )


def check_publish(results, tmp):
    """publish against local bare repositories: first run ships, second is a no-op, a moved tag fails."""
    remotes = tmp / "remotes"
    for spec in ACTIONS.values():
        bare = remotes / f"{spec['repo']}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "-q", "--bare", "-b", "main", str(bare)])
    env = {**os.environ, "NORU_ACTIONS_REMOTE_BASE": remotes.as_uri()}
    version = marketplace_version()
    argv = [sys.executable, str(pathlib.Path(__file__).resolve()), "publish", f"--version={version}"]

    first = run(argv, env=env, check=False)
    probe = remotes / f"{ACTIONS['noru-ci']['repo']}.git"
    tags = run(["git", "tag", "-l"], cwd=probe, check=False).stdout.split()
    results.check(
        "[publish] first run commits, tags vX.Y.Z and moves the major tag on an empty mirror",
        first.returncode == 0 and f"v{version}" in tags and f"v{version.split('.')[0]}" in tags,
        first.stderr or first.stdout,
    )
    second = run(argv, env=env, check=False)
    results.check(
        "[publish] a second run of the same version is a no-op",
        second.returncode == 0 and "nothing to commit" in second.stdout and "already exists and matches" in second.stdout,
        second.stderr or second.stdout,
    )
    # Retag v<version> at an unrelated tree in one mirror: republishing must refuse to move it.
    clone = tmp / "tamper"
    run(["git", "clone", "-q", str(probe), str(clone)])
    (clone / "TAMPERED").write_text("x\n", encoding="utf-8")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    run(["git", "add", "-A"], cwd=clone)
    run([*git, "commit", "-q", "-m", "tamper"], cwd=clone)
    run([*git, "tag", "-f", f"v{version}"], cwd=clone)
    run(["git", "push", "-q", "--force", "origin", f"refs/tags/v{version}"], cwd=clone)
    third = run(argv, env=env, check=False)
    results.check(
        "[publish] a released tag that points elsewhere is a hard failure, never overwritten",
        third.returncode != 0 and "immutable" in third.stderr,
        third.stderr or third.stdout,
    )


def check(output_json, quiet):
    results = Results()
    check_metadata(results)
    with tempfile.TemporaryDirectory(prefix="noru-actions-") as tmp:
        tmp = pathlib.Path(tmp)
        mirrors = build(tmp / "build", ACTIONS)
        for action, mirror in mirrors.items():
            results.check(
                f"[{action}] mirror root holds action.yml, README.md, DISTRIBUTION.json and the toolkit",
                all((mirror / name).exists() for name in ("action.yml", "README.md", "DISTRIBUTION.json", "scripts/ci_check.py", "plugins", "contract", "LICENSE")),
                sorted(p.name for p in mirror.iterdir()),
            )
            readme = (mirror / "README.md").read_text(encoding="utf-8")
            results.check(
                f"[{action}] README points at the mirror and has no dangling relative links",
                f"uses: {ACTIONS[action]['repo']}@v" in readme and "](../" not in readme,
            )
        check_ci(results, mirrors["noru-ci"], tmp)
        check_review(results, mirrors["noru-review"], tmp)
        check_enforce(results, mirrors["enforce"], tmp)
        check_publish(results, tmp)
    return results.finish(output_json, quiet)


# --- publish -------------------------------------------------------------------------------------


def parse_version(tag):
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
    return tuple(int(part) for part in match.groups()) if match else None


def log(message):
    print(message, flush=True)


def publish_one(action, mirror, version, commit, dry_run):
    spec = ACTIONS[action]
    repo = spec["repo"]
    tag = f"v{version}"
    major = f"v{version.split('.', 1)[0]}"
    url = f"{REMOTE_BASE.rstrip('/')}/{repo}.git"
    on_github = REMOTE_BASE.startswith("https://github.com")
    log(f"\n== {action} -> {repo} @ {tag}")

    work = mirror.parent / f"{mirror.name}.git-work"
    try:
        run(["git", "clone", "--quiet", url, str(work)])
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"error: cannot clone {url}: {exc.stderr.strip()}\n"
            f"Create it first (once): gh repo create {repo} --public "
            f"--description \"GitHub Marketplace distribution of the {action} action from {SOURCE_REPO}\""
        ) from exc

    git = ["git", "-c", f"user.name={COMMITTER[0]}", "-c", f"user.email={COMMITTER[1]}"]
    has_head = run(["git", "rev-parse", "--verify", "HEAD"], cwd=work, check=False).returncode == 0
    if has_head:
        branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=work).stdout.strip()
    else:
        branch = "main"
        run(["git", "checkout", "-q", "-b", branch], cwd=work)

    for entry in work.iterdir():
        if entry.name == ".git":
            continue
        shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    for entry in mirror.iterdir():
        target = work / entry.name
        shutil.copytree(entry, target) if entry.is_dir() else shutil.copy2(entry, target)

    run(["git", "add", "-A"], cwd=work)
    changed = bool(run(["git", "status", "--porcelain"], cwd=work).stdout.strip())
    pushes = []
    if changed:
        message = (
            f"chore: release {version}\n\n"
            f"Generated from {SOURCE_REPO}@{commit} ({tag}) by scripts/publish_actions.py.\n"
        )
        run([*git, "commit", "-q", "-m", message], cwd=work)
        pushes.append(["git", "push", "origin", f"HEAD:refs/heads/{branch}"])
        log(f"   commit: tree changed, committed release {version}")
    else:
        log("   commit: tree already matches, nothing to commit")

    head_tree = run(["git", "rev-parse", "HEAD^{tree}"], cwd=work).stdout.strip()
    existing = run(["git", "tag", "-l", tag], cwd=work).stdout.strip()
    if existing:
        tagged_tree = run(["git", "rev-parse", f"{tag}^{{tree}}"], cwd=work).stdout.strip()
        if tagged_tree != head_tree:
            raise SystemExit(
                f"error: {repo} already has {tag} pointing at a different tree. A released tag is "
                f"immutable; bump the version instead of republishing different content under it."
            )
        log(f"   tag: {tag} already exists and matches")
    else:
        run([*git, "tag", "-a", tag, "-m", f"{action} {tag} (from {SOURCE_REPO}@{commit})"], cwd=work)
        pushes.append(["git", "push", "origin", f"refs/tags/{tag}"])
        log(f"   tag: created {tag}")

    # Move the floating major tag only when this really is the newest release of that major line,
    # so republishing an old patch never drags v0 backwards.
    known = [parse_version(t) for t in run(["git", "tag", "-l", f"{major}.*"], cwd=work).stdout.split()]
    known = [v for v in known if v] + [parse_version(tag)]
    is_latest = parse_version(tag) == max(known)
    if is_latest:
        run([*git, "tag", "-f", major, "HEAD"], cwd=work)
        pushes.append(["git", "push", "--force", "origin", f"refs/tags/{major}"])
        log(f"   tag: {major} -> {tag}")
    else:
        log(f"   tag: {major} left alone ({tag} is not the newest {major}.x)")

    for command in pushes:
        if dry_run:
            log(f"   dry-run: {' '.join(command)}")
        else:
            run(command, cwd=work)
            log(f"   pushed: {command[-1]}")

    if not on_github:
        log("   release: skipped, remote is not github.com")
        return
    exists = run(["gh", "release", "view", tag, "--repo", repo], check=False).returncode == 0
    if exists:
        log(f"   release: {tag} already exists")
        return
    notes = work / "RELEASE_NOTES.tmp.md"
    notes.write_text(
        f"Distribution of [{SOURCE_REPO} {tag}]({SOURCE_URL}/releases/tag/{tag}).\n\n"
        f"- What changed: [CHANGELOG.md]({SOURCE_URL}/blob/{tag}/CHANGELOG.md)\n"
        f"- Source: `{SOURCE_REPO}@{commit}`, path `{spec['source']}`\n"
        f"- Use: `uses: {repo}@{tag}`\n",
        encoding="utf-8",
    )
    command = [
        "gh", "release", "create", tag, "--repo", repo, "--title", tag,
        "--notes-file", str(notes), "--verify-tag", f"--latest={'true' if is_latest else 'false'}",
    ]
    if dry_run:
        log(f"   dry-run: {' '.join(command[:-3])} ...")
    else:
        run(command)
        log(f"   release: created {tag}")


def publish(version, actions, dry_run):
    expected = marketplace_version()
    if version != expected:
        raise SystemExit(
            f"error: --version={version} but the checked-out tree is {expected}. Publish from the "
            f"v{version} tag so the mirror carries exactly the released toolkit."
        )
    commit = source_commit()
    with tempfile.TemporaryDirectory(prefix="noru-actions-") as tmp:
        mirrors = build(pathlib.Path(tmp) / "build", actions, version, commit)
        for action, mirror in mirrors.items():
            publish_one(action, mirror, version, commit, dry_run)
    log("\ndone" + (" (dry run, nothing pushed)" if dry_run else ""))
    return 0


# --- cli -----------------------------------------------------------------------------------------


USAGE = """usage:
  publish_actions.py build --out=<dir> [--action=<name>]
  publish_actions.py --check [--output=json] [--quiet]
  publish_actions.py publish --version=X.Y.Z [--action=<name>] [--dry-run]
actions: """ + ", ".join(ACTIONS)


def main(argv):
    command = None
    opts = {"out": None, "action": None, "version": None, "dry_run": False, "json": False, "quiet": False, "check": False}
    for arg in argv:
        if arg in ("build", "publish") and command is None:
            command = arg
        elif arg == "--check":
            opts["check"] = True
        elif arg.startswith("--out="):
            opts["out"] = pathlib.Path(arg.split("=", 1)[1]).resolve()
        elif arg.startswith("--action="):
            opts["action"] = arg.split("=", 1)[1]
        elif arg.startswith("--version="):
            opts["version"] = arg.split("=", 1)[1].lstrip("v")
        elif arg == "--dry-run":
            opts["dry_run"] = True
        elif arg == "--output=json":
            opts["json"] = True
        elif arg == "--output=text":
            opts["json"] = False
        elif arg == "--quiet":
            opts["quiet"] = True
        elif arg in ("-h", "--help"):
            print(USAGE)
            return 0
        else:
            sys.stderr.write(f"error: unknown option '{arg}'\n{USAGE}\n")
            return 2

    if opts["action"] and opts["action"] not in ACTIONS:
        sys.stderr.write(f"error: unknown action '{opts['action']}'; one of {', '.join(ACTIONS)}\n")
        return 2
    actions = [opts["action"]] if opts["action"] else list(ACTIONS)

    if opts["check"]:
        return check(opts["json"], opts["quiet"])
    if command == "build":
        if not opts["out"]:
            sys.stderr.write("error: build needs --out=<dir>\n")
            return 2
        for action, mirror in build(opts["out"], actions).items():
            print(f"{action}: {mirror}")
        return 0
    if command == "publish":
        if not opts["version"] or not re.fullmatch(r"\d+\.\d+\.\d+", opts["version"]):
            sys.stderr.write("error: publish needs --version=X.Y.Z\n")
            return 2
        return publish(opts["version"], actions, opts["dry_run"])
    sys.stderr.write(USAGE + "\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
