# ChangeGuard AI — Codebase Map & Directory Architecture

This document provides a complete map of the ChangeGuard AI repository. It details every significant directory, its purpose, architectural ownership, relationships, and current implementation status across branches.

---

## 1. Directory Tree Overview

```
BJM-SLIIT/
├── .gitignore                                   # Standard Git exclusion file
├── README.md                                    # Project description & hackathon metadata
├── docs/                                        # Documentation suite
│   ├── Proposal.docx                            # Official proposal document (373 KB)
│   ├── PROJECT_DETAILS.md                       # High-level project specifications (placeholder)
│   ├── Workflow.md                              # Planned agent workflow specifications (placeholder)
│   └── project-understanding/                   # Architectural & deep-dive documentation
│       ├── PROJECT_OVERVIEW.md
│       ├── ARCHITECTURE.md
│       ├── COMPONENT_GUIDE.md
│       ├── CODEBASE_MAP.md
│       ├── WORKFLOWS.md
│       ├── TECHNOLOGY_STACK.md
│       ├── PROPOSAL_VS_IMPLEMENTATION.md
│       ├── DEVELOPER_GUIDE.md
│       ├── STUDENT_EXPLANATION.md
│       └── LLM_CONTEXT.md
├── frontend/                                    # Web UI dashboard (React + Tailwind CSS)
│   └── .gitkeep                                 # Directory structure keeper
├── project/                                     # Core application source code
│   ├── main.py                                  # Primary FastAPI application entry point (stub)
│   ├── "requirements.txt │"                     # Dependency specifications (contains unicode artifact)
│   ├── marsadh_orchestrator/                    # M1: Orchestration engine & state machine
│   │   └── .gitkeep
│   ├── agents/                                  # Multi-agent implementations
│   │   ├── mujahidh_codebase/                   # M2: Codebase Intelligence & Impact Agent
│   │   │   ├── .gitkeep                         # (on main)
│   │   │   ├── __init__.py                      # Package initialization (in feature branch)
│   │   │   ├── analyzer.py                      # AST static codebase analyzer (in feature branch)
│   │   │   ├── codebase_agent.py                # High-level runner & coordinator (in feature branch)
│   │   │   ├── git_diff_analyzer.py             # Git diff & changed function parser (in feature branch)
│   │   │   └── impact_agent.py                  # Reverse dependency graph & BFS (in feature branch)
│   │   └── munshif_testing/                     # M4: Testing & execution agent
│   │       └── .gitkeep
│   ├── nafris_verification/                     # M4: Verification & independent reviewer agent
│   │   └── .gitkeep
│   └── schemas/                                 # Pydantic data models & state schemas
│       └── .gitkeep
├── sample_projects/                             # Testbed applications for evaluation
│   └── mini_pos/                                # Miniature Point-of-Sale reference system
│       ├── .gitkeep                             # (on main)
│       ├── billing.py                           # Checkout & pricing logic (in feature branch)
│       ├── customer_service.py                  # Customer refund processing (in feature branch)
│       ├── dashboard.py                         # Customer refund UI adapter (in feature branch)
│       ├── refunds.py                           # Refund calculations (in feature branch)
│       └── reports.py                           # Sales report aggregator (in feature branch)
└── tests/                                       # Project unit & integration tests
    └── .gitkeep
```

---

## 2. Branch Topology & State of Development

The repository has an intentional team-branching strategy reflecting active hackathon milestone progress (Status as of 24 November 2026):

| Branch Name | Tracking / Status | Contents & Purpose |
| :--- | :--- | :--- |
| **`main`** | Active Base | Initial repository scaffolding. Contains root documentation, `Proposal.docx`, and placeholder `.gitkeep` files establishing team folder ownership. |
| **`develop`** | Integration Base | Synchronized with `main` (`0` commit difference). Intended target for merged features. |
| **`feature/mujahidh-codebase`** | **Active Implementation (+4 Commits)** | Contains **436 lines of verified Python implementation** by M2 (Mujahidh). Implements AST parsing, Git diff function mapping, BFS reverse dependency graph analysis, and the mini POS reference application. |
| **`feature/nafris-verification`** | Scaffolding | Synchronized with `main`. Assigned to M4 (Nafris) for test execution and verification logic. |

---

## 3. Detailed File & Module Analysis

### 3.1 Root & Configuration Files

#### `README.md`
- **Location:** `README.md`
- **Role:** Public landing documentation.
- **Content:** Outlines ChangeGuard AI project name, team (BJM-SLIIT), hackathon context (Innodata AI Catalyst ’26), objective, planned features, and repository structure.
- **Implementation Status:** Operational.

