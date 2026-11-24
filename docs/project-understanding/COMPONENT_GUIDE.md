# ChangeGuard AI — Detailed Component Guide

This guide details every component, agent, service, and testbed across the ChangeGuard AI project. It outlines their engineering responsibilities, internal processing logic, inputs/outputs, dependencies, implementation status, and failure impact.

---

## 1. Codebase Intelligence & AST Analyzer

- **Component Name:** `CodebaseAnalyzer` (`project/agents/mujahidh_codebase/analyzer.py`)
- **Owner / Member:** M2: Mujahidh (`feature/mujahidh-codebase`)
- **Purpose:** Statically indexes a target Python repository without executing untrusted code, generating an inventory of files, functions, classes, and imports.
- **Problem Solved:** Prevents hallucination of nonexistent functions or classes by grounding downstream LLMs in actual repository symbol tables.
- **Inputs:** `repository_path: str` (Absolute or relative directory path).
- **Processing Steps:**
  1. Resolves path using `pathlib.Path(repository_path).resolve()`.
  2. Recursively scans for `*.py` files while strictly pruning excluded directories (`.git`, `.venv`, `venv`, `__pycache__`, `node_modules`).
  3. Reads source content as UTF-8 text.
  4. Parses source into an AST using Python's built-in `ast.parse()`.
  5. Walks the AST (`ast.walk`) to identify:
     - `ast.FunctionDef` and `ast.AsyncFunctionDef` nodes.
     - `ast.ClassDef` nodes.
     - `ast.Import` and `ast.ImportFrom` statements.
  6. Deduplicates and sorts symbol names; catches `SyntaxError`, `UnicodeError`, or `OSError` without crashing.
- **Outputs:** Python dictionary / JSON report:
  ```json
  {
    "repository": "/path/to/repo",
    "total_files": 5,
    "files": [
      {
        "path": "billing.py",
        "functions": ["apply_discount", "calculate_total", "checkout"],
        "classes": [],
        "imports": []
      }
    ]
  }
  ```
- **Languages & Frameworks:** Python 3 standard library (`ast`, `json`, `pathlib`).
- **Dependencies:** None (Pure Python standard library).
- **Current Status:** **Implemented and verified** in branch `feature/mujahidh-codebase`.
- **Known Limitations:** Does not parse dynamically injected variables or `__all__` exports; does not yet parse docstrings or type hints.
- **Failure Impact:** If unavailable, the system cannot discover repository structure or symbols, blocking downstream impact mapping.

---

## 2. Git Diff Analyzer

- **Component Name:** `GitDiffAnalyzer` (`project/agents/mujahidh_codebase/git_diff_analyzer.py`)
- **Owner / Member:** M2: Mujahidh (`feature/mujahidh-codebase`)
- **Purpose:** Inspects Git modifications between working directory / commit and a base reference (`HEAD`), pinpointing exact functions affected by changed line ranges.
- **Problem Solved:** Connects raw textual Git diff hunks (`@@ -start,count +start,count @@`) to high-level semantic language constructs (functions).
- **Inputs:** `repository_path: str`, `base: str` (default `"HEAD"`).
- **Processing Steps:**
  1. Executes `git diff --name-only <base> -- *.py` via `subprocess.run`.
  2. For each modified Python file, reads the modified source and parses its AST.
  3. Executes `git diff --unified=0 <base> -- <file>` to extract added/modified line numbers using regex (`r"\+(\d+)(?:,(\d+))?"`).
  4. Iterates through AST function nodes (`ast.FunctionDef`, `ast.AsyncFunctionDef`) and computes intersection:
     `node.lineno <= line <= node.end_lineno`.
  5. Collects matching function names.
- **Outputs:** List of dictionaries containing:
  - `file`: Relative file path.
  - `changed_lines`: Sorted list of integer line numbers.
  - `changed_functions`: List of affected function names.
- **Languages & Frameworks:** Python 3 (`ast`, `subprocess`, `re`, `pathlib`).
- **Dependencies:** Git CLI installed on the system host.
- **Current Status:** **Implemented and verified** in branch `feature/mujahidh-codebase`.
- **Known Limitations:** Detects top-level and method functions, but if a change occurs at global module scope outside any function, `changed_functions` is empty while `changed_lines` records the alteration.
- **Failure Impact:** If Git CLI is missing or the directory is not a Git repo, `subprocess.run` raises `CalledProcessError`, halting diff tracking.

