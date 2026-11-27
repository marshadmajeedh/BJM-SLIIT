# ChangeGuard AI — Authoritative LLM Context & Architectural Blueprint

> **NOTICE FOR AI MODELS / LLMs:**  
> This document is the primary, self-contained context snapshot for **ChangeGuard AI**. When assisting with this repository, read this file first to obtain complete architectural grounding, component responsibilities, verified implementation status, and strict operational constraints without needing to inspect the entire repository tree.  
> *Note: This documentation reflects a snapshot as of 24 November 2026. The live repository remains the ultimate source of truth for ongoing commits.*

---

## 1. Project Identity & Research Mission

- **Project Title:** ChangeGuard AI (Full: *A Multi-Agent Framework for Requirement-Aware Software Change Impact Analysis, Automated Patch Generation and Regression Verification*)
- **Target Event:** Innodata AI Catalyst ’26 (University Track) | MVP Deadline: 29 November 2026
- **Institution & Team:** Sri Lanka Institute of Information Technology (SLIIT) | Team: BJM-SLIIT (`marshadmajeedh/BJM-SLIIT`)
- **Domain:** AI and Robotic Engineering / Automated Software Engineering
- **Core Research Question:** *Can a requirement-traceable multi-agent workflow produce more verifiably correct software changes than a comparable single-agent workflow under similar task, model, and resource conditions?*
- **Primary Objective:** Ingest natural-language change requests and local Python repository snapshots; produce auditable, minimally scoped code diffs; execute automated acceptance and regression test suites; evaluate reverse dependency impact; and provide an evidence-based change package for explicit human sign-off.

---

## 2. System Architecture & Component Specialization

The system replaces open-ended single-agent code generation with a deterministic finite state machine (FSM) coordinating six specialized agents:

```
[User Request + Repo Snapshot]
              │
              ▼
    ┌──────────────────┐
    │  Orchestrator    │◄────────────────────────┐
    │  (State Machine) │                         │ (Bounded repair loop:
    └─────────┬────────┘                         │  max 2 retries)
              │                                  │
    ┌─────────▼────────┐                         │
    │ A1: Requirement  │                         │
    │ Analyst Agent    │                         │
    └─────────┬────────┘                         │
              ▼                                  │
    ┌──────────────────┐                         │
    │ A2/A3: Codebase  │                         │
    │ & Impact Agents  │                         │
    └─────────┬────────┘                         │
              ▼                                  │
    ┌──────────────────┐                         │
    │ A4: Developer    │──(Isolated Patch)───────┤
    │ Patch Agent      │                         │
    └─────────┬────────┘                         │
              ▼                                  │
    ┌──────────────────┐                         │
    │ A5: Testing &    │──(Pytest Failure Log)───┘
    │ Verification     │
    └─────────┬────────┘
              ▼
    ┌──────────────────┐
    │ A6: Independent  │
    │ Reviewer Agent   │
    └─────────┬────────┘
              │
              ▼
     [Human Approval Gate] ──(Explicit Sign-Off)──► [Main Branch Merge]
```

### Component Breakdown & Member Ownership
1. **M1: Orchestrator (`project/marsadh_orchestrator/` - Marshadh Majeedh):**
   - Finite state machine managing task execution state, agent handoffs, structured JSON communication, and bounded repair loops.
2. **M2: Codebase Intelligence & Impact Engine (`project/agents/mujahidh_codebase/` - Mujahidh):**
   - **`analyzer.py`:** Static AST indexing of `.py` files. Extracts functions, classes, imports without code execution.
   - **`git_diff_analyzer.py`:** Parses `git diff --unified=0` line intervals and correlates them to AST `FunctionDef` nodes.
   - **`impact_agent.py`:** Constructs inverted dependency graphs and executes Breadth-First Search (BFS) to map direct (depth 1) and indirect (depth > 1) affected modules.
   - **`codebase_agent.py`:** Composite coordinator running AST analysis, diff mapping, and impact assessment.
3. **M3: Developer Agent (`project/agents/`):**
   - Synthesizes minimal unified Git diffs strictly constrained to target functions.
4. **M4: Testing & Verification Agents (`project/agents/munshif_testing/`, `project/nafris_verification/` - Munshif & Nafris):**
   - **A5 Testing:** Synthesizes acceptance test cases, executes `pytest` inside isolated workspaces, captures return codes and traces.
   - **A6 Reviewer:** Adversarial inspector verifying diff scope, test validity, and requirement fulfillment.
