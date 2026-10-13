#!/usr/bin/env node
// Shared read-only source-scope query. Never read or execute migration contents.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { migrationPaths } from "./collect.mjs";

const files = JSON.parse(readFileSync(0, "utf8"));
if (!Array.isArray(files) || files.some((path) => typeof path !== "string")) {
  throw new Error("Expected a JSON array of repository-relative file paths");
}
process.stdout.write(JSON.stringify(migrationPaths(resolve(process.argv[2]), files)));
