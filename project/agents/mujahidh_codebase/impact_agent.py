
import ast
import json
from pathlib import Path


def analyze_impact(repository_path, changed_file):
    root = Path(repository_path).resolve()
    changed = Path(changed_file)

    if not root.is_dir():
        raise ValueError("Repository not found")

    if changed.is_absolute() or ".." in changed.parts:
        raise ValueError("Use a relative file path inside the repository")

    target = (root / changed).resolve()

    if not target.is_file() or not target.is_relative_to(root):
        raise ValueError("Changed file not found")

    changed_module = changed.with_suffix("").as_posix().replace("/", ".")

    report = {
        "changed_file": changed.as_posix(),
        "potentially_impacted_files": [],
        "analysis_method": "static_import_analysis"
    }

    for file in sorted(root.rglob("*.py")):
        if file == target:
            continue

        if any(part in {".git", ".venv", "venv", "__pycache__"}
               for part in file.relative_to(root).parts):
            continue

        try:
            tree = ast.parse(file.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeError, OSError):
            continue

        evidence = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == changed_module:
                    names = [alias.name for alias in node.names]
                    evidence.append({
                        "line": node.lineno,
                        "imported_symbols": names
                    })

            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == changed_module:
                        evidence.append({
                            "line": node.lineno,
                            "imported_module": alias.name
                        })

        if evidence:
            report["potentially_impacted_files"].append({
                "file": file.relative_to(root).as_posix(),
                "reason": "Directly imports the changed module",
                "evidence": evidence
            })

    report["total_impacted"] = len(
        report["potentially_impacted_files"]
    )

    return report


if __name__ == "__main__":
    result = analyze_impact(
        "sample_projects/mini_pos",
        "billing.py"
    )
    print(json.dumps(result, indent=2))
