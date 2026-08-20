import { existsSync } from "node:fs";
import * as path from "node:path";
import { findOnPath } from "./python-locator";

const IS_WIN = process.platform === "win32";

/**
 * How we invoke the dashboard.
 *
 * - `brew`: the user has `claude-usage` on PATH (typically from the Homebrew
 *   formula). We run `claude-usage dashboard ...` directly; no Python lookup
 *   needed because the brew shim already wraps Python.
 *
 * - `clone`: we have a path to a `cli.py` from a git clone. We need a Python
 *   interpreter to run it: `python3 /path/to/cli.py dashboard ...`.
 *
 * - `none`: we couldn't find either.
 */
export type InstallMode =
  | { kind: "brew"; binary: string }
  | { kind: "clone"; cliPy: string; pythonHint?: string }
  | { kind: "none" };

interface ResolveOptions {
  /** Value of the `claudeUsage.cliPath` setting (empty string if unset). */
  configuredCliPath: string;
  /** Path to the bundled `python/cli.py` shipped inside the .vsix.
   *  Always present in a packaged extension; absent only in tests. This is
   *  the default-and-most-reliable mode for marketplace users — they only
   *  need Python on PATH, no separate claude-usage install. */
  bundledCliPath?: string;
  /** Absolute path to this extension's directory. Used to walk up to the repo root for the dev case. */
  extensionDir?: string;
  /** VS Code workspace folder paths. Used as a discovery hint: if the user has
   *  the claude-usage repo open as their workspace, we'll find cli.py there. */
  workspaceFolders?: ReadonlyArray<string>;
  /** Override env for tests. */
  env?: NodeJS.ProcessEnv;
}

function claudeUsageCandidateNames(): string[] {
  return IS_WIN ? ["claude-usage.exe", "claude-usage.cmd", "claude-usage"] : ["claude-usage"];
}

/**
 * Check whether a path looks like a usable `cli.py`.
 * Accepts either the file itself or a directory that contains it.
 */
function resolveCliPy(p: string): string | undefined {
  if (!p) return undefined;
  if (existsSync(p)) {
    const stat = require("node:fs").statSync(p);
    if (stat.isFile()) return p;
    if (stat.isDirectory()) {
      const candidate = path.join(p, "cli.py");
      if (existsSync(candidate)) return candidate;
    }
  }
  return undefined;
}

/**
 * Decide how we'll run the dashboard.
 *
 * Resolution order:
 * 1. `configuredCliPath` setting → `clone` (explicit user override always wins)
 * 2. **Bundled `python/cli.py`** shipped inside the .vsix → `clone`
 *    (this is the default for marketplace users — they only need Python on PATH)
 * 3. `claude-usage` on PATH → `brew` (Homebrew users; falls below bundled
 *    because brew may be stale or differ; explicit setting still overrides)
 * 4. Any VS Code workspace folder that contains a `cli.py` (legacy: the
 *    "open the cloned repo as workspace" path, still works)
 * 5. Monorepo sibling: extension dir is `<repo>/vscode-extension/` and the
 *    .py files live in `<repo>/` (dev case, F5 from source)
 * 6. `none`
 */
export function resolveInstallMode(opts: ResolveOptions): InstallMode {
  const env = opts.env ?? process.env;

  // 1. Explicit setting.
  const cli = resolveCliPy(opts.configuredCliPath);
  if (cli) return { kind: "clone", cliPy: cli };

  // 2. Bundled cli.py inside the packaged extension. Most reliable: ships
  //    with the extension version, no separate install needed.
  if (opts.bundledCliPath) {
    const bundled = resolveCliPy(opts.bundledCliPath);
    if (bundled) return { kind: "clone", cliPy: bundled };
  }

  // 3. Homebrew (or any) `claude-usage` shim on PATH.
  for (const name of claudeUsageCandidateNames()) {
    const found = findOnPath(name, env.PATH);
    if (found) return { kind: "brew", binary: found };
  }

  // 4. Workspace folders.
  for (const folder of opts.workspaceFolders ?? []) {
    const inWorkspace = resolveCliPy(folder);
    if (inWorkspace) return { kind: "clone", cliPy: inWorkspace };
  }

  // 5. Monorepo sibling (dev / F5 case).
  if (opts.extensionDir) {
    const sibling = resolveCliPy(path.dirname(opts.extensionDir));
    if (sibling) return { kind: "clone", cliPy: sibling };
  }

  return { kind: "none" };
}

/**
 * Build the spawn args for starting the dashboard, given the install mode +
 * a python interpreter path (used only in `clone` mode).
 *
 * Returned as { command, args } so callers can pass straight to spawn() with
 * no shell.
 */
export function dashboardSpawnArgs(
  mode: InstallMode,
  python: string | undefined,
  extraArgs: string[],
): { command: string; args: string[] } | undefined {
  if (mode.kind === "brew") {
    return { command: mode.binary, args: ["dashboard", ...extraArgs] };
  }
  if (mode.kind === "clone") {
    if (!python) return undefined;
    return { command: python, args: [mode.cliPy, "dashboard", ...extraArgs] };
  }
  return undefined;
}
