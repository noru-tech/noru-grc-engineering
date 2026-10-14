#!/usr/bin/env python3
"""Offline end-to-end evaluation against independently specified synthetic applications."""
import copy
import hashlib
import importlib
import json
import pathlib
import shutil
import subprocess
import sys
from privacy_semantic_test_support import semantic_review
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "plugins/privacy-datamap/scripts"
FIXTURES = ROOT / "tests/classification-fixtures"
sys.path.insert(0, str(SCRIPTS))
import analysis_storage


EVALUATIONS = {}


def command(*args, repo=None):
    return subprocess.run([str(arg) for arg in args], cwd=repo or ROOT, text=True,
                          capture_output=True, timeout=120)


def field_key(row):
    return "/".join(row["entity_id"].split("/")[-2:]).replace("[]", "")


def descendants(row):
    for child in row.get("nested_members", []):
        yield child
        yield from descendants(child)


def metrics(rows, expected):
    """Keep abstention, classification error and taxonomy error in separate denominators."""
    indexed = {field_key(row): row for row in rows}
    truth = expected["fields"]
    labeled = {key: value for key, value in truth.items() if value["personal_data"] in {"yes", "no"} and value.get("category_fit") != "unresolved"}
    predicted = {key: indexed[key] for key in labeled if key in indexed
                 and indexed[key].get("personal_data") in {"yes", "no"}
                 and indexed[key].get("status") == "proposed"}
    personal_predictions = {key: row for key, row in predicted.items() if row["personal_data"] == "yes"}
    correct_personal = sum(truth[key]["personal_data"] == "yes" for key in personal_predictions)
    false_exclusions = sum(row["personal_data"] == "no" and truth[key]["personal_data"] == "yes"
                           for key, row in predicted.items())
    gold_personal = sum(row["personal_data"] == "yes" for row in labeled.values())
    correct_categories = sum(sorted(row.get("categories", [])) == sorted(truth[key]["categories"])
                             for key, row in personal_predictions.items())
    return {
        "inventory": {"observed": len(indexed), "expected": len(truth),
                      "missing": sorted(set(truth) - set(indexed)), "unexpected": sorted(set(indexed) - set(truth))},
        "personal_precision": {"correct": correct_personal, "predicted_personal": len(personal_predictions),
                               "rate": correct_personal / len(personal_predictions) if personal_predictions else None},
        "personal_recall": {"correct": correct_personal, "gold_personal": gold_personal,
                            "rate": correct_personal / gold_personal if gold_personal else None},
        "unresolved_gold_personal": sum(value["personal_data"] == "yes" and key not in predicted for key, value in labeled.items()),
        "false_exclusions": {"count": false_exclusions, "gold_personal": gold_personal,
                             "rate": false_exclusions / gold_personal if gold_personal else None},
        "category_accuracy": {"correct": correct_categories, "predicted_personal": len(personal_predictions),
                              "rate": correct_categories / len(personal_predictions) if personal_predictions else None},
        "resolution_coverage": {"resolved": len(predicted), "gold_resolvable": len(labeled),
                                "rate": len(predicted) / len(labeled) if labeled else None},
        "unresolved": sum(row.get("status") != "proposed" for row in rows),
    }


