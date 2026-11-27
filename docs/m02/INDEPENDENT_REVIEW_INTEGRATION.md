# ChangeGuard AI — P0-9 Independent Review Integration (A6 / M02)

> **Module:** M02 — Central Orchestration & Backend (Owner: Marsadh)  
> **Branch:** `feature/m02-independent-review-and-reliability`  
> **Scope:** Independent Reviewer (A6) schemas, injectable `IndependentReviewAdapter` & `IndependentReviewerProvider` protocol, deterministic offline reviewer fixture, post-verification/post-repair workflow integration (`RUNNING_INDEPENDENT_REVIEW` -> `RECONCILING_REVIEW_EVIDENCE`), deterministic evidence reconciliation, unified Gate 2 approval policy, SQLite persistence & restart safety, API/OpenAPI parity, and controlled offline demonstrations.  
> **Important Honesty Disclosure:** Production AI independent reviewer intelligence (A6) and production AI patch generation intelligence (A4) are **not** implemented in this task. All offline demonstrations in this document execute real local `pytest` through unmodified M04 (`run_reviewed_tests`) against isolated `mini_pos` Git fixtures using **explicitly labeled deterministic offline fixtures** (`is_deterministic_fixture=True`, `is_mock_output=True`), NOT live AI review or live AI repair results.

---

## 1. Architecture & Workflow Position

Independent review (A6) executes **after** modified verification, baseline-versus-modified regression comparison, and any authorized bounded patch repair attempts have finished, and **before** final change-package compilation (`COMPILING_REPORT`) and Human Review Gate 2 (`AWAITING_FINAL_APPROVAL`).

```text
[COMPARING_VERIFICATION_RESULTS] (verify_existing_change)
   or
[EVALUATING_REPAIR_ELIGIBILITY] (generate_and_verify_change — after repair finishes or is skipped)
   │
   ▼  Freeze final candidate evidence + verify candidate snapshot/patch/test-hash integrity
[RUNNING_INDEPENDENT_REVIEW]
   ├── Construct bounded read-only IndependentReviewRequest for the FINAL candidate only
   ├── Invoke injectable IndependentReviewAdapter.review(request)
   │     ├── Live mode + no production A6 provider -> availability="unavailable" (NEVER falls back to fixture)
   │     ├── Invalid schema / mislabeled fixture / stale candidate / fabricated reference -> status="invalid"
   │     ├── Provider exception -> status="error" (secret-redacted, bounded error)
   │     └── Valid completed review -> status="completed" (availability="completed" or "completed_offline_fixture")
   ├── Persist sanitized IndependentReviewRequest (raw source excerpts stripped) & IndependentReviewResult
   │
   ▼
[RECONCILING_REVIEW_EVIDENCE]
   ├── Deterministic EvidenceReconciliationEngine.reconcile(run)
   │     ├── Verify candidate & review snapshot/patch identities
   │     ├── Cross-check actual verification, baseline regressions, traceability, patch validation, repair history & A6 findings
   │     ├── Flag conflicting/unsubstantiated reviewer claims
   │     ├── Classify assurance level & compute approval_restrictions / complete_requirement_assurance
   │     └── NEVER trigger additional automatic patch repair solely on reviewer disagreement
   │
   ▼
[COMPILING_REPORT]
   ├── Compile auditable ChangePackageReport with honest recommendation_label & evidence_limitations
   │
   ▼
[AWAITING_FINAL_APPROVAL] Human Review Gate 2
   ├── Single unified Gate 2 policy (evaluate_approval_policy) shared by API allowed_actions & submit_final_decision
   └── A6 NEVER approves, merges, commits, pushes, or deploys changes
```

### Core Components Added/Extended