5. **Reference Testbed (`sample_projects/mini_pos/`):**
   - Multi-tier Point-of-Sale testbed demonstrating 3-tier cascade: `billing.py` <- `refunds.py` <- `customer_service.py` <- `dashboard.py`; and `billing.py` <- `reports.py`.
6. **Backend & Frontend (`project/main.py`, `frontend/`):**
   - FastAPI server exposing REST endpoints; React + Tailwind CSS dashboard.

---

## 3. Verified Implementation Status (Source-Level Audit)

| Component / Layer | Branch Location | Implementation State | Verification Evidence |
| :--- | :--- | :--- | :--- |
| **AST Repository Analyzer** | `feature/mujahidh-codebase` | **Fully Implemented** | `project/agents/mujahidh_codebase/analyzer.py` (72 lines) |
| **Git Diff Line Mapper** | `feature/mujahidh-codebase` | **Fully Implemented** | `project/agents/mujahidh_codebase/git_diff_analyzer.py` (83 lines) |
| **Reverse Graph BFS Impact** | `feature/mujahidh-codebase` | **Fully Implemented** | `project/agents/mujahidh_codebase/impact_agent.py` (151 lines) |
| **Composite Codebase Agent** | `feature/mujahidh-codebase` | **Fully Implemented** | `project/agents/mujahidh_codebase/codebase_agent.py` (87 lines) |
| **Mini POS Testbed Code** | `feature/mujahidh-codebase` | **Fully Implemented** | `sample_projects/mini_pos/*.py` (43 lines total) |
| **Proposal Document** | `main`, `develop` | **Complete** | `docs/Proposal.docx` (373 KB, 166 paragraphs) |
| **Team Directory Skeleton** | `main`, `develop` | **Scaffolded** | Root and subfolder `.gitkeep` files |
| **Orchestrator FSM** | `project/marsadh_orchestrator/` | Pending (Scheduled M1) | Directory scaffold only |
| **Developer Patch Agent** | `project/agents/` | Pending (Scheduled M3) | Design specified in proposal |
| **Testing & Reviewer Agents**| `munshif_testing/`, `nafris_verification/`| Pending (Scheduled M4) | Scaffolding on `feature/nafris-verification` |
| **FastAPI Backend Server** | `project/main.py` | Stub (0 bytes) | File exists, code pending |
| **Web UI Dashboard** | `frontend/` | Stub | Directory exists with `.gitkeep` |

---

## 4. Key Source Locations & API Contracts

### Codebase Intelligence Output Contract
When `run_codebase_agent()` executes, it produces the following standardized schema:
```json
{
  "agent": "CodebaseIntelligenceAgent",
  "repository": "G:/INNODATA/BJM-SLIIT",
  "source_path": "sample_projects/mini_pos",
  "analysis_type": "static_code_change_impact",
  "status": "completed",
  "repository_summary": { "total_files": 5 },
  "reports": [
    {
      "changed_file": "sample_projects/mini_pos/billing.py",
      "changed_functions": ["apply_discount"],
      "changed_lines": [6, 7, 8],
      "impact_analysis": {
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
    }
  ]
}
```

---

## 5. Architectural Invariants & Constraints

1. **Strict Human-in-the-Loop:** Under no circumstances should any agent autonomously commit or merge code to the `main` branch or production environments. The system outputs an auditable proposal package; merging requires manual human interaction.
2. **Zero Shell Execution Risk:** External shell commands are strictly forbidden from using `shell=True` or unescaped string formatting. All calls use `subprocess.run` with list parameters.
3. **Path Traversal Guards:** All file paths passed into analyzers must be checked via `pathlib.Path.resolve()` and must be relative to the designated repository root. Reject any path containing `..`.
4. **Bounded Self-Healing:** The repair loop between Testing Agent (A5) and Developer Agent (A4) must be strictly capped at **2 iterations** to avoid infinite API loops and budget exhaustion.
5. **No Blind Dependency Installation:** The system must not execute arbitrary `pip install` on untrusted target repositories.
6. **Active Branch Notice:** The primary implementation lives on `feature/mujahidh-codebase`. Do not assume `main` contains the working code until a pull request merges `feature/mujahidh-codebase` into `develop` or `main`.
7. **Known Filename Artifact:** `project/requirements.txt │` contains a trailing Unicode box character `│`. Future tooling should reference the corrected standard name `requirements.txt`.
