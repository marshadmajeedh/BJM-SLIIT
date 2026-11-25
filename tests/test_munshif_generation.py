import json

import pytest

from project.agents.munshif_testing.agent import generate_tests


def test_generation_passes_source_and_existing_tests_to_model(tmp_path):
    (tmp_path / "billing.py").write_text("def checkout(x): return x\n")
    (tmp_path / "test_billing.py").write_text("def test_checkout(): assert True\n")
    impact = {"source_path": ".", "reports": [{"changed_file": "billing.py"}]}
    def model(prompt):
        assert "def checkout" in prompt and "test_checkout" in prompt
        return json.dumps({"scenarios": [{"title": "zero", "rationale": "boundary", "assumptions": "zero allowed", "test_code": "def test_zero():\n    assert checkout(0) == 0\n"}]})
    result = generate_tests(tmp_path, impact, model)
    assert result["executed"] is False
    assert result["scenarios"][0]["title"] == "zero"


def test_rejects_invalid_output_and_paths(tmp_path):
    (tmp_path / "billing.py").write_text("pass\n")
    impact = {"reports": [{"changed_file": "billing.py"}]}
    with pytest.raises(ValueError):
        generate_tests(tmp_path, impact, lambda _: '{"scenarios": []}')
    with pytest.raises(ValueError):
        generate_tests(tmp_path, {"reports": [{"changed_file": "../outside.py"}]}, lambda _: "{}")
