
import json
from pathlib import Path

try:
    from .git_diff_analyzer import changed_functions
    from .impact_agent import analyze_impact
    from .analyzer import analyze_repository
except ImportError:
    from git_diff_analyzer import changed_functions
    from impact_agent import analyze_impact
    from analyzer import analyze_repository


def run_codebase_agent(
    repository_path=".",
    source_path="sample_projects/mini_pos",
    base_ref="HEAD"
):
    git_root = Path(repository_path).resolve()
    source_root = (git_root / source_path).resolve()

    if not git_root.is_dir():
        raise ValueError("Git repository not found")

    if not source_root.is_dir():
        raise ValueError("Source directory not found")

    if not source_root.is_relative_to(git_root):
        raise ValueError("Source must be inside repository")

    repository_analysis = analyze_repository(
        str(source_root)
    )

    changes = changed_functions(
        str(git_root),
        base=base_ref
    )

    reports = []

    for change in changes:
        changed_path = (
            git_root / change["file"]
        ).resolve()

        # Only analyze changes inside selected source directory.
        if not changed_path.is_relative_to(source_root):
            continue

        relative_file = changed_path.relative_to(
            source_root
        ).as_posix()

        impact = analyze_impact(
            str(source_root),
            relative_file
        )

        reports.append({
            "changed_file": change["file"],
            "changed_functions": change["changed_functions"],
            "changed_lines": change["changed_lines"],
            "impact_analysis": impact
        })

    return {
        "agent": "CodebaseIntelligenceAgent",
        "repository": str(git_root),
        "source_path": source_path,
        "analysis_type": "static_code_change_impact",
        "status": "completed",
        "repository_summary": {
            "total_files": repository_analysis["total_files"]
        },
        "reports": reports
    }


if __name__ == "__main__":
    result = run_codebase_agent(
        repository_path=".",
        source_path="sample_projects/mini_pos",
        base_ref="HEAD"
    )
    print(json.dumps(result, indent=2))
