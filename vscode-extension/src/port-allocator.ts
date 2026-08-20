import * as net from "node:net";

/**
 * Pick a free TCP port the OS hands us.
 *
 * We bind a server to port 0 on the given host, read back the OS-assigned
 * port, close the server, and return. Brief race window exists between close
 * and the server-manager's spawn, but that's the standard pattern and the
 * server-manager will retry on EADDRINUSE.
 */
export function pickFreePort(host = "127.0.0.1"): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once("error", reject);
    server.listen(0, host, () => {
      const address = server.address();
      if (!address || typeof address === "string") {
        server.close();
        reject(new Error("could not read assigned port"));
        return;
      }
      const port = address.port;
      server.close(() => resolve(port));
    });
  });
}

/**
 * Validate a configured port value. 0 means auto-pick.
 * Anything outside 1024-65535 is treated as "auto" rather than failing.
 */
export function normalizeConfiguredPort(configured: number | undefined | null): number {
  if (typeof configured !== "number" || !Number.isFinite(configured)) return 0;
  if (configured === 0) return 0;
  if (configured < 1024 || configured > 65535) return 0;
  return Math.floor(configured);
}

/**
 * Resolve a port: if configured is non-zero use it; otherwise ask the OS.
 */
export async function resolvePort(configured: number | undefined | null, host = "127.0.0.1"): Promise<number> {
  const c = normalizeConfiguredPort(configured);
  if (c !== 0) return c;
  return pickFreePort(host);
}

/**
 * True if `port` can be bound on `host` right now — used to decide whether a
 * previously-used port is still available to reuse (see resolveStablePort).
 */
export function isPortFree(port: number, host = "127.0.0.1"): Promise<boolean> {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.unref();
    server.once("error", () => resolve(false));
    server.listen(port, host, () => {
      server.close(() => resolve(true));
    });
  });
}

/**
 * Resolve a port that stays stable across launches when possible.
 *
 * The dashboard is embedded as an iframe at http://<host>:<port>/, and the
 * webview's localStorage (collapsed-section state, the update-check cache) is
 * keyed by that origin — so a brand-new port on every launch silently wipes it.
 * When the port is auto-assigned (configured 0) we therefore reuse `saved` if
 * it's still free, only picking a fresh one when it isn't. A user-pinned port is
 * already stable, so it's returned as-is.
 *
 * Returns the chosen port; the caller is expected to persist it for next time.
 */
export async function resolveStablePort(
  configured: number | undefined | null,
  saved: number | undefined | null,
  host = "127.0.0.1",
): Promise<number> {
  const c = normalizeConfiguredPort(configured);
  if (c !== 0) return c;
  const s = normalizeConfiguredPort(saved);
  if (s !== 0 && (await isPortFree(s, host))) return s;
  return pickFreePort(host);
}
