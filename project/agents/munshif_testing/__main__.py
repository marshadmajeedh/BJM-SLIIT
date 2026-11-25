"""Generate proposals from a saved codebase-agent JSON report."""
import argparse
import json
from pathlib import Path

from .agent import generate_tests
from .groq_model import generate

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("repository", help="Repository root")
parser.add_argument("impact_report", help="Codebase-agent JSON report")
parser.add_argument("--requirements", default="", help="Business requirements for expected behavior")
args = parser.parse_args()
report = json.loads(Path(args.impact_report).read_text(encoding="utf-8"))
print(json.dumps(generate_tests(args.repository, report, generate, args.requirements), indent=2))
