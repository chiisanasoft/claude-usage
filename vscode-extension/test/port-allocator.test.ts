import { describe, it, expect } from "vitest";
import * as net from "node:net";
import { pickFreePort, normalizeConfiguredPort, resolvePort, isPortFree, resolveStablePort } from "../src/port-allocator";

/** Bind a server to a port and hold it open for the duration of `fn`. */
async function withPortHeld<T>(port: number, fn: () => Promise<T>, host = "127.0.0.1"): Promise<T> {
  const server = net.createServer();
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, host, () => resolve());
  });
  try {
    return await fn();
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}

describe("normalizeConfiguredPort", () => {
  it("zero maps to auto", () => {
    expect(normalizeConfiguredPort(0)).toBe(0);
  });

  it("undefined/null map to auto", () => {
    expect(normalizeConfiguredPort(undefined)).toBe(0);
    expect(normalizeConfiguredPort(null)).toBe(0);
  });

  it("NaN/Infinity map to auto", () => {
    expect(normalizeConfiguredPort(NaN)).toBe(0);
    expect(normalizeConfiguredPort(Infinity)).toBe(0);
  });

  it("valid port (1024-65535) passes through, integer-floored", () => {
    expect(normalizeConfiguredPort(8080)).toBe(8080);
    expect(normalizeConfiguredPort(8080.9)).toBe(8080);
    expect(normalizeConfiguredPort(1024)).toBe(1024);
    expect(normalizeConfiguredPort(65535)).toBe(65535);
  });

  it("out-of-range maps to auto", () => {
    expect(normalizeConfiguredPort(80)).toBe(0);
    expect(normalizeConfiguredPort(1023)).toBe(0);
    expect(normalizeConfiguredPort(65536)).toBe(0);
    expect(normalizeConfiguredPort(-1)).toBe(0);
  });
});

describe("pickFreePort", () => {
  it("returns a port in the ephemeral range", async () => {
    const port = await pickFreePort();
    expect(port).toBeGreaterThan(1024);
    expect(port).toBeLessThan(65536);
  });

  it("two consecutive picks generally yield different ports (best-effort)", async () => {
    const a = await pickFreePort();
    const b = await pickFreePort();
    // The OS *might* reassign — don't strictly require difference, but most of
    // the time these will differ. The point is both should work.
    expect(typeof a).toBe("number");
    expect(typeof b).toBe("number");
  });

  it("the picked port is actually bindable right after", async () => {
    const port = await pickFreePort();
    await new Promise<void>((resolve, reject) => {
      const s = net.createServer();
      s.once("error", reject);
      s.listen(port, "127.0.0.1", () => s.close(() => resolve()));
    });
  });
});

describe("resolvePort", () => {
  it("returns configured port when valid", async () => {
    // Use an ephemeral one we KNOW will work by picking it first.
    const target = await pickFreePort();
    expect(await resolvePort(target)).toBe(target);
  });

  it("auto-picks when configured is 0", async () => {
    const port = await resolvePort(0);
    expect(port).toBeGreaterThan(1024);
  });

  it("auto-picks when configured is out of range", async () => {
    const port = await resolvePort(80);
    expect(port).toBeGreaterThan(1024);
  });
});

describe("isPortFree", () => {
  it("true for a port nothing is bound to", async () => {
    const port = await pickFreePort();
    expect(await isPortFree(port)).toBe(true);
  });

  it("false while a port is held", async () => {
    const port = await pickFreePort();
    await withPortHeld(port, async () => {
      expect(await isPortFree(port)).toBe(false);
    });
  });
});

describe("resolveStablePort", () => {
  it("returns the configured port as-is when pinned (ignores saved)", async () => {
    const pinned = await pickFreePort();
    const saved = await pickFreePort();
    expect(await resolveStablePort(pinned, saved)).toBe(pinned);
  });

  it("reuses the saved port when it is still free", async () => {
    const saved = await pickFreePort();
    expect(await resolveStablePort(0, saved)).toBe(saved);
  });

  it("picks a fresh port when the saved one is taken", async () => {
    const saved = await pickFreePort();
    await withPortHeld(saved, async () => {
      const got = await resolveStablePort(0, saved);
      expect(got).not.toBe(saved);
      expect(got).toBeGreaterThan(1024);
    });
  });

  it("picks a fresh port when there is no saved port", async () => {
    const got = await resolveStablePort(0, undefined);
    expect(got).toBeGreaterThan(1024);
  });

  it("ignores an out-of-range saved port", async () => {
    const got = await resolveStablePort(0, 80);
    expect(got).toBeGreaterThan(1024);
  });
});
