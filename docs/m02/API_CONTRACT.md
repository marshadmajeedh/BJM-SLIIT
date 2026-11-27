# ChangeGuard AI — Module 02 REST API Contract & Dashboard Handoff

> **Target Consumer:** Module 04 / React Dashboard (`frontend/`, owner: Nafris)  
> **Backend Owner:** Module 02 — Central Orchestration & Backend (`project/main.py`, owner: Marsadh)  
> **API Version:** `0.2.0-mvp`  
> **OpenAPI Specification:** [`docs/m02/openapi.json`](./openapi.json)

---

## 1. Local Development Startup & URLs

### Startup Command (Loopback-Only Default)
```bash
python -m uvicorn project.main:app --host 127.0.0.1 --port 8000
```

To expose a trusted demonstration repository for local runs, configure allowlisted roots and trusted execution roots via environment variables:
```bash
# PowerShell example
$env:CHANGEGUARD_ALLOWED_REPO_ROOTS="G:\INNODATA\BJM-SLIIT"
$env:CHANGEGUARD_TRUSTED_EXECUTION_ROOTS="G:\INNODATA\BJM-SLIIT"
$env:CHANGEGUARD_CONFIGURED_REPOSITORIES="mini-pos-demo=G:\INNODATA\BJM-SLIIT"
python -m uvicorn project.main:app --host 127.0.0.1 --port 8000
```

| Resource | URL |
| :--- | :--- |
| **Base URL** | `http://127.0.0.1:8000` |
| **Swagger UI** | `http://127.0.0.1:8000/docs` |
| **ReDoc** | `http://127.0.0.1:8000/redoc` |
| **OpenAPI JSON** | `http://127.0.0.1:8000/openapi.json` |

### Default Allowed CORS Origins
`http://localhost:5173`, `http://127.0.0.1:5173`, `http://localhost:3000`, `http://127.0.0.1:3000` (configurable via comma-separated `CHANGEGUARD_CORS_ORIGINS`; wildcard `*` is rejected; `allow_credentials=False`).

---

## 2. Canonical Endpoints Summary

> **Path Alignment Note:** Earlier audit notes discussed `/api/health` and `/review-tests`. The canonical paths established in [`docs/m02/M02_FOUNDATION.md`](./M02_FOUNDATION.md) and implemented here are **`/health`** and **`/api/runs/{run_id}/test-review`**.

| Method | Path | Success Status | Purpose |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | `200 OK` | Service health, DB availability, live LLM readiness, verification dependency checks |
| `POST` | `/api/runs` | `202 Accepted` | Create workflow run and schedule A1 + M01 + M03 in-process |
| `GET` | `/api/runs` | `200 OK` | List persisted runs with bounded pagination and optional `state` filter |
| `GET` | `/api/runs/{run_id}` | `200 OK` | Retrieve full workflow state, A1/M01/M03/M04 evidence, change package, and `allowed_actions` |
| `GET` | `/api/runs/{run_id}/logs` | `200 OK` | Retrieve secret-redacted inter-agent communication logs in deterministic order |
| `POST` | `/api/runs/{run_id}/test-review` | `202 Accepted` (approved) / `200 OK` (rejected) | Human Review Gate 1: approve selected scenarios (schedules M04) or reject proposals |
| `POST` | `/api/runs/{run_id}/decision` | `200 OK` | Human Review Gate 2: record final decision (`approve`, `reject`, `needs_review`) |

---

## 3. Workflow State Machine & Dashboard Actions

