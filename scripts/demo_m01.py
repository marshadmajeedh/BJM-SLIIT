import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from project.agents.mujahidh_codebase.codebase_agent import run_codebase_agent


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="changeguard-m01-") as directory:
        repo = Path(directory)
        git(repo, "init")
        git(repo, "config", "user.name", "Demo User")
        git(repo, "config", "user.email", "demo@example.invalid")

        source = repo / "mini_pos"
        source.mkdir()
        billing = source / "billing.py"
        service = source / "customer_service.py"
        checkout = source / "checkout.py"

        billing.write_text(
            "def apply_discount(total, customer_type):\n"
            "    if customer_type == 'premium':\n"
            "        return total * 0.90\n"
            "    return total\n",
            encoding="utf-8",
        )
        service.write_text("from billing import apply_discount\n", encoding="utf-8")
        checkout.write_text("from customer_service import apply_discount\n", encoding="utf-8")
        git(repo, "add", "mini_pos")
        git(repo, "commit", "-m", "baseline 10 percent discount")

        billing.write_text(billing.read_text(encoding="utf-8").replace("0.90", "0.85"), encoding="utf-8")

        report = run_codebase_agent(str(repo), "mini_pos", "HEAD")
        diff = git(repo, "diff", "HEAD", "--", "mini_pos/billing.py")

        print(json.dumps({
            "discount_direction": "increased from 10% to 15% (0.90 to 0.85 multiplier)",
            "git_diff": diff,
            "codebase_analysis": report,
            "requires_human_verification": True,
            "groq_analysis": "Set GROQ_API_KEY to run live risk reasoning; no key was used by this demo.",
        }, indent=2))


if __name__ == "__main__":
    main()