---

## 3. Change Impact Agent (Reverse Dependency Graph BFS)

- **Component Name:** `ImpactAgent` (`project/agents/mujahidh_codebase/impact_agent.py`)
- **Owner / Member:** M2: Mujahidh (`feature/mujahidh-codebase`)
- **Purpose:** Constructs an in-memory reverse dependency graph across all modules and performs Breadth-First Search (BFS) to identify direct and indirect dependents of a modified file.
- **Problem Solved:** Eliminates regression blindness by alerting engineers when changing a core module (like `billing.py`) alters downstream dependents (e.g., `refunds.py`, `reports.py`, `dashboard.py`).
- **Inputs:** `repository_path: str`, `changed_file: str` (relative path).
- **Processing Steps:**
  1. **Graph Construction (`build_dependency_graph`):**
     - Scans all `.py` files in the repository.
     - Maps module dot-names (e.g., `billing`, `customer_service`) to relative file paths.
     - Parses each file's AST for `ast.Import` and `ast.ImportFrom` statements (absolute imports).
     - Inverts relationships: creates `reverse_graph[target_file] = [{"file": importing_file, "import_line": lineno}]`.
  2. **Breadth-First Search (`analyze_impact`):**
     - Validates that `changed_file` is relative and does not escape repository root (`..`).
     - Initializes BFS queue: `deque([(changed_file, 0)])` with a `visited` set to prevent cyclic import infinite loops.
     - Traverses dependents, assigning:
       - `depth == 1`: `"direct"` impact.
       - `depth > 1`: `"indirect"` impact.
     - Records transit path (`via_file`) and line number of import statement.
- **Outputs:** Comprehensive impact report:
  ```json
  {
    "changed_file": "billing.py",
    "analysis_method": "static_reverse_dependency_bfs",
    "total_impacted": 4,
    "direct_count": 2,
    "indirect_count": 2,
    "potentially_impacted_files": [
      {
        "file": "refunds.py",
        "impact_level": "direct",
        "dependency_depth": 1,
        "via_file": "billing.py",
        "import_line": 1
      },
      {
        "file": "reports.py",
        "impact_level": "direct",
        "dependency_depth": 1,
        "via_file": "billing.py",
        "import_line": 1
      },
      {
        "file": "customer_service.py",
        "impact_level": "indirect",
        "dependency_depth": 2,
        "via_file": "refunds.py",
        "import_line": 1
      },
      {
        "file": "dashboard.py",
        "impact_level": "indirect",
        "dependency_depth": 3,
        "via_file": "customer_service.py",
        "import_line": 1
      }
    ]
  }
  ```
- **Languages & Frameworks:** Python 3 (`collections.deque`, `ast`, `pathlib`).
- **Dependencies:** None.
- **Current Status:** **Implemented and verified** in branch `feature/mujahidh-codebase`.
- **Known Limitations:** In Phase 1 MVP, `ImportFrom` resolves top-level absolute imports; relative dot-imports (`from .billing import ...`) are explicitly documented for Phase 2 expansion.
- **Failure Impact:** If impact analysis fails, the system cannot rank regression testing priorities, risking unverified downstream bugs.

---

## 4. Codebase Intelligence Runner

- **Component Name:** `CodebaseAgent` (`project/agents/mujahidh_codebase/codebase_agent.py`)
- **Owner / Member:** M2: Mujahidh (`feature/mujahidh-codebase`)
- **Purpose:** Acts as the composite facade coordinating `analyzer.py`, `git_diff_analyzer.py`, and `impact_agent.py`.
- **Inputs:** `repository_path: str`, `source_path: str`, `base_ref: str`.
- **Processing Steps:**
  1. Validates directory boundaries (`source_root.is_relative_to(git_root)`).
  2. Runs repository-wide AST indexing.
  3. Extracts active Git diffs and changed functions against `base_ref`.
  4. Filters changes to only those within `source_path`.
  5. Computes reverse dependency impact for each changed file.
  6. Formats composite report.
- **Outputs:** Composite execution report with agent identity, status, file summaries, and impact arrays.
- **Current Status:** **Implemented and verified** in branch `feature/mujahidh-codebase`.

---

## 5. Workflow Orchestrator

