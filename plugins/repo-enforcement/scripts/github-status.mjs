#!/usr/bin/env node
import { main } from "./github-cli.mjs";
process.exit(main(["status", ...process.argv.slice(2)]));