| State | Category | Meaning | Frontend Action |
| :--- | :--- | :--- | :--- |
| `CREATED` | Active | Run persisted; workspace preparation queued | Poll `GET /api/runs/{run_id}` |
| `ANALYZING_REQUIREMENTS` | Active | A1 decomposing natural-language requirement into structured criteria, constraints, assumptions, and ambiguities | Poll `GET /api/runs/{run_id}` |
| `ANALYZING_CODEBASE` | Active | M01 static Git diff & reverse-dependency BFS running | Poll `GET /api/runs/{run_id}` |
| `GENERATING_TESTS` | Active | M03 generating unexecuted test proposals | Poll `GET /api/runs/{run_id}` |
| `AWAITING_TEST_REVIEW` | **Paused Gate 1** | Proposals ready; **no tests exported or executed yet** | Stop polling; render M01 impact + M03 scenarios; enable `POST /test-review` |
| `RUNNING_VERIFICATION` | Active | Selected approved tests exported & running under M04 `pytest` | Poll `GET /api/runs/{run_id}` |
| `COMPILING_REPORT` | Active | Aggregating auditable `ChangePackageReport` | Poll `GET /api/runs/{run_id}` |
| `AWAITING_FINAL_APPROVAL` | **Paused Gate 2** | Change package ready for human sign-off | Stop polling; render evidence, risk, limitations, logs; enable `POST /decision` |
| `COMPLETED_NO_CHANGES` | Terminal | Zero tracked Python changes found; M03 & M04 skipped (**not verification proof**) | Display informational notice; stop polling |
| `APPROVED` | Terminal | Final human approval recorded (**no Git commit/merge/push performed**) | Display decision badge; stop polling |
| `REJECTED` | Terminal | Rejected at Gate 1 (tests) or Gate 2 (change package) | Display rejection notes; stop polling |
| `NEEDS_REVIEW` | Terminal | Escalated for manual investigation (e.g., failed/incomplete verification) | Display escalation comments; stop polling |
| `FAILED` | Terminal | Unrecoverable adapter, workspace, integrity, or restart interruption error | Display structured `error.code` & `error.message`; stop polling |

### Polling Recommendations
- Poll `GET /api/runs/{run_id}` every **1000 ms – 1500 ms** while `state` is in active execution states (`CREATED`, `ANALYZING_REQUIREMENTS`, `ANALYZING_CODEBASE`, `GENERATING_TESTS`, `RUNNING_VERIFICATION`, `COMPILING_REPORT`).
- **Stop polling immediately** when `state` enters `AWAITING_TEST_REVIEW`, `AWAITING_FINAL_APPROVAL`, or any terminal state (`COMPLETED_NO_CHANGES`, `APPROVED`, `REJECTED`, `NEEDS_REVIEW`, `FAILED`).
- Use `allowed_actions` from `GET /api/runs/{run_id}` to enable/disable buttons cleanly:
  - `["submit_test_review"]` -> enable Gate 1 approval/rejection controls.
  - `["submit_final_decision:approve", "submit_final_decision:reject", "submit_final_decision:needs_review"]` -> enable all Gate 2 buttons.
  - `["submit_final_decision:reject", "submit_final_decision:needs_review"]` -> disable `approve` (backend blocks `approve` with HTTP `409` whenever mandatory verification did not pass OR mandatory unresolved requirement ambiguities remain open).

---

## 4. Execution Mode Distinctions

| Mode | Value | Behavior | UI Badge Requirement |
| :--- | :--- | :--- | :--- |
| Offline deterministic stub (default) | `"offline_test"` | Uses deterministic local stub (`is_mock_output: true`, `model_label: "deterministic_offline_stub"`). Requires no `GROQ_API_KEY`. Note: the built-in default stub emits import/callable plumbing smoke tests only; behavioral offline demonstrations must inject fixture-specific behavioral tests. | Must clearly label scenarios as **Offline Mock/Stub Output** — never present as live AI performance or functional requirement proof. |
| Live Groq LLM | `"live_llm"` | Requires server-side `GROQ_API_KEY` + `GROQ_MODEL`. **Never silently falls back to mock output**; fails explicitly with `LIVE_LLM_UNCONFIGURED` if credentials are missing. | Display `test_generation.model_label` (e.g., `groq:llama-3.3-70b-versatile`). |

---

## 5. Endpoint Contracts & Illustrative Examples

> **Note:** All JSON payloads below are illustrative examples conforming strictly to the live Pydantic schemas and [`openapi.json`](./openapi.json).