- **Component Name:** `MarsadhOrchestrator` (`project/marsadh_orchestrator/`)
- **Owner / Member:** M1: Marshadh Majeedh
- **Purpose:** Implements the deterministic finite state machine governing agent execution order, task state persistence, retry quotas, and stop conditions.
- **Planned Responsibilities:**
  1. Receives incoming task requests from FastAPI.
  2. Hands task to Requirement Analyst (A1).
  3. Triggers Codebase Intelligence (A2/A3).
  4. Passes context to Developer Agent (A4) to produce diff.
  5. Passes diff to Testing Agent (A5) for execution.
  6. Enforces repair retry loop (maximum 2 retries if tests fail).
  7. Calls Reviewer Agent (A6) and holds state for Human Approval.
- **Current Status:** **Scaffolded** (`project/marsadh_orchestrator/.gitkeep` on main).

---

## 6. Developer Agent (Patch Generator)

- **Component Name:** `DeveloperAgent` (`project/agents/`)
- **Owner / Member:** M3
- **Purpose:** Generates targeted code modifications strictly limited to the files and functions identified during requirement and impact analysis.
- **Planned Responsibilities:**
  1. Takes requirement acceptance criteria and target file ASTs.
  2. Uses LLM prompt engineering with constrained system prompts to generate unified diffs.
  3. Validates that diff only touches allowable files (scope bounding).
- **Current Status:** **Design / Proposal stage**.

---

## 7. Verification & Testing Agent

- **Component Name:** `MunshifTesting` & `NafrisVerification` (`project/agents/munshif_testing/`, `project/nafris_verification/`)
- **Owner / Member:** M4: Munshif & Nafris
- **Purpose:** Executes automated tests against the patched code in an isolated directory, records regression logs, and synthesizes adversarial review decisions.
- **Planned Responsibilities:**
  1. Sets up temporary test environment.
  2. Formulates unit test cases verifying acceptance criteria.
  3. Executes `pytest` against existing regression suites.
  4. Captures return code, stdout, stderr, and failure traces.
- **Current Status:** **Scaffolded** (`.gitkeep` placeholders on main and `feature/nafris-verification`).

---

## 8. Mini Point-of-Sale (POS) Testbed

- **Component Name:** `mini_pos` (`sample_projects/mini_pos/`)
- **Owner / Purpose:** Curated demonstration and evaluation benchmark for software change impact analysis.
- **Architecture & Dependency Hierarchy:**
  ```
  billing.py (Base Module)
    ▲                ▲
    │                │ (direct import)
    │                reports.py
  refunds.py (direct import)
    ▲
    │ (direct import)
  customer_service.py (indirect import of billing)
    ▲
    │ (direct import)
  dashboard.py (indirect import of billing at depth 3)
  ```
- **Files & Logic:**
  - `billing.py`: Contains `calculate_total()`, `apply_discount()`, `checkout()`.
  - `refunds.py`: Imports `apply_discount` from `billing`, defines `calculate_refund()`.
  - `customer_service.py`: Imports `calculate_refund` from `refunds`, defines `process_customer_refund()`.
  - `dashboard.py`: Imports `process_customer_refund` from `customer_service`, defines `display_refund()`.
  - `reports.py`: Imports `checkout` from `billing`, defines `generate_sales_report()`.
- **Significance:** Perfectly models a 3-tier dependency chain. When `billing.py` is edited to introduce a discount, static BFS immediately discovers that `refunds.py` and `reports.py` are directly impacted (depth 1), `customer_service.py` is indirectly impacted (depth 2), and `dashboard.py` is indirectly impacted (depth 3).
- **Current Status:** **Implemented and verified** in branch `feature/mujahidh-codebase`.

---

## 9. Frontend & Backend Infrastructure

- **Backend (`project/main.py`):** Planned FastAPI entry point exposing `/tasks`, `/analyze`, `/verify`, and `/approve`. Currently a placeholder.
- **Frontend (`frontend/`):** Planned React + Tailwind CSS dashboard providing a timeline of agent actions, Git diff viewer, test results pane, and human approval button. Currently scaffolded with `.gitkeep`.
- **Schemas (`project/schemas/`):** Planned Pydantic data schemas representing `TaskRecord`, `RequirementSpec`, `ImpactResult`, and `VerificationLog`. Currently scaffolded.
