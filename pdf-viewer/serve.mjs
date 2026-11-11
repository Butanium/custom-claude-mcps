// Run @modelcontextprotocol/server-pdf as ONE process for every Claude Code
// session, over streamable HTTP on loopback.
//
//   node serve.mjs [--port N] [--host H] [DIR ...]
//
// DIR ... are the directories sessions may open PDFs from and save them to
// (default: home, the temp dir, and /var/tmp when it exists).
//
// Why not the package's own HTTP entry (`mcp-pdf-server` without --stdio): it
// listens on every interface with open CORS, and its --use-client-roots would
// let each connecting session overwrite the one process-wide directory
// allowlist. See ../ENGINEERING_LOGS.md (2026-11-11).

import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { NodeStreamableHTTPServerTransport } from "@modelcontextprotocol/node";
import { allowedLocalDirs, createServer } from "@modelcontextprotocol/server-pdf";

function parseArgs(argv) {
  const out = { port: 8875, host: "127.0.0.1", dirs: [] };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--port") out.port = Number.parseInt(argv[++i], 10);
    else if (argv[i] === "--host") out.host = argv[++i];
    else if (argv[i].startsWith("-")) throw new Error(`unknown flag ${argv[i]}`);
    else out.dirs.push(argv[i]);
  }
  if (!Number.isInteger(out.port)) throw new Error("--port needs an integer");
  return out;
}

const { port, host, dirs } = parseArgs(process.argv.slice(2));
const defaults = [os.homedir(), os.tmpdir(), "/var/tmp"];
for (const dir of dirs.length > 0 ? dirs : defaults) {
  const resolved = path.resolve(dir);
  if (fs.existsSync(resolved) && fs.statSync(resolved).isDirectory()) {
    allowedLocalDirs.add(resolved);
  } else if (dirs.length > 0) {
    throw new Error(`not a directory: ${resolved}`);
  }
}

// The package's list_pdfs walks every allowed directory and returns up to 500
// paths. With one session's project as the only root that is a useful listing;
// with a whole home directory shared by every session it is 100 KB of other
// projects' files. Replace it with the directory list alone.
function makeServer() {
  const server = createServer({ enableInteract: true, useClientRoots: false });
  const listPdfs = server._registeredTools?.list_pdfs;
  if (!listPdfs) throw new Error("server-pdf no longer registers list_pdfs the way serve.mjs expects");
  listPdfs.update({
    description:
      "List the local directories PDFs can be displayed from. Does not list files: this server is shared by every session, so find the PDF with your own file tools and pass its absolute path to display_pdf.",
    callback: async () => {
      const allowedDirectories = [...allowedLocalDirs];
      return {
        content: [
          {
            type: "text",
            text:
              `Any PDF under these directories can be displayed (pass its absolute path to display_pdf):\n` +
              allowedDirectories.map((d) => `- ${d}`).join("\n") +
              `\n\nFiles are not listed here; search for *.pdf in your project with your own file tools.` +
              `\nAny remote PDF accessible via HTTPS can also be loaded.`,
          },
        ],
        structuredContent: { localFiles: [], allowedDirectories, truncated: false },
      };
    },
  });
  return server;
}
makeServer(); // fail at startup, not on the first request, if the package changed shape

// A browser page cannot reach this server by DNS rebinding: the Host header
// must name loopback, and no CORS headers are ever sent.
const allowedHosts = new Set(
  ["127.0.0.1", "localhost", "[::1]", host].map((h) => `${h}:${port}`),
);

const httpServer = http.createServer(async (req, res) => {
  if (!allowedHosts.has(req.headers.host ?? "")) {
    res.writeHead(403).end("forbidden host");
    return;
  }
  if (new URL(req.url ?? "/", "http://loopback").pathname !== "/mcp") {
    res.writeHead(404).end("not found");
    return;
  }
  // Stateless, like the package's own HTTP entry: one MCP server object per
  // request. Everything a viewer needs between requests (command queue, poll
  // waiters, source path) lives in the package's module-level maps, keyed by
  // the random viewUUID that display_pdf returns.
  const server = makeServer();
  const transport = new NodeStreamableHTTPServerTransport({
    sessionIdGenerator: undefined,
  });
  res.on("close", () => {
    transport.close().catch(() => {});
    server.close().catch(() => {});
  });
  try {
    await server.connect(transport);
    await transport.handleRequest(req, res);
  } catch (error) {
    console.error("[pdf-viewer] MCP error:", error);
    if (!res.headersSent) {
      res.writeHead(500, { "content-type": "application/json" }).end(
        JSON.stringify({
          jsonrpc: "2.0",
          error: { code: -32603, message: "Internal server error" },
          id: null,
        }),
      );
    }
  }
});

httpServer.listen(port, host, () => {
  console.error(
    `[pdf-viewer] http://${host}:${port}/mcp, directories: ${[...allowedLocalDirs].join(", ")}`,
  );
});
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    httpServer.close(() => process.exit(0));
    httpServer.closeAllConnections();
  });
}