### 5.1 `GET /health`
**Example Response (`200 OK`):**
```json
{
  "status": "ok",
  "service": "ChangeGuard AI API",
  "version": "0.2.0-mvp",
  "environment": "development",
  "database_available": true,
  "live_llm_configured": false,
  "groq_model": null,
  "verification_dependencies_available": true,
  "git_available": true,
  "configured_repository_ids": ["mini-pos-demo"],
  "execution_model": "in_process_bounded_threadpool",
  "limitations": [
    "Background workflow stages execute in a single-process bounded thread pool (not a durable distributed queue).",
    "Untrusted repository execution is disabled; pytest runs only inside server-trusted roots.",
    "Reviewer labels are local MVP metadata and do not represent authenticated user identities.",
    "Live LLM mode is unconfigured (GROQ_API_KEY and GROQ_MODEL required); offline_test mode remains available."
  ]
}
```

### 5.2 `POST /api/runs`
**Request DTO (`CreateRunRequest`):**
```json
{
  "repository_id": "mini-pos-demo",
  "source_path": "sample_projects/mini_pos",
  "base_ref": "HEAD",
  "requirements": "Apply a 10% discount to premium customers only without affecting regular checkout or refunds.",
  "execution_mode": "offline_test"
}
```
> Either `repository_id` (recommended for dashboard) or an allowlisted `repository_path` must be supplied. Disallowed repositories return `403 Forbidden`.

**Example Response (`202 Accepted` — `CreateRunResponse`):**
```json
{
  "run_id": "run_20261127T050000Z_a1b2c3d4e5f6",
  "state": "CREATED",
  "execution_mode": "offline_test",
  "created_at": "2026-11-27T05:00:00.123456Z",
  "execution_model": "in_process_background_task",
  "message": "Workflow run created and scheduled on the in-process background executor. Poll GET /api/runs/{run_id} for progress until AWAITING_TEST_REVIEW (or COMPLETED_NO_CHANGES / FAILED)."
}
```

### 5.3 `GET /api/runs?limit=50&offset=0&state=AWAITING_TEST_REVIEW`
- `limit`: integer `1..200` (default `50`)
- `offset`: integer `>= 0` (default `0`)
- `state`: optional `WorkflowState` enum value

**Example Response (`200 OK` — `RunListResponse`):**
```json
{
  "runs": [
    {
      "run_id": "run_20261127T050000Z_a1b2c3d4e5f6",
      "state": "AWAITING_TEST_REVIEW",
      "execution_mode": "offline_test",
      "repository": {
        "repository_id": "mini-pos-demo",
        "repository_name": "mini-pos-demo",
        "source_path": "sample_projects/mini_pos",
        "base_ref": "HEAD",
        "resolved_base_commit": "317b93b7ae2ef3e0c0646719198f53d8de170c8a",
        "change_mode": "working_tree"
      },
      "requirements_preview": "Apply a 10% discount to premium customers only without affecting regular checkout or refunds.",
      "created_at": "2026-11-27T05:00:00.123456Z",
      "updated_at": "2026-11-27T05:00:01.654321Z",
      "acceptance_criteria_count": 4,
      "has_unresolved_ambiguities": false,
      "changed_file_count": 1,
      "proposed_scenario_count": 2,
      "verification_outcome": null,
      "verification_passed": null,
      "has_error": false
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0,
  "state_filter": "AWAITING_TEST_REVIEW"
}
```

### 5.4 `GET /api/runs/{run_id}`
Returns `RunDetailResponse`. Internal host absolute paths (`workspace_path`, absolute `repository_path`) and raw internal agent payloads (`raw_report`, `raw_proposals`, `raw_result`) are deliberately omitted.

