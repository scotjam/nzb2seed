// node run.mjs <classic|new> <scenario> <web folder> <settings.json>
// Prints what the scenario saw, as JSON. One process per run, so the two versions of the
// page never share anything.
import { readFileSync } from "node:fs";
import { load } from "./dom.mjs";
import { SCENARIOS } from "./scenarios.mjs";

const [ui, name, web, settingsFile] = process.argv.slice(2);
const settings = JSON.parse(readFileSync(settingsFile, "utf8"));
const page = await load(ui, web, "build", settings);
let seen, failure = null;
try { seen = await SCENARIOS[name](page); }
catch (err) { failure = String(err && err.stack || err); }
process.stdout.write(JSON.stringify({ seen, confirms: page.confirms, errors: page.errors, failure }));
process.exit(0);