| File | Responsibility |
| :--- | :--- |
| `project/schemas/review.py` | Strict (`extra="forbid"`) Pydantic v2 schemas: `ReviewRecommendation`, `ReviewAvailabilityStatus`, `ReviewFindingSeverity`, `ReviewFindingCategory`, `ReviewEvidenceReferenceType`, `ReviewEvidenceReference`, `ReviewFinding`, `ReviewModelMetadata`, `IndependentReviewRequest`, `IndependentReviewResult`, `EvidenceReconciliationResult`. |
| `project/marsadh_orchestrator/adapters/review_adapter.py` | Injectable `IndependentReviewerProvider` protocol, `IndependentReviewAdapter`, `DeterministicIndependentReviewerFixture`, `build_known_artifact_registry`, `validate_review_evidence_references`, `sanitize_review_result`. |
| `project/marsadh_orchestrator/review_reconciliation.py` | `CandidateIntegrityCheck`, `ApprovalPolicyDecision`, `verify_candidate_evidence_integrity`, `EvidenceReconciliationEngine`, and unified `evaluate_approval_policy`. |
| `project/marsadh_orchestrator/orchestrator.py` | `RUNNING_INDEPENDENT_REVIEW` and `RECONCILING_REVIEW_EVIDENCE` state transitions, `_execute_independent_review_and_reconciliation`, report enrichment, Gate 2 enforcement via `evaluate_approval_policy`, and safe restart recovery. |
| `project/marsadh_orchestrator/store.py` | Persistence sanitization for `independent_review_request` (stripping `relevant_source_excerpts`), `independent_review`, `review_reconciliation`, and `report.verification`. |
| `project/api/schemas.py`, `project/api/routes.py`, `project/main.py` | Safe dashboard DTOs (`IndependentReviewResponse`, `EvidenceReconciliationResponse`), `/health` capability flags (`independent_reviewer_configured`, `require_independent_review`), DI parameters on `create_app`, and OpenAPI parity. |

---

## 2. Independent Review Schemas (`project/schemas/review.py`)

No arbitrary confidence percentages or fabricated numerical severity scores are required or permitted. All schemas use `extra="forbid"` and model-level truthfulness validators.

### 2.1 `IndependentReviewRequest` (Bounded Read-Only Input to A6)

| Field Group | Fields |
| :--- | :--- |
| Run & Mode | `run_id`, `workflow_mode`, `execution_mode`, `require_independent_review` |
| Requirements | `original_requirement`, `acceptance_criteria`, `business_constraints`, `assumptions`, `unresolved_ambiguities`, `has_mandatory_unresolved_ambiguities` |
| Final Candidate Identity | `candidate_proposal_id`, `candidate_patch_sha256`, `baseline_commit_sha`, `baseline_source_snapshot_sha256`, `modified_source_snapshot_sha256`, `modified_git_diff_sha256` |
| Bounded Evidence | `changed_files`, `impacted_modules`, `patch_unified_diff` (size-bounded), `relevant_source_excerpts` (bounded dict, stripped prior to SQLite storage), `impact_analysis_summary` |
| Verification & Comparison | `approved_test_ids`, `approved_test_hashes`, `verification_summary`, `baseline_comparison_summary`, `requirement_traceability_summary` |
| Repair & Limitations | `repair_attempts_consumed`, `repair_termination_reason`, `repair_attempt_summaries`, `existing_evidence_limitations`, `known_artifact_ids` |

### 2.2 `IndependentReviewResult` (Validated Output from A6)

| Field | Semantics |
| :--- | :--- |
| `status` | `"completed" \| "unavailable" \| "invalid" \| "error"` |
| `availability` | `ReviewAvailabilityStatus`: `completed`, `completed_offline_fixture`, `unavailable`, `invalid_output`, `failed`, `skipped` |
| `recommendation` | `ReviewRecommendation \| None`: `support`, `reject`, `needs_review` (mandatory when `status == "completed"`) |
| `reviewed_run_id`, `reviewed_candidate_proposal_id`, `reviewed_candidate_patch_sha256`, `reviewed_baseline_snapshot_sha256`, `reviewed_modified_snapshot_sha256` | Identity binding checked strictly against the current frozen candidate before acceptance (`STALE_REVIEW_CANDIDATE` on mismatch) |
| `findings` | List of structured `ReviewFinding` (`finding_id`, `category`, `severity`: `critical \| high \| medium \| low \| info`, `title`, `description`, `is_material`, `affected_files`, `related_criterion_ids`, `evidence_references`) |
| Structured Concern Lists | `requirement_compliance_concerns`, `potential_behavioral_regressions`, `security_and_maintainability_concerns`, `missing_verification_evidence`, `unsupported_assumptions` |
| `evidence_references` | List of `ReviewEvidenceReference` (`reference_type`, `reference_id`, `description`) validated against actual known run artifacts (`INVALID_REVIEW_EVIDENCE_REFERENCE` on unknown/fabricated reference) |
| Provenance & Errors | `model_metadata` (`ReviewModelMetadata`), `is_deterministic_fixture`, `is_mock_output`, `error` (`StructuredError`) |

