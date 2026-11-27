# Munshif's module: file guide

```text
project/agents/munshif_testing/
├── agent.py          # Builds model context and validates proposed scenarios
├── groq_model.py     # Optional Groq model adapter
├── export.py         # Exports proposals as files for human review
├── __main__.py       # Command-line entry point
├── __init__.py       # Python package marker
├── README.md         # Setup, usage and limitations
└── STRUCTURE.md      # This guide

project/nafris_verification/generated_test_runner.py  # Reviewed-test handoff
tests/test_munshif_generation.py                     # Generator checks
tests/test_generated_handoff.py                      # Verification handoff checks
tests/test_agent_handoff_integration.py              # Agent integration check
docs/examples/                                       # Outputs and evidence
```

The module consumes a codebase impact report, produces proposed tests, and can export them for review. The verification runner is a separate component and executes only when called. The orchestrator and dashboard are still under development. See [README.md](README.md) for setup and the [handoff guide](../../../docs/examples/agent_handoff.md) for integration.
