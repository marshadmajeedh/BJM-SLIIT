# ChangeGuard AI — Student Explanation & Defense Guide

This guide is designed to help students, junior developers, and researchers master ChangeGuard AI, understand its architectural concepts, and confidently defend it in front of university supervisors, hackathon judges, and technical interviewers.

---

## 1. Three Levels of Explanation

### Level 1: The Beginner Explanation (No Jargon, Everyday Analogy)
> **The Car Mechanic & Quality Inspector Analogy**
> 
> Imagine you bring your car to a mechanic and say: *"Please tune the engine to make it 10% more fuel-efficient."*
> 
> An over-eager rookie mechanic might immediately swap out engine parts. The car might indeed use 10% less fuel, but when you drive away, you discover the air conditioning has stopped working, the speedometer is broken, and the brakes are squeaking! The mechanic didn't check how the engine connected to the electrical and hydraulic systems.
> 
> **ChangeGuard AI is like an expert garage team with strict safety rules:**
> 1. One specialist (**Requirements Analyst**) listens carefully to your request and writes down exact checklist rules.
> 2. A second specialist (**Code Inspector**) examines the car's complete wiring diagram to see every wire connected to the engine.
> 3. A third specialist (**Impact Assessor**) warns: *"If we touch this wire, the dashboard and the air conditioner might lose power!"*
> 4. A mechanic (**Developer Agent**) makes the smallest possible adjustment on a replica test engine.
> 5. A test driver (**Testing Agent**) turns the key, drives the replica, and checks if the air conditioning and brakes still work. If something fails, the mechanic fixes it immediately.
> 6. An independent inspector (**Reviewer Agent**) verifies that only the requested parts were touched.
> 7. Finally, they hand you a full checklist and test report: **You decide whether to approve the changes to your actual car.**

---

### Level 2: The Intermediate Explanation (Software Engineering Concepts)
> **Multi-Agent Regression Prevention**
> 
> Today, tools like ChatGPT and GitHub Copilot generate code easily. However, in a real software system, code files are tightly coupled through imports and function calls. When an AI generates a patch to modify a function in `billing.py`, it can inadvertently break `refunds.py` or `reports.py`.
> 
> ChangeGuard AI solves this using a **multi-agent workflow**:
> - Instead of asking a single AI to *"do everything"* (which causes hallucinations and scope creep), we break the job into specialist agents.
> - We analyze the repository using **Abstract Syntax Trees (AST)**. This reads the Python grammar directly without executing the code, extracting which functions call which other functions.
> - We build a **Reverse Dependency Graph** and run a **Breadth-First Search (BFS)**. If you edit `billing.py`, the system calculates that `refunds.py` and `reports.py` are directly impacted (depth 1), while `customer_service.py` and `dashboard.py` are indirectly impacted (depth 2 and 3).
> - We generate a minimal Git diff on a sandboxed branch, execute `pytest` suites to verify that old features still work (regression testing) and new features work (acceptance testing), and show the whole evidence report on a web dashboard for human approval.

---

### Level 3: The Deep Technical Explanation (Architectural & Algorithmic Rigor)
> **Deterministic AST-Grounded Multi-Agent Verification**
> 
> ChangeGuard AI investigates the research hypothesis: *Can a requirement-traceable multi-agent workflow produce more verifiably correct software changes than a single-agent baseline under identical compute and model constraints?*
> 
> The system architecture comprises:
> 1. **Deterministic Orchestration:** A finite state machine (FSM) controlling agent handoffs, bounded retry loops (maximum 2 repair attempts), and stopping criteria. This prevents open-ended conversation drift.
> 2. **Static AST Symbol & Import Extraction:** Utilizing Python's native `ast` library, the analyzer visits `FunctionDef`, `ClassDef`, `Import`, and `ImportFrom` nodes. This creates a deterministic symbol index with zero execution security risk.
> 3. **Semantic Git Diff Localization:** By executing `git diff --unified=0`, the analyzer extracts modified line intervals via regex, mapping them onto AST function nodes where `node.lineno <= line <= node.end_lineno`.
> 4. **Reverse Import Graph Traversal:** Inverts directed import edges ($A \rightarrow B$ becomes $B \leftarrow A$) and executes queue-based BFS traversal to identify transitive closures of affected modules across arbitrary dependency depths.
> 5. **Adversarial Validation & Isolated Execution:** Patches are generated and tested in isolated Git worktrees. Acceptance and regression suites are executed via `pytest`. The independent reviewer agent (A6) cross-checks the unified diff against the impact graph to catch unapproved modifications (scope creep).
> 6. **Human Approval Boundary:** Enforces zero autonomous merges to production or protected branches.

