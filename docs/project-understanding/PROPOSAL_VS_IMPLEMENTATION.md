# ChangeGuard AI — Proposal vs. Actual Implementation

This document provides a rigorous, evidence-based audit comparing the vision outlined in `Proposal.docx` against the actual codebase implementation across repository branches.

---

## 1. Feature Comparison & Verification Matrix

| Proposed Feature | Expected Behavior (from Proposal) | Actual Implementation | Source Evidence | Status |
| :--- | :--- | :--- | :--- | :--- |
| **Static Codebase Indexing (A2)** | Parse Python repository, extract function definitions, classes, and imports. | Built-in AST analyzer parses Python files, skips virtualenvs/git directories, records functions, classes, imports. | `project/agents/mujahidh_codebase/analyzer.py` (Lines 12–69) | **Implemented** |
| **Git Diff Parsing & Function Mapping** | Extract Git changes and map modified line ranges to affected source code constructs. | Executes `git diff --unified=0`, extracts added line numbers via regex, and checks line intersection with AST `FunctionDef` nodes. | `project/agents/mujahidh_codebase/git_diff_analyzer.py` (Lines 20–75) | **Implemented** |
| **Reverse Dependency Impact Analysis (A3)** | Map direct and indirect module dependencies affected by a code modification. | Builds reverse import graph and traverses dependents using BFS; categorizes direct (depth 1) vs indirect (depth > 1) with line citations. | `project/agents/mujahidh_codebase/impact_agent.py` (Lines 82–162) | **Implemented** |
| **Composite Codebase Agent Runner** | Unified runner coordinating AST indexing, diff detection, and impact evaluation. | `run_codebase_agent()` orchestrates AST indexing, diff analysis, and impact analysis over target directory. | `project/agents/mujahidh_codebase/codebase_agent.py` (Lines 16–78) | **Implemented** |
| **Reference POS Testbed (`mini_pos`)** | Multi-tier Point-of-Sale testbed with billing, refunds, customer service, dashboard, reports. | Fully written Python modules with multi-tier imports (`billing` <- `refunds` <- `customer_service` <- `dashboard`; `billing` <- `reports`). | `sample_projects/mini_pos/*.py` | **Implemented** |
| **Requirement Analyst Agent (A1)** | Deconstruct natural-language change requests into structured acceptance criteria & assumptions. | Directory placeholder exists; prompt engineering and LLM parser logic not yet implemented. | `project/marsadh_orchestrator/.gitkeep` | **Not implemented** |
| **Developer / Patch Agent (A4)** | Generate scoped code diffs on an isolated branch to fulfill acceptance criteria. | Module not yet implemented; directory structure established. | `project/agents/` | **Not implemented** |
| **Automated Testing & Test Synthesis (A5)** | Generate acceptance tests, run regression test suites in isolated sandbox with pytest. | Directory placeholders exist on main and feature branch; execution script not yet implemented. | `project/agents/munshif_testing/.gitkeep`, `tests/.gitkeep` | **Not implemented** |
| **Independent Reviewer Agent (A6)** | Adversarially validate patch scope, test results, and requirement alignment. | Verification directory exists; LLM prompt and verification logic pending. | `project/nafris_verification/.gitkeep` | **Not implemented** |
| **Deterministic State Machine Orchestrator** | Manage agent handoffs, task state, repair retry loop (max 2 retries), and budget limits. | Scaffolding directory present; state machine implementation pending. | `project/marsadh_orchestrator/.gitkeep` | **Not implemented** |
| **FastAPI Backend Server** | Expose REST endpoints to ingest change requests, manage runs, and retrieve audit packages. | `project/main.py` exists as an empty 0-byte file. | `project/main.py` | **Partially implemented** (Scaffold only) |
| **React Web Dashboard** | Web interface displaying change requests, agent timeline, diff view, test logs, and approval button. | `frontend/` directory exists with `.gitkeep` placeholder. | `frontend/.gitkeep` | **Not implemented** |
| **Structured Communication Schemas** | Pydantic data schemas for TaskRecord, RequirementSpec, ImpactReport, VerificationEvidence. | `project/schemas/` exists with `.gitkeep` placeholder. | `project/schemas/.gitkeep` | **Partially implemented** (Scaffold only) |
| **Single-Agent vs Multi-Agent Benchmark** | Empirical evaluation comparing specialist multi-agent framework against single-agent LLM baseline. | Experimental protocol defined in proposal; benchmark runner scripts pending. | Proposal Section 8 | **Not implemented** |
| **Second Generalization Testbed** | Independent second Python repository (e.g. low-stock alert logic) to prove framework generality. | Concept documented in proposal; repository files not yet added. | Proposal Section 7 | **Not implemented** |

