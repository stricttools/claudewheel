#!/usr/bin/env node
"use strict";

const { execFileSync, spawnSync } = require("child_process");
const path = require("path");

// Resolve package root (one level up from bin/)
const pkgRoot = path.resolve(__dirname, "..");

// Check python3 availability and version
let pyVersion;
try {
  pyVersion = execFileSync("python3", ["--version"], { encoding: "utf8" }).trim();
} catch {
  console.error("claudewheel requires Python 3.14+, but python3 was not found in PATH.");
  process.exit(1);
}

// Parse version: "Python 3.14.3" -> [3, 14]
const match = pyVersion.match(/Python (\d+)\.(\d+)/);
if (!match || Number(match[1]) < 3 || (Number(match[1]) === 3 && Number(match[2]) < 14)) {
  console.error(`claudewheel requires Python 3.14+. Found: ${pyVersion}`);
  process.exit(1);
}

// Launch the Python TUI with PYTHONPATH set to the package root
const result = spawnSync("python3", ["-m", "claudewheel", ...process.argv.slice(2)], {
  stdio: "inherit",
  env: { ...process.env, PYTHONPATH: pkgRoot },
});

process.exit(result.status ?? 1);
