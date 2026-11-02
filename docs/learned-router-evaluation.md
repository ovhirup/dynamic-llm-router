# Learned Router Evaluation: RouteLLM vs DLR Scorecard & Bandit Routing (#56)

Status: decided (31 Oct 2026). Parent: #52 (Model-news adaptation). *Note: learned-router frameworks may age quickly.*

## 1. Context & Constraints

RouteLLM (`lm-sys/RouteLLM`) trains matrix-factorization, BERT, or causal-LLM routers over preference data (such as Chatbot Arena) to route prompts between a strong model and a weak model at a given cost threshold $\tau$.

DLR operates under very different constraints:
- **Subscription seats, not per-token API keys:** DLR routes across fixed-subscription coding agent CLIs (`claude`, `codex`, `grok-build`, `cursor`) plus local models (`nativ`, `aider-ollama`), governed by live 5-hour/weekly/monthly usage meters and week-curve pacing.
- **16 GB RAM Apple Silicon M4 & zero dependencies:** No local model fine-tuning (`docs/ROUTER_BRAIN.md` section (g)), and no PyTorch/Transformers dependency in the router package (`dependencies = []`).
- **Verifiable outcomes:** DLR jobs end with objective pass/fail test results, diff-scope gates, and stronger-model review verdicts rather than subjective chat preference pairs.

## 2. Comparison

| Dimension | RouteLLM (`lm-sys/RouteLLM`) | DLR Scorecard + Bandit + Decision Shadow (#49, #68, #69, #114) |
|---|---|---|
| **Training data** | Static Chatbot Arena human preference pairs (general chat) | Live per-repo, per-class job outcomes (`tests_status == 'passed'`, diff scope, review verdict) |
| **Runtime footprint** | PyTorch / HuggingFace / OpenAI embedding calls | Pure Python stdlib (`scorecards.py`, `adaptive.py`, `decision_router.py`), 0 MB extra RAM |
| **Quota & cooldown awareness** | None (assumes pay-per-token API availability) | First-class meter freshness, 85% cap, week-curve pacing, and reset-bound cooldowns |
| **Adaptation to new models** | Requires re-collecting arena pairs and re-fitting router weights | Immediate via `capabilities.json` (#54) + UCB1 exploration with safety floor (#69) gated by `eval_harness` (#61) |

## 3. Decision

1. **Do not bundle or train RouteLLM inside DLR.** Static chat-arena routers require heavy ML dependencies, ignore subscription quota windows, and lag behind fast-moving coding CLIs.
2. **Use DLR's empirical scorecard + UCB1 bandit (`#49`, `#68`, `#69`) + one-pass `P(yes)` decision shadow (`#114`), gated by the evaluation harness (`#61` / `#66` / `#71`).**
3. **Adopt one idea from RouteLLM (`win_prob >= threshold`):** When routing between a strong worker and a cheaper/local worker, expose a configurable `escalation_threshold` $\tau \in [0, 1]$ over the empirical/estimated win probability so tasks with high local win probability stay on cheaper workers while harder tasks escalate to frontier workers.
