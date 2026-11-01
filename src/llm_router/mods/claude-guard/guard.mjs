#!/usr/bin/env node
// DLR Claude Code PreToolUse guard hook (#101). Fails closed on any error (.catch).
import fs from "node:fs";
import path from "node:path";

function isForbiddenPath(rawPath) {
  const text = String(rawPath || "").trim();
  if (!text) return null;
  const base = path.basename(text).toLowerCase();
  const norm = text.replace(/\\/g, "/");
  if (base === ".env" || (base.startsWith(".env.") && base !== ".env.example")) {
    return `blocked secret file access: ${base}`;
  }
  if (base.endsWith(".p8") || base === "auth.json") {
    return `blocked credential file access: ${base}`;
  }
  if (norm.includes("/.codex/") || norm.endsWith("/.codex") || text.startsWith("~/.codex")) {
    return "blocked access to ~/.codex";
  }
  if (norm.includes("/.config/llm-router/") || norm.endsWith("/.config/llm-router") || text.startsWith("~/.config/llm-router")) {
    return "blocked access to ~/.config/llm-router";
  }
  return null;
}

const FORBIDDEN_GIT = /\bgit\s+(?:\S+\s+)*push\b|\bgit\s+(?:\S+\s+)*reset\s+--hard\b|\bgit\s+(?:\S+\s+)*merge\b[^\n;|&]*\b(?:main|master)\b|\bgh\s+pr\s+merge\b|\bgh\s+pr\s+review\b[^\n;|&]*--approve\b/i;

export function evaluateEvent(event, worktree = process.env.DLR_WORKTREE || process.cwd()) {
  const toolName = String(event?.tool_name || "").toLowerCase();
  const input = event?.tool_input && typeof event.tool_input === "object" ? event.tool_input : {};
  const fileCandidate = input.file_path || input.path || input.notebook_path || "";
  const pathBlock = isForbiddenPath(fileCandidate);
  if (pathBlock) return { allow: false, reason: pathBlock };

  if (toolName === "bash" || typeof input.command === "string") {
    const cmd = String(input.command || "");
    if (FORBIDDEN_GIT.test(cmd)) {
      return { allow: false, reason: "blocked destructive git/gh command in headless Claude job" };
    }
    for (const token of cmd.split(/[\s"'=]+/)) {
      const tokenBlock = isForbiddenPath(token);
      if (tokenBlock) return { allow: false, reason: tokenBlock };
    }
    const rmMatch = cmd.match(/\brm\s+-(?:[A-Za-z]*r[A-Za-z]*f|[A-Za-z]*f[A-Za-z]*r|--recursive)\s+(.+)$/);
    if (rmMatch) {
      const wt = path.resolve(worktree);
      const targets = rmMatch[1].split(/\s+/).filter((t) => t && !t.startsWith("-"));
      for (const target of targets) {
        const cleaned = target.replace(/^['"]|['"]$/g, "");
        const resolved = path.resolve(wt, cleaned);
        if (resolved === wt || !resolved.startsWith(wt + path.sep)) {
          return { allow: false, reason: `blocked rm -rf outside worktree: ${cleaned}` };
        }
      }
    }
  }
  return { allow: true, reason: null };
}

async function main() {
  const raw = fs.readFileSync(0, "utf8");
  const event = raw.trim() ? JSON.parse(raw) : {};
  const verdict = evaluateEvent(event);
  if (!verdict.allow) {
    if (process.env.DLR_GUARD_LOG) {
      fs.appendFileSync(process.env.DLR_GUARD_LOG, `${new Date().toISOString()} blocked: ${verdict.reason}\n`);
    }
    process.stderr.write(`dlr-claude-guard: ${verdict.reason}\n`);
    process.exit(2);
  }
  process.stdout.write(JSON.stringify({ decision: "allow" }) + "\n");
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => {
    process.stderr.write(`dlr-claude-guard internal error (failing closed): ${err?.message || err}\n`);
    process.exit(2);
  });
}
