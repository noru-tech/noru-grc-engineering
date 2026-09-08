// Projecting the manifest down to privacy-relevant Fideslang.
//
// One implementation, used by both the renderer in collect.mjs and the plan builder in diff.mjs, so
// that `.fides/datamap.yml` and the payload `ingestDatamap` receives are the same content by
// construction. If they were built separately they would drift, and the failure would be silent and
// horrible: a data map handed to an auditor that says something different from the one in Noru.

/**
 * Keys this piece adds for human review, stripped before anything leaves the repository.
 *
 * `refs` says which line produced a claim, `interpretation` says who stands behind it,
 * `needs_review` marks what nobody has resolved, `non_personal_fields` compactly records reviewed
 * structural exclusions, and `structure_digest` pins the shape a signature was given for. These
 * exist for review and drift detection. None is Fideslang.
 *
 * Adding a review field to the manifest means adding it here. The idempotency test does not check
 * this list — it checks the payload against the keys Fideslang actually defines — so forgetting is
 * caught by what the wire looks like rather than by remembering to update two places.
 */
export const BOOKKEEPING = new Set([
  "refs",
  "interpretation",
  "needs_review",
  "non_personal_fields",
  "structure_digest",
]);

/**
 * Fides puts identity first: what a thing is called, then what it is, then what it holds. The
 * parsed manifest arrives with its keys sorted alphabetically, which is fine for a hash and poor
 * for a file somebody reads — `collections` above `fides_key` buries the name of the thing being
 * described. Anything unlisted keeps its position after the listed keys, so a field added upstream
 * is never silently dropped.
 */
const KEY_ORDER = [
  "fides_key", "name", "description", "system_type", "dataset_references",
  "data_categories", "data_use", "data_subjects",
  "collections", "fields", "privacy_declarations",
];

function ordered(entries) {
  const rank = (key) => {
    const at = KEY_ORDER.indexOf(key);
    return at === -1 ? KEY_ORDER.length : at;
  };
  return entries.slice().sort((a, b) => rank(a[0]) - rank(b[0]));
}

export function toFideslang(manifest) {
  const assertResolved = (node) => {
    if (Array.isArray(node)) {
      for (const item of node) assertResolved(item);
    } else if (node && typeof node === "object") {
      if (node.needs_review === true) {
        throw new Error("cannot project a privacy data map while needs_review is true");
      }
      for (const value of Object.values(node)) assertResolved(value);
    }
  };
  const strip = (node) => {
    if (Array.isArray(node)) return node.map(strip);
    if (node && typeof node === "object") {
      return Object.fromEntries(
        ordered(Object.entries(node).filter(([key]) => !BOOKKEEPING.has(key))).map(
          ([key, value]) => [key, strip(value)],
        ),
      );
    }
    return node;
  };
  const projectField = (field) => {
    const children = (field.fields ?? []).map(projectField).filter(Boolean);
    const privacyRelevant = (field.data_categories ?? []).length > 0;
    if (!privacyRelevant && children.length === 0) return null;
    const projected = strip(field);
    if (children.length > 0) projected.fields = children;
    else delete projected.fields;
    return projected;
  };

  assertResolved(manifest);
  const datasets = (manifest.dataset ?? []).flatMap((dataset) => {
    const collections = (dataset.collections ?? []).flatMap((collection) => {
      const fields = (collection.fields ?? []).map(projectField).filter(Boolean);
      return fields.length > 0 ? [{ ...strip(collection), fields }] : [];
    });
    return collections.length > 0 ? [{ ...strip(dataset), collections }] : [];
  });
  const datasetKeys = new Set(datasets.map((dataset) => dataset.fides_key));
  const systems = (manifest.system ?? []).map((system) => ({
    ...strip(system),
    dataset_references: (system.dataset_references ?? []).filter((key) => datasetKeys.has(key)),
  }));
  return {
    dataset: datasets,
    system: systems,
  };
}
