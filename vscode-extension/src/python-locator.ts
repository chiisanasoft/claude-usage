import { existsSync, statSync } from "node:fs";
import * as path from "node:path";

const IS_WIN = process.platform === "win32";

function pythonCandidateNames(): string[] {
  return IS_WIN ? ["python.exe", "python3.exe", "python"] : ["python3", "python"];
}

/**
 * Walk the PATH manually looking for a command, with platform-aware extensions.
 * No shell involvement — safe to pass any string without sanitisation, but we
 * only ever call this with hard-coded candidate names anyway.
 *
 * Exported for tests; the env var lookup makes it trivial to stub.
 */
export function findOnPath(commandName: string, envPath: string | undefined = process.env.PATH): string | undefined {
  if (!envPath) return undefined;
  const sep = IS_WIN ? ";" : ":";
  const dirs = envPath.split(sep).filter(Boolean);
  for (const dir of dirs) {
    const candidate = path.join(dir, commandName);
    try {
      if (existsSync(candidate) && statSync(candidate).isFile()) return candidate;
    } catch {
      // Permission errors etc — skip.
    }
  }
  return undefined;
}

/**
 * Locate a Python interpreter to run the Claude usage CLI.
 *
 * Resolution order:
 * 1. `configuredPath` — explicit override from settings; returned only if the file exists.
 * 2. First match from `python3`, `python` on PATH (Windows adds `.exe` variants).
 * 3. `undefined` if nothing usable was found.
 *
 * Note: this resolves the interpreter only. The dashboard CLI itself
 * (`claude-usage` brew shim, or a `cli.py` from a clone) is resolved by
 * `install-mode.ts`. When the brew shim is present we don't need a separate
 * Python at all — the shim is self-contained.
 */
export function locatePython(configuredPath: string): string | undefined {
  if (configuredPath) {
    return existsSync(configuredPath) ? configuredPath : undefined;
  }
  for (const name of pythonCandidateNames()) {
    const found = findOnPath(name);
    if (found) return found;
  }
  return undefined;
}

// Exposed for tests.
export const __testing = { pythonCandidateNames };
