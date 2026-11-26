
import ast
import json
from collections import deque
from pathlib import Path


EXCLUDED = {".git", ".venv", "venv", "__pycache__"}


def build_dependency_graph(repository_path):
    root = Path(repository_path).resolve()

    if not root.is_dir():
        raise ValueError("Repository folder not found")

    files = {
        p.relative_to(root).as_posix(): p
        for p in root.rglob("*.py")
        if not any(
            part in EXCLUDED
            for part in p.relative_to(root).parts
        )
    }

    # Module name -> source file
    modules = {
        name[:-3].replace("/", "."): name
        for name in files
    }

    # Maps imported file -> files depending on it
    reverse_graph = {name: [] for name in files}

    for filename, path in files.items():
        try:
            tree = ast.parse(
                path.read_text(encoding="utf-8")
            )
        except (SyntaxError, UnicodeError, OSError):
            continue

        dependencies = set()

        for node in ast.walk(tree):
            imported_modules = []

            if isinstance(node, ast.Import):
                imported_modules = [
                    alias.name for alias in node.names
                ]

            elif isinstance(node, ast.ImportFrom):
                # MVP supports absolute imports only.
                if node.level == 0 and node.module:
                    imported_modules = [node.module]

            for module in imported_modules:
                if module in modules:
                    target = modules[module]

                    if target != filename:
                        dependencies.add(
                            (target, node.lineno)
                        )

        for target, line in sorted(dependencies):
            reverse_graph[target].append({
                "file": filename,
                "import_line": line
            })

    return reverse_graph


def analyze_impact(repository_path, changed_file):
    root = Path(repository_path).resolve()
    graph = build_dependency_graph(root)

    changed_path = Path(changed_file)

    if changed_path.is_absolute() or ".." in changed_path.parts:
        raise ValueError("Use a relative repository file path")

    changed = changed_path.as_posix()

    if changed not in graph:
        raise ValueError("Changed Python file not found")

    queue = deque([(changed, 0)])
    visited = {changed}
    impacted = []

    while queue:
        current, depth = queue.popleft()

        for dependent in graph[current]:
            filename = dependent["file"]

            if filename in visited:
                continue

            visited.add(filename)

            level = depth + 1

            impacted.append({
                "file": filename,
                "impact_level": (
                    "direct" if level == 1 else "indirect"
                ),
                "dependency_depth": level,
                "via_file": current,
                "import_line": dependent["import_line"],
                "reason": (
                    "Depends on a changed or potentially "
                    "impacted module"
                )
            })

            queue.append((filename, level))

    impacted.sort(
        key=lambda x: (
            x["dependency_depth"],
            x["file"]
        )
    )

    return {
        "changed_file": changed,
        "analysis_method": "static_reverse_dependency_bfs",
        "potentially_impacted_files": impacted,
        "total_impacted": len(impacted),
        "direct_count": sum(
            x["dependency_depth"] == 1
            for x in impacted
        ),
        "indirect_count": sum(
            x["dependency_depth"] > 1
            for x in impacted
        )
    }


if __name__ == "__main__":
    result = analyze_impact(
        "sample_projects/mini_pos",
        "billing.py"
    )
    print(json.dumps(result, indent=2))