class ClassificationEvaluation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = importlib.import_module("field_review")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="privacy-evaluation-")
        self.addCleanup(self.temp.cleanup)
        self.base = pathlib.Path(self.temp.name)

    def repo(self, fixture, name="subject"):
        destination = self.base / name
        shutil.copytree(FIXTURES / fixture, destination)
        expected = json.loads((destination / "expected.json").read_text())
        (destination / "expected.json").unlink()
        (destination / ".gitignore").write_text(".noru/\n.fides/\n")
        for args in (("git", "init", "-q"), ("git", "add", ".")):
            result = command(*args, repo=destination)
            self.assertEqual(result.returncode, 0, result.stderr)
        return destination, expected

    def scan(self, repo):
        for args in (("node", SCRIPTS / "collect.mjs", f"--repo={repo}", "--output=json", "--quiet"),
                     ("python3", SCRIPTS / "reconcile.py", f"--repo={repo}", "--output=json", "--quiet"),
                     ("python3", SCRIPTS / "field_review.py", f"--repo={repo}", "--fresh")):
            result = command(*args)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        document = analysis_storage.load(repo / ".noru/.cache/privacy-datamap.analysis.json")
        self.assertIn("classification_review", document)
        derived = document.get("preview", {}).get("derived")
        if not derived:
            derived = json.loads((repo / ".noru/.cache/privacy-datamap.derived.json").read_text())
        return document, derived

    def rows(self, document):
        return document["classification_review"]["fields"]

    def row(self, document, key):
        return next(row for row in self.rows(document) if field_key(row) == key)

    def gate(self, document, derived, repo, complete=True):
        return self.engine.validate_contract(document, derived, repo, require_complete=complete)

    def decision(self, document, key, personal_data, categories, association, meaning, basis="direct"):
        row = self.row(document, key)
        decision = self.engine.decision_template(row)
        evidence = row["evidence_ids"]
        decision.update(personal_data=personal_data, categories=categories, meaning=meaning,
                        identification_basis=basis, rationale=meaning,
                        person_association={"kind": association, "target": key.split("/")[0],
                                            "rationale": meaning, "evidence_ids": evidence},
                        category_fit=[{"category": category, "rationale": meaning, "evidence_ids": evidence}
                                      for category in categories],
                        investigation=[{"question": "What value do the declaration and writers establish?",
                                        "checked_fact_ids": row["supporting_fact_ids"], "outcome": meaning}],
                        uncertainty=None, resolution_owner=None, confidence="high")
        decision["semantic_review"] = semantic_review(self.engine, decision["evidence_ids"], decision["rationale"])
        return decision

    def test_investigation_cli_applies_evidenced_decisions_without_acceptance(self):
        repo, expected = self.repo("relational-app")
        document, derived = self.scan(repo)
        before_files = {str(path.relative_to(repo)) for path in (repo / ".noru").rglob("*") if path.is_file()}
        batch = command("python3", SCRIPTS / "investigate.py", f"--repo={repo}", "--limit=2")
        self.assertEqual(batch.returncode, 0, batch.stderr)
        request = json.loads(batch.stdout)
        self.assertEqual(len(request["requests"]), 2)
        self.assertEqual(request["counts"]["included"], 2 + len(request["extraction_gaps"]))
        self.assertGreater(request["counts"]["remaining_agent"], 0)
        self.assertEqual(request["counts"]["human_pending"], 0)
        decisions = [
            self.decision(document, "people/email", "yes", ["user.contact.email"], "natural_person",
                          "createPerson persists contact.email from PersonalContact alongside firstName and lastName for an individual contact; the value is the contact address itself."),
            self.decision(document, "people/first_name", "yes", ["user.name.first"], "natural_person",
                          "createPerson copies the individual contact firstName directly from PersonalContact into first_name."),
            self.decision(document, "ambiguous_values/email", "no", [], "none",
                          "storeBuildManifest writes a constant false to this boolean flag; it is neither an email address nor a person identifier.", "operational"),
            self.decision(document, "ambiguous_values/fingerprint", "no", [], "none",
                          "storeBuildManifest computes a SHA-256 digest of fileContents for a build artifact; no biological measurement or person source enters this writer.", "operational"),
        ]
        decisions_path = self.base / "decisions.json"
        decisions_path.write_text(json.dumps(decisions))
        applied = command("python3", SCRIPTS / "investigate.py", f"--repo={repo}", f"--decisions={decisions_path}", "--limit=2")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        result = analysis_storage.load(repo / ".noru/.cache/privacy-datamap.analysis.json")
        self.assertFalse(self.gate(result, derived, repo, complete=False))
        self.assertTrue(self.gate(result, derived, repo), "The rest of the application still requires investigation")
        for decision in decisions:
            row = next(row for row in self.rows(result) if row["entity_id"] == decision["entity_id"])
            self.assertEqual(row["status"], "proposed")
            self.assertEqual(row["personal_data"], decision["personal_data"])
            self.assertEqual(row["categories"], decision["categories"])
        report = metrics(self.rows(result), expected)
        EVALUATIONS["relational-app:investigated"] = report
        self.assertEqual(report["personal_precision"]["correct"], 2)
        self.assertEqual(report["personal_precision"]["predicted_personal"], 2)
        self.assertEqual(report["false_exclusions"]["count"], 0)
        self.assertEqual(report["category_accuracy"]["correct"], 2)
        after_files = {str(path.relative_to(repo)) for path in (repo / ".noru").rglob("*") if path.is_file()}
        self.assertEqual(after_files, before_files, "Investigation uses the existing canonical artifact layout")
        self.assertFalse((repo / ".noru/privacy-datamap.lock.json").exists())
        self.assertFalse((repo / ".fides/datamap.yml").exists())

    def test_complete_field_investigation_preserves_unbound_migration_blockers(self):
        reviewed = json.loads((FIXTURES / "reviewed-decisions.json").read_text())["fixtures"]
        for fixture in ("relational-app", "sql-app"):
            with self.subTest(fixture=fixture):
                repo, expected = self.repo(fixture, fixture)
                document, derived = self.scan(repo)
                rows = {field_key(row): row for row in self.engine.all_rows(self.rows(document))}
                decisions = []
                for key, authored in reviewed[fixture]["fields"].items():
                    self.assertIn(key, rows, "Reviewed source contract must match observed members")
                    row = rows[key]
                    spans = [self.engine.span_from_ref(repo, ref) for ref in authored["refs"]]
                    self.assertTrue(all(spans), key)
                    evidence = sorted(set(row["evidence_ids"]) | {"evidence:" + self.engine.digest(span) for span in spans})
                    decision = self.engine.decision_template(row)
                    reason = authored["meaning"] + " Counterevidence: " + authored["counterevidence"]
                    decision.update(personal_data=authored["personal_data"], categories=authored["categories"],
                                    meaning=authored["meaning"], rationale=reason,
                                    identification_basis=authored["basis"],
                                    person_association={"kind": authored["association"], "target": key.split("/")[0],
                                                        "rationale": reason, "evidence_ids": evidence},
                                    category_fit=[{"category": category, "rationale": reason, "evidence_ids": evidence}
                                                  for category in authored["categories"]],
                                    evidence_ids=evidence, additional_evidence=spans,
                                    investigation=[{"question": "Do all observed writers, identity links and alternatives support this meaning?",
                                                    "checked_fact_ids": row["supporting_fact_ids"], "outcome": reason}],
                                    uncertainty=authored.get("uncertainty"), resolution_owner=authored.get("resolution_owner"), confidence="high")
                    for field in ("business_context_question", "resolution_needed", "decision_impact", "decision_summary"):
                        if field in authored:
                            decision[field] = authored[field]
                    decision["semantic_review"] = semantic_review(self.engine, evidence, reason)
                    decisions.append(decision)
                decision_path = self.base / (fixture + "-decisions.json")
                decision_path.write_text(json.dumps(decisions))
                applied = command("python3", SCRIPTS / "investigate.py", f"--repo={repo}", f"--decisions={decision_path}", "--limit=200")
                self.assertEqual(applied.returncode, 0, applied.stderr)
                document = analysis_storage.load(repo / ".noru/.cache/privacy-datamap.analysis.json")
                gap_decisions = []
                for authored in reviewed[fixture]["gaps"]:
                    matching = [gap for gap in document["classification_review"]["gaps"]
                                if gap.get("kind") == authored["kind"] and gap.get("subject", "").endswith(authored["subject_suffix"])]
                    self.assertEqual(len(matching), 1, matching)
                    gap = matching[0]
                    spans = [self.engine.span_from_ref(repo, ref) for ref in authored["refs"]]
                    evidence = ["evidence:" + self.engine.digest(span) for span in spans]
                    gap_decisions.append({"gap_id": gap["gap_id"], "outcome": authored["outcome"], "rationale": authored["rationale"],
                                          "investigation": [{"question": "What does the unsupported writer store?", "outcome": authored["rationale"]}],
                                          "field_ids": [rows[key]["entity_id"] for key in authored["field_keys"]],
                                          "evidence_ids": evidence, "additional_evidence": spans,
                                          **({"scope_rationale": authored["scope_rationale"]} if "scope_rationale" in authored else {})})
                if gap_decisions:
                    gap_path = self.base / (fixture + "-gaps.json")
                    gap_path.write_text(json.dumps(gap_decisions))
                    applied = command("python3", SCRIPTS / "investigate.py", f"--repo={repo}", f"--gaps={gap_path}", "--limit=200")
                    self.assertEqual(applied.returncode, 0, applied.stderr)
                    document = analysis_storage.load(repo / ".noru/.cache/privacy-datamap.analysis.json")
                if fixture == "relational-app":
                    reviewed_rows = {field_key(row): row for row in self.engine.all_rows(self.rows(document))}
                    for key in ("people/contact.primary", "people/open_contact.contact", "ambiguous_values/payload"):
                        self.assertEqual(reviewed_rows[key]["status"], "needs_investigation", key)
                        self.assertEqual(reviewed_rows[key]["resolution_owner"], "human", key)
                        self.assertTrue(reviewed_rows[key]["open_content"], key)
                    for key in ("people/contact.primary.email", "people/open_contact.contact.email"):
                        self.assertEqual(reviewed_rows[key]["status"], "proposed", key)
                        self.assertEqual(reviewed_rows[key]["categories"], ["user.contact.email"], key)
                report = metrics(self.rows(document), expected)
                EVALUATIONS[fixture + ":complete_investigation"] = report
                self.assertEqual(report["resolution_coverage"]["resolved"], report["resolution_coverage"]["gold_resolvable"])
                self.assertEqual(report["false_exclusions"]["count"], 0)
                self.assertEqual(report["personal_precision"]["correct"], report["personal_precision"]["predicted_personal"])
                self.assertEqual(report["category_accuracy"]["correct"], report["category_accuracy"]["predicted_personal"])
                self.assertEqual(report["unresolved_gold_personal"], 0)
                current = {field_key(row): row for row in self.rows(document)}
                for key, gold in expected["fields"].items():
                    if gold.get("category_fit") == "unresolved":
                        self.assertEqual(current[key]["personal_data"], gold["personal_data"], key)
                        self.assertEqual(current[key]["status"], "needs_investigation", key)
                        self.assertEqual(current[key]["categories"], [], key)
                # These fixtures deliberately contain unbound historical migrations. Completing
                # field meaning must not dismiss their separate current-schema coverage question.
                self.assertEqual(self.engine.validate_contract(document, derived, repo, require_complete="agent"),
                    ["1 evidence extraction gaps remain; resolve them through plugin investigation."])
                partition = self.engine.gap_partition(document, derived, repo)
                self.assertEqual([gap.get("coverage_key") for gap in partition["active_blockers"]], ["migration_gaps"])
                self.assertTrue(self.gate(document, derived, repo), "Unavailable business facts still prevent acceptance")
                batch = command("python3", SCRIPTS / "investigate.py", f"--repo={repo}", "--limit=200")
                self.assertEqual(batch.returncode, 0, batch.stderr)
                counts = json.loads(batch.stdout)["counts"]
                self.assertEqual(counts["agent_pending_fields"], 0)
                self.assertEqual(counts["agent_pending_gaps"], 1)
                self.assertEqual(counts["agent_pending"], 1)
                self.assertEqual(len(json.loads(batch.stdout)["extraction_gaps"]), 1)
                self.assertGreater(counts["human_pending"], 0)
                self.assertFalse((repo / ".fides/datamap.yml").exists())

    def test_semantic_contradictions_reject_the_whole_decision_batch(self):
        repo, _ = self.repo("relational-app")
        document, _ = self.scan(repo)
        cases = [
            ("ambiguous_values/email", ["user.unique_id"], "natural_person", "boolean"),
            ("ambiguous_values/email_digest", ["user.contact.email"], "natural_person", "hash"),
            ("assignments/company_owner", ["user.unique_id"], "organization", "association"),
            ("integrations/service_credential", ["user.authorization.credentials"], "service", "association"),
        ]
        valid = self.decision(document, "people/email", "yes", ["user.contact.email"], "natural_person",
                              "The createPerson writer copies the individual's PersonalContact email address directly.")
        for key, categories, association, reason in cases:
            with self.subTest(field=key):
                changed = copy.deepcopy(document)
                before = copy.deepcopy(changed)
                invalid = self.decision(document, key, "yes", categories, association,
                                        "This deliberately contradictory proposal must not become a resolved classification.")
                errors = self.engine.apply_decisions(changed, [valid, invalid], repo)
                self.assertTrue(any(reason in error.lower() for error in errors), errors)
                self.assertEqual(changed, before, "A rejected batch must not partially apply its valid rows")

    def test_reviewed_organization_target_cannot_be_recast_as_person_identity(self):
        repo, _ = self.repo("relational-app")
        document, _ = self.scan(repo)
        target = self.decision(document, "companies/id", "no", [], "organization",
                               "registerCompany creates this key for the registered legal entity, alongside registeredName and registryNumber.", "operational")
        link = self.decision(document, "assignments/company_owner", "yes", ["user.unique_id"], "natural_person",
                             "This intentionally incorrect claim recasts the referenced legal entity as a natural person.", "relational")
        for batch in ([target, link], [link, target]):
            changed = copy.deepcopy(document)
            errors = self.engine.apply_decisions(changed, batch, repo)
            self.assertTrue(any("foreign-key target" in error for error in errors), errors)
            self.assertEqual(changed, document, "Contradictory batches must fail regardless of decision order")

    def test_timestamp_with_or_without_writer_requires_investigation(self):
        repo, _ = self.repo("relational-app")
        document, _ = self.scan(repo)
        self.assertEqual(self.row(document, "people/created_at")["personal_data"], "unknown")
        writer = repo / "src/writers.ts"
        writer.write_text(writer.read_text().replace("accountEnabled: true,", "accountEnabled: true, createdAt: new Date(),"))
        changed, _ = self.scan(repo)
        self.assertEqual(self.row(changed, "people/created_at")["personal_data"], "unknown")
        self.assertEqual(self.row(changed, "people/created_at")["status"], "needs_investigation")

    def test_migrations_never_supply_classification_evidence(self):
        repo, _ = self.repo("sql-app")
        names = ["custom-migrations/001.sql", "db/changelog/history.sql", "drizzle/0002.sql",
                 "V002__old.sql", "alembic/versions/old.py", "schema.migration.ts"]
        for index, name in enumerate(names):
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".ts":
                path.write_text('import { pgTable, text } from "drizzle-orm/pg-core";\nexport const old = pgTable("historical_5", { email: text("email") });\n')
            else:
                path.write_text(f"CREATE TABLE historical_{index} (email TEXT);\n")
        self.assertEqual(command("git", "add", "-A", repo=repo).returncode, 0)
        document, derived = self.scan(repo)
        self.assertFalse(any("historical_" in row["entity_id"] for row in self.rows(document)))
        for name in names:
            span = {"path": name, "start_line": 1, "end_line": 1, "start_column": 1, "end_column": 2,
                    "sha256": hashlib.sha256((repo / name).read_bytes()).hexdigest(), "extractor": "independent-fixture@1"}
            self.assertTrue(self.engine.evidence_errors(span, repo), name)
        self.assertFalse(self.gate(document, derived, repo, complete=False))

    def test_whole_app_inventory_and_honest_metrics(self):
        for fixture in ("relational-app", "sql-app"):
            with self.subTest(fixture=fixture):
                repo, expected = self.repo(fixture, fixture)
                document, derived = self.scan(repo)
                rows = self.rows(document)
                report = metrics(rows, expected)
                EVALUATIONS[fixture + ":static"] = report
                self.assertEqual(report["inventory"]["missing"], [])
                self.assertEqual(report["inventory"]["unexpected"], [])
                self.assertEqual(report["false_exclusions"]["count"], 0)
                for row in rows:
                    gold = expected["fields"][field_key(row)]
                    self.assertFalse(set(row.get("categories", [])) & set(gold.get("forbidden_categories", [])), field_key(row))
                    if row.get("status") == "proposed" and row.get("personal_data") in {"yes", "no"}:
                        self.assertEqual(row["personal_data"], gold["personal_data"], field_key(row))
                        self.assertEqual(sorted(row["categories"]), sorted(gold["categories"]), field_key(row))
                self.assertTrue(self.gate(document, derived, repo), "An incomplete static pass must block acceptance")
                self.assertFalse((repo / ".noru/.cache/privacy-field-review.json").exists())
                self.assertFalse((repo / ".noru/.cache/privacy-field-review.md").exists())
                self.assertFalse((repo / ".fides/datamap.yml").exists())
                for collection in expected["excluded_collections"]:
                    self.assertFalse(any(field_key(row).startswith(collection + "/") for row in rows))

    def test_nested_members_and_open_branches_remain_reviewable(self):
        repo, expected = self.repo("relational-app")
        document, derived = self.scan(repo)
        for parent_key, required in expected["nested_members"].items():
            parent = self.row(document, parent_key)
            observed = {field_key(child).removeprefix(parent_key + ".") for child in descendants(parent)}
            self.assertTrue(set(required) <= observed, f"{parent_key}: missing {set(required) - observed}")
            self.assertEqual(parent.get("categories"), [])
        parent = self.row(document, "people/open_contact")
        changed = copy.deepcopy(document)
        changed_parent = self.row(changed, "people/open_contact")
        parent_decision = self.decision(changed, "people/open_contact", "yes", [], "natural_person",
                                        "OpenEnvelope contains a PersonalContact member; its extension still needs separate investigation.", "content")
        errors = self.engine.apply_decisions(changed, [parent_decision], repo)
        self.assertTrue(any("unresolved members" in error for error in errors), errors)
        self.assertEqual(changed, document, "Rejecting the parent leaves the canonical review intact")
        changed_parent = self.row(changed, "people/open_contact")
        self.assertTrue(any("fields/members" in error for error in self.gate(changed, derived, repo)),
                        "Unresolved descendants remain a complete-review blocker")
        changed_parent["nested_members"] = []
        self.assertTrue(self.gate(changed, derived, repo), "Removing discovered leaves cannot complete a review")
        self.assertTrue(list(descendants(parent)))

    def test_no_prior_labels_or_exact_name_bypass(self):
        repo, _ = self.repo("sql-app")
        document, derived = self.scan(repo)
        first = copy.deepcopy(self.rows(document))
        changed = copy.deepcopy(document)
        for proposal in changed.get("proposals", []):
            proposal.update(kind="personal", data_categories=["user.contact.email"], rationale="Previous unrelated label")
        for dataset in derived["datasets"]:
            for collection in dataset["collections"]:
                for field in collection["fields"]:
                    field["data_categories"] = ["user.contact.email"]
                    field["needs_review"] = False
        self.engine.build_contract(changed, repo, fresh=True, derived=derived)
        previous = {field_key(row): (row["personal_data"], row["categories"]) for row in first}
        current = {field_key(row): (row["personal_data"], row["categories"]) for row in self.rows(changed)}
        self.assertEqual(current, previous)
        for key in ("build_artifacts/fingerprint", "build_artifacts/race", "build_artifacts/email"):
            self.assertEqual(self.row(changed, key)["categories"], [])

    def test_source_and_discovery_mutations_invalidate_review(self):
        repo, _ = self.repo("relational-app")
        document, derived = self.scan(repo)
        self.assertFalse(self.gate(document, derived, repo, complete=False))
        type_path = repo / "src/contracts.ts"
        before = type_path.read_text()
        type_path.write_text(before.replace("checksum: string;", "checksum: string;\n  optionalContact?: PersonalContact;"))
        self.assertTrue(self.gate(document, derived, repo, complete=False), "An imported type is part of freshness")
        type_path.write_text(before)
        self.assertFalse(self.gate(document, derived, repo, complete=False))
        extra_writer = repo / "src/alternative-writer.ts"
        extra_writer.write_text('import { people } from "./schema";\ndeclare const db: { update(table: unknown): { set(row: unknown): unknown } };\nexport function alternate(raw: unknown) { return db.update(people).set({ email: raw }); }\n')
        self.assertEqual(command("git", "add", "src/alternative-writer.ts", repo=repo).returncode, 0)
        self.assertTrue(self.gate(document, derived, repo, complete=False), "New alternative writers must invalidate discovery")

    def test_metamorphic_source_moves_aliases_and_reordering(self):
        repo, _ = self.repo("relational-app", "original")
        document, _ = self.scan(repo)
        other, _ = self.repo("relational-app", "transformed")
        contract = other / "src/contracts.ts"
        (other / "src/types").mkdir()
        contract.rename(other / "src/types/contact-shapes.ts")
        for name in ("schema.ts", "writers.ts"):
            path = other / "src" / name
            path.write_text(path.read_text().replace('"./contracts"', '"./types/contact-shapes"'))
        schema = other / "src/schema.ts"
        source = schema.read_text().replace("pgTable as defineTable", "pgTable as tableBuilder").replace("defineTable(", "tableBuilder(")
        source = source.replace('  email: text("email").notNull(),\n  firstName: text("first_name").notNull(),',
                                '  firstName: text("first_name").notNull(),\n\n  deliveryAddress: text("email").notNull(),')
        writer = other / "src/writers.ts"
        writer.write_text(writer.read_text().replace("id: randomId(), email: contact.email,", "id: randomId(), deliveryAddress: contact.email,"))
        schema.write_text(source)
        self.assertEqual(command("git", "add", "-A", repo=other).returncode, 0)
        transformed, _ = self.scan(other)
        def semantic_shape(doc):
            return {field_key(row): (row["personal_data"], sorted(row["categories"]),
                    sorted(field_key(child) for child in descendants(row))) for row in self.rows(doc)}
        self.assertEqual(semantic_shape(document), semantic_shape(transformed))

    def test_missing_or_incomplete_contract_blocks_public_export(self):
        repo, _ = self.repo("sql-app")
        document, derived = self.scan(repo)
        document.pop("classification_review")
        self.assertTrue(self.gate(document, derived, repo))
        analysis_storage.save(repo / ".noru/.cache/privacy-datamap.analysis.json", document)
        result = command("node", SCRIPTS / "collect.mjs", f"--repo={repo}", "--export", "--quiet")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((repo / ".fides/datamap.yml").exists())


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ClassificationEvaluation)
    outcome = unittest.TextTestRunner(verbosity=2).run(suite)
    print(json.dumps({"classification_evaluation": EVALUATIONS}, indent=2, sort_keys=True))
    raise SystemExit(0 if outcome.wasSuccessful() else 1)