**Example Response Excerpt (`200 OK` — `RunDetailResponse` at Gate 2):**
```json
{
  "run_id": "run_20261127T050000Z_a1b2c3d4e5f6",
  "state": "AWAITING_FINAL_APPROVAL",
  "execution_mode": "offline_test",
  "requirements": "Apply a 10% discount to premium customers only without affecting regular checkout or refunds.",
  "repository": {
    "repository_id": "mini-pos-demo",
    "repository_name": "mini-pos-demo",
    "source_path": "sample_projects/mini_pos",
    "base_ref": "HEAD",
    "resolved_base_commit": "317b93b7ae2ef3e0c0646719198f53d8de170c8a",
    "change_mode": "working_tree"
  },
  "source_snapshot_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "git_diff_sha256": "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
  "created_at": "2026-11-27T05:00:00.123456Z",
  "updated_at": "2026-11-27T05:00:05.000000Z",
  "allowed_actions": [
    "submit_final_decision:approve",
    "submit_final_decision:reject",
    "submit_final_decision:needs_review"
  ],
  "requirement_analysis": {
    "status": "completed",
    "analysis_mode": "offline_test",
    "is_deterministic_fixture": true,
    "is_mock_output": true,
    "model_label": "deterministic_offline_requirement_analyst",
    "requirement_summary": "Decomposed requirement into 4 acceptance criterion(s), 3 business constraint(s), 2 assumption(s), and 1 ambiguity item(s) (0 blocking).",
    "acceptance_criteria": [],
    "business_constraints": [],
    "assumptions": [],
    "ambiguities": [],
    "preserved_behaviors": [],
    "edge_cases": [],
    "has_mandatory_unresolved_ambiguities": false,
    "error": null,
    "notes": []
  },
  "codebase_analysis": {
    "status": "completed",
    "source_path": "sample_projects/mini_pos",
    "base_ref": "HEAD",
    "resolved_base_commit": "317b93b7ae2ef3e0c0646719198f53d8de170c8a",
    "total_indexed_files": 5,
    "has_relevant_changes": true,
    "changed_files_repo_relative": ["sample_projects/mini_pos/billing.py"],
    "changed_files_source_relative": ["billing.py"],
    "impacted_modules_repo_relative": [
      "sample_projects/mini_pos/customer_service.py",
      "sample_projects/mini_pos/dashboard.py",
      "sample_projects/mini_pos/refunds.py",
      "sample_projects/mini_pos/reports.py"
    ],
    "impacted_modules_source_relative": [
      "customer_service.py",
      "dashboard.py",
      "refunds.py",
      "reports.py"
    ],
    "file_impacts": [],
    "error": null,
    "assumptions": [
      "M01 static reverse-dependency analysis resolves absolute Python imports only; dynamic or relative imports may not appear."
    ]
  },
  "test_generation": {
    "status": "proposed",
    "executed": false,
    "generation_mode": "offline_test",
    "is_mock_output": true,
    "model_label": "deterministic_offline_stub",
    "scenarios": [
      {
        "scenario_id": "SCN-001",
        "title": "Premium checkout applies 10% discount",
        "rationale": "billing.apply_discount was modified.",
        "assumptions": "Deterministic behavioral offline fixture test; human review required.",
        "test_code": "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sample_projects/mini_pos'))\nfrom billing import checkout\n\ndef test_premium_discount():\n    assert checkout(100, 2, 'premium') == 180.0\n",
        "content_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "exported_relative_path": ".changeguard_generated_tests/test_generated_001.py",
        "exported_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "approved": true
      }
    ],
    "export_dir_relative": ".changeguard_generated_tests",
    "exported_files": {
      "SCN-001": ".changeguard_generated_tests/test_generated_001.py"
    },
    "exported_hashes": {
      "SCN-001": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    },
    "error": null,
    "assumptions": []
  },
  "test_review": {
    "approved": true,
    "selected_test_ids": ["SCN-001"],
    "approved_test_hashes": {
      "SCN-001": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    },
    "reviewer": "nafris",
    "notes": "Verified discount expectation.",
    "decided_at": "2026-11-27T05:00:03.000000Z"
  },
  "verification": {
    "execution_status": "completed",
    "verification_outcome": "passed",
    "verification_passed": true,
    "executed_test_ids": ["SCN-001"],
    "executed_test_paths": [".changeguard_generated_tests/test_generated_001.py"],
    "exit_code": 0,
    "total_tests": 1,
    "passed": 1,
    "failed": 0,
    "duration_seconds": 0.14,
    "collection_errors": [],
    "failures": [],
    "risk_summary": {
      "severity": "Unknown",
      "score": null,
      "method": "unavailable",
      "rationale": "Reviewed tests passed against the modified working copy, but baseline-versus-modified regression evidence and business-impact risk analysis are unavailable.",
      "evidence_available": false
    },
    "coverage_percent": null,
    "coverage_status": "unavailable",
    "ai_root_cause_status": "unavailable",
    "baseline_comparison": {
      "status": "unavailable",
      "baseline_ref": "317b93b7ae2ef3e0c0646719198f53d8de170c8a",
      "baseline_passed": null,
      "baseline_failed": null,
      "modified_passed": 1,
      "modified_failed": 0,
      "newly_failing_tests": null,
      "notes": "Baseline-versus-modified comparison was not executed; running generated tests against the modified repository alone is not proof of regression safety."
    },
    "stdout_excerpt": "1 passed in 0.14s",
    "stderr_excerpt": "",
    "error": null,
    "assumptions": []
  },
  "report": {
    "package_id": "pkg_run_20261127T050000Z_a1b2c3d4e5f6_12345678",
    "package_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "run_id": "run_20261127T050000Z_a1b2c3d4e5f6",
    "generated_at": "2026-11-27T05:00:05.000000Z",
    "requirements": "Apply a 10% discount to premium customers only without affecting regular checkout or refunds.",
    "repository_id": "mini-pos-demo",
    "repository_name": "mini-pos-demo",
    "source_path": "sample_projects/mini_pos",
    "base_ref": "HEAD",
    "resolved_base_commit": "317b93b7ae2ef3e0c0646719198f53d8de170c8a",
    "change_mode": "working_tree",
    "execution_mode": "offline_test",
    "model_label": "deterministic_offline_stub",
    "is_mock_output": true,
    "changed_files": ["sample_projects/mini_pos/billing.py"],
    "impacted_modules": [
      "sample_projects/mini_pos/customer_service.py",
      "sample_projects/mini_pos/dashboard.py",
      "sample_projects/mini_pos/refunds.py",
      "sample_projects/mini_pos/reports.py"
    ],
    "approved_scenario_ids": ["SCN-001"],
    "executed_scenario_ids": ["SCN-001"],
    "risk_summary": {
      "severity": "Unknown",
      "score": null,
      "method": "unavailable",
      "rationale": "Reviewed tests passed against the modified working copy, but baseline-versus-modified regression evidence and business-impact risk analysis are unavailable.",
      "evidence_available": false
    },
    "recommendation": "needs_review",
    "recommendation_label": "ready_for_human_review",
    "recommendation_summary": "All approved generated tests passed against the modified working copy. Ready for human review; passing generated tests alone is not proof of full requirement verification, regression safety, or production readiness. Tests were produced by offline mock/stub model 'deterministic_offline_stub' (is_mock_output=True), not a live LLM.",
    "unverified_requirements": [
      "Full end-to-end fulfillment of supplied natural-language requirements is not automatically proven because structured acceptance criteria decomposition and baseline regression comparison are unavailable."
    ],
    "traceability": {
      "requirement_traceability_status": "incomplete",
      "structured_acceptance_criteria_status": "available",
      "criterion_mapping_status": "unavailable",
      "requirement_verification_proven": false,
      "baseline_regression_comparison_status": "unavailable",
      "user_supplied_requirements": "Apply a 10% discount to premium customers only without affecting regular checkout or refunds.",
      "changed_files": ["sample_projects/mini_pos/billing.py"],
      "impacted_modules": [
        "sample_projects/mini_pos/customer_service.py",
        "sample_projects/mini_pos/dashboard.py",
        "sample_projects/mini_pos/refunds.py",
        "sample_projects/mini_pos/reports.py"
      ],
      "proposed_scenario_ids": ["SCN-001"],
      "human_approved_scenario_ids": ["SCN-001"],
      "executed_scenario_ids": ["SCN-001"],
      "observed_verification_outcome": "passed",
      "verification_passed": true,
      "unverified_scenario_ids": []
    },
    "criterion_traceability": [],
    "agent_communication_summary": [],
    "evidence_limitations": [
      "Baseline-versus-modified regression comparison was not executed; passing generated tests on the modified working copy alone does not guarantee complete regression safety.",
      "Structured requirement decomposition (A1), automated patch generation (A4), independent adversarial review (A6), and code coverage measurement are not executed in this M02 MVP.",
      "Reviewer identity labels are unauthenticated local MVP inputs and do not constitute cryptographic or SSO identity proof.",
      "Proposed tests were produced by an offline mock/stub model ('deterministic_offline_stub', is_mock_output=True) and must not be presented as live LLM performance.",
      "Code coverage percentage is unavailable (not measured).",
      "AI root-cause analysis is unavailable (deterministic keyword diagnostics only)."
    ],
    "human_approval_required": true
  },
  "final_decision": null,
  "agent_records": [],
  "error": null
}
```

