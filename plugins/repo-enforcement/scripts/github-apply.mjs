#!/usr/bin/env node
import { main } from "./github-cli.mjs";
process.exit(main(["apply", ...process.argv.slice(2)]));