#### `.gitignore`
- **Location:** `.gitignore`
- **Role:** Ensures developer environments, temporary files, and OS artifacts are not committed.
- **Content:** Ignores `__pycache__/`, `.pytest_cache/`, `.venv/`, `env/`, `dist/`, `build/`, `.DS_Store`.

#### `project/requirements.txt │`
- **Location:** `project/requirements.txt │`
- **Architectural Note:** Note the trailing unicode box-drawing character `│` (UTF-8 `\342\224\202`) in the filename. This is a common artifact when files are generated via shell copy-paste from terminal tree views.
- **Contents:** Currently empty.
- **Action for Developers:** Standardize file name to `requirements.txt` and populate with `fastapi`, `uvicorn`, `pydantic`, `pytest`, `networkx`.

#### `project/main.py`
- **Location:** `project/main.py`
- **Role:** Planned central entry point for the backend API server.
- **Contents:** Currently empty stub.

---

### 3.2 Codebase Intelligence Component (`project/agents/mujahidh_codebase/`)
*Implemented in `feature/mujahidh-codebase`*

#### `analyzer.py` (72 lines)
- **Role:** Static Python AST analysis.
- **Key Functions:**
  - `analyze_repository(repository_path: str) -> dict`: Recursively locates `.py` files, excludes build/virtual environments, parses AST, and records defined functions, classes, and import declarations.
- **Entry Point:** Can be executed directly via `python -m project.agents.mujahidh_codebase.analyzer`.

#### `git_diff_analyzer.py` (83 lines)
- **Role:** Git diff parser and semantic function mapper.
- **Key Functions:**
  - `run_git(repo, *args)`: Wrapper invoking `subprocess.run(["git", "-C", str(repo), *args])`.
  - `changed_functions(repository_path, base="HEAD") -> list[dict]`: Inspects unified Git diffs (`git diff --unified=0`), parses modified line numbers with regex, and correlates them to AST `FunctionDef` nodes.
- **Entry Point:** Executable directly via CLI.

#### `impact_agent.py` (151 lines)
- **Role:** Reverse dependency graph builder and BFS impact engine.
- **Key Functions:**
  - `build_dependency_graph(repository_path) -> dict`: Builds adjacency list mapping target modules to files importing them.
  - `analyze_impact(repository_path, changed_file) -> dict`: Executes Breadth-First Search (BFS) starting at `changed_file`, categorizing dependents into `direct` (depth 1) and `indirect` (depth > 1), retaining import line references.
- **Entry Point:** Executable directly via CLI.

#### `codebase_agent.py` (87 lines)
- **Role:** Composite agent runner.
- **Key Functions:**
  - `run_codebase_agent(repository_path=".", source_path="sample_projects/mini_pos", base_ref="HEAD") -> dict`: Coordinates AST analysis, diff detection, and impact evaluation into a consolidated JSON report.
- **Entry Point:** Primary CLI entry point for Codebase Intelligence.

---

### 3.3 Reference Testbed Application (`sample_projects/mini_pos/`)
*Implemented in `feature/mujahidh-codebase`*

The miniature POS application demonstrates real multi-tier software coupling:

1. **`billing.py` (14 lines):**
   - Core pricing and checkout calculations (`calculate_total`, `apply_discount`, `checkout`).
2. **`refunds.py` (6 lines):**
   - Directly imports `apply_discount` from `billing.py`.
3. **`customer_service.py` (6 lines):**
   - Directly imports `calculate_refund` from `refunds.py` (indirect dependency on `billing.py` at depth 2).
4. **`dashboard.py` (10 lines):**
   - Directly imports `process_customer_refund` from `customer_service.py` (indirect dependency on `billing.py` at depth 3).
5. **`reports.py` (7 lines):**
   - Directly imports `checkout` from `billing.py` (direct dependency on `billing.py` at depth 1).

---

### 3.4 Scaffolding & Pending Modules

| Directory | Owner | Current Contents | Target Implementation |
| :--- | :--- | :--- | :--- |
| `project/marsadh_orchestrator/` | M1 | `.gitkeep` | Finite state machine orchestrating agent workflows, managing task state, and enforcing retry limits. |
| `project/agents/munshif_testing/` | M4 | `.gitkeep` | Test synthesis, pytest runner, test log parser. |
| `project/nafris_verification/` | M4 | `.gitkeep` | Adversarial reviewer agent, regression analyzer. |
| `project/schemas/` | Team | `.gitkeep` | Pydantic schema models for all inter-agent communication records. |
| `frontend/` | Team | `.gitkeep` | React dashboard for viewing diffs, impact graphs, and test evidence. |
| `tests/` | Team | `.gitkeep` | Unit tests for ChangeGuard AI's own agent components. |