### 5.5 `GET /api/runs/{run_id}/logs`
**Example Response (`200 OK` — `RunLogsResponse`):**
```json
{
  "run_id": "run_20261127T050000Z_a1b2c3d4e5f6",
  "count": 2,
  "logs": [
    {
      "log_id": "log_0123456789abcdef",
      "run_id": "run_20261127T050000Z_a1b2c3d4e5f6",
      "agent_name": "OrchestratorService",
      "stage": "CREATED",
      "timestamp": "2026-11-27T05:00:00.123456Z",
      "status": "succeeded",
      "duration_ms": 0,
      "input_summary": "repo_id=mini-pos-demo, source_path=sample_projects/mini_pos, base_ref=HEAD, mode=offline_test",
      "output_summary": "Workflow run created and persisted.",
      "error": null
    },
    {
      "log_id": "log_fedcba9876543210",
      "run_id": "run_20261127T050000Z_a1b2c3d4e5f6",
      "agent_name": "CodebaseAdapter",
      "stage": "ANALYZING_CODEBASE",
      "timestamp": "2026-11-27T05:00:00.456789Z",
      "status": "succeeded",
      "duration_ms": 84,
      "input_summary": "source_path=sample_projects/mini_pos, base_ref=HEAD",
      "output_summary": "Detected 1 changed file(s) and 4 impacted module(s).",
      "error": null
    }
  ]
}
```

