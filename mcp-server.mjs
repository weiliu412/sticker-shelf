import { spawn } from "node:child_process";
import { createHmac, timingSafeEqual } from "node:crypto";
import { createServer, request as httpRequest } from "node:http";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

import {
  registerAppResource,
  registerAppTool,
  RESOURCE_MIME_TYPE,
} from "@modelcontextprotocol/ext-apps/server";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { z } from "zod";

const BACKEND_HOST = "127.0.0.1";
const BACKEND_PORT = Number(process.env.STICKER_BACKEND_PORT || 8767);
const PUBLIC_PORT = Number(process.env.PORT || 8787);
const API_TOKEN = (process.env.STICKER_API_TOKEN || "").trim();
const MCP_TOKEN = (process.env.STICKER_MCP_TOKEN || "").trim();
const MEDIA_SECRET = (
  process.env.STICKER_MEDIA_SECRET || API_TOKEN || MCP_TOKEN || "sticker-shelf-local-media"
).trim();
const CLOUD_MODE = Boolean(process.env.RAILWAY_ENVIRONMENT || process.env.RAILWAY_PROJECT_ID);
const WIDGET_URI = "ui://sticker-shelf/sticker-v1.html";
const WIDGET_HTML = readFileSync(new URL("./web/sticker-widget.html", import.meta.url), "utf8");

if (MCP_TOKEN && !/^[A-Za-z0-9_-]{16,}$/.test(MCP_TOKEN)) {
  throw new Error("STICKER_MCP_TOKEN must contain at least 16 URL-safe characters.");
}
if (CLOUD_MODE && !MCP_TOKEN) {
  throw new Error("Cloud deployment requires STICKER_MCP_TOKEN.");
}

export const mcpPath = MCP_TOKEN ? `/mcp/${MCP_TOKEN}` : "/mcp";

function publicOrigin(req) {
  const first = (value) => String(value || "").split(",", 1)[0].trim();
  const protocol = first(req.headers["x-forwarded-proto"]) || "http";
  const host = first(req.headers["x-forwarded-host"]) || first(req.headers.host);
  const origin = new URL(`${protocol}://${host || `localhost:${PUBLIC_PORT}`}`).origin;
  if (!origin.startsWith("http://") && !origin.startsWith("https://")) {
    throw new Error("Unsupported public origin.");
  }
  return origin;
}

export function mediaSignature(stickerId, secret = MEDIA_SECRET) {
  return createHmac("sha256", secret).update(`sticker:${stickerId}`).digest("base64url");
}

function safeEqual(left, right) {
  const a = Buffer.from(String(left || ""));
  const b = Buffer.from(String(right || ""));
  return a.length === b.length && timingSafeEqual(a, b);
}

function mediaUrl(origin, stickerId) {
  return `${origin}/plugin-media/${encodeURIComponent(stickerId)}?sig=${mediaSignature(stickerId)}`;
}

async function backendJson(path) {
  const headers = { accept: "application/json" };
  if (API_TOKEN) headers.authorization = `Bearer ${API_TOKEN}`;
  const response = await fetch(`http://${BACKEND_HOST}:${BACKEND_PORT}${path}`, { headers });
  if (!response.ok) {
    throw new Error(`Sticker Shelf backend returned ${response.status}.`);
  }
  return response.json();
}

function compactSticker(item) {
  return {
    id: String(item.id),
    label: String(item.label || "未命名表情"),
    description: String(item.description || ""),
    media_type: String(item.media_type || "image/png"),
  };
}

function normalizeSearchText(value) {
  return String(value || "").toLocaleLowerCase("zh-CN").replace(/\s+/g, "");
}

function lexicalScore(item, context) {
  const query = normalizeSearchText(context);
  if (!query) return 0;
  const text = normalizeSearchText(`${item.label}${item.description}`);
  let score = text.includes(query) ? 100 : 0;
  const chars = [...new Set([...query].filter((char) => /[\p{L}\p{N}]/u.test(char)))];
  for (const char of chars) if (text.includes(char)) score += 1;
  for (let index = 0; index < query.length - 1; index += 1) {
    if (text.includes(query.slice(index, index + 2))) score += 4;
  }
  return score;
}

