import { describe, it, expect, beforeEach } from "vitest";
import { EventEmitter } from "node:events";
import { ServerManager, SpawnedLike, SpawnFn, OutputSink } from "../src/server-manager";

class FakeStream extends EventEmitter {}

class FakeProcess extends EventEmitter implements SpawnedLike {
  pid = 1234;
  stdout = new FakeStream();
  stderr = new FakeStream();
  killed = false;
  kill(_signal?: NodeJS.Signals | number): boolean {
    this.killed = true;
    return true;
  }
}

class MemorySink implements OutputSink {
  lines: string[] = [];
  appendLine(line: string): void {
    this.lines.push(line);
  }
}

function fakeProbe(answers: Array<boolean | (() => boolean)>): (url: string) => Promise<boolean> {
  let i = 0;
  return async () => {
    const ans = answers[Math.min(i, answers.length - 1)];
    i++;
    return typeof ans === "function" ? ans() : !!ans;
  };
}

describe("ServerManager", () => {
  let proc: FakeProcess;
  let spawnFn: SpawnFn;
  let sink: MemorySink;

  beforeEach(() => {
    proc = new FakeProcess();
    spawnFn = () => proc;
    sink = new MemorySink();
  });

  it("status starts as stopped", () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([false]),
      readinessTimeoutMs: 100,
      readinessPollMs: 10,
    });
    expect(mgr.status).toBe("stopped");
  });

  it("becomes ready when the probe succeeds", async () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([false, false, true]),
      readinessTimeoutMs: 500,
      readinessPollMs: 10,
    });
    await mgr.start();
    expect(mgr.status).toBe("ready");
    expect(sink.lines.some((l) => l.includes("ready at"))).toBe(true);
  });

  it("fails when the process exits before becoming ready", async () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([false]),
      readinessTimeoutMs: 500,
      readinessPollMs: 10,
    });
    const startPromise = mgr.start();
    // Simulate process dying before the first successful probe
    setTimeout(() => proc.emit("exit", 1, null), 20);
    await expect(startPromise).rejects.toThrow(/exited before becoming ready/);
    expect(mgr.status).toBe("failed");
  });

  it("fails after the readiness timeout when the probe never succeeds", async () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([false]),
      readinessTimeoutMs: 60,
      readinessPollMs: 10,
    });
    await expect(mgr.start()).rejects.toThrow(/did not become ready/);
    expect(mgr.status).toBe("failed");
  });

  it("dispose kills the child process", async () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([true]),
      readinessTimeoutMs: 200,
      readinessPollMs: 10,
    });
    await mgr.start();
    expect(mgr.status).toBe("ready");
    mgr.dispose();
    expect(proc.killed).toBe(true);
    expect(mgr.status).toBe("stopped");
  });

  it("dispose is safe when nothing was started", () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([false]),
      readinessTimeoutMs: 100,
      readinessPollMs: 10,
    });
    expect(() => mgr.dispose()).not.toThrow();
    expect(mgr.status).toBe("stopped");
  });

  it("stdout is forwarded to the sink", async () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([true]),
      readinessTimeoutMs: 200,
      readinessPollMs: 10,
    });
    await mgr.start();
    proc.stdout.emit("data", Buffer.from("Dashboard running at http://127.0.0.1:9000\n"));
    expect(sink.lines.some((l) => l.includes("[server] Dashboard running"))).toBe(true);
  });

  it("stderr is forwarded with [server:err] prefix", async () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([true]),
      readinessTimeoutMs: 200,
      readinessPollMs: 10,
    });
    await mgr.start();
    proc.stderr.emit("data", Buffer.from("Address already in use\n"));
    expect(sink.lines.some((l) => l.startsWith("[server:err]"))).toBe(true);
  });

  it("propagates spawn-time errors as failure", async () => {
    const failingSpawn: SpawnFn = () => {
      throw new Error("ENOENT: python3 not found");
    };
    const mgr = new ServerManager({
      command: "python3",
      args: ["cli.py"],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn: failingSpawn,
      probeFn: fakeProbe([false]),
      readinessTimeoutMs: 100,
      readinessPollMs: 10,
    });
    await expect(mgr.start()).rejects.toThrow(/ENOENT/);
    expect(mgr.status).toBe("failed");
  });

  it("can be restarted after dispose", async () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn: () => new FakeProcess(),
      probeFn: fakeProbe([true]),
      readinessTimeoutMs: 200,
      readinessPollMs: 10,
    });
    await mgr.start();
    mgr.dispose();
    expect(mgr.status).toBe("stopped");
    await mgr.start();
    expect(mgr.status).toBe("ready");
  });

  it("refuses to start while already starting/ready", async () => {
    const mgr = new ServerManager({
      command: "noop",
      args: [],
      url: "http://127.0.0.1:9000/",
      output: sink,
      spawnFn,
      probeFn: fakeProbe([true]),
      readinessTimeoutMs: 200,
      readinessPollMs: 10,
    });
    await mgr.start();
    await expect(mgr.start()).rejects.toThrow(/cannot start/);
  });
});

