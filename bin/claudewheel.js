#!/usr/bin/env node
"use strict";

const { execFileSync, spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

// Resolve package root (one level up from bin/)
const pkgRoot = path.resolve(__dirname, "..");

// Read minimum Python version from pyproject.toml
let reqMajor = 3;
let reqMinor = 10;
try {
  const toml = fs.readFileSync(path.join(pkgRoot, "pyproject.toml"), "utf8");
  const reqMatch = toml.match(/requires-python\s*=\s*">=(\d+)\.(\d+)"/);
  if (reqMatch) {
    reqMajor = Number(reqMatch[1]);
    reqMinor = Number(reqMatch[2]);
  } else {
    console.warn(`Warning: could not parse requires-python from pyproject.toml, defaulting to ${reqMajor}.${reqMinor}`);
  }
} catch {
  console.warn(`Warning: could not read pyproject.toml, defaulting to Python ${reqMajor}.${reqMinor}`);
}

const reqVersion = `${reqMajor}.${reqMinor}`;

// Check python3 availability and version
let pyVersion;
try {
  pyVersion = execFileSync("python3", ["--version"], { encoding: "utf8" }).trim();
} catch {
  console.error(`claudewheel requires Python ${reqVersion}+, but python3 was not found in PATH.`);
  process.exit(1);
}

// Parse version: "Python 3.14.3" -> [3, 14]
const match = pyVersion.match(/Python (\d+)\.(\d+)/);
if (!match || Number(match[1]) < reqMajor || (Number(match[1]) === reqMajor && Number(match[2]) < reqMinor)) {
  console.error(`claudewheel requires Python ${reqVersion}+. Found: ${pyVersion}`);
  process.exit(1);
}

// Launch the Python TUI with PYTHONPATH set to the package root
const result = spawnSync("python3", ["-m", "claudewheel", ...process.argv.slice(2)], {
  stdio: "inherit",
  env: { ...process.env, PYTHONPATH: pkgRoot },
});

process.exit(result.status ?? 1);