---

## 3. Injectable A6 Adapter & Provider Protocol

Future production A6 implementations connect through `IndependentReviewerProvider` without modifying M02 orchestration internals:

```python
from project.schemas.review import IndependentReviewRequest, IndependentReviewResult
from project.marsadh_orchestrator.adapters.review_adapter import IndependentReviewerProvider

class ProductionA6Reviewer:
    def review_change(
        self, request: IndependentReviewRequest
    ) -> IndependentReviewResult:
        # 1. Inspect ONLY bounded data in `request` (read-only; never touch repo/tests/workflow state).
        # 2. Bind reviewed_run_id, reviewed_candidate_proposal_id, reviewed_candidate_patch_sha256,
        #    reviewed_baseline_snapshot_sha256, and reviewed_modified_snapshot_sha256 from `request`.
        # 3. Cite ONLY artifact IDs present in `request.known_artifact_ids` / criteria / files / tests.
        # 4. Return status="completed", availability="completed",
        #    is_deterministic_fixture=False, is_mock_output=False.
        ...
```

### Dependency Injection Entry Point (`project/main.py`)

```python
from project.main import create_app

app = create_app(
    config,
    review_production_provider=ProductionA6Reviewer(),
)
```

### Adapter Safety Guarantees (`IndependentReviewAdapter`)

1. **No Silent Offline Fallback in Live Mode:** When `request.execution_mode == "live_llm"` and `production_provider is None`, the adapter returns `status="unavailable"`, `availability="unavailable"`, `error.code="INDEPENDENT_REVIEWER_UNAVAILABLE"` without invoking any offline fixture.
2. **Rejects Mislabeled Outputs:** In `live_llm` mode, any provider returning `is_deterministic_fixture=True`, `is_mock_output=True`, or `availability="completed_offline_fixture"` is rejected (`LIVE_MODE_MOCK_REJECTED`). In `offline_test` mode, a fixture failing to label `is_deterministic_fixture=True`, `is_mock_output=True`, and `availability="completed_offline_fixture"` is rejected (`INVALID_FIXTURE_LABELING`).
3. **Stale Candidate Rejection:** Any output whose `reviewed_run_id`, `reviewed_candidate_proposal_id`, `reviewed_candidate_patch_sha256`, `reviewed_baseline_snapshot_sha256`, or `reviewed_modified_snapshot_sha256` disagrees with the frozen `IndependentReviewRequest` is rejected (`STALE_REVIEW_CANDIDATE`).
4. **Artifact Reference Validation:** Every `ReviewEvidenceReference`, `affected_files` entry, and `related_criterion_ids` entry is validated against `build_known_artifact_registry(request)` (`INVALID_REVIEW_EVIDENCE_REFERENCE`).
5. **Sanitized Errors & Read-Only Boundary:** Provider exceptions are caught, secret-redacted (`[REDACTED_GROQ_KEY]`, `[REDACTED_BEARER]`, etc.), and converted into `status="error"`, `availability="failed"`, `error.code="A6_PROVIDER_EXCEPTION"`. The adapter passes a deep-copied read-only request and never gives A6 filesystem write access or workflow transition privileges.

---

## 4. Evidence Reconciliation & Unified Gate 2 Approval Policy

`EvidenceReconciliationEngine.reconcile(run)` (`project/marsadh_orchestrator/review_reconciliation.py`) deterministically reconciles actual test execution, baseline comparison, requirement traceability, patch validation, repair history, candidate integrity, and A6 review findings under nine mandatory principles:

| # | Principle | Enforced Behavior |
| :-: | :--- | :--- |
| 1 | Reviewer support cannot override failed mandatory verification | `blocks_ordinary_approval=True`, `blocking_error_code="VERIFICATION_APPROVAL_BLOCKED"`, reviewer support flagged in `unsubstantiated_claims` & `material_disagreements`. |
| 2 | Reviewer support cannot override confirmed regressions | `blocks_ordinary_approval=True`, `blocking_error_code="REGRESSION_APPROVAL_BLOCKED"`, reviewer support flagged as unsubstantiated. |
| 3 | Reviewer support cannot override unresolved mandatory ambiguities | `blocks_ordinary_approval=True`, `blocking_error_code="UNRESOLVED_REQUIREMENT_AMBIGUITY"`. |
| 4 | Reviewer support cannot override workspace / candidate integrity failures | `verify_candidate_evidence_integrity(run)` checks candidate patch/snapshot/test-hash/review bindings; mismatch blocks approval (`CANDIDATE_VERIFICATION_MISMATCH` or `STALE_REVIEW_CANDIDATE`). |
| 5 | Material reviewer concerns produce explicit disagreement | Any `critical`/`high`/`is_material` finding, `recommendation == "reject"`, or material concern in `generate_and_verify_change` yields `assurance_classification="reviewer_disagreement"` and blocks ordinary approval (`INDEPENDENT_REVIEW_DISAGREEMENT` / `INDEPENDENT_REVIEW_REJECTED`), while allowing `reject` and `needs_review`. |
| 6 | Unsupported reviewer claims marked unsubstantiated | Conflicting support claims or invalid/unverified review outputs are recorded in `unsubstantiated_claims`. |
| 7 | Unavailable review reported honestly | `availability="unavailable"`, `assurance_classification="reviewer_unavailable"`, `complete_requirement_assurance=False`. When `require_independent_review=True`, ordinary approval is blocked (`INDEPENDENT_REVIEW_REQUIRED`); otherwise backward-compatible human approval of an incomplete evidence package remains permitted. |
| 8 | Offline fixtures never presented as live independent AI | `reviewer_is_deterministic_fixture=True` keeps `complete_requirement_assurance=False`, sets `assurance_classification="incomplete_evidence_package"` (when aligned), and records explicit fixture limitations. |
| 9 | Complete requirement assurance requires full live evidence | `complete_requirement_assurance=True` (`assurance_classification="fully_supported_requirement_assurance"`) ONLY when mandatory verification passed, zero confirmed regressions, candidate integrity verified, no mock/fixture outputs in A1/M03/A4/A6, and live production A6 returned `recommendation="support"` with zero unresolved material findings. |

### Single Source of Truth for Gate 2 Enforcement

Both `OrchestratorService.submit_final_decision` and `project/api/schemas.py._allowed_actions` delegate to `evaluate_approval_policy(run)`. A6 never modifies `state` directly and can never approve, commit, merge, push, or deploy changes.

---

## 5. Controlled Offline Demonstration Results (Phase 9)

> **Disclosure:** All three demonstrations below were executed locally against isolated `sample_projects/mini_pos` Git repositories using real local `pytest` via unmodified M04 (`run_reviewed_tests`), **explicitly labeled deterministic offline fixtures** (`DeterministicRegressiveThenFixedPatchFixture`, `DeterministicPremiumDiscountPatchFixture`, `_behavioral_mini_pos_m03_stub`, and `DeterministicIndependentReviewerFixture`), and **zero external LLM calls**. They validate orchestration, reconciliation, and API enforcement — NOT production AI quality.