describe("default probe (integration via fake http server)", () => {
  // Live test of the default probe behavior — start a tiny http server that
  // returns various responses and confirm only the right shape passes.
  // This is the strictness Codex asked for: any old localhost service
  // returning 404/HTML on /api/data must NOT be treated as healthy.

  import("node:http").then(/* type-only resolve so vitest doesn't get confused */);

  async function makeServer(handler: (req: any, res: any) => void): Promise<{ url: string; close: () => void }> {
    const http = await import("node:http");
    return new Promise((resolve) => {
      const server = http.createServer(handler);
      server.listen(0, "127.0.0.1", () => {
        const addr = server.address();
        if (!addr || typeof addr === "string") throw new Error("no addr");
        resolve({
          url: `http://127.0.0.1:${addr.port}/api/data`,
          close: () => server.close(),
        });
      });
    });
  }

  it("accepts 200 + dashboard-shape JSON", async () => {
    const srv = await makeServer((_req, res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ all_models: ["claude-opus-4-7"], daily_by_model: [], sessions_all: [] }));
    });
    try {
      const mgr = new ServerManager({
        command: "noop",
        args: [],
        url: srv.url,
        output: new MemorySink(),
        spawnFn: () => new FakeProcess(),
        // intentionally NOT injecting probeFn so the real one runs
        readinessTimeoutMs: 500,
        readinessPollMs: 30,
      });
      await mgr.start();
      expect(mgr.status).toBe("ready");
      mgr.dispose();
    } finally {
      srv.close();
    }
  });

  it("accepts 200 + DB-missing 'error' JSON (also our server)", async () => {
    const srv = await makeServer((_req, res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "Database not found. Run: python cli.py scan" }));
    });
    try {
      const mgr = new ServerManager({
        command: "noop",
        args: [],
        url: srv.url,
        output: new MemorySink(),
        spawnFn: () => new FakeProcess(),
        readinessTimeoutMs: 500,
        readinessPollMs: 30,
      });
      await mgr.start();
      expect(mgr.status).toBe("ready");
      mgr.dispose();
    } finally {
      srv.close();
    }
  });

  it("rejects 200 + JSON that isn't dashboard-shaped", async () => {
    const srv = await makeServer((_req, res) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ greeting: "hello" }));
    });
    try {
      const mgr = new ServerManager({
        command: "noop",
        args: [],
        url: srv.url,
        output: new MemorySink(),
        spawnFn: () => new FakeProcess(),
        readinessTimeoutMs: 200,
        readinessPollMs: 30,
      });
      await expect(mgr.start()).rejects.toThrow(/did not become ready/);
    } finally {
      srv.close();
    }
  });

  it("rejects non-200 status (e.g. 404 from random localhost service)", async () => {
    const srv = await makeServer((_req, res) => {
      res.writeHead(404);
      res.end("Not found");
    });
    try {
      const mgr = new ServerManager({
        command: "noop",
        args: [],
        url: srv.url,
        output: new MemorySink(),
        spawnFn: () => new FakeProcess(),
        readinessTimeoutMs: 200,
        readinessPollMs: 30,
      });
      await expect(mgr.start()).rejects.toThrow(/did not become ready/);
    } finally {
      srv.close();
    }
  });

  it("rejects 200 + non-JSON body (HTML)", async () => {
    const srv = await makeServer((_req, res) => {
      res.writeHead(200, { "Content-Type": "text/html" });
      res.end("<html><body>some other server</body></html>");
    });
    try {
      const mgr = new ServerManager({
        command: "noop",
        args: [],
        url: srv.url,
        output: new MemorySink(),
        spawnFn: () => new FakeProcess(),
        readinessTimeoutMs: 200,
        readinessPollMs: 30,
      });
      await expect(mgr.start()).rejects.toThrow(/did not become ready/);
    } finally {
      srv.close();
    }
  });
});
