import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { mediaSignature, rankStickers } from "../mcp-server.mjs";

const ROOT = new URL("..", import.meta.url);
const GIF = Buffer.concat([Buffer.from("GIF89a", "ascii"), Buffer.alloc(32)]);

async function freePort() {
  const server = createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  await new Promise((resolve) => server.close(resolve));
  return port;
}

async function waitFor(url, child) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (child.exitCode !== null) throw new Error(`server exited with ${child.exitCode}`);
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {
      // Still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`timed out waiting for ${url}`);
}

test("ranking keeps enabled stickers and signed media is stable", () => {
  const items = [
    { id: "u-0000000001", label: "抱抱", description: "对方委屈时安慰", media_type: "image/png" },
    { id: "u-0000000002", label: "翻白眼", description: "无语", media_type: "image/png", ai_enabled: false },
  ];
  assert.deepEqual(rankStickers(items, "委屈", 8).map((item) => item.id), ["u-0000000001"]);
  assert.equal(mediaSignature("u-0000000001", "secret"), mediaSignature("u-0000000001", "secret"));
  assert.notEqual(mediaSignature("u-0000000001", "secret"), mediaSignature("u-0000000002", "secret"));
});

test("MCP catalog, widget result, and signed GIF work end to end", async (t) => {
  const dataDir = await mkdtemp(join(tmpdir(), "sticker-shelf-mcp-"));
  const imageDir = join(dataDir, "images");
  await mkdir(imageDir);
  const sticker = {
    id: "u-0123456789",
    label: "抱紧不放",
    description: "对方委屈或关系不安时主动靠近安慰",
    file: "u-0123456789.gif",
    media_type: "image/gif",
    ai_enabled: true,
    created_at: "2026-10-17T00:00:00Z",
  };
  await writeFile(join(dataDir, "stickers.json"), JSON.stringify([sticker]));
  await writeFile(join(imageDir, sticker.file), GIF);

  const publicPort = await freePort();
  const backendPort = await freePort();
  const mcpToken = "test_mcp_token_1234567890";
  const child = spawn(process.execPath, ["mcp-server.mjs"], {
    cwd: ROOT,
    env: {
      ...process.env,
      PORT: String(publicPort),
      STICKER_BACKEND_PORT: String(backendPort),
      STICKER_DATA_DIR: dataDir,
      STICKER_ADMIN_PASSWORD: "correct horse battery staple",
      STICKER_API_TOKEN: "internal-api-token",
      STICKER_MCP_TOKEN: mcpToken,
      STICKER_MEDIA_SECRET: "stable-media-secret",
      PYTHON: process.env.PYTHON || (process.platform === "win32" ? "python" : "python3"),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });

  let logs = "";
  child.stdout.on("data", (chunk) => { logs += chunk; });
  child.stderr.on("data", (chunk) => { logs += chunk; });
  t.after(async () => {
    child.kill();
    await Promise.race([
      new Promise((resolve) => child.once("exit", resolve)),
      new Promise((resolve) => setTimeout(resolve, 2000)),
    ]);
    await rm(dataDir, { recursive: true, force: true });
  });

  const base = `http://127.0.0.1:${publicPort}`;
  try {
    await waitFor(`${base}/healthz`, child);
    const client = new Client({ name: "sticker-shelf-test", version: "1.0.0" });
    const transport = new StreamableHTTPClientTransport(new URL(`${base}/mcp/${mcpToken}`));
    await client.connect(transport);
    t.after(() => transport.close());

    const tools = await client.listTools();
    assert.deepEqual(tools.tools.map((tool) => tool.name).sort(), [
      "list_stickers_for_context",
      "show_sticker",
    ]);

    const catalog = await client.callTool({
      name: "list_stickers_for_context",
      arguments: { context: "她有点委屈，需要安慰" },
    });
    assert.equal(catalog.structuredContent.stickers[0].id, sticker.id);
    assert.match(catalog.structuredContent.stickers[0].description, /委屈/);

    const shown = await client.callTool({ name: "show_sticker", arguments: { id: sticker.id } });
    assert.equal(shown.structuredContent.sticker.media_type, "image/gif");
    const imageUrl = shown.structuredContent.sticker.image_url;
    assert.match(imageUrl, new RegExp(`^${base.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/plugin-media/`));

    const imageResponse = await fetch(imageUrl);
    assert.equal(imageResponse.status, 200);
    assert.equal(imageResponse.headers.get("content-type"), "image/gif");
    assert.deepEqual(Buffer.from(await imageResponse.arrayBuffer()), GIF);
  } catch (error) {
    throw new Error(`${error.message}\nServer output:\n${logs}`, { cause: error });
  }
});
