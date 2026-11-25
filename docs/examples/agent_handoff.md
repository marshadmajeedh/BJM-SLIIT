# Orchestrator handoff

The repository currently has no Marsadh orchestrator implementation. These Python interfaces provide a handoff for it; they do not implement a backend, shared state, or automatic execution.

1. Call `run_codebase_agent(repository_path, source_path, base_ref)` from `project.agents.mujahidh_codebase.codebase_agent`. The agent compares tracked Python files with the selected Git base. Inspect the resulting `reports` and handle an empty list as no detected changes.
2. Call `generate_tests(repository_path, impact_report, model, requirements)` from `project.agents.munshif_testing.agent`. The model is a callable returning JSON text. `project.agents.munshif_testing.groq_model.generate` is an optional external model adapter. The response is a proposed artifact, not execution evidence.
3. Call `export_scenarios(proposals, output_dir)` from `project.agents.munshif_testing.export`, choosing an empty directory inside the repository copy where tests will eventually run. Present the files, assumptions, and expected values for human review. Persist the reviewed file paths separately from the original model response.
4. After review, in an appropriately isolated environment, call `run_reviewed_tests(repository_path, reviewed_file_paths)` from `project.nafris_verification.generated_test_runner`. Paths must be within that repository; the result includes status, exit code, counts, and failure evidence. Timeout and runner errors are not passing results.

These functions perform no automatic transition from generation to execution. The verification runner executes Python code and is not itself a sandbox. Model input includes affected source and discovered tests, so an external provider must only receive data the user is authorized to share. A model proposal, execution result, and human-reviewed business expectation are distinct artifacts.

`tests/test_agent_handoff_integration.py` exercises these interfaces against a real temporary Git change and a deterministic model stub. It validates the handoff, not model quality or production isolation.