---

## 2. Timed Presentation Scripts

### The 1-Minute Elevator Pitch
> *"Hello! Every software team has experienced the nightmare of making a small, innocent code change—like adding a 10% discount for premium customers—only to discover that it broke refund calculations and corrupted the sales reports. Today, developers use AI to generate code, but AI often introduces regressions and scope creep.*
> 
> *Our project is **ChangeGuard AI**. It's an evidence-oriented multi-agent framework developed at SLIIT for the Innodata AI Catalyst. Instead of blind code generation, ChangeGuard AI breaks requirements into acceptance criteria, uses static AST analysis to trace every direct and indirect dependent file, generates a minimal patch in an isolated sandbox, runs automated regression tests, and gives human reviewers an auditable evidence package before any code is merged.*
> 
> *In short: ChangeGuard AI ensures AI writes code that is provably safe, requirement-compliant, and regression-free."*

---

### The 5-Minute Complete Project Explanation
> **1. The Problem (1 min):**
> Modern software development has embraced AI code generation. As GitHub Octoverse 2026 notes, over 40% of PR drafts start with AI. But as Google's DORA 2024 report proved, AI adoption without verification reduces delivery stability. AI doesn't know what it broke.
> 
> **2. The Solution & Multi-Agent Concept (1.5 min):**
> ChangeGuard AI replaces unconstrained single-agent coding with a team of six specialized agents:
> - Requirement Analyst deconstructs user prompts into testable criteria.
> - Codebase Intelligence indexes all symbols using Abstract Syntax Trees.
> - Change Impact builds a reverse dependency graph and performs BFS to map affected files.
> - Developer Agent authors a minimal diff on an isolated branch.
> - Testing Agent executes pytest suites for acceptance and regression checks.
> - Independent Reviewer adversarially evaluates the diff for scope creep.
> 
> **3. Engineering Implementation (1.5 min):**
> We chose Python's standard `ast` library for static analysis because it parses source code safely without executing untrusted code. Our `git_diff_analyzer` correlates line hunks with AST function nodes. Our BFS algorithm in `impact_agent.py` tracks multi-tier dependencies, as demonstrated in our `mini_pos` testbed where changing `billing.py` accurately flags `refunds.py`, `reports.py`, `customer_service.py`, and `dashboard.py`.
> 
> **4. Human Oversight & Benchmarking (1 min):**
> ChangeGuard AI never deploys autonomously. It presents an auditable change package on a web dashboard for human sign-off. Furthermore, we are benchmarking our multi-agent framework against a single-agent baseline on identical tasks, token budgets, and models to empirically measure correctness and regression prevention.

---

### The 10-Minute Technical Walkthrough
1. **Introduction & Motivation (2 min):** Detail SWE-bench real-world challenge, coupling problems in legacy systems, and the motivation for automated impact analysis.
2. **Architecture & State Machine (2 min):** Walk through the FSM diagram in `ARCHITECTURE.md`. Explain why deterministic orchestration is superior to free-form agent chat.
3. **Deep Dive into Implemented Code (3 min):**
   - Open `project/agents/mujahidh_codebase/analyzer.py` and explain AST parsing.
   - Open `project/agents/mujahidh_codebase/impact_agent.py` and explain inverted graph construction and BFS traversal.
   - Open `sample_projects/mini_pos/` and trace how `billing.py` cascades to `dashboard.py`.
