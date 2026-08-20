import { describe, it, expect, beforeEach, afterEach } from "vitest";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { resolveInstallMode, dashboardSpawnArgs } from "../src/install-mode";

const IS_WIN = process.platform === "win32";
const PATH_SEP = IS_WIN ? ";" : ":";

function writeShim(dir: string, name: string): string {
  const p = path.join(dir, name);
  if (IS_WIN) {
    fs.writeFileSync(p, "@echo fake\r\n");
  } else {
    fs.writeFileSync(p, "#!/bin/sh\necho fake\n");
    fs.chmodSync(p, 0o755);
  }
  return p;
}

describe("resolveInstallMode", () => {
  let tmpDir: string;
  let cleanEnv: NodeJS.ProcessEnv;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "install-mode-"));
    cleanEnv = { ...process.env, PATH: tmpDir };
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it("returns brew mode when claude-usage is on PATH", () => {
    const shim = writeShim(tmpDir, IS_WIN ? "claude-usage.exe" : "claude-usage");
    const mode = resolveInstallMode({ configuredCliPath: "", env: cleanEnv });
    expect(mode).toEqual({ kind: "brew", binary: shim });
  });

  it("returns clone mode when configuredCliPath points at a cli.py file", () => {
    const cli = path.join(tmpDir, "cli.py");
    fs.writeFileSync(cli, "# placeholder\n");
    const mode = resolveInstallMode({ configuredCliPath: cli, env: cleanEnv });
    expect(mode).toEqual({ kind: "clone", cliPy: cli });
  });

  it("returns clone mode when configuredCliPath points at a clone directory", () => {
    const cli = path.join(tmpDir, "cli.py");
    fs.writeFileSync(cli, "# placeholder\n");
    const mode = resolveInstallMode({ configuredCliPath: tmpDir, env: cleanEnv });
    expect(mode).toEqual({ kind: "clone", cliPy: cli });
  });

  it("explicit setting wins even when brew is also on PATH", () => {
    writeShim(tmpDir, IS_WIN ? "claude-usage.exe" : "claude-usage");
    const otherDir = fs.mkdtempSync(path.join(os.tmpdir(), "install-mode-other-"));
    try {
      const cli = path.join(otherDir, "cli.py");
      fs.writeFileSync(cli, "# placeholder\n");
      const mode = resolveInstallMode({ configuredCliPath: cli, env: cleanEnv });
      expect(mode).toEqual({ kind: "clone", cliPy: cli });
    } finally {
      fs.rmSync(otherDir, { recursive: true, force: true });
    }
  });

  it("falls back to monorepo sibling cli.py when neither setting nor PATH find anything", () => {
    // Simulate this extension dir being inside a Python repo: <root>/vscode-extension/
    const repoRoot = fs.mkdtempSync(path.join(os.tmpdir(), "install-mode-mono-"));
    const extDir = path.join(repoRoot, "vscode-extension");
    fs.mkdirSync(extDir);
    const sibling = path.join(repoRoot, "cli.py");
    fs.writeFileSync(sibling, "# placeholder\n");
    try {
      const mode = resolveInstallMode({
        configuredCliPath: "",
        extensionDir: extDir,
        env: cleanEnv, // empty PATH, no brew
      });
      expect(mode).toEqual({ kind: "clone", cliPy: sibling });
    } finally {
      fs.rmSync(repoRoot, { recursive: true, force: true });
    }
  });

  it("discovers cli.py in a VS Code workspace folder (the Windows clone case)", () => {
    const ws = fs.mkdtempSync(path.join(os.tmpdir(), "install-mode-ws-"));
    const cli = path.join(ws, "cli.py");
    fs.writeFileSync(cli, "# placeholder\n");
    try {
      const mode = resolveInstallMode({
        configuredCliPath: "",
        workspaceFolders: [ws],
        env: cleanEnv,
      });
      expect(mode).toEqual({ kind: "clone", cliPy: cli });
    } finally {
      fs.rmSync(ws, { recursive: true, force: true });
    }
  });

  it("scans multiple workspace folders, returns first match", () => {
    const ws1 = fs.mkdtempSync(path.join(os.tmpdir(), "install-mode-ws1-"));
    const ws2 = fs.mkdtempSync(path.join(os.tmpdir(), "install-mode-ws2-"));
    const cli2 = path.join(ws2, "cli.py");
    fs.writeFileSync(cli2, "# placeholder\n");
    try {
      // ws1 first, has no cli.py — ws2 should win.
      const mode = resolveInstallMode({
        configuredCliPath: "",
        workspaceFolders: [ws1, ws2],
        env: cleanEnv,
      });
      expect(mode).toEqual({ kind: "clone", cliPy: cli2 });
    } finally {
      fs.rmSync(ws1, { recursive: true, force: true });
      fs.rmSync(ws2, { recursive: true, force: true });
    }
  });

  it("brew on PATH wins over a workspace folder cli.py", () => {
    const shim = writeShim(tmpDir, IS_WIN ? "claude-usage.exe" : "claude-usage");
    const ws = fs.mkdtempSync(path.join(os.tmpdir(), "install-mode-ws-"));
    const cli = path.join(ws, "cli.py");
    fs.writeFileSync(cli, "# placeholder\n");
    try {
      const mode = resolveInstallMode({
        configuredCliPath: "",
        workspaceFolders: [ws],
        env: cleanEnv,
      });
      expect(mode).toEqual({ kind: "brew", binary: shim });
    } finally {
      fs.rmSync(ws, { recursive: true, force: true });
    }
  });

  it("returns none when nothing is found anywhere", () => {
    const mode = resolveInstallMode({ configuredCliPath: "", env: cleanEnv });
    expect(mode).toEqual({ kind: "none" });
  });

  it("ignores configuredCliPath that doesn't exist", () => {
    const mode = resolveInstallMode({
      configuredCliPath: path.join(tmpDir, "no-such-cli.py"),
      env: cleanEnv,
    });
    expect(mode).toEqual({ kind: "none" });
  });

  it("returns bundled clone mode when bundledCliPath exists (marketplace install)", () => {
    const bundled = path.join(tmpDir, "cli.py");
    fs.writeFileSync(bundled, "# bundled placeholder\n");
    const mode = resolveInstallMode({
      configuredCliPath: "",
      bundledCliPath: bundled,
      env: cleanEnv,
    });
    expect(mode).toEqual({ kind: "clone", cliPy: bundled });
  });

  it("bundled cli.py beats brew on PATH (extension ships its own — predictable version)", () => {
    const shim = writeShim(tmpDir, IS_WIN ? "claude-usage.exe" : "claude-usage");
    const bundled = path.join(tmpDir, "bundled-cli.py");
    fs.writeFileSync(bundled, "# bundled\n");
    const mode = resolveInstallMode({
      configuredCliPath: "",
      bundledCliPath: bundled,
      env: cleanEnv,
    });
    expect(mode).toEqual({ kind: "clone", cliPy: bundled });
    // shim exists but bundled won — confirm via path mismatch
    expect((mode as { kind: "clone"; cliPy: string }).cliPy).not.toBe(shim);
  });

  it("explicit setting still wins over bundled (user override)", () => {
    const bundled = path.join(tmpDir, "bundled-cli.py");
    fs.writeFileSync(bundled, "# bundled\n");
    const configured = path.join(tmpDir, "user-cli.py");
    fs.writeFileSync(configured, "# user override\n");
    const mode = resolveInstallMode({
      configuredCliPath: configured,
      bundledCliPath: bundled,
      env: cleanEnv,
    });
    expect(mode).toEqual({ kind: "clone", cliPy: configured });
  });

  it("ignores bundledCliPath that doesn't exist (tests / pre-package)", () => {
    const mode = resolveInstallMode({
      configuredCliPath: "",
      bundledCliPath: path.join(tmpDir, "not-here", "cli.py"),
      env: cleanEnv,
    });
    expect(mode).toEqual({ kind: "none" });
  });
});

