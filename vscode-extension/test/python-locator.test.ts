import { describe, it, expect, beforeEach, afterEach } from "vitest";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { locatePython, findOnPath, __testing } from "../src/python-locator";

const IS_WIN = process.platform === "win32";
const PATH_SEP = IS_WIN ? ";" : ":";

describe("python-locator", () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "py-locate-"));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  function writeFakeBinary(dir: string, name: string): string {
    const p = path.join(dir, name);
    if (IS_WIN) {
      fs.writeFileSync(p, "@echo fake\r\n");
    } else {
      fs.writeFileSync(p, "#!/bin/sh\necho fake\n");
      fs.chmodSync(p, 0o755);
    }
    return p;
  }

  describe("locatePython", () => {
    it("returns the configured path when it exists", () => {
      const fake = writeFakeBinary(tmpDir, IS_WIN ? "python.exe" : "python3");
      expect(locatePython(fake)).toBe(fake);
    });

    it("returns undefined when configured path does not exist", () => {
      const missing = path.join(tmpDir, "no-such-python");
      expect(locatePython(missing)).toBeUndefined();
    });

    it("falls through to PATH discovery when configured path is empty", () => {
      // Drop a fake python3 / python.exe in tmpDir and put it first on PATH.
      const name = IS_WIN ? "python.exe" : "python3";
      const fake = writeFakeBinary(tmpDir, name);
      const orig = process.env.PATH;
      process.env.PATH = tmpDir + PATH_SEP + (orig ?? "");
      try {
        expect(locatePython("")).toBe(fake);
      } finally {
        process.env.PATH = orig;
      }
    });

    it("returns undefined when PATH has no python anywhere", () => {
      const orig = process.env.PATH;
      // Point PATH at an empty tmp dir so the candidate names can't resolve.
      process.env.PATH = tmpDir;
      try {
        expect(locatePython("")).toBeUndefined();
      } finally {
        process.env.PATH = orig;
      }
    });
  });

  describe("findOnPath", () => {
    it("returns first matching directory entry", () => {
      const a = path.join(tmpDir, "a");
      const b = path.join(tmpDir, "b");
      fs.mkdirSync(a);
      fs.mkdirSync(b);
      const wantedName = IS_WIN ? "thing.exe" : "thing";
      const inA = writeFakeBinary(a, wantedName);
      writeFakeBinary(b, wantedName);
      // a comes first → should win.
      expect(findOnPath(wantedName, a + PATH_SEP + b)).toBe(inA);
    });

    it("returns undefined when envPath is an empty string", () => {
      // Note: passing undefined here would default to process.env.PATH, which
      // is platform-dependent — that's the JS default-arg machinery, not our
      // function's concern. We test the falsy-guard with an empty string.
      expect(findOnPath("python3", "")).toBeUndefined();
    });

    it("skips directories that don't contain the name", () => {
      const wantedName = IS_WIN ? "needle.exe" : "needle";
      const dirWithIt = path.join(tmpDir, "yes");
      const dirWithoutIt = path.join(tmpDir, "no");
      fs.mkdirSync(dirWithIt);
      fs.mkdirSync(dirWithoutIt);
      const target = writeFakeBinary(dirWithIt, wantedName);
      expect(findOnPath(wantedName, dirWithoutIt + PATH_SEP + dirWithIt)).toBe(target);
    });
  });

  describe("platform candidate names", () => {
    it("on win32 prefers python.exe first", () => {
      if (!IS_WIN) return;
      const names = __testing.pythonCandidateNames();
      expect(names[0]).toBe("python.exe");
    });

    it("on unix prefers python3 first", () => {
      if (IS_WIN) return;
      const names = __testing.pythonCandidateNames();
      expect(names[0]).toBe("python3");
    });
  });
});
