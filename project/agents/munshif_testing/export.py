"""Export proposed scenarios for human review, never execute them."""
import ast
from pathlib import Path


def export_scenarios(proposals, output_dir):
    """Write numbered files to an empty directory and return their paths."""
    if proposals.get("status") != "proposed" or proposals.get("executed") is not False:
        raise ValueError("Expected unexecuted proposals")
    scenarios = proposals.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("No scenarios to export")
    codes = []
    for item in scenarios:
        code = item.get("test_code") if isinstance(item, dict) else None
        if not isinstance(code, str):
            raise ValueError("Missing test code")
        tree = ast.parse(code)
        if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_") for n in tree.body):
            raise ValueError("Scenario has no test function")
        codes.append(code)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Export directory must be empty")
    paths = []
    for number, code in enumerate(codes, 1):
        path = output / f"test_generated_{number:03}.py"
        path.write_text(code + "\n", encoding="utf-8")
        paths.append(str(path))
    return paths
