# ChangeGuard AI — Developer Setup & Contribution Guide

This guide provides practical instructions for setting up the ChangeGuard AI development environment, running existing static analysis modules, navigating branches, and implementing pending agents.

---

## 1. Prerequisites & Environment Setup

### 1.1 Required Tools
- **Python:** Version 3.10 or higher (Python 3.14 tested and verified).
- **Git:** Version 2.30+ installed and accessible on system `PATH`.
- **Node.js & npm:** (Optional for Phase 1 frontend development) Node.js 18+.

### 1.2 Clone and Setup
```bash
# Clone the repository
git clone https://github.com/marshadmajeedh/BJM-SLIIT.git
cd BJM-SLIIT

# Check available branches
git branch -a
```

### 1.3 Virtual Environment Setup
```bash
# Create a virtual environment
python -m venv .venv

# Activate on Windows (PowerShell)
.venv\Scripts\Activate.ps1

# Activate on Linux/macOS
source .venv/bin/activate

# Upgrade pip
python -m pip install --upgrade pip
```

---

## 2. Accessing & Running the Implemented Modules

Because the codebase analysis modules are currently developed on the `feature/mujahidh-codebase` branch, follow these steps to inspect and run them:

### 2.1 Checkout the Feature Branch
```bash
# Fetch all remote branches
git fetch origin

# Switch to the feature branch
git checkout feature/mujahidh-codebase
```

### 2.2 Verify the Implemented Codebase Analyzer
You can execute each module directly using standard Python:

#### 1. Test Static Repository AST Analysis
```bash
python -m project.agents.mujahidh_codebase.analyzer
```
*Expected Output:* JSON report listing all Python files in `sample_projects/mini_pos`, their declared functions (`apply_discount`, `checkout`, etc.), classes, and imports.

#### 2. Test Git Diff & Function Mapping
```bash
python -m project.agents.mujahidh_codebase.git_diff_analyzer
```
*Expected Output:* Analyzes uncommitted diffs against `HEAD`. If no files are modified, returns empty list `[]`.

#### 3. Test Reverse Dependency BFS Impact Analysis
```bash
python -m project.agents.mujahidh_codebase.impact_agent
```
*Expected Output:*
```json
{
  "changed_file": "billing.py",
  "analysis_method": "static_reverse_dependency_bfs",
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
  ],
  "total_impacted": 4,
  "direct_count": 2,
  "indirect_count": 2
}
```

#### 4. Run Composite Codebase Intelligence Agent
```bash
python -m project.agents.mujahidh_codebase.codebase_agent
```

---

## 3. Developing Next-Stage Components

### 3.1 Resolving the `requirements.txt` Typo
Rename `project/requirements.txt │` to `project/requirements.txt`:
```powershell
# On PowerShell
Rename-Item -Path "project/requirements.txt `│" -NewName "requirements.txt"
```
Recommended dependencies to add:
```text
fastapi>=0.100.0
uvicorn[standard]>=0.23.0
pydantic>=2.0.0
pytest>=7.4.0
networkx>=3.1
requests>=2.31.0
```

### 3.2 Implementing the Orchestrator (`project/marsadh_orchestrator/`)
The orchestrator should implement a simple state machine:
```python
class TaskState:
    PENDING = "pending"
    ANALYZING = "analyzing"
    IMPACT_MAPPED = "impact_mapped"
    PATCHING = "patching"
    TESTING = "testing"
    REPAIRING = "repairing"
    REVIEWING = "reviewing"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
```
Key constraints:
- Enforce `max_retries = 2` on the repair loop.
- Persist task payloads as JSON in an `execution_logs/` folder.

### 3.3 Adding Baseline Regression Tests to `mini_pos`
To support verification by A5, add test files under `sample_projects/mini_pos/tests/` or `tests/`:
```python
# sample_projects/mini_pos/tests/test_billing.py
from sample_projects.mini_pos.billing import calculate_total, apply_discount, checkout

def test_calculate_total():
    assert calculate_total(100, 3) == 300

def test_apply_discount_regular():
    assert apply_discount(1000, "regular") == 1000

def test_checkout_regular():
    assert checkout(500, 2, "regular") == 1000
```

---

## 4. Coding Conventions & Safety Guardrails

1. **Path Safety:** Always validate paths using `pathlib.Path.resolve()` and verify that paths do not contain `..` or escape the repository root.
2. **Subprocess Isolation:** Never use `shell=True` when invoking external processes. Pass arguments as lists:
   ```python
   # Correct:
   subprocess.run(["git", "diff", "--unified=0"], capture_output=True, text=True)
   
   # Forbidden:
   subprocess.run(f"git diff {user_input}", shell=True)
   ```
3. **No Secret Commits:** Never hardcode API keys or credentials. Use environment variables (`os.getenv("OPENAI_API_KEY")` or `os.getenv("GEMINI_API_KEY")`).
4. **Isolated Worktrees:** When applying patches, create a dedicated temporary worktree:
   ```bash
   git worktree add -b temp-patch-sandbox ../sandbox-worktree HEAD
   ```
   Never apply speculative code patches directly onto the active development branch.
