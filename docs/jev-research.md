# Jev (TypeSafe AI) Research: API, Pricing & Data Policy (#58)

Status: research complete (31 Oct 2026). Parent: #57 (Jev decision layer spike). Related: #59 (Prototype behind interface), #60 (Compare Jev vs heuristic on DLR evals), #114 (Decision-model router). *Note: vendor claims and pricing may age quickly.*

## 1. What Jev Is

Jev (`https://typesafe.ai/`) is TypeSafe AI's "System One" typed-decision model. Instead of autoregressive free-text generation, Jev takes a structured schema (enum choices or boolean predicates) and returns a **typed decision with calibrated confidence probabilities** (`choice`, `confidence` $\in [0, 1]$, and per-option probability distribution) in a single forward pass.

## 2. API Shape

- **Input:** context text + a closed set of typed options (e.g. `task_class` $\in \{\text{feature}, \text{bugfix}, \text{review}, \text{mechanical}, \text{draft}\}$, `size` $\in \{\text{draft}, \text{full}\}$, or `action` $\in \{\text{auto\_proceed}, \text{escalate}\}$).
- **Output:**
  ```json
  {
    "choice": "bugfix",
    "confidence": 0.91,
    "probabilities": {
      "bugfix": 0.91,
      "feature": 0.05,
      "review": 0.02,
      "mechanical": 0.01,
      "draft": 0.01
    }
  }
  ```
- **Role in DLR:** candidate for the cheap decision layer (task class + size classification, escalate-by-confidence threshold, and vault note triage), **never** a coding worker.

## 3. Pricing (Vendor Claims, Unverified)

- **Reported rate:** ~$0.042 / 1M input tokens (per vendor site / bookmark `https://x.com/Mr_Buzzoni/status/2115527626806763917`).
- **Implication for DLR:** requires a paid API key (`JEV_API_KEY` in the user's environment, opt-in only). Because DLR's default policy never spends paid API credits without explicit user opt-in, Jev must sit behind a local/heuristic fallback interface (`#59`) and default to `enabled: false`.

## 4. Data & Privacy Policy Requirements for DLR

1. **Opt-in only (`jev.enabled: false` by default):** No task text or vault note is ever sent to a remote Jev endpoint unless the user explicitly enables `jev.enabled` and configures `jev.endpoint` + `jev.api_key_env`.
2. **Pre-send redaction & leak gate:** Any prompt snippet sent to an external decision endpoint must first pass through `security.redact` and private-term filtering (`private_terms.txt`), stripping secrets and private repo identifiers before leaving the machine.
3. **No-redirect, no-proxy HTTP:** Same hardening as `developer_draft` (`NoRedirect`, `ProxyHandler({})`, strict timeout, key read only from `os.environ[api_key_env]`).
4. **Adoption gate (#60 / #61):** Jev is adopted as the active classifier only if it beats the zero-cost local heuristic on the DLR evaluation harness (`#60`).
