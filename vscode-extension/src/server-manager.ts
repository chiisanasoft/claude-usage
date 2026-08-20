import { spawn as defaultSpawn } from "node:child_process";
import * as http from "node:http";

/**
 * Anything the server-manager needs from a spawned process. Designed to make
 * tests trivial — pass a fake spawn function that returns an EventEmitter.
 */
export interface SpawnedLike {
  readonly pid?: number;
  kill(signal?: NodeJS.Signals | number): boolean;
  on(event: "exit", listener: (code: number | null, signal: NodeJS.Signals | null) => void): void;
  on(event: "error", listener: (err: Error) => void): void;
  stdout?: { on(event: "data", listener: (chunk: Buffer) => void): void } | null;
  stderr?: { on(event: "data", listener: (chunk: Buffer) => void): void } | null;
}

export type SpawnFn = (command: string, args: ReadonlyArray<string>) => SpawnedLike;

export interface OutputSink {
  appendLine(line: string): void;
}

export interface ServerManagerOptions {
  command: string;
  args: ReadonlyArray<string>;
  /** URL used to probe readiness, e.g. http://127.0.0.1:9000/. */
  url: string;
  output: OutputSink;
  /** Defaults to node:child_process.spawn. */
  spawnFn?: SpawnFn;
  /** Defaults to the built-in http.get probe. */
  probeFn?: (url: string) => Promise<boolean>;
  /** Total time to wait for the server to become healthy. Default 10s. */
  readinessTimeoutMs?: number;
  /** Time between probes. Default 200ms. */
  readinessPollMs?: number;
}

export type ServerStatus = "stopped" | "starting" | "ready" | "exited" | "failed";

/**
 * Owns the lifecycle of a single Python dashboard process.
 *
 * State machine:
 *   stopped --start()--> starting --probe ok--> ready
 *                                  \--timeout--> failed
 *                       \--exit before ready--> failed
 *   ready --process exits--> exited
 *   any   --dispose()--> stopped (process killed)
 */
export class ServerManager {
  private proc: SpawnedLike | undefined;
  private _status: ServerStatus = "stopped";
  private readonly opts: ServerManagerOptions;
  private readonly spawnFn: SpawnFn;
  private readonly probeFn: (url: string) => Promise<boolean>;
  private readonly readinessTimeoutMs: number;
  private readonly readinessPollMs: number;

  constructor(opts: ServerManagerOptions) {
    this.opts = opts;
    this.spawnFn = opts.spawnFn ?? ((cmd, args) => defaultSpawn(cmd, args as string[]));
    this.probeFn = opts.probeFn ?? defaultProbe;
    this.readinessTimeoutMs = opts.readinessTimeoutMs ?? 10_000;
    this.readinessPollMs = opts.readinessPollMs ?? 200;
  }

  get status(): ServerStatus {
    return this._status;
  }

  /**
   * Spawn the process and resolve when it answers HTTP, or reject if it
   * exits before that or the timeout fires. Callers are expected to wrap
   * port-collision recovery at a higher level (catch failure → pick new port
   * → new ServerManager).
   */
  async start(): Promise<void> {
    if (this._status !== "stopped" && this._status !== "failed" && this._status !== "exited") {
      throw new Error(`cannot start: server is ${this._status}`);
    }
    this._status = "starting";
    this.opts.output.appendLine(`[server] spawning: ${this.opts.command} ${this.opts.args.join(" ")}`);

    let proc: SpawnedLike;
    try {
      proc = this.spawnFn(this.opts.command, this.opts.args);
    } catch (err) {
      this._status = "failed";
      this.opts.output.appendLine(`[server] spawn failed: ${(err as Error).message}`);
      throw err;
    }
    this.proc = proc;

    proc.stdout?.on("data", (chunk) => this.opts.output.appendLine(`[server] ${chunk.toString().trimEnd()}`));
    proc.stderr?.on("data", (chunk) => this.opts.output.appendLine(`[server:err] ${chunk.toString().trimEnd()}`));

    let exitedEarly = false;
    let earlyExitCode: number | null = null;
    proc.on("exit", (code) => {
      if (this._status === "starting" || this._status === "ready") {
        exitedEarly = this._status === "starting";
        earlyExitCode = code;
        this._status = this._status === "starting" ? "failed" : "exited";
        this.opts.output.appendLine(`[server] process exited with code ${code}`);
      }
    });
    proc.on("error", (err) => {
      this.opts.output.appendLine(`[server] error: ${err.message}`);
      if (this._status === "starting") this._status = "failed";
    });

    const deadline = Date.now() + this.readinessTimeoutMs;
    while (Date.now() < deadline) {
      if (exitedEarly) {
        throw new Error(`server exited before becoming ready (code ${earlyExitCode})`);
      }
      const healthy = await this.probeFn(this.opts.url);
      if (healthy) {
        // Process may have died after the probe but before we checked status.
        if (this._status === "starting") {
          this._status = "ready";
          this.opts.output.appendLine(`[server] ready at ${this.opts.url}`);
          return;
        }
      }
      await delay(this.readinessPollMs);
    }
    this._status = "failed";
    this.dispose();
    throw new Error(`server did not become ready within ${this.readinessTimeoutMs}ms at ${this.opts.url}`);
  }

  dispose(): void {
    if (this.proc) {
      try {
        this.proc.kill();
      } catch {
        // Already gone — fine.
      }
      this.proc = undefined;
    }
    if (this._status !== "exited" && this._status !== "failed") {
      this._status = "stopped";
    }
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Built-in readiness probe — true only on a 200 OK whose body parses as
 * JSON containing keys we expect from the dashboard's `/api/data` endpoint.
 *
 * Stricter than "anything <500" because a random localhost service on the
 * same port can still return 404, and we don't want to mistake that for
 * "our server is up."
 */
function defaultProbe(url: string): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.get(url, { timeout: 1_500 }, (res) => {
      if (res.statusCode !== 200) {
        res.resume();
        resolve(false);
        return;
      }
      const chunks: Buffer[] = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => {
        try {
          const body = JSON.parse(Buffer.concat(chunks).toString("utf-8"));
          // /api/data returns { all_models, daily_by_model, sessions_all, ... }
          // OR { error: "..." } if the DB doesn't exist yet. Either is OUR server.
          const ok =
            typeof body === "object" &&
            body !== null &&
            ("all_models" in body || "error" in body);
          resolve(ok);
        } catch {
          resolve(false);
        }
      });
    });
    req.on("error", () => resolve(false));
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
  });
}