---

## 2. In-Depth Analysis of Discrepancies & Deviations

### 2.1 Timeline Alignment
The project timeline specified in `Proposal.docx` Section 10 scheduled:
- **20 Nov:** Submit proposal, grant organizer access, initialize repo (Done).
- **21–22 Nov:** Requirements and orchestration; sample repositories/tests (M1 + M4).
- **23–24 Nov:** Codebase analysis and impact mapping (M2).
- **24–26 Nov:** Patch generation and test/retry workflow (M3 + M4).
- **27–28 Nov:** Baseline runs, evaluation, and UI integration.
- **29 Nov:** Final MVP submission.

**Observation:** As of 24 November 2026, **M2's milestone (Codebase analysis & impact mapping) is fully complete and verified in `feature/mujahidh-codebase`**. The remaining agent modules (M1 Orchestrator, M3 Developer, M4 Testing/Verification) are on schedule according to the work plan for the 24–28 November development window.

### 2.2 Architectural Inconsistencies & Anomalies
1. **Unmerged Feature Branch:**
   - All working code currently resides on `remotes/origin/feature/mujahidh-codebase`. The `main` and `develop` branches hold only the initial directory skeleton.
   - **Risk:** New team members pulling `main` will see empty directories unless they switch to or merge the feature branch.
2. **Filename Anomaly (`requirements.txt │`):**
   - The file `project/requirements.txt │` contains a trailing Unicode character `│` (U+2502).
   - **Risk:** Standard tooling (`pip install -r requirements.txt`) fails with `No such file or directory`.
3. **Empty Documentation Files:**
   - `docs/PROJECT_DETAILS.md` and `docs/Workflow.md` are currently 0 bytes.
   - **Resolution:** The documentation generated in `docs/project-understanding/` provides the complete architectural and workflow specifications needed to populate these files.
4. **Import Limitation in Static Impact Analysis:**
   - `impact_agent.py` currently handles absolute imports (`import billing` or `from billing import apply_discount`).
   - Relative package imports (`from .billing import apply_discount`) are filtered out (`node.level == 0`).
   - **Impact:** Works seamlessly for flat repositories like `mini_pos`, but will omit package-relative internal imports in complex nested packages.

---

## 3. Implementation Risk Assessment

| Risk Item | Severity | Description | Mitigation Strategy |
| :--- | :--- | :--- | :--- |
| **Branch Integration Lag** | **Medium** | M2's completed work in `feature/mujahidh-codebase` is not yet merged to `develop` or `main`. | Perform a clean pull request or fast-forward merge from `feature/mujahidh-codebase` into `develop`. |
| **Test Suite Absence** | **High** | The `mini_pos` application currently lacks pre-existing pytest files (`test_billing.py`, etc.). | Create baseline regression test cases for `mini_pos` so A5 has existing tests to execute. |
| **Single-Agent Baseline Overhead** | **Medium** | Benchmark requires running parallel single-agent runs, consuming token quotas and time. | Implement a script that sends the same prompt to a single LLM and logs passing/failing tests against the same test suite. |
| **LLM Output Nondeterminism** | **High** | Code generation LLMs might output markdown codeblocks rather than raw Git diffs. | Enforce strict regex parsing or structured output schemas (JSON with patch string) in A4 prompt engineering. |
