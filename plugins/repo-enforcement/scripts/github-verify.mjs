#!/usr/bin/env node
import { main } from "./github-cli.mjs";
process.exit(main(["verify", ...process.argv.slice(2)]));
