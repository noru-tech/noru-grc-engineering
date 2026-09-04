#!/usr/bin/env node
import { main } from "./github-cli.mjs";
process.exit(main(["inspect", ...process.argv.slice(2)]));
