// ─────────────────────────────────────────────────────────────
// Slack slash commands → GitHub, backed by the shared CHANNEL_MAP.
//
//   /build  <android|ios|both> <version>   → workflow_dispatch
//     Endpoint: POST /slack/build
//
//   /invite-to-repo <github-username>      → repo collaborator invite
//     Endpoint: POST /slack/invite
//
// Both resolve the target repo from the channel the command was run
// in. Channel-to-repo mapping lives in the CHANNEL_MAP secret as
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

    const isBuild = req.method === "POST" && url.pathname === "/slack/build";
    const isInvite = req.method === "POST" && url.pathname === "/slack/invite";
    if (!isBuild && !isInvite) {
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

    // Both commands need the channel → repo mapping, so resolve it up front.
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

    // ── /invite-to-repo <github-username> ──────────────────────────
    if (isInvite) {
      return handleInvite(text, repo, userId, env.GITHUB_TOKEN);
    }

    // ── /build <android|ios|both> <version> [branch] [nogate] ──────
    const parsed = parseArgs(text);
    if ("error" in parsed) {
      return slackReply(parsed.error, "ephemeral");
    }

    const inputs: Record<string, string> = {
      action: "build + upload",
      build_android: String(parsed.android),
      build_ios: String(parsed.ios),
      do_asana: "true",
      do_slack: "true",
      version_name: parsed.version,
    };
    // Only send the gate inputs when SKIPPING. The reusable workflow defaults both
    // to true, so omitting them keeps existing behaviour — and avoids a 422
    // "unexpected input" for any caller repo whose build.yml predates these inputs.
    // The skip flag bypasses BOTH pre-build gates: e2e_tests (Phase 1, Playwright)
    // and integration_gate (Phase 2, qa/). A repo must expose both inputs in its
    // build.yml for `nogate` to take effect on each.
    if (!parsed.gate) {
      inputs.e2e_tests = "false";
      inputs.integration_gate = "false";
    }

    const ref = parsed.ref ?? env.DEFAULT_REF;
    const dispatch = await dispatchWorkflow(
      repo,
      env.WORKFLOW_FILE,
      ref,
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
    const gateNote = parsed.gate ? "" : " :warning: *Pre-build gates skipped (e2e + integration gate).*";

    return slackReply(
      `:rocket: <@${userId}> kicked off a *${platformLabel}* build of \`${repo}\` (\`${ref}\`) — v${parsed.version}.${gateNote} <${actionsUrl}|Watch the run>`,
      "in_channel",
    );
  },
};

// ─────────────────────────────────────────────────────────────
// Arg parsing
// ─────────────────────────────────────────────────────────────

type ParsedArgs =
  | { android: boolean; ios: boolean; version: string; ref?: string; gate: boolean }
  | { error: string };

// Reserved keywords that turn OFF the pre-build QA integration gate (build directly).
const GATE_SKIP_FLAGS = new Set(["nogate", "no-gate", "skip-gate", "skipgate", "--no-gate"]);

function parseArgs(text: string): ParsedArgs {
  const usage =
    "Usage: `/build <android|ios|both> <version> [branch] [nogate]` — e.g. `/build android 1.2.3`. Add `nogate` to skip both pre-build gates (Playwright e2e + QA integration gate) and build directly.";

  // Pull flags out FIRST (they can appear anywhere) so `nogate` is never mistaken
  // for a branch in the positional slots below.
  let gate = true;
  const tokens: string[] = [];
  for (const t of text.split(/\s+/).filter(Boolean)) {
    if (GATE_SKIP_FLAGS.has(t.toLowerCase())) {
      gate = false;
      continue;
    }
    tokens.push(t);
  }

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

  let ref: string | undefined;
  if (tokens.length >= 3) {
    ref = tokens[2];
    // Loose git-ref check: rejects spaces, quotes, and shell metachars while
    // allowing normal branch names like feature/foo, release-1.2, v1.2.3.
    if (!/^[A-Za-z0-9._\/-]+$/.test(ref) || ref.length > 200) {
      return { error: `:x: Branch \`${ref}\` doesn't look like a valid git ref.` };
    }
  }

  return {
    android: platform === "android" || platform === "both",
    ios: platform === "ios" || platform === "both",
    version,
    ref,
    gate,
  };
}

// ─────────────────────────────────────────────────────────────
// /invite-to-repo handler
//
// Invites a GitHub user as a collaborator (write access) to the repo
// mapped to the current channel. No inviter allowlist — anyone in a
// mapped channel can invite. Reuses GITHUB_TOKEN; that token must have
// collaborator-management permission on the repo (classic `repo`
// scope, or a fine-grained PAT with Administration: read & write).
// ─────────────────────────────────────────────────────────────

async function handleInvite(
  text: string,
  repo: string,
  userId: string,
  token: string,
): Promise<Response> {
  const usage = "Usage: `/invite-to-repo <github-username>` — e.g. `/invite-to-repo octocat`.";

  // First whitespace-bounded token is the username; ignore anything trailing.
  const username = text.split(/\s+/).filter(Boolean)[0] ?? "";
  if (!username) {
    return slackReply(`:x: Missing GitHub username. ${usage}`, "ephemeral");
  }
  // GitHub usernames: 1–39 chars, alphanumeric with single internal hyphens.
  // Loose check here; GitHub rejects anything truly invalid with a 404 below.
  if (!/^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/.test(username)) {
    return slackReply(`:x: \`${username}\` doesn't look like a valid GitHub username. ${usage}`, "ephemeral");
  }

  const invite = await inviteCollaborator(repo, username, "push", token);

  switch (invite.status) {
    case 201:
      return slackReply(
        `:white_check_mark: <@${userId}> invited \`${username}\` to \`${repo}\` with *write* access. They'll get an email + GitHub notification to accept. <https://github.com/${repo}/invitations|Pending invites>`,
        "in_channel",
      );
    case 204:
      return slackReply(
        `:information_source: \`${username}\` already has access to \`${repo}\` — no invite needed.`,
        "ephemeral",
      );
    case 404:
      return slackReply(
        `:x: GitHub user \`${username}\` not found. Double-check the handle (it's their GitHub username, not their display name).`,
        "ephemeral",
      );
    case 403:
      return slackReply(
        `:lock: GitHub refused the invite (HTTP 403). The bot's token likely lacks collaborator/Administration permission on \`${repo}\`, or the org blocks outside collaborators.\n\`\`\`${invite.body.slice(0, 300)}\`\`\``,
        "ephemeral",
      );
    default:
      return slackReply(
        `:x: GitHub invite failed (HTTP ${invite.status}).\n\`\`\`${invite.body.slice(0, 400)}\`\`\``,
        "ephemeral",
      );
  }
}

// ─────────────────────────────────────────────────────────────
// GitHub add/invite collaborator
// PUT /repos/{owner}/{repo}/collaborators/{username}
//   201 → invitation created · 204 → already had access
// ─────────────────────────────────────────────────────────────

async function inviteCollaborator(
  repo: string,
  username: string,
  permission: "pull" | "triage" | "push" | "maintain" | "admin",
  token: string,
): Promise<{ status: number; body: string }> {
  const res = await fetch(
    `https://api.github.com/repos/${repo}/collaborators/${encodeURIComponent(username)}`,
    {
      method: "PUT",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "build-agent-slack-bot",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ permission }),
    },
  );

  const body = res.ok ? "" : await res.text();
  return { status: res.status, body };
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