### 5.6 `POST /api/runs/{run_id}/test-review` (Human Review Gate 1)
**Approve Selected Scenarios (`TestReviewRequest`) -> `202 Accepted`:**
```json
{
  "approved": true,
  "selected_test_ids": ["SCN-001"],
  "reviewer": "nafris",
  "notes": "Verified discount test assertions before execution."
}
```
- Only stable scenario IDs belonging to the run are accepted (arbitrary file paths are rejected with `422`).
- Exports **only** `selected_test_ids`, hashes exported files, transitions atomically to `RUNNING_VERIFICATION`, and schedules M04 execution.
- Duplicate or concurrent approval calls return `409 Conflict`.

**Reject Generated Proposals (`TestReviewRequest`) -> `200 OK`:**
```json
{
  "approved": false,
  "selected_test_ids": [],
  "reviewer": "nafris",
  "notes": "Assumptions do not match premium refund specification."
}
```
- Transitions atomically to `REJECTED`; zero test files are exported or executed.

### 5.7 `POST /api/runs/{run_id}/decision` (Human Review Gate 2)
**Approve (`HumanDecisionRequest`) -> `200 OK` (only permitted if `verification.verification_passed == true`):**
```json
{
  "decision": "approve",
  "reviewer": "nafris",
  "comments": "Reviewed impact report, test logs, and limitations."
}
```
**Escalate / Reject (`HumanDecisionRequest`) -> `200 OK` (`comments` mandatory):**
```json
{
  "decision": "needs_review",
  "reviewer": "nafris",
  "comments": "Verification failed on refund edge case; requires manual code inspection."
}
```
- Approving when verification failed, timed out, had collection/zero-test errors, or was unavailable returns `409 Conflict` (`code: "VERIFICATION_APPROVAL_BLOCKED"`).
- Approving when A1 detected mandatory unresolved requirement ambiguities returns `409 Conflict` (`code: "UNRESOLVED_REQUIREMENT_AMBIGUITY"`).
- Final decisions never commit, merge, push, deploy, or modify the original repository.

