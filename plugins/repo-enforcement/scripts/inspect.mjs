#!/usr/bin/env node
import { main } from "./configure.mjs";
process.exit(main(["inspect", ...process.argv.slice(2)]));
