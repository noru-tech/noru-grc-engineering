// Projecting the manifest down to plain Fideslang.
//
// One implementation, used by both the renderer in collect.mjs and the plan builder in diff.mjs, so
// that `.fides/datamap.yml` and the payload `ingestDatamap` receives are the same content by
// construction. If they were built separately they would drift, and the failure would be silent and
// horrible: a data map handed to an auditor that says something different from the one in Noru.

/**
 * Keys this piece adds for human review, stripped before anything leaves the repository.
 *
 * `refs` says which line produced a claim, `interpretation` says who stands behind it,
 * `needs_review` marks what nobody has resolved, and `structure_digest` pins the shape a signature
 * was given for. All four exist for the pull request. None is Fideslang, and a manifest carrying
 * them is one no other Fides tool can read.
 *
 * Adding a review field to the manifest means adding it here. The idempotency test does not check
 * this list — it checks the payload against the keys Fideslang actually defines — so forgetting is
 * caught by what the wire looks like rather than by remembering to update two places.
 */
export const BOOKKEEPING = new Set([
  "refs",
  "interpretation",
  "needs_review",
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
  return {
    dataset: strip(manifest.dataset ?? []),
    system: strip(manifest.system ?? []),
  };
}
