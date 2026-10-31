# DLR Router Brain: parallel dispatch, handoffs, second brain, Jev

Status: proposal (31 Oct 2026). Board: https://github.com/users/ovhirup/projects/2

## Principle
No capable worker sits idle while it has quota and there is eligible work. Grok Bot orchestrates; workers
(Claude personal Pro > Grok Build > Codex > Cursor (after 3 Nov) > local Nativ/aider-ollama) execute in parallel.

## (a) Parallel dispatch
- **Decompose** each job into independent units (issue/sub-issue = unit) with declared files/paths, class
  (code/review/docs/research), size, and dependencies (DAG). Units touching the same files are serialized.
- **Assign** with a score per (unit, worker): capability for class (scorecard) x remaining-quota headroom vs
  week-curve pace x freshness of meter reading. Highest quality first; overflow goes to the next eligible worker
  instead of waiting.
- **Concurrency caps** per worker (config): Claude 2-3, Grok Build 2, Codex 2, Cursor 2, local 1 (M4 16 GB fits one
  large local model; never two).
- **Isolation:** one git worktree + branch per unit (`dlr/<issue>-<worker>`). Never push.
- **Merge queue:** finished units go to a local queue; tests + diff-scope gate + stronger-model review (#12, #13), then
  a human "yes" before any push/merge.
- Prior art: Claude Code creator runs ~5 terminal + 5-10 browser sessions in parallel (bookmark below);
  LiteLLM Router (load balancing, cooldowns, fallbacks) https://docs.litellm.ai/docs/routing ;
  OpenRouter provider fallback https://openrouter.ai/docs .

## (b) Meter watching and mid-task handoff
- Poll meters (#20-#24) before dispatch and every N minutes during a run; switch when headroom < threshold,
  on 429/quota errors (cooldown until reset_at, #10) or on repeated failure.
- **Handoff note** (every worker writes it at checkpoint and on stop), `handoffs/<job>/<ts>-<worker>.md`:
  Goal · Done · Next steps · Files touched · Test status (command + result) · Gotchas/decisions · Meter at stop.
- Router validates the note (schema check), then re-dispatches the unit to the next eligible worker in the same
  worktree with the note as the first prompt context.

## (c) Second brain: needed? Yes, lightweight.
Recommendation: an **Obsidian vault as plain markdown in git** (`~/dev/dlr-brain`), written by the router, read by
workers via an MCP filesystem/Obsidian server. Not a heavy memory DB yet.
- Folders: `jobs/` (outcome per job), `handoffs/`, `scorecards/` (worker x class: pass rate, review fixes, tokens/unit),
  `research/` (model-news digests), `rules.md` (10-20 always-loaded rules; wiki pages pulled on demand, the
  two-layer pattern from the bookmarked llm-wiki repos).
- Claims need a source (the "vouch" idea from bookmarks): capability-table changes must cite an eval run or URL.
- Later, only if search over the vault becomes the bottleneck: temporal knowledge graph (Graphiti/Zep
  https://github.com/getzep/graphiti) or Letta memory (https://github.com/letta-ai/letta). *May age quickly.*

### Adapting to new breakthroughs
Weekly model-news digest (X bookmarks + release notes) -> candidate updates to `capabilities.json` ->
run the DLR eval set (fixed tasks per class, judged by tests + stronger-model review) -> accept only measured wins.
Never route on hype alone. Learned routers (RouteLLM https://github.com/lm-sys/RouteLLM) are an option once enough
scorecard data exists.

## (d) Jev
Jev is TypeSafe AI's "System One Model" (https://typesafe.ai/): returns **typed decisions with calibrated
confidence**, very cheap ($0.042/M input per their site; vendor claims, unverified). Found via bookmark
https://x.com/Mr_Buzzoni/status/2115527626806763917 (Jev as the "reader that decides what matters" over an Obsidian vault).
**Fit:** a candidate for the router's cheap decision layer: classify task class/size, pick worker, decide
"auto-proceed vs escalate" by confidence threshold, triage which vault notes matter. Not a coding worker.
Plan: spike behind an interface with a local/LLM fallback; adopt only if it beats current heuristic on DLR evals.
Paid API key needed (user decision). *May age quickly.*

## (e) Roadmap
- **Now:** finish #4 strategy (#6 grok-bot worker, #7 rework), meters (#20-#22), cooldown/failover (#10, #11),
  handoff note schema + writer, vault skeleton.
- **Next:** parallel scheduler with caps + worktree per unit + local merge queue; scorecards; model-news digest +
  eval set; Jev spike.
- **Later (may age quickly):** learned router (RouteLLM-style), graph memory (Graphiti/Letta), Hark voice input
  to create issues, auto-decomposition by an LLM planner.

## Bookmark sources
- Second-brain repos list: https://x.com/imryven/status/2115936477171392905
- Obsidian + Opus + Jev loop: https://x.com/Mr_Buzzoni/status/2115527626806763917
- Parallel Claudes (10 levels): https://x.com/Slonski_rt/status/2115519302338281801
- Grok Bots incl. outer/inner loop + Ship Evidence Auditor: https://x.com/maestrooth/status/2115548587174068225
- Claude Code skill repos: https://x.com/neerajjj6785/status/2115683570886828270

## (f) Evaluation harness (ranks above self-learning)
Replay real past router jobs (`~/.local/state/llm-router/logs/jobs`) against any worker or routing policy.
Score quality (tests pass, diff scope, reviewer score), cost (tokens / quota %) and time. Modes: offline (fake
workers) and live. Output: policy A/B report. **Gate:** no routing/config change is adopted unless it beats the
baseline in the harness. Related: #26 #55 #12 #13 #60.

## (g) Self-learning router (no fine-tuning)
Learns from its own outcomes: feedback loop updates worker scorecards; adaptive routing weights (bandit-style
exploration with a safety floor); drift detection when a worker's quality drops; every weight change validated by
the harness before applying; human-readable change log in the Obsidian vault. **No local model fine-tuning**
(16 GB Mac, and frontier models outpace it). Related: #49 #56 #53-#55.

## (h) External tool integrations (plugins)
Once public, DLR lets users plug in productivity tools over MCP via a simple plugin manifest: name, capabilities
(input source / context / notifier / output sink), and how to call it. Reference plugins: Hark (voice dictation and
meeting transcripts become task files, searchable via hark-mcp; #19), the Obsidian vault (#50), notifiers
(Slack/email summaries). Per-plugin opt-in consent and permissions; no secrets in logs.

## (i) Notch actionable approvals
Code lives outside this repo: `~/dev/notch-dashboard` (ovhirup/notch-dashboard; `RouterNotchCore/OrchestrationLogic.swift`
banners, `ProjectsLogic.swift` counts, `RouterNotch/ActivityWorkspace.swift`, `RequestCenter.swift`) and `~/dev/router-live`
(DLR Live iPhone; `RouterLiveCore/RejectAction.swift`, `RouterLiveRelayKit/RequestInbox.swift`, `PushPolicy.swift`).
Plan: approval rows deep-link to the decision owner (Grok Bot chat vs worker job/worktree/log); low-risk items get inline
Approve/Reject with a one-line summary; high-risk actions (merge, push, PR, config/secrets edits, spend, deletes) are never
one-tap: full context/diff + confirm; audit log to job log + vault; actionable notices (Grok Bot fallback / wait until
reset; meter heads-up -> Usage tab); same on iPhone with Face ID; blocked items show reason + one next step; undo/expiry.
