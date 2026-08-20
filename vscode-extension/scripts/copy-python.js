// Copies the Python sources from the repo root into vscode-extension/python/
// so they're bundled into the .vsix. Each release of the extension embeds the
// exact cli.py/scanner.py/dashboard.py snapshot from the commit it was
// packaged at, so end users get a self-contained install — their only
// dependency is Python 3.8+ on PATH.
//
// Run from vscode-extension/. Invoked automatically by `vscode:prepublish`.

const fs = require("node:fs");
const path = require("node:path");

const repoRoot = path.resolve(__dirname, "..", "..");
const targetDir = path.resolve(__dirname, "..", "python");
const files = ["cli.py", "scanner.py", "dashboard.py"];

fs.mkdirSync(targetDir, { recursive: true });

let missing = false;
for (const file of files) {
  const src = path.join(repoRoot, file);
  if (!fs.existsSync(src)) {
    console.error(`copy-python: ERROR - missing source ${src}`);
    missing = true;
    continue;
  }
  const dst = path.join(targetDir, file);
  fs.copyFileSync(src, dst);
  console.log(`copy-python: ${file} -> python/${file}`);
}

if (missing) {
  console.error("copy-python: aborting — run from the vscode-extension/ subdirectory of the claude-usage repo.");
  process.exit(1);
}