| Metric / Observed Runtime Field | Case A: Pass + Fixture Supports (After Repair) | Case B: Pass + Material Reviewer Disagreement | Case C: Verification Fails + Reviewer Falsely Supports |
| :--- | :--- | :--- | :--- |
| **Workflow Mode** | `generate_and_verify_change` | `generate_and_verify_change` | `verify_existing_change` |
| **Fixture Mode (A6)** | `DeterministicIndependentReviewerFixture(mode="support")` | `DeterministicIndependentReviewerFixture(mode="material_disagreement")` | `DeterministicIndependentReviewerFixture(mode="false_support")` |
| **Repair Attempts Consumed** | `1` (`repair_termination_reason="repair_succeeded"`) | `0` (`repair_termination_reason="not_needed"` — no extra repair triggered on disagreement) | `0` (`None`) |
| **Verification Outcome** | `passed` (`verification_passed=True`, `has_confirmed_regression=False`) | `passed` (`verification_passed=True`, `has_confirmed_regression=False`) | `failed_tests` (`verification_passed=False`, `has_confirmed_regression=True`) |
| **Reviewed Candidate Matches Final Candidate** | `True` (reviewed Attempt #2 repaired patch, NOT superseded Attempt #1) | `True` | `True` |
| **Review Availability / Provenance** | `completed_offline_fixture` (`is_deterministic_fixture=True`, `is_mock_output=True`) | `completed_offline_fixture` (`is_deterministic_fixture=True`, `is_mock_output=True`) | `completed_offline_fixture` (`is_deterministic_fixture=True`, `is_mock_output=True`) |
| **Reviewer Recommendation** | `support` (`0` findings) | `needs_review` (`1` `HIGH` material finding `REV-001`) | `support` (`0` findings — conflicting with failed tests) |
| **Reconciliation Status** | `reconciled_aligned` | `reconciled_with_disagreement` | `reconciled_deterministic_block` |
| **Assurance Classification** | `incomplete_evidence_package` | `reviewer_disagreement` | `deterministic_verification_blocked` |
| **`complete_requirement_assurance`** | `False` (offline fixtures cannot prove live assurance) | `False` | `False` |
| **`blocks_ordinary_approval`** | `False` (`blocking_error_code=None`) | `True` (`blocking_error_code="INDEPENDENT_REVIEW_DISAGREEMENT"`) | `True` (`blocking_error_code="VERIFICATION_APPROVAL_BLOCKED"`) |
| **Unsubstantiated Claims** | `[]` | `[]` | `1` entry flagging conflicting reviewer `support` claim |
| **`recommendation_label`** | `ready_for_human_review` | `additional_review_required` | `verification_failed` |
| **Gate 2 `allowed_actions`** | `approve`, `reject`, `needs_review` | `reject`, `needs_review` | `reject`, `needs_review` |
| **Gate 2 `POST /decision` (`approve`)** | `200 OK` -> `APPROVED` (explicit human decision; original repo untouched) | `409 Conflict` (`INDEPENDENT_REVIEW_DISAGREEMENT`) -> `needs_review` succeeds (`200 OK` -> `NEEDS_REVIEW`) | `409 Conflict` (`VERIFICATION_APPROVAL_BLOCKED`) -> `needs_review` succeeds (`200 OK` -> `NEEDS_REVIEW`) |

---

## 6. Frontend Dashboard Requirements (Module 04 / Nafris)

1. **Poll Active Review States:** Continue polling `GET /api/runs/{run_id}` while `state` is `RUNNING_INDEPENDENT_REVIEW` or `RECONCILING_REVIEW_EVIDENCE`.
2. **Display Review Provenance Honestly:** Inspect `review_availability`, `independent_review.is_deterministic_fixture`, and `independent_review.is_mock_output`. Never display `completed_offline_fixture` as live AI verification and never display `unavailable` as "passed".
3. **Render Reconciliation & Disagreements:** Display `review_reconciliation.assurance_classification`, `complete_requirement_assurance`, `material_disagreements`, `unsubstantiated_claims`, and `approval_restrictions`.
4. **Enforce `allowed_actions`:** Disable the Gate 2 Approve button whenever `"submit_final_decision:approve"` is absent from `allowed_actions`, displaying `approval_restrictions` explaining why only `reject` and `needs_review` are enabled.

---

## 7. Security & Reliability Limitations

- **Unauthenticated Local API:** The FastAPI backend has no authentication/authorization layer (`reviewer` strings are unauthenticated local metadata). It is intended for loopback-only local development (`127.0.0.1`).
- **No OS/Container Execution Sandbox:** Workspace isolation is not an OS security sandbox. Pytest executes only inside explicitly allowlisted server-trusted roots (`CHANGEGUARD_TRUSTED_EXECUTION_ROOTS`).
- **Static Checks Are Not Execution Sandboxes:** Patch validation blocks protected paths, test files, and execution-config/import-hook files (`pytest.ini`, `conftest.py`, `sitecustomize.py`, `*.pth`), but static checks cannot guarantee safe execution of arbitrary Python code.
- **No Production A6 Intelligence:** Until the future A6 owner supplies an `IndependentReviewerProvider`, `GET /health` reports `independent_reviewer_configured: false` and live runs honestly report `review_availability: "unavailable"`.
