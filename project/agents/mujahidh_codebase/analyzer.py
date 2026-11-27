
import ast
import json
from pathlib import Path


EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules", ".tox", ".nox"
}


def _qualified_names(tree: ast.Module) -> tuple[list[dict], list[dict]]:
    functions = []
    classes = []

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes.append({
                "name": node.name,
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
            })
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions.append({
                        "name": f"{node.name}.{item.name}",
                        "line": item.lineno,
                        "end_line": getattr(item, "end_lineno", item.lineno),
                        "kind": "async_function" if isinstance(item, ast.AsyncFunctionDef) else "method",
                    })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({
                "name": node.name,
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "kind": "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function",
            })

    return sorted(functions, key=lambda item: item["line"]), sorted(classes, key=lambda item: item["line"])


def analyze_repository(repository_path: str) -> dict:
    root = Path(repository_path).resolve()
    if not root.is_dir():
        raise ValueError(f"Repository not found: {root}")

    report = {"repository": str(root), "files": [], "total_files": 0, "errors": []}

    for path in sorted(root.rglob("*.py")):
        relative_path = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in relative_path.parts):
            continue

        file_info = {
            "path": relative_path.as_posix(),
            "functions": [],
            "classes": [],
            "imports": [],
        }

        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            functions, classes = _qualified_names(tree)
            file_info["functions"] = functions
            file_info["classes"] = classes

            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend({
                        "name": alias.name,
                        "line": node.lineno,
                        "kind": "absolute",
                        "level": 0,
                    } for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.append({
                        "name": node.module or "",
                        "line": node.lineno,
                        "kind": "relative" if node.level else "absolute",
                        "level": node.level,
                    })
            file_info["imports"] = sorted(imports, key=lambda item: (item["line"], item["name"]))
        except (SyntaxError, UnicodeError, OSError) as error:
            error_info = {
                "path": relative_path.as_posix(),
                "error": str(error),
                "kind": type(error).__name__,
            }
            file_info["error"] = str(error)
            report["errors"].append(error_info)

        report["files"].append(file_info)

    report["total_files"] = len(report["files"])
    return report


if __name__ == "__main__":
    result = analyze_repository("sample_projects/mini_pos")
    print(json.dumps(result, indent=2))
