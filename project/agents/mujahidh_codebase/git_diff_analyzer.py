
import ast
import json
import re
import subprocess
from pathlib import Path


EXCLUDED_DIRS = {".git", ".venv", "venv", "env", "__pycache__", "node_modules", ".tox", ".nox"}


def run_git(repo, *args, timeout=15):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    return result.stdout


def resolve_base(repo, base="HEAD"):
    if not isinstance(base, str) or not base.strip():
        raise ValueError("Base revision cannot be empty")
    return run_git(repo, "rev-parse", "--verify", "--end-of-options", f"{base.strip()}^{{commit}}").strip()


def _parse_name_status(output: str) -> list[dict]:
    fields = output.split("\0")
    changes = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if not status:
            continue
        code = status[:1]
        if code == "R":
            if index + 1 >= len(fields):
                break
            old_name, new_name = fields[index], fields[index + 1]
            index += 2
            changes.append({"status": "renamed", "file": new_name, "old_file": old_name})
        elif code == "D":
            if index >= len(fields):
                break
            changes.append({"status": "deleted", "file": fields[index]})
            index += 1
        else:
            if index >= len(fields):
                break
            status_name = "added" if code == "A" else "modified"
            changes.append({"status": status_name, "file": fields[index]})
            index += 1
    return changes


def changed_functions(repository_path, base="HEAD"):
    root = Path(repository_path).resolve()
    if not root.is_dir():
        raise ValueError("Repository folder not found")

    resolved_base = resolve_base(root, base)
    output = run_git(root, "diff", "--name-status", "-z", resolved_base, "--", "*.py")
    results = []

    for change in _parse_name_status(output):
        relative_name = change["file"]
        if not relative_name.endswith(".py"):
            continue
        parts = Path(relative_name.replace("\\", "/")).parts
        if any(part in EXCLUDED_DIRS for part in parts):
            continue

        changed_lines = set()
        diff = run_git(root, "diff", "--unified=0", resolved_base, "--", relative_name)
        for line in diff.splitlines():
            if line.startswith("@@"):
                match = re.search(r"\+(\d+)(?:,(\d+))?", line)
                if match:
                    start = int(match.group(1))
                    count = int(match.group(2) or 1)
                    changed_lines.update(range(start, start + count))

        functions = []
        parse_error = None
        path = root / relative_name
        if change["status"] != "deleted" and path.is_file():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        end_line = getattr(node, "end_lineno", node.lineno)
                        if any(node.lineno <= line <= end_line for line in changed_lines):
                            functions.append(node.name)
                    elif isinstance(node, ast.ClassDef):
                        end_line = getattr(node, "end_lineno", node.lineno)
                        if any(node.lineno <= line <= end_line for line in changed_lines):
                            functions.append(node.name)
            except (SyntaxError, UnicodeError, OSError) as error:
                parse_error = str(error)

        result = {
            "file": relative_name,
            "status": change["status"],
            "changed_lines": sorted(changed_lines),
            "changed_functions": sorted(set(functions)),
            "base_commit": resolved_base,
        }
        if change.get("old_file"):
            result["old_file"] = change["old_file"]
        if parse_error:
            result["analysis_error"] = parse_error
        results.append(result)

    return results


if __name__ == "__main__":
    print(json.dumps(changed_functions("."), indent=2))