export function rankStickers(items, context, limit = 80) {
  const enabled = items.filter((item) => item.ai_enabled !== false).map(compactSticker);
  if (enabled.length <= limit) return enabled;
  return enabled
    .map((item, index) => ({ item, index, score: lexicalScore(item, context) }))
    .sort((a, b) => b.score - a.score || b.index - a.index)
    .slice(0, limit)
    .map(({ item }) => item);
}

const stickerSchema = z.object({
  id: z.string(),
  label: z.string(),
  description: z.string(),
  media_type: z.string(),
});

function createStickerMcpServer(origin) {
  const server = new McpServer(
    { name: "sticker-shelf", version: "0.2.0" },
    {
      instructions:
        "在情绪表达、撒娇、安慰、庆祝、调侃或关系互动中，如果一张表情能自然增强语气，先调用 list_stickers_for_context，再从返回结果中选择一张并调用 show_sticker。每轮最多一张，不要每轮都发；不要在正文里念出表情名称、编号或解释调用过程。没有贴切表情时正常回复，不要硬发。",
    },
  );

  registerAppResource(server, "sticker-card", WIDGET_URI, {}, async () => ({
    contents: [
      {
        uri: WIDGET_URI,
        mimeType: RESOURCE_MIME_TYPE,
        text: WIDGET_HTML,
        _meta: {
          ui: {
            prefersBorder: false,
            csp: { resourceDomains: [origin] },
          },
        },
      },
    ],
  }));

  server.registerTool(
    "list_stickers_for_context",
    {
      title: "查看适合当前语境的表情",
      description:
        "Use this before sending a sticker. It returns the user's enabled sticker names and human-written meanings/usages so you can choose semantically. Pass only a short summary of the immediate emotional situation, never the full transcript. Do not use when no sticker would add value.",
      inputSchema: {
        context: z.string().max(500).describe("当前一句话可以概括的情绪或使用场景，不要传完整聊天记录。"),
        max_results: z.number().int().min(1).max(80).optional().describe("最多返回多少个候选，默认 80。"),
      },
      outputSchema: {
        stickers: z.array(stickerSchema),
        instruction: z.string(),
      },
      annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
    },
    async ({ context, max_results }) => {
      const data = await backendJson("/stickers/catalog");
      const stickers = rankStickers(data.items || [], context, max_results || 80);
      return {
        content: [
          {
            type: "text",
            text: stickers.length
              ? "请依据描述选择至多一张最贴切的表情；若没有贴切项，不要调用 show_sticker。"
              : "目前没有允许 AI 使用的表情。",
          },
        ],
        structuredContent: {
          stickers,
          instruction: "选择至多一张；不贴切就不发。选定后调用 show_sticker，正文不要复述其名称或含义。",
        },
      };
    },
  );

  registerAppTool(
    server,
    "show_sticker",
    {
      title: "在对话中显示一张表情",
      description:
        "Use this only after list_stickers_for_context and only with an id returned by it. It renders that single image or GIF inline. Call at most once per assistant turn and do not narrate, caption, or repeat the sticker label in the final response.",
      inputSchema: {
        id: z.string().regex(/^u-[a-f0-9]{10}$/).describe("由 list_stickers_for_context 返回的表情编号。"),
      },
      outputSchema: {
        sticker: stickerSchema.extend({ image_url: z.string().url() }),
      },
      annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
      _meta: { ui: { resourceUri: WIDGET_URI } },
    },
    async ({ id }) => {
      const data = await backendJson("/stickers/catalog");
      const source = (data.items || []).find((item) => item.id === id && item.ai_enabled !== false);
      if (!source) {
        return {
          isError: true,
          content: [{ type: "text", text: "这张表情不存在或已关闭 AI 使用权限。" }],
        };
      }
      const sticker = { ...compactSticker(source), image_url: mediaUrl(origin, id) };
      return {
        content: [{ type: "text", text: "表情已显示；继续自然回复，不要复述表情名称。" }],
        structuredContent: { sticker },
      };
    },
  );

  return server;
}

