# M01 Codebase Intelligence and Risk Reasoning Handover

## Ownership
M01 implementation is owned under `project/agents/mujahidh_codebase/`. M02, M03, M04, shared schemas, APIs, CI, and protected baseline projects are not modified by M01.

## Architecture
- `analyzer.py`: discovers Python source, parses ASTs, records functions/classes/imports/source locations, and reports syntax/read errors.
- `git_diff_analyzer.py`: resolves an explicit base commit and detects added, modified, renamed, and deleted Python files plus changed functions and lines.
- `impact_agent.py`: builds a reverse local-dependency graph, supports absolute and relative imports, and performs breadth-first direct/indirect impact analysis.
- `codebase_agent.py`: combines AST, Git, and impact evidence into the M01 report.
- `risk_reasoning_agent.py`: calls Groq for a bounded, validated JSON risk analysis. Human verification remains mandatory.

## M01 input and output
`run_codebase_agent(repository_path, source_path, base_ref="HEAD")` returns the Codebase Intelligence report. `analyze_regression_risk(codebase_report, requirement, code_diff)` returns:

- `summary`
- `affected_files`
- `potential_risks` (`file`, `issue`, `evidence`, `severity`)
- `recommended_tests`
- `requires_verification`
- `agent`, `model`, and `status`

## M02 integration
M02's `CodebaseAdapter` consumes `run_codebase_agent()` and normalizes it to `CodebaseAnalysisResult`, `ChangedFileImpact`, and `ImpactedFile`. M02 distinguishes `completed`, `no_changes`, and `error`. M01's raw payload is preserved by M02 for evidence review. The Groq risk report is separate and has no additional M02 schema extension; M02 ownership must decide later whether to add it.

## Dependency and Git limitations
Static dependencies indicate possible impact, not confirmed defects. Third-party imports are not treated as local modules. Dynamic dispatch, reflection, decorators, and configuration-driven imports may be missed. Deleted files have no live AST evidence. Diff analysis is bounded to tracked Python paths under the selected source directory.

## Groq configuration
Set `GROQ_API_KEY` and optionally `GROQ_MODEL`. The SDK integration never requires a key for mocked tests. Source and diff input are JSON-bounded before external delivery, but repository requirements and source content must still be reviewed as confidential data before live use.

## Run and test
```powershell
python -m pytest -q tests/test_m01_codebase_intelligence.py
python -m pytest -q tests/test_m02_adapters.py tests/test_agent_handoff_integration.py
python scripts/demo_m01.py
```

## Demo behavior
The demo creates a temporary Git repository, proves 10% to 15% discount direction, detects `apply_discount`, and reports direct/indirect impact evidence. It does not alter the shared Mini POS baseline.
