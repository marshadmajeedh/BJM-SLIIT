# ChangeGuard AI — Module 02 Live LLM Integration Verification Report (P0-4)

**Project:** ChangeGuard AI  
**Team:** BJM-SLIIT  
**Module:** M02 — Multi-Agent Orchestration & Backend (Owner: Marsadh)  
**Branch:** `test/m02-live-llm-integration`  
**Base Commit:** `c7a5ecfb264d6b0ef5a8504dfe91d039aa53cb89` (PR #12 — P0-3 Offline End-to-End Verification; intermediate commit `c35334d` also present on branch)  
**Configured Provider & Model:** Groq (`groq 1.7.0`) — `openai/gpt-oss-120b` (`model_label: "groq:openai/gpt-oss-120b"`)  
**Verification Verdict:** **`M02 LIVE INTEGRATION VERIFIED WITH TEST QUALITY LIMITATIONS`**

---

## Executive Summary

Live end-to-end verification confirms that the ChangeGuard AI Module 02 orchestration engine (`project/main.py`, `project/api/`, `project/marsadh_orchestrator/`) integrates with the real Groq API via Munshif's unmodified M03 adapter (`project/agents/munshif_testing/groq_model.py` and `agent.py`), executes real M01 static impact analysis and M04 `pytest` verification over isolated trusted Git fixtures, enforces both human review gates (including across server restarts), blocks ordinary final approval with HTTP `409 Conflict` when verification fails, and never silently falls back to offline mock output.

Using the project-owner-configured model **`openai/gpt-oss-120b`**, live-generated behavioral tests (`SCN-001`, `SCN-002`) **detected the known regular-customer discount regression** on Scenario B (`2 passed, 2 failed`, `exit_code=1`) and verified core `billing.py` behavior on Scenario A regeneration (`7/7 passed`, `exit_code=0`).

However, two genuine **AI-generated test quality limitations** were observed during live execution:
1. **Generated-test syntax defect (Scenario A, Attempt 1):** The model emitted unparseable Python inside JSON `test_code` (`SyntaxError: unexpected character after line continuation character (<unknown>, line 1)`), which M03's `ast.parse()` rejected before export/execution.
2. **Generated-test import compatibility defect (Scenario A Attempt 2 & Scenario B):** In both successful live generations, 3 of 5 scenarios (`SCN-003` `refunds`, `SCN-004` `dashboard`, `SCN-005` `reports`) imported modules as `mini_pos.<module>` without adding `mini_pos/` to `sys.path`, conflicting with `mini_pos`'s internal flat top-level imports (`from billing import ...`, `from customer_service import ...`). Human review caught and excluded those 3 scenarios at Gate 1, leaving downstream modules unverified and motivating a Gate 2 decision of `NEEDS_REVIEW` on Scenario A.

Accordingly, the verified verdict is **`M02 LIVE INTEGRATION VERIFIED WITH TEST QUALITY LIMITATIONS`**.

---

## 1. Environment Configuration & Provider Integration Audit

### 1.1 Supported Configuration Mechanism
- Both `OrchestratorConfig.from_env()` (`project/marsadh_orchestrator/config.py`) and Munshif's `groq_model.generate()` (`project/agents/munshif_testing/groq_model.py`) read **process environment variables only**:
  - `GROQ_API_KEY` (stored as Pydantic `SecretStr` with `exclude=True, repr=False` in `OrchestratorConfig`)
  - `GROQ_MODEL` (`openai/gpt-oss-120b` for this verification)
- **`.env` files are NOT auto-loaded:** `python-dotenv` is not in `project/requirements.txt` and not installed in `.venv`.
- **Git Secret Exclusion:** `.env` is excluded in `.gitignore`; `.github/workflows/ci.yml` and `tests/test_ci_smoke.py` reject committed `.env` files while permitting `.env.example`. A safe template was added at `.env.example`.

### 1.2 Safe Windows PowerShell 5.1 Setup & Startup Instructions
Never paste API keys into source files, documentation, logs, or chat transcripts. Set variables in your local PowerShell session (or User scope for multi-session runs):

```powershell
# Interactive secure prompt compatible with Windows PowerShell 5.1 (never echoes key)
$sec = Read-Host "Groq API Key" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
try {
    $env:GROQ_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr).Trim()
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    Remove-Variable sec -ErrorAction SilentlyContinue
}
$env:GROQ_MODEL = "openai/gpt-oss-120b"

# Verify presence safely without printing the key
"GROQ_API_KEY set=$([bool]$env:GROQ_API_KEY), len=$($env:GROQ_API_KEY.Length), model=$env:GROQ_MODEL"

# Configure trusted local fixture roots and start loopback-only backend
$env:CHANGEGUARD_ALLOWED_REPO_ROOTS = "<trusted-temp-repos-root>"
$env:CHANGEGUARD_TRUSTED_EXECUTION_ROOTS = "<trusted-temp-repos-root>"
$env:CHANGEGUARD_CONFIGURED_REPOSITORIES = "pos-correct=<repo-a>,pos-regressive=<repo-b>"
.\.venv\Scripts\python.exe -m uvicorn project.main:app --host 127.0.0.1 --port 8000
```

### 1.3 Existing M03 Groq Adapter Audit (`project/agents/munshif_testing/groq_model.py`)
Reused **unmodified** via lazy import inside `TestingAdapter._resolve_live_model()`:

| Property | Verified Behavior |
| :--- | :--- |
| Callable signature | `generate(prompt) -> str \| None` (returns `response.choices[0].message.content`) |
| Provider SDK | `groq 1.7.0` (`from groq import Groq`, lazy-imported only when `live_llm` runs) |
| Client timeout & retries | `Groq(api_key=..., timeout=60, max_retries=2)` inside SDK; **zero** outer retry loop in M02 (`call_count == 1`) |
| Completion parameters | `model=GROQ_MODEL`, `response_format={"type": "json_object"}`, `temperature=0.1` |
| Response validation | `json.loads(...)` + non-empty `scenarios` + required string fields + `ast.parse(test_code)` + top-level `def test_*` check (`agent.py`) |
| Silent fallback check | **Fail-closed:** `execution_mode="live_llm"` never falls back to offline mock output |
| Concurrency behavior | `TestingAdapter` wraps `groq_generate(prompt)` in `_GROQ_ENV_LOCK`, safely isolating and restoring `GROQ_API_KEY`/`GROQ_MODEL` across threads (serializes concurrent live Groq calls within a process) |
| Token usage metadata | **Unavailable** through current callable signature (`groq_model.generate` returns plain `str` and discards `response.usage`) |

### 1.4 Live Pre-Flight & `GET /health` Verification
- Read-only `groq.Groq(...).models.list()` verified provider authentication (`11` models accessible; `openai/gpt-oss-120b` confirmed present).
- Live `GET http://127.0.0.1:8000/health` returned `200 OK`:
```json
{
  "status": "ok",
  "service": "ChangeGuard AI API",
  "version": "0.2.0-mvp",
  "environment": "development",
  "database_available": true,
  "live_llm_configured": true,
  "groq_model": "openai/gpt-oss-120b",
  "verification_dependencies_available": true,
  "git_available": true,
  "configured_repository_ids": ["pos-correct", "pos-regressive"],
  "execution_model": "in_process_bounded_threadpool"
}
```
*(No credential appeared in `/health` response text.)*

---

## 2. Trusted Demonstration Fixtures & Live Workflow Requests

Two isolated temporary Git repositories were created under `%TEMP%\changeguard_p04_live_session\repos\` using the exact 5-module `sample_projects/mini_pos` structure (`billing.py`, `refunds.py`, `customer_service.py`, `dashboard.py`, `reports.py`), leaving the ChangeGuard repository completely untouched:
- **Requirement:** *"Apply a 10% discount to premium customers only, without changing regular customer checkout or breaking dependent refund, dashboard and report modules."*
- **Scenario A (`pos-correct`, base commit `25bc26767b30`):** `billing.apply_discount` returns `total * 0.90` if `customer_type == 'premium'` else `total`.
- **Scenario B (`pos-regressive`, base commit `e622f16c7e92`):** `billing.apply_discount` unconditionally returns `total * 0.90` (erroneously discounting regular customers).

**Live Workflow Request (`POST /api/runs`):**
```json
{
  "repository_id": "pos-correct",
  "source_path": "mini_pos",
  "base_ref": "HEAD",
  "requirements": "Apply a 10% discount to premium customers only, without changing regular customer checkout or breaking dependent refund, dashboard and report modules.",
  "execution_mode": "live_llm"
}
```

---

## 3. Live Multi-Agent Execution Results

All three persisted live runs executed against `http://127.0.0.1:8000` with `execution_mode="live_llm"`, `is_mock_output=false`, and `model_label="groq:openai/gpt-oss-120b"`:

| Run ID | Scenario | M03 Live Duration | Stage 1 Result | Human Gate 1 Selection | M04 Pytest Result | Gate 2 Decision | Final State |
| :--- | :--- | ---: | :--- | :--- | :--- | :--- | :--- |
| `run_20261127T063745Z_638d5329d8ca` | Scenario A (Attempt 1 — Correct) | `4,188 ms` | `FAILED` (`SyntaxError` in `test_code`) | N/A (failed before Gate 1) | Not executed | N/A | `FAILED` |
| `run_20261127T063750Z_8209da97d451` | Scenario B (Regressive) | `3,652 ms` | `AWAITING_TEST_REVIEW` (5 scenarios) | Approved `SCN-001`, `SCN-002`; Excluded `SCN-003`–`SCN-005` | **`2 passed, 2 failed`** (`exit_code=1`, `0.076 s`) | `approve` blocked (`409`); submitted `reject` | `REJECTED` |
| `run_20261127T064238Z_32be3b110850` | Scenario A (Authorized Regeneration — Correct) | `3,709 ms` | `AWAITING_TEST_REVIEW` (5 scenarios) | Approved `SCN-001`, `SCN-002`; Excluded `SCN-003`–`SCN-005` | **`7 passed, 0 failed`** (`exit_code=0`, `0.032 s`) | Submitted `needs_review` (incomplete downstream coverage) | `NEEDS_REVIEW` |

### 3.1 Initial Scenario A Failure (Generated-Test Syntax Defect)
- **Run ID:** `run_20261127T063745Z_638d5329d8ca` (preserved intact in SQLite; never deleted or overwritten).
- **M01 Analysis (`194 ms`):** Detected `mini_pos/billing.py` (`apply_discount`) and all 4 impacted modules (`refunds.py`, `reports.py`, `customer_service.py`, `dashboard.py`).
- **M03 Live Groq Result (`4,188 ms`, wall `5.458 s`):**
  - Provider authentication and HTTP completion **succeeded**, returning JSON from `openai/gpt-oss-120b`.
  - However, `agent.py`'s `ast.parse(scenario["test_code"])` raised `SyntaxError: unexpected character after line continuation character (<unknown>, line 1)` (a generated-test formatting defect where the model emitted literal backslash escapes inside JSON-encoded Python source).
  - M02 persisted `error.code="M03_GENERATION_ERROR"`, `details={"provider_error_type": "SyntaxError"}`, `retryable=false`, `is_mock_output=false`, `scenarios=[]`, logged 10 structured entries, and transitioned cleanly to `FAILED` without silently retrying or falling back to mock output.

### 3.2 Scenario B — Regressive Implementation (Known Incorrect Behavior Detected)
- **Run ID:** `run_20261127T063750Z_8209da97d451`
- **M01 Analysis (`175 ms`):** Detected `mini_pos/billing.py` (`apply_discount`) and all 4 impacted modules.
- **M03 Live Groq Generation (`3,652 ms`, wall `4.941 s`):** Produced 5 unexecuted scenarios (`SCN-001`–`SCN-005`) and paused at `AWAITING_TEST_REVIEW` (`executed=false`, `verification=null`).
- **Human Review (Gate 1 — resumed across server restart):**
  - Verified exact stored `content_sha256`, syntax validity, meaningful assertions, and safety of `SCN-001` and `SCN-002`.
  - Approved **only `SCN-001` and `SCN-002`** (`POST /test-review` -> `202 Accepted`; exported 2 files in `22 ms`).
  - Excluded `SCN-003`, `SCN-004`, and `SCN-005` due to `mini_pos` flat-import compatibility defects (original generated content preserved unapproved in SQLite).
- **M04 Real Pytest Verification (`711 ms` adapter / `0.076 s` pytest / `1.164 s` wall):**
  - `execution_status="completed"`, `verification_outcome="failed_tests"`, `verification_passed=false`, `exit_code=1`, `total_tests=4`, `passed=2`, `failed=2`.
  - **Passed (2):** `test_apply_discount_premium`, `test_checkout_premium`.
  - **Failed (2 — Known Regular-Customer Regression Detected):**
    1. `.changeguard_generated_tests/test_generated_001.py::test_apply_discount_regular` — `AssertionError`: Obtained `90.0`, Expected `100.0 ± 1.0e-04`.
    2. `.changeguard_generated_tests/test_generated_002.py::test_checkout_regular` — `AssertionError`: Obtained `54.0`, Expected `60.0 ± 6.0e-05`.
- **Gate 2 Enforcement & Final Decision:**
  - `report.recommendation="reject"`, `recommendation_label="verification_failed"`, `allowed_actions=["submit_final_decision:reject", "submit_final_decision:needs_review"]`.
  - Attempted ordinary approval (`POST /decision` with `decision="approve"`) was **blocked with HTTP `409 Conflict`** (`code="VERIFICATION_APPROVAL_BLOCKED"`).
  - Final decision `decision="reject"` succeeded (`200 OK`, terminal state `REJECTED`, `package_id=pkg_run_20261127T063750Z_8209da97d451_d96a9868`).
  - Communication logs: **20 structured UTC entries** across all 8 orchestrator/agent/gate components.

### 3.3 Scenario A — Authorized Regeneration (Correct Implementation)
- **Run ID:** `run_20261127T064238Z_32be3b110850` (one authorized regeneration call; original failed run preserved).
- **M01 Analysis (`115 ms`):** Detected `mini_pos/billing.py` (`apply_discount`) and all 4 impacted modules.
- **M03 Live Groq Generation (`3,709 ms`, wall `4.358 s`):** Produced 5 unexecuted scenarios (`SCN-001`–`SCN-005`) and paused at `AWAITING_TEST_REVIEW`.
- **Human Review (Gate 1 — resumed across server restart):**
  - Verified exact stored SHA-256 hashes (`cd25462e...` and `03db9bc2...`), syntax validity, import compatibility, and safety.
  - Approved **only `SCN-001` and `SCN-002`** (`POST /test-review` -> `202 Accepted`; exported 2 files in `23 ms`).
  - Excluded `SCN-003`, `SCN-004`, and `SCN-005` due to the same `mini_pos` flat-import compatibility defect.
  - Explicitly distinguished confirmed requirements (premium 10% discount, regular customer unchanged) from model-inferred assumptions (`'gold'` unknown customer type, zero total, zero quantity).
- **M04 Real Pytest Verification (`548 ms` adapter / `0.032 s` pytest / `0.754 s` wall):**
  - `execution_status="completed"`, `verification_outcome="passed"`, `verification_passed=true`, `exit_code=0`, `total_tests=7`, `passed=7`, `failed=0`.
  - **Passed (7):** `test_apply_discount_premium`, `test_apply_discount_regular`, `test_apply_discount_unknown_type`, `test_apply_discount_zero_total`, `test_checkout_premium`, `test_checkout_regular`, `test_checkout_zero_quantity`.
- **Change Package & Gate 2 Decision (`NEEDS_REVIEW`):**
  - `report.recommendation="needs_review"`, `recommendation_label="ready_for_human_review"`, `traceability.requirement_verification_proven=false`, `traceability.unverified_scenario_ids=["SCN-003", "SCN-004", "SCN-005"]`, `baseline_comparison.status="unavailable"`.
  - Final decision recorded as **`needs_review`** (`200 OK`, terminal state `NEEDS_REVIEW`, `package_id=pkg_run_20261127T064238Z_32be3b110850_88fbe05d`) because downstream `refunds`, `customer_service`, `dashboard`, and `reports` behaviors remained unverified and baseline regression comparison is unavailable.
  - Communication logs: **20 structured UTC entries**.

---

## 4. Phase 5 — AI-Generated Test Quality Assessment

### 4.1 Classification of All Live-Generated Scenarios

| Run | ID | Target API | Classification | Gate 1 Decision | Findings |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Scenario B | `SCN-001` | `mini_pos.billing.apply_discount` | **Meaningful behavioral test** | **Approved** | Valid import, `pytest.approx` assertions for premium (`90.0`) & regular (`100.0`), safe, caught regression. |
| Scenario B | `SCN-002` | `mini_pos.billing.checkout` | **Meaningful behavioral test** | **Approved** | Valid import, `pytest.approx` assertions for premium (`54.0`) & regular (`60.0`), safe, caught regression. |
| Scenario B | `SCN-003` | `mini_pos.refunds.calculate_refund` | **Invalid test (import defect)** | **Excluded** | Meaningful assertions & safe, but `mini_pos/refunds.py` uses flat `from billing import apply_discount` -> `ModuleNotFoundError` without `mini_pos/` on `sys.path`. |
| Scenario B | `SCN-004` | `mini_pos.dashboard.display_refund` | **Invalid test (import defect)** | **Excluded** | Meaningful assertions & safe, but `mini_pos/dashboard.py` uses flat `from customer_service import ...` -> `ModuleNotFoundError`. |
| Scenario B | `SCN-005` | `mini_pos.reports.generate_sales_report` | **Invalid test (import defect)** | **Excluded** | Meaningful assertions & safe, but `mini_pos/reports.py` uses flat `from billing import checkout` -> `ModuleNotFoundError`. |
| Scenario A (Retry) | `SCN-001` | `mini_pos.billing.apply_discount` | **Meaningful behavioral test** *(with unconfirmed edge-case assumptions)* | **Approved** | Valid import, 4 tests (`premium`, `regular`, unknown `'gold'`, `0` total). `'gold'` and `0` are model-inferred assumptions, not confirmed requirements. |
| Scenario A (Retry) | `SCN-002` | `mini_pos.billing.checkout` | **Meaningful behavioral test** *(with unconfirmed edge-case assumption)* | **Approved** | Valid import, 3 tests (`premium`, `regular`, `0` quantity). Zero-quantity behavior is an inferred assumption. |
| Scenario A (Retry) | `SCN-003` | `mini_pos.refunds.calculate_refund` | **Invalid test (import defect)** | **Excluded** | Same `mini_pos` flat-import clash (`from billing import apply_discount`). |
| Scenario A (Retry) | `SCN-004` | `mini_pos.dashboard.display_refund` | **Invalid test (import defect)** | **Excluded** | Same `mini_pos` flat-import clash (`from customer_service import ...`). |
| Scenario A (Retry) | `SCN-005` | `mini_pos.reports.generate_sales_report` | **Invalid test (import defect)** | **Excluded** | Same `mini_pos` flat-import clash (`from billing import checkout`). |

### 4.2 Observed Model Strengths & Weaknesses
- **Strengths:** Zero meaningless `assert True` tests; zero fabricated APIs; zero unsafe filesystem/network/subprocess operations; zero mocking of code under test; genuine `pytest.approx` behavioral assertions; accurately identified the business rule and caught the regular-customer discount regression on Scenario B.
- **Weaknesses:**
  1. **Non-deterministic JSON string escaping (`SyntaxError`):** 1 of 3 live calls produced unparseable Python inside JSON `test_code`.
  2. **Flat-import vs. package-import blindness:** On all downstream modules (`refunds`, `dashboard`, `reports`), the model wrote `from mini_pos.<mod>` without adjusting `sys.path` for `mini_pos`'s internal flat `from billing import ...` imports.
  3. **Missing edge-case coverage:** No direct `customer_service.process_customer_refund` or `billing.calculate_total` unit tests; no floating-point rounding policy tests (`0.99 * 100`); no case-sensitivity (`'Premium'`) or negative-amount checks.
  4. **Mandatory human review validated:** Without Gate 1 human review, executing `SCN-003`–`SCN-005` would have caused pytest `COLLECTION_ERROR` (`exit_code=2`), masking the clean regression signal from `SCN-001`–`SCN-002`.

---

## 5. Distinction of Failure & Outcome Categories

| Category | Observed Evidence in P0-4 |
| :--- | :--- |
| **Provider authentication success** | `models.list()` succeeded (11 models, `openai/gpt-oss-120b` present); 3 live chat completions succeeded at HTTP 200. (Earlier 1-char key misconfiguration failed cleanly at HTTP 400 `BadRequestError` without mock fallback.) |
| **Model generation success** | 2 of 3 live calls produced valid, AST-parseable 5-scenario payloads (`3,652 ms`, `3,709 ms`). |
| **Generated-test syntax defect** | Scenario A Attempt 1 (`run_20261127T063745Z_638d5329d8ca`): `SyntaxError` in `test_code` caught by M03 `ast.parse()`. |
| **Generated-test import defect** | `SCN-003`, `SCN-004`, `SCN-005` in both Scenario B and Scenario A Retry: `from mini_pos.<mod>` incompatible with internal flat imports. |
| **Application regression** | Scenario B (`run_20261127T063750Z_8209da97d451`): `test_apply_discount_regular` (`90.0 != 100.0`) and `test_checkout_regular` (`54.0 != 60.0`) failed on `pos-regressive`. |
| **Human review decisions** | Gate 1 excluded defective downstream tests; Gate 2 recorded `REJECTED` on Scenario B (after HTTP 409 blocked `approve`) and `NEEDS_REVIEW` on Scenario A due to incomplete downstream coverage. |

---

## 6. Offline Failure Handling & Security Verification (Phases 7 & 8)

All 10 Phase 7 failure scenarios and security invariants are covered offline via controlled mocks in `tests/test_m02_live_llm_integration.py` (never requiring real Groq credentials in CI):

1. Missing `GROQ_API_KEY` (`LIVE_LLM_UNCONFIGURED`, `is_mock_output=false`, no fallback).
2. Missing `GROQ_MODEL` (`LIVE_LLM_UNCONFIGURED`, API key not leaked in `/health` or errors).
3. Invalid model responses (`None`, non-dict JSON `[]`, missing scenario fields, missing `def test_*`, Python `SyntaxError`).
4. Provider authentication failure (`groq.AuthenticationError` HTTP 401, `retryable=false`, secrets redacted, `call_count == 1`).
5. Rate limit (`groq.RateLimitError` HTTP 429, `retryable=true`, `call_count == 1`).
6. Provider timeout (`groq.APITimeoutError` and `TimeoutError`, `retryable=true`, `call_count == 1`).
7. Invalid JSON (`INVALID_MODEL_JSON`, `retryable=true`, secret redacted).
8. Empty test scenarios (`INVALID_MODEL_OUTPUT`).
9. Unsupported / decommissioned model (`groq.NotFoundError`, `groq.BadRequestError`, `retryable=false`).
10. Deeply nested secret redaction (recursive dict/list redaction + `Bearer` token + `gsk_*` + `api_key=...` patterns verified across `RunStore`, `GET /api/runs/{run_id}`, `GET /api/runs/{run_id}/logs`, and raw SQLite database bytes).

**Live Session Security Audit:**
- Zero occurrences of `GROQ_API_KEY` in `/health`, `GET /api/runs/{run_id}`, `GET /api/runs/{run_id}/logs`, uvicorn server logs, or raw SQLite file bytes.
- Original temporary fixtures (`pos_correct`, `pos_regressive`) and main ChangeGuard repository remained completely unchanged (zero automatic Git commits, merges, or pushes).
- Temporary session directory `%TEMP%\changeguard_p04_live_session` was cleaned up after preserving sanitized evidence.

---

## 7. Available vs. Unavailable Metrics

| Metric | Status | Observed Value |
| :--- | :--- | :--- |
| Model identifier | Available | `groq:openai/gpt-oss-120b` |
| Live M03 response durations | Available | `4,188 ms` (A1 `SyntaxError`), `3,652 ms` (B), `3,709 ms` (A2) |
| M04 pytest durations | Available | `0.076 s` (B, 4 tests), `0.032 s` (A2, 7 tests) |
| Token usage (prompt / completion / total) | **Unavailable** | `groq_model.generate()` returns plain `str` and discards `response.usage` |
| Monetary API cost | **Unavailable** | Provider usage metadata not exposed by M03 callable |
| Code coverage percentage | **Unavailable** | Not measured in M02 MVP (`coverage_status="unavailable"`) |
| Baseline-vs-modified regression delta | **Unavailable** | Not executed in M02 MVP (`baseline_comparison.status="unavailable"`) |
| AI root-cause analysis | **Unavailable** | Deterministic keyword diagnostics only (`ai_root_cause_status="unavailable"`) |

---

## 8. Reproducibility & Remaining Limitations

### Reproducibility
- **Offline CI verification (no credentials required):**
  ```powershell
  .\.venv\Scripts\python.exe -m pytest -v --tb=short
  ```
- **Manual live Groq verification (requires explicit opt-in & process env vars):**
  ```powershell
  .\.venv\Scripts\python.exe scripts/demo_m02_live_llm.py --confirm-live-calls
  ```

### Remaining Project Gaps & Limitations
1. **Import-path guidance / execution context:** `mini_pos` uses flat sibling imports, whereas `run_reviewed_tests` executes `pytest` from repository root without `source_path` on `PYTHONPATH`; future coordination with M03/M04 is needed to align generated import paths or test execution `PYTHONPATH`.
2. **Token usage observability:** Returning structured metadata (`content`, `usage`, `model`) from M03's Groq adapter requires a coordinated M03 contract update.
3. **Structured Requirement Analysis (A1) missing:** Natural-language requirements are not decomposed into verifiable acceptance criteria (`requirement_verification_proven=false`).
4. **Baseline-vs-modified regression comparison missing:** Running generated tests on the modified working copy alone does not prove absence of regressions against baseline.
5. **No OS/container sandbox:** Isolated Git working copies protect repository files only; untrusted repository execution remains disabled.
6. **Single-run scope:** This report validates live M02–M03 Groq integration, not a statistically significant multi-model comparative benchmark.
