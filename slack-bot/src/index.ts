// ─────────────────────────────────────────────────────────────
// Slack /build slash command → GitHub workflow_dispatch
//
// Endpoint:  POST /slack/build
// Slash cmd: /build <android|ios|both> <version>     e.g. /build android 1.2.3
//
// Channel-to-repo mapping lives in the CHANNEL_MAP secret as
// JSON, e.g. { "C0123456789": "webavenue/flight-manager", ... }
// The channel ID is the Slack-internal ID, not the channel name.
// ─────────────────────────────────────────────────────────────

export interface Env {
  SLACK_SIGNING_SECRET: string;
  GITHUB_TOKEN: string;
  CHANNEL_MAP: string;
  WORKFLOW_FILE: string;
  DEFAULT_REF: string;
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);

    if (req.method === "GET" && url.pathname === "/healthz") {
      return new Response("ok", { status: 200 });
    }

    if (req.method !== "POST" || url.pathname !== "/slack/build") {
      return new Response("Not found", { status: 404 });
    }

    const raw = await req.text();

    if (!(await verifySlackSignature(req, raw, env.SLACK_SIGNING_SECRET))) {
      return new Response("Invalid signature", { status: 401 });
    }

    const params = new URLSearchParams(raw);
    const channelId = params.get("channel_id") ?? "";
    const userId = params.get("user_id") ?? "";
    const text = (params.get("text") ?? "").trim();

    const parsed = parseArgs(text);
    if ("error" in parsed) {
      return slackReply(parsed.error, "ephemeral");
    }

    let channelMap: Record<string, string>;
    try {
      channelMap = JSON.parse(env.CHANNEL_MAP || "{}");
    } catch {
      return slackReply(":x: CHANNEL_MAP is not valid JSON. Ask an admin to fix the bot config.", "ephemeral");
    }

    const repo = channelMap[channelId];
    if (!repo) {
      return slackReply(
        `:x: This channel (\`${channelId}\`) isn't mapped to a repo. Ask an admin to add it to CHANNEL_MAP.`,
        "ephemeral",
      );
    }

    const inputs: Record<string, string> = {
      action: "build + upload",
      build_android: String(parsed.android),
      build_ios: String(parsed.ios),
      do_asana: "true",
      do_slack: "true",
      version_name: parsed.version,
    };

    const dispatch = await dispatchWorkflow(
      repo,
      env.WORKFLOW_FILE,
      env.DEFAULT_REF,
      inputs,
      env.GITHUB_TOKEN,
    );

    if (!dispatch.ok) {
      return slackReply(
        `:x: GitHub dispatch failed (HTTP ${dispatch.status}).\n\`\`\`${dispatch.body.slice(0, 400)}\`\`\``,
        "ephemeral",
      );
    }

    const platformLabel =
      parsed.android && parsed.ios ? "Android + iOS" : parsed.android ? "Android" : "iOS";
    const actionsUrl = `https://github.com/${repo}/actions/workflows/${env.WORKFLOW_FILE}`;

    return slackReply(
      `:rocket: <@${userId}> kicked off a *${platformLabel}* build of \`${repo}\` — v${parsed.version}. <${actionsUrl}|Watch the run>`,
      "in_channel",
    );
  },
};

// ─────────────────────────────────────────────────────────────
// Arg parsing
// ─────────────────────────────────────────────────────────────

type ParsedArgs =
  | { android: boolean; ios: boolean; version: string }
  | { error: string };

function parseArgs(text: string): ParsedArgs {
  const usage =
    "Usage: `/build <android|ios|both> <version>` — e.g. `/build android 1.2.3`";

  const tokens = text.split(/\s+/).filter(Boolean);
  if (tokens.length < 2) return { error: `:x: Missing arguments. ${usage}` };

  const raw = tokens[0].toLowerCase();
  const platform =
    raw === "android" ? "android"
    : raw === "ios" || raw === "iphone" ? "ios"
    : raw === "both" || raw === "all" ? "both"
    : null;

  if (!platform) {
    return { error: `:x: Unknown platform \`${tokens[0]}\` — expected android, ios, or both. ${usage}` };
  }

  const version = tokens[1];
  if (!/^\d+\.\d+\.\d+$/.test(version)) {
    return { error: `:x: Version must look like \`1.2.3\` — got \`${version}\`. ${usage}` };
  }

  return {
    android: platform === "android" || platform === "both",
    ios: platform === "ios" || platform === "both",
    version,
  };
}

// ─────────────────────────────────────────────────────────────
// Slack signature verification
// https://api.slack.com/authentication/verifying-requests-from-slack
// ─────────────────────────────────────────────────────────────

async function verifySlackSignature(req: Request, raw: string, secret: string): Promise<boolean> {
  const sig = req.headers.get("x-slack-signature") ?? "";
  const ts = req.headers.get("x-slack-request-timestamp") ?? "";
  if (!sig || !ts) return false;

  const tsNum = Number(ts);
  if (!Number.isFinite(tsNum) || Math.abs(Date.now() / 1000 - tsNum) > 300) {
    return false; // replay protection: reject anything older than 5 min
  }

  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = await crypto.subtle.sign("HMAC", key, enc.encode(`v0:${ts}:${raw}`));
  const expected = "v0=" + bytesToHex(new Uint8Array(mac));

  return timingSafeEqual(sig, expected);
}

function bytesToHex(b: Uint8Array): string {
  let s = "";
  for (const byte of b) s += byte.toString(16).padStart(2, "0");
  return s;
}

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// ─────────────────────────────────────────────────────────────
// GitHub workflow_dispatch
// ─────────────────────────────────────────────────────────────

async function dispatchWorkflow(
  repo: string,
  workflowFile: string,
  ref: string,
  inputs: Record<string, string>,
  token: string,
): Promise<{ ok: boolean; status: number; body: string }> {
  const res = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${workflowFile}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "build-agent-slack-bot",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref, inputs }),
    },
  );

  // 204 No Content on success. Anything else, capture the body for the error reply.
  const body = res.ok ? "" : await res.text();
  return { ok: res.ok, status: res.status, body };
}

// ─────────────────────────────────────────────────────────────
// Slack reply helper
// ─────────────────────────────────────────────────────────────

function slackReply(text: string, responseType: "in_channel" | "ephemeral"): Response {
  return new Response(JSON.stringify({ response_type: responseType, text }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}
