# ChangeGuard AI — Project Overview

> **Repository:** `https://github.com/marshadmajeedh/BJM-SLIIT`  
> **Institution:** Sri Lanka Institute of Information Technology (SLIIT)  
> **Team:** BJM-SLIIT (BJM)  
> **Event:** Innodata AI Catalyst ’26 — University Track  
> **Domain:** AI and Robotic Engineering  
> **Target MVP Completion:** 29 November 2026  

---

## 1. Executive Summary

In modern software engineering, AI code-generation tools (e.g., GitHub Copilot, Cursor, ChatGPT) have drastically accelerated code authoring. However, writing a code patch is fundamentally distinct from demonstrating that the patch fulfills the specified business intent without introducing subtle regressions in untouched software components.

**ChangeGuard AI** is an evidence-oriented multi-agent framework designed to analyze, patch, and rigorously verify software modifications against natural-language change requests. Rather than functioning as a black-box code generator or an autonomous production deployer, ChangeGuard AI deconstructs requirements into verifiable acceptance criteria, performs static code and Git impact analysis, generates minimally scoped code diffs in isolation, executes regression and acceptance test suites, and presents an auditable change package with test logs and outstanding risk summaries for explicit human review and approval.

---

## 2. Real-World Problem Definition & Justification

### The Problem
When software engineers modify existing systems, seemingly simple change requests frequently introduce unintended side effects across indirect dependency paths:
- **Hidden Coupling:** In a Point-of-Sale (POS) application, introducing a rule such as *"Apply a 10% discount only to premium customers"* appears trivial. However, altering checkout math can ripple into refund calculations, sales reports, inventory deduction audits, tax aggregates, and financial reconciliation.
- **LLM Hallucination and Scope Creep:** Single-turn LLMs or general-purpose coding agents frequently rewrite entire files, discard unrelated comments or logic, introduce subtle regressions, or fail to write edge-case tests.
- **Verification Deficit:** As highlighted by the Google Cloud DORA 2024 report, increased AI code generation without rigorous verification is correlated with reduced delivery stability. Producing code is cheap; verifying that code is safe and requirement-compliant remains the primary bottleneck in software delivery.

### The Research Question
> **Can a requirement-traceable multi-agent workflow produce more verifiably correct changes than a comparable single-agent workflow under similar task, model, and resource conditions?**

### Distinctive Contributions
1. **End-to-End Requirement-to-Code-to-Test Traceability:** Every change links back to an extracted acceptance criterion and forward to a passing unit or regression test.
2. **Reverse Dependency Impact Analysis:** Static AST and reverse graph analysis pinpoint both direct and indirect dependents before and after code modifications.
3. **Evidence-Based Oversight (Human-in-the-Loop):** The framework operates as an auditable advisor, enforcing a strict boundary where no code is merged or deployed without human sign-off.
4. **Fair Single-Agent Benchmarking:** Rigorous empirical evaluation comparing multi-agent specialist coordination against a single-agent baseline using identical LLM models, test splits, token budgets, and timeout constraints.

---

## 3. Proposed Solution

ChangeGuard AI accepts two primary inputs:
1. A **local repository snapshot** (Git-controlled Python codebase).
2. A **natural-language change request** (e.g., "Add 10% discount for premium customers").

Instead of outputting raw code snippets, the system coordinates specialized agents governed by a deterministic orchestrator to produce an **Auditable Change Package**:
- **Structured Acceptance Criteria & Assumptions:** Deconstructed requirements and edge cases.
- **Codebase & Impact Map:** Ranked list of affected files, functions, and dependency chains.
- **Isolated Scoped Git Diff:** A minimal code patch applied to an isolated workspace.
- **Automated Test Evidence:** Execution logs of both regression tests (verifying existing behavior remains unbroken) and new acceptance tests (verifying requested behavior works).
- **Independent Review & Risk Assessment:** An adversarial evaluation verifying whether the patch exceeded its scope or introduced untested behavior.
- **Human Approval Gate:** A user interface dashboard where developers review diffs, test logs, and impact reports before approving the merge.

---

