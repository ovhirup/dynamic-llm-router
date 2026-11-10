#!/usr/bin/env node
// DLR Claude Code PreToolUse guard hook (#101, hardened in #215). Claude Code runs this file only as a hook, so it
// always evaluates: there is no "am I the entrypoint?" check that a path with spaces or a symlink could defeat.
// Exit 2 blocks the tool call. Any failure (bad input, missing rules file, internal error) also exits 2: only
// exit 2 blocks in Claude Code, so every error path must end there. An allow prints nothing and grants nothing,
// so Claude Code's own permission rules still apply.
import fs from "node:fs";

function block(reason) {
  if (process.env.DLR_GUARD_LOG) {
    try {
      fs.appendFileSync(process.env.DLR_GUARD_LOG, `${new Date().toISOString()} blocked: ${reason}\n`);
    } catch {}
  }
  process.stderr.write(`dlr-claude-guard: ${reason}\n`);
  process.exit(2);
}

async function main() {
  const raw = fs.readFileSync(0, "utf8");
  if (!raw.trim()) block("empty hook event (failing closed)");
  let event;
  try {
    event = JSON.parse(raw);
  } catch {
    block("unparseable hook event (failing closed)");
  }
  const { evaluateEvent, jobRoot } = await import("./guard-lib.mjs");
  // Containment is checked against the job root, DLR_WORKTREE, never a later event.cwd; relative paths resolve
  // against the real cwd of this call. Without DLR_WORKTREE there is no trusted boundary: block everything.
  const root = jobRoot();
  if (!root) block("DLR_WORKTREE is not set: no trusted job root (failing closed)");
  const cwd = (typeof event?.cwd === "string" && event.cwd) || process.cwd();
  const verdict = evaluateEvent(event, root, cwd);
  if (!verdict.allow) block(verdict.reason);
  process.exit(0);
}

main().catch((err) => block(`internal error (failing closed): ${err?.message || err}`));