---

## 6. Error Contract & HTTP Status Codes

All error responses use the `ErrorResponse` envelope (no tracebacks or secrets):
```json
{
  "code": "VERIFICATION_APPROVAL_BLOCKED",
  "message": "Cannot approve a change package when mandatory verification did not pass (verification_outcome='failed_tests'). Submit 'needs_review' or 'reject' instead.",
  "details": {
    "verification_outcome": "failed_tests"
  },
  "run_id": "run_20261127T050000Z_a1b2c3d4e5f6"
}
```

| HTTP Status | Meaning | Example Error Codes |
| :--- | :--- | :--- |
| `403 Forbidden` | Repository path or ID outside server allowlist | `REPOSITORY_ACCESS_DENIED` |
| `404 Not Found` | Unknown workflow `run_id` | `RUN_NOT_FOUND` |
| `409 Conflict` | Invalid state transition, duplicate review/decision, tampered content/snapshot, or blocked approval | `INVALID_STATE_TRANSITION`, `INVALID_REVIEW_STATE`, `DUPLICATE_TEST_REVIEW`, `TEST_CONTENT_MODIFIED`, `SOURCE_SNAPSHOT_MODIFIED`, `VERIFICATION_APPROVAL_BLOCKED`, `UNRESOLVED_REQUIREMENT_AMBIGUITY`, `DUPLICATE_FINAL_DECISION` |
| `422 Unprocessable Content` | Invalid request DTO or unknown scenario ID for run | `VALIDATION_ERROR`, `UNKNOWN_SCENARIO_ID` |
| `503 Service Unavailable` | Database or required local infrastructure failure | `INFRASTRUCTURE_UNAVAILABLE` |
| `500 Internal Server Error` | Unexpected server error (safe generic message) | `INTERNAL_SERVER_ERROR` |

---

## 7. Recommended React Dashboard Flow (Nafris)

1. Call `GET /health` on load to display backend readiness, `configured_repository_ids`, and whether `live_llm_configured` is active.
2. Submit `POST /api/runs` (`202 Accepted`) with selected `repository_id`, `source_path`, `base_ref`, `requirements`, and `execution_mode`.
3. Poll `GET /api/runs/{run_id}` until `state` leaves active stages.
4. When `state === "AWAITING_TEST_REVIEW"`:
   - Render `codebase_analysis` (changed files, direct/indirect impacted modules).
   - Render `test_generation.scenarios` with checkboxes keyed by `scenario_id`, showing `is_mock_output` / `model_label` badges and `assumptions`.
   - Submit `POST /api/runs/{run_id}/test-review`.
5. If approved (`202 Accepted`), poll `GET /api/runs/{run_id}` until `state === "AWAITING_FINAL_APPROVAL"` (or `FAILED`).
6. Render `verification` (outcome, pass/fail counts, failure diagnostics, `risk_summary`), `report` (`recommendation_label`, `evidence_limitations`, `unverified_requirements`), and `GET /api/runs/{run_id}/logs`.
7. Submit `POST /api/runs/{run_id}/decision` using `allowed_actions` to enforce UI guardrails.

---

## 8. Known MVP Limitations

1. **In-process background execution:** Runs execute in a single-process bounded thread pool (not Redis/Celery). Process restarts mark interrupted active runs as `FAILED` without replaying external LLM calls or pytest; paused human-review runs remain resumable.
2. **No OS/container sandbox:** Pytest runs only on explicitly server-trusted roots (`CHANGEGUARD_TRUSTED_EXECUTION_ROOTS`); untrusted execution remains disabled.
3. **No authentication layer:** Reviewer labels are unauthenticated local metadata.
4. **No automated patch generation / repair loop:** `max_repair_attempts` is reserved in config only.
5. **No baseline-vs-modified regression comparison, coverage measurement, or AI root-cause analysis:** Explicitly reported as `unavailable` in responses rather than fabricated.