## 4. Target Users and Stakeholders

| Stakeholder / User | Primary Need | Value Delivered by ChangeGuard AI |
| :--- | :--- | :--- |
| **Software Engineers & Developers** | Fast, scoped code modifications without breaking existing features. | Automatic change localization, minimal diff generation, and pre-run unit tests. |
| **Code Reviewers & Tech Leads** | Clear understanding of what was changed and why. | Traceability matrix (Requirement -> AST Node -> Test Case) and risk reports. |
| **QA Engineers** | Verification that existing regression suites still pass. | Automated regression test execution in isolated sandboxes with pass/fail logs. |
| **Open Source Maintainers** | Guardrails against low-quality, AI-generated pull requests. | Verification that PRs adhere strictly to scope and do not cause regressions. |
| **Student Development Teams** | Learning safe software evolution and architectural impact. | Transparent visibility into dependency graphs and multi-agent coordination. |

---

## 5. Main Objectives & Deliverables (Phase 1 MVP)

| Objective | Description | Phase 1 Deliverable / Evidence |
| :--- | :--- | :--- |
| **O1. Interpret Requirements** | Convert natural language into formal acceptance criteria. | Structured JSON acceptance criteria, assumptions, and task record. |
| **O2. Understand Code Impact** | Map dependencies and identify files affected by proposed changes. | AST index, reverse dependency graph, and ranked impact report. |
| **O3. Propose Targeted Edits** | Author minimal, high-precision code patches in an isolated branch. | Reviewable Git diff and bounded edit history. |
| **O4. Validate Behavior** | Execute acceptance tests and pre-existing regression test suites. | Pytest test execution logs, pass/fail status, and regression summary. |
| **O5. Fair Benchmarking** | Empirically compare multi-agent workflow vs. single-agent baseline. | Comparative benchmark logs (success rate, regression rate, token/cost efficiency). |
| **O6. Support Oversight** | Provide human reviewers with audit logs and an approval/rejection gate. | Web UI dashboard, audit trail, and human approval workflow. |

---

## 6. Scope and Boundaries

### In-Scope (Phase 1 MVP)
- Python 3 repositories with existing unit test suites.
- Local Git branch and working-copy analysis (isolated checkout/worktree).
- Static AST parsing, import graph construction, and reverse dependency BFS.
- Git diff parsing mapping changed lines to AST function definitions.
- Automated testing via `pytest` with pass/fail logging.
- Reference testbed: `sample_projects/mini_pos` (miniature Point-of-Sale).
- Generalization testbed: A second independent Python repository (e.g., low-stock alert or library management).
- Web-based review dashboard (React + FastAPI).

### Out of Scope (Phase 1 MVP)
- **Automatic Production Deployment:** No autonomous commits to protected branches or CI/CD pipelines without explicit human sign-off.
- **Arbitrary Language Support:** Polyglot codebases (JavaScript, C++, Java, Rust) are deferred to future iterations; Phase 1 exclusively analyzes Python.
- **Guaranteed Bug-Free Patches:** Passing tests do not constitute mathematical proof of correctness; untested edge cases remain a stated limitation.
- **Unrestricted Code Execution:** Arbitrary dependency installation (`pip install <untrusted>`) on host machines is prohibited.

---

## 7. Component Team Allocation

The project structure is mapped to the four student developers from SLIIT:

| Member Alias | Directory / Component | Core Responsibility |
| :--- | :--- | :--- |
| **M1: Marshadh Majeedh** | `project/marsadh_orchestrator/` | Deterministic workflow orchestration, state machine, and requirement deconstruction. |
| **M2: Mujahidh** | `project/agents/mujahidh_codebase/` | Static codebase indexing (AST), Git diff function mapping, and reverse dependency impact analysis. |
| **M3: Developer Agent** | `project/agents/` (Patch Generation) | Isolated workspace patch generation, minimal scoped edits, and automated repair loop. |
| **M4: Munshif & Nafris** | `project/agents/munshif_testing/` & `project/nafris_verification/` | Automated test execution, regression verification, independent adversarial review, and review dashboard. |
