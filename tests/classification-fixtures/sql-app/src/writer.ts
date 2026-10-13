declare function execute(query: string, values: unknown[]): void;
export function persistArtifact(digest: string, ordinal: number) {
  execute("INSERT INTO build_artifacts(id, fingerprint, race, email) VALUES ($1,$2,$3,$4)", ["release", digest, ordinal, false]);
}
