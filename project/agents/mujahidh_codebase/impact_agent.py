
import ast
import json
from collections import deque
from pathlib import Path


EXCLUDED = {".git", ".venv", "venv", "env", "__pycache__", "node_modules", ".tox", ".nox"}


def _module_names(relative_name: str) -> set[str]:
    parts = relative_name[:-3].replace("\\", "/").split("/")
    if parts[-1] == "__init__":
        package = ".".join(parts[:-1])
        return {package} if package else {""}
    return {".".join(parts)}


def build_dependency_graph(repository_path):
    root = Path(repository_path).resolve()
    if not root.is_dir():
        raise ValueError("Repository folder not found")

    files = {
        p.relative_to(root).as_posix(): p
        for p in root.rglob("*.py")
        if not any(part in EXCLUDED for part in p.relative_to(root).parts)
    }

    modules = {}
    for filename in files:
        for module_name in _module_names(filename):
            if module_name:
                modules.setdefault(module_name, set()).add(filename)

    reverse_graph = {name: [] for name in files}

    for filename, path in files.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeError, OSError):
            continue

        package_parts = Path(filename).parent.as_posix().split("/") if Path(filename).parent.as_posix() != "." else []
        dependencies = set()

        for node in ast.walk(tree):
            candidates = []
            if isinstance(node, ast.Import):
                candidates = [(alias.name, 0) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = list(package_parts)
                    remove = node.level - 1
                    if remove:
                        base = base[:-remove] if remove <= len(base) else []
                    module = node.module or ""
                    resolved = ".".join(base + ([module] if module else []))
                    if resolved:
                        candidates = [(resolved, node.level)]
                        candidates.append((resolved.rsplit(".", 1)[0], node.level))
                elif node.module:
                    candidates = [(node.module, 0)]

            for imported_name, level in candidates:
                targets = set(modules.get(imported_name, set()))
                if not targets:
                    prefix = imported_name + "."
                    targets = {
                        target
                        for module_name, names in modules.items()
                        if module_name.startswith(prefix)
                        for target in names
                    }
                for target in targets:
                    if target != filename:
                        dependencies.add((target, node.lineno, imported_name))

        for target, line, imported_name in sorted(dependencies):
            reverse_graph[target].append({
                "file": filename,
                "import_line": line,
                "import": imported_name,
            })

    return reverse_graph


def analyze_impact(repository_path, changed_file):
    root = Path(repository_path).resolve()
    graph = build_dependency_graph(root)

    changed_path = Path(str(changed_file).replace("\\", "/"))
    if changed_path.is_absolute() or ".." in changed_path.parts:
        raise ValueError("Use a relative repository file path")

    changed = changed_path.as_posix()
    queue = deque([(changed, 0)])
    visited = {changed}
    impacted = []
    limitations = [] if changed in graph else [
        "Changed Python file is absent, deleted, or excluded from the source analysis"
    ]

    while queue and changed in graph:
        current, depth = queue.popleft()
        for dependent in graph.get(current, []):
            filename = dependent["file"]
            if filename in visited:
                continue
            visited.add(filename)
            level = depth + 1
            impacted.append({
                "file": filename,
                "impact_level": "direct" if level == 1 else "indirect",
                "dependency_depth": level,
                "via_file": current,
                "import_line": dependent["import_line"],
                "import": dependent.get("import", ""),
                "reason": "This file statically imports the changed or potentially impacted module",
            })
            queue.append((filename, level))

    impacted.sort(key=lambda item: (item["dependency_depth"], item["file"]))
    return {
        "changed_file": changed,
        "analysis_method": "static_reverse_dependency_bfs",
        "potentially_impacted_files": impacted,
        "total_impacted": len(impacted),
        "direct_count": sum(item["dependency_depth"] == 1 for item in impacted),
        "indirect_count": sum(item["dependency_depth"] > 1 for item in impacted),
        "analysis_limitations": limitations,
        "evidence_disclaimer": "Dependency relationships indicate possible impact, not that a bug exists",
    }


if __name__ == "__main__":
    result = analyze_impact("sample_projects/mini_pos", "billing.py")
    print(json.dumps(result, indent=2))