function proxyHeaders(req, { authorized = false } = {}) {
  const headers = { ...req.headers };
  delete headers.connection;
  delete headers["proxy-connection"];
  delete headers.upgrade;
  headers.host = req.headers.host || `${BACKEND_HOST}:${BACKEND_PORT}`;
  if (authorized && API_TOKEN) headers.authorization = `Bearer ${API_TOKEN}`;
  return headers;
}

function proxyRequestToBackend(req, res, path, options = {}) {
  const outgoing = httpRequest(
    {
      hostname: BACKEND_HOST,
      port: BACKEND_PORT,
      method: req.method,
      path,
      headers: proxyHeaders(req, options),
    },
    (incoming) => {
      res.writeHead(incoming.statusCode || 502, incoming.headers);
      incoming.pipe(res);
    },
  );
  outgoing.on("error", (error) => {
    console.error("Sticker Shelf backend proxy error:", error.message);
    if (!res.headersSent) res.writeHead(502, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: "表情包服务暂时不可用。" }));
  });
  req.pipe(outgoing);
}

function serveSignedMedia(req, res, url) {
  const prefix = "/plugin-media/";
  const stickerId = decodeURIComponent(url.pathname.slice(prefix.length));
  if (!/^u-[a-f0-9]{10}$/.test(stickerId) || !safeEqual(url.searchParams.get("sig"), mediaSignature(stickerId))) {
    res.writeHead(404, { "content-type": "text/plain; charset=utf-8" }).end("Not found");
    return;
  }
  proxyRequestToBackend(req, res, `/stickers/img/${stickerId}`, { authorized: true });
}

async function waitForBackend(attempts = 50) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const response = await fetch(`http://${BACKEND_HOST}:${BACKEND_PORT}/healthz`);
      if (response.ok) return;
    } catch {
      // The Python process is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("Sticker Shelf backend did not start.");
}

function spawnBackend() {
  const command = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
  const child = spawn(command, ["server.py"], {
    cwd: new URL(".", import.meta.url),
    env: {
      ...process.env,
      HOST: BACKEND_HOST,
      PORT: String(BACKEND_PORT),
    },
    stdio: "inherit",
  });
  child.on("exit", (code, signal) => {
    if (!shuttingDown) {
      console.error(`Sticker Shelf backend exited (${code ?? signal}).`);
      process.exit(code || 1);
    }
  });
  return child;
}

let shuttingDown = false;

export async function startStickerServer() {
  const backend = spawnBackend();
  await waitForBackend();

  const httpServer = createServer(async (req, res) => {
    if (!req.url) return res.writeHead(400).end("Missing URL");
    const url = new URL(req.url, `http://${req.headers.host || `localhost:${PUBLIC_PORT}`}`);

    if (url.pathname === mcpPath && req.method === "OPTIONS") {
      res.writeHead(204, {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, GET, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "content-type, mcp-session-id, mcp-protocol-version",
        "Access-Control-Expose-Headers": "Mcp-Session-Id",
      }).end();
      return;
    }

    if (url.pathname === mcpPath && new Set(["POST", "GET", "DELETE"]).has(req.method || "")) {
      res.setHeader("Access-Control-Allow-Origin", "*");
      res.setHeader("Access-Control-Expose-Headers", "Mcp-Session-Id");
      const server = createStickerMcpServer(publicOrigin(req));
      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: undefined,
        enableJsonResponse: true,
      });
      res.on("close", () => {
        transport.close();
        server.close();
      });
      try {
        await server.connect(transport);
        await transport.handleRequest(req, res);
      } catch (error) {
        console.error("MCP request failed:", error);
        if (!res.headersSent) res.writeHead(500).end("Internal server error");
      }
      return;
    }

    if (req.method === "GET" && url.pathname.startsWith("/plugin-media/")) {
      serveSignedMedia(req, res, url);
      return;
    }

    proxyRequestToBackend(req, res, req.url);
  });

  await new Promise((resolve) => httpServer.listen(PUBLIC_PORT, "0.0.0.0", resolve));
  console.log(`Sticker Shelf listening on http://0.0.0.0:${PUBLIC_PORT}`);
  console.log(`Private MCP endpoint: ${mcpPath}`);

  const stop = () => {
    shuttingDown = true;
    httpServer.close();
    backend.kill();
  };
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
  return { httpServer, backend, stop };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  startStickerServer().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
