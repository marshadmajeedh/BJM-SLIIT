# Regression test generation agent

`generate_tests(repository_path, impact_report, model, requirements="")` consumes the codebase agent's report, reads affected Python files and existing `test_*.py` files, and asks a model for structured pytest scenarios. It checks the response structure and Python syntax; it does not execute generated code or establish test coverage or a failure. Review assumptions, imports and expected values before passing tests to the verification agent.

Use a callable accepting a prompt and returning JSON text. `groq_model.generate` is an optional adapter requiring the `groq` package and `GROQ_API_KEY` and `GROQ_MODEL` environment variables. It sends the selected source, existing tests, impact report and requirements to Groq: use it only when the data may be shared. No model is called automatically. Model output is untrusted code and must be reviewed and run only in an appropriate isolated environment. Existing test discovery is limited to files named `test_*.py`; their presence is not proof of coverage.

Example integration:

```python
from project.agents.munshif_testing.agent import generate_tests
from project.agents.munshif_testing.groq_model import generate

proposals = generate_tests(repo_path, codebase_report, generate, requirements)
```

The report must contain at least one changed file. Pass the resulting scenarios to the orchestrator for review and verification. A discounted refund expectation such as 900 for a 1000 payment at 10% is an assumption unless supported by the target's requirements; it is not evidence of a failure.

Install the optional adapter with `python -m pip install -r project/requirements.txt`. To generate JSON from a saved codebase-agent report, run `python -m project.agents.munshif_testing . impact.json --requirements "State the approved business rules here"`. Set `GROQ_API_KEY` and `GROQ_MODEL` in the environment first. Save output only after checking it for sensitive information.