describe("dashboardSpawnArgs", () => {
  it("brew mode emits the bin directly with subcommand", () => {
    const mode = { kind: "brew" as const, binary: "/usr/local/bin/claude-usage" };
    expect(dashboardSpawnArgs(mode, undefined, ["--host", "127.0.0.1", "--port", "9000"]))
      .toEqual({ command: "/usr/local/bin/claude-usage", args: ["dashboard", "--host", "127.0.0.1", "--port", "9000"] });
  });

  it("clone mode emits python + cli.py + subcommand", () => {
    const mode = { kind: "clone" as const, cliPy: "/repo/cli.py" };
    expect(dashboardSpawnArgs(mode, "/usr/bin/python3", ["--host", "127.0.0.1"]))
      .toEqual({ command: "/usr/bin/python3", args: ["/repo/cli.py", "dashboard", "--host", "127.0.0.1"] });
  });

  it("clone mode returns undefined when no python is available", () => {
    const mode = { kind: "clone" as const, cliPy: "/repo/cli.py" };
    expect(dashboardSpawnArgs(mode, undefined, [])).toBeUndefined();
  });

  it("none mode returns undefined regardless of python", () => {
    expect(dashboardSpawnArgs({ kind: "none" }, "/usr/bin/python3", [])).toBeUndefined();
    expect(dashboardSpawnArgs({ kind: "none" }, undefined, [])).toBeUndefined();
  });
});
