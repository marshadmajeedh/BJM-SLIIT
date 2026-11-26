
import ast
import json
import subprocess
from pathlib import Path


def run_git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True
    )
    return result.stdout


def changed_functions(repository_path, base="HEAD"):
    root = Path(repository_path).resolve()

    changed_paths = run_git(
        root, "diff", "--name-only",
        base, "--", "*.py"
    ).splitlines()

    results = []

    for relative_name in changed_paths:
        path = root / relative_name

        if not path.is_file():
            continue

        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        diff = run_git(
            root, "diff", "--unified=0",
            base, "--", relative_name
        )

        changed_lines = set()

        for line in diff.splitlines():
            if line.startswith("@@"):
                import re
                match = re.search(
                    r"\+(\d+)(?:,(\d+))?", line
                )
                if match:
                    start = int(match.group(1))
                    count = int(match.group(2) or 1)
                    changed_lines.update(
                        range(start, start + count)
                    )

        functions = []

        for node in ast.walk(tree):
            if isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                if any(
                    node.lineno <= line <= node.end_lineno
                    for line in changed_lines
                ):
                    functions.append(node.name)

        results.append({
            "file": relative_name,
            "changed_lines": sorted(changed_lines),
            "changed_functions": sorted(set(functions))
        })

    return results


if __name__ == "__main__":
    print(json.dumps(
        changed_functions("."),
        indent=2
    ))