4. **Verification, Sandbox & Security Guardrails (2 min):** Explain Git worktrees, path validation, pytest isolation, and why `shell=True` and arbitrary `pip install` are prohibited.
5. **Evaluation, Status & Roadmap (1 min):** Show progress against the Nov 20–29 hackathon timeline, explain current milestone completion (M2), and detail next steps (orchestrator and UI integration).

---

## 3. Common Supervisor & Judge Questions (With Accurate Answers)

### Q1: *"Why use multiple agents instead of a single LLM with a detailed prompt?"*
**Answer:** Single-agent LLMs suffer from severe role confusion, context dilution, and confirmation bias. When the same agent writes the code and checks its own work, it consistently overlooks its own blind spots. Decoupling into specialist agents (where one indexes symbols, another writes minimal diffs, and an independent adversarial reviewer verifies test evidence) enforces separation of concerns. Most importantly, our project includes an empirical benchmark specifically comparing our multi-agent framework against a single-agent baseline on identical tasks.

### Q2: *"Why did you write your own AST analyzer instead of just asking an LLM which files are affected?"*
**Answer:** LLMs hallucinate dependencies, especially in large codebases. Static AST analysis is 100% deterministic, mathematically sound, runs in milliseconds, costs zero API tokens, and poses zero security risk because it never executes the target code. We use deterministic algorithms where they excel (AST parsing, graph search) and reserve LLMs for where reasoning is required (interpreting requirements and authoring diffs).

### Q3: *"What happens if a repository has circular imports?"*
**Answer:** In `impact_agent.py`, our BFS implementation maintains an explicit `visited` set. When traversing dependencies from `queue.popleft()`, if an imported file has already been visited, it is skipped (`if filename in visited: continue`). This guarantees that circular imports cannot trigger an infinite loop or stack overflow.

### Q4: *"Can ChangeGuard AI automatically deploy code to production?"*
**Answer:** Absolutely not. By design, ChangeGuard AI enforces a mandatory Human-in-the-Loop gate. The system outputs an auditable change package containing the unified diff, test execution logs, and risk notes. No branch is merged or pushed without explicit human approval.

### Q5: *"What is the current status of the repository?"*
**Answer:** The project is on schedule for the Innodata AI Catalyst ’26 MVP deadline (29 November). As of 24 November, the entire static codebase intelligence and impact analysis engine (M2) is fully implemented and verified with 436 lines of Python code and a multi-tier POS reference testbed on the `feature/mujahidh-codebase` branch. The orchestrator, test runner, and UI dashboard are scheduled for the 24–28 November milestone window.

---

## 4. Key Technical Terms Glossary

- **AST (Abstract Syntax Tree):** A tree representation of the abstract syntactic structure of source code written in a programming language.
- **Regression:** A software bug where an existing, working feature stops working after a new change or bugfix is introduced.
- **Change Impact Analysis (CIA):** The technique of identifying the potential consequences of a change or estimating what needs to be modified to accomplish a change.
- **Transitive Dependency:** An indirect dependency (if A depends on B, and B depends on C, then A transitively depends on C).
- **BFS (Breadth-First Search):** An algorithm for traversing tree or graph data structures layer-by-layer, guaranteeing discovery of shortest dependency paths.
- **Git Worktree:** A Git feature allowing multiple working directories attached to the same repository, enabling isolated branch checkouts without re-cloning.
- **Scope Creep:** Unnecessary or unrequested modifications introduced during a code change that touch files outside the required boundaries.
- **Adversarial Review:** A verification approach where an agent actively attempts to find flaws, missing tests, or unapproved diffs in proposed work.
