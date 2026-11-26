
import ast
import json
from pathlib import Path


EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules"
}


def analyze_repository(repository_path: str) -> dict:
    root = Path(repository_path).resolve()

    if not root.is_dir():
        raise ValueError(f"Repository not found: {root}")

    report = {
        "repository": str(root),
        "files": [],
        "total_files": 0
    }

    for path in sorted(root.rglob("*.py")):
        relative_path = path.relative_to(root)

        if any(part in EXCLUDED_DIRS for part in relative_path.parts):
            continue

        file_info = {
            "path": relative_path.as_posix(),
            "functions": [],
            "classes": [],
            "imports": []
        }

        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)

            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    file_info["functions"].append(node.name)

                elif isinstance(node, ast.ClassDef):
                    file_info["classes"].append(node.name)

                elif isinstance(node, ast.Import):
                    file_info["imports"].extend(
                        alias.name for alias in node.names
                    )

                elif isinstance(node, ast.ImportFrom):
                    file_info["imports"].append(
                        node.module or ""
                    )

            for key in ("functions", "classes", "imports"):
                file_info[key] = sorted(set(file_info[key]))

        except (SyntaxError, UnicodeError, OSError) as error:
            file_info["error"] = str(error)

        report["files"].append(file_info)

    report["total_files"] = len(report["files"])
    return report


if __name__ == "__main__":
    result = analyze_repository("sample_projects/mini_pos")
    print(json.dumps(result, indent=2))
