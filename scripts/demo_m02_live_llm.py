"""Manual live Groq LLM verification script for ChangeGuard AI (M02 — P0-4).

IMPORTANT:
- This script is NEVER executed by pytest or CI.
- It makes REAL external API calls to Groq, transmits the temporary mini_pos
  fixture code/impact report/requirement, and consumes API quota.
- Requires explicit ``--confirm-live-calls`` flag plus ``GROQ_API_KEY`` and
  ``GROQ_MODEL`` set in the current process environment.
- Never prints or logs ``GROQ_API_KEY``.
- Leaves the main ChangeGuard repository completely untouched.

Windows PowerShell Usage:
    $env:GROQ_API_KEY = "<your-groq-api-key>"
    $env:GROQ_MODEL = "<your-configured-groq-model>"
    .\\.venv\\Scripts\\python.exe scripts/demo_m02_live_llm.py --confirm-live-calls
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from project.main import create_app  # noqa: E402
from project.marsadh_orchestrator import OrchestratorConfig  # noqa: E402

MINI_POS_SRC = REPO_ROOT / "sample_projects" / "mini_pos"
REQUIREMENT = (
    "Apply a 10% discount to premium customers only, without changing regular "
    "customer checkout or breaking dependent refund, dashboard and report modules."
)

BASELINE_BILLING = (
    "def calculate_total(price, quantity):\n"
    "    return price * quantity\n\n\n"
    "def apply_discount(total, customer_type):\n"
    "    return total\n\n\n"
    "def checkout(price, quantity, customer_type):\n"
    "    total = calculate_total(price, quantity)\n"
    "    return apply_discount(total, customer_type)\n"
)

CORRECT_BILLING = (
    "def calculate_total(price, quantity):\n"
    "    return price * quantity\n\n\n"
    "def apply_discount(total, customer_type):\n"
    "    if customer_type == 'premium':\n"
    "        return total * 0.90\n"
    "    return total\n\n\n"
    "def checkout(price, quantity, customer_type):\n"
    "    total = calculate_total(price, quantity)\n"
    "    return apply_discount(total, customer_type)\n"
)

REGRESSIVE_BILLING = (
    "def calculate_total(price, quantity):\n"
    "    return price * quantity\n\n\n"
    "def apply_discount(total, customer_type):\n"
    "    return total * 0.90\n\n\n"
    "def checkout(price, quantity, customer_type):\n"
    "    total = calculate_total(price, quantity)\n"
    "    return apply_discount(total, customer_type)\n"
)

UNSAFE_MODULES = frozenset(
    {"subprocess", "socket", "urllib", "requests", "httpx", "shutil", "ftplib", "http"}
)
UNSAFE_CALLS = frozenset({"eval", "exec", "compile", "__import__", "system", "popen", "rmtree", "unlink"})


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _prepare_fixture(repo: Path, billing_code: str) -> dict[str, str]:
    app_dir = repo / "mini_pos"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "billing.py").write_text(BASELINE_BILLING, encoding="utf-8")
    for fname in ("refunds.py", "customer_service.py", "dashboard.py", "reports.py"):
        shutil.copyfile(MINI_POS_SRC / fname, app_dir / fname)

    _git(repo, "init")
    _git(repo, "config", "user.name", "ChangeGuard Live Demo")
    _git(repo, "config", "user.email", "live-demo@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline mini_pos")
    base_commit = _git(repo, "rev-parse", "HEAD")

    (app_dir / "billing.py").write_text(billing_code, encoding="utf-8")
    return {"base_commit": base_commit}


def _static_safety_check(test_code: str) -> dict[str, Any]:
    """Static pre-review safety & structure inspection (never executes code)."""
    flags: list[str] = []
    imports: list[str] = []
    test_funcs: list[str] = []
    assert_count = 0
    uses_mock = False
    try:
        tree = ast.parse(test_code)
    except SyntaxError as exc:
        return {
            "syntax_valid": False,
            "safe_for_review": False,
            "flags": [f"SyntaxError: {exc}"],
            "imports": [],
            "test_functions": [],
            "assert_count": 0,
            "uses_mock": False,
        }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                imports.append(alias.name)
                if top in UNSAFE_MODULES:
                    flags.append(f"Potentially unsafe import: {alias.name}")
                if "mock" in alias.name.lower():
                    uses_mock = True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            top = mod.split(".")[0]
            imports.append(mod)
            if top in UNSAFE_MODULES:
                flags.append(f"Potentially unsafe import-from: {mod}")
            if "mock" in mod.lower():
                uses_mock = True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                test_funcs.append(node.name)
        elif isinstance(node, ast.Assert):
            assert_count += 1
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in UNSAFE_CALLS:
                flags.append(f"Potentially unsafe call: {node.func.id}()")
            elif isinstance(node.func, ast.Attribute) and node.func.attr in UNSAFE_CALLS:
                flags.append(f"Potentially unsafe attribute call: .{node.func.attr}()")

    if assert_count == 0:
        flags.append("No explicit assert statements detected")

    return {
        "syntax_valid": True,
        "safe_for_review": not any("unsafe" in f.lower() for f in flags),
        "flags": flags,
        "imports": sorted(set(imports)),
        "test_functions": test_funcs,
        "assert_count": assert_count,
        "uses_mock": uses_mock,
    }


def _poll_until(
    client: httpx.Client,
    base_url: str,
    run_id: str,
    stop_states: set[str],
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        detail = client.get(f"{base_url}/api/runs/{run_id}").json()
        if detail["state"] in stop_states:
            return detail
        time.sleep(0.25)
    raise TimeoutError(f"Run {run_id} did not reach {stop_states} within {timeout_s}s")


def _find_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])


def _run_live_scenario(
    client: httpx.Client,
    base_url: str,
    *,
    label: str,
    repo_id: str,
    expect_pass: bool,
    auto_approve_safe: bool,
) -> dict[str, Any]:
    print(f"\n{'=' * 78}")
    print(f"{label} (repository_id={repo_id}, execution_mode=live_llm)")
    print(f"{'=' * 78}")

    t_create = time.perf_counter()
    create_resp = client.post(
        f"{base_url}/api/runs",
        json={
            "repository_id": repo_id,
            "source_path": "mini_pos",
            "base_ref": "HEAD",
            "requirements": REQUIREMENT,
            "execution_mode": "live_llm",
        },
    )
    create_resp.raise_for_status()
    run_id = create_resp.json()["run_id"]
    print(f"[1] POST /api/runs -> HTTP {create_resp.status_code}, run_id={run_id}")

    gate1 = _poll_until(client, base_url, run_id, {"AWAITING_TEST_REVIEW", "FAILED"})
    gen_wall_s = round(time.perf_counter() - t_create, 3)
    print(f"[2] Stage 1+2 finished in {gen_wall_s}s -> state={gate1['state']}")

    if gate1["state"] == "FAILED":
        print(f"    ERROR: {gate1.get('error')}")
        logs = client.get(f"{base_url}/api/runs/{run_id}/logs").json()
        return {
            "label": label,
            "run_id": run_id,
            "state": gate1["state"],
            "error": gate1.get("error"),
            "gate1": gate1,
            "logs": logs,
        }

    tg = gate1["test_generation"]
    print(
        f"    generation_mode={tg['generation_mode']}, is_mock_output={tg['is_mock_output']}, "
        f"model_label={tg['model_label']}, scenarios={len(tg['scenarios'])}"
    )

    reviewed_safe_ids: list[str] = []
    rejected_ids: list[str] = []
    scenario_assessments: list[dict[str, Any]] = []

    for sc in tg["scenarios"]:
        check = _static_safety_check(sc["test_code"])
        scenario_assessments.append(
            {
                "scenario_id": sc["scenario_id"],
                "title": sc["title"],
                "rationale": sc["rationale"],
                "assumptions": sc["assumptions"],
                "test_code": sc["test_code"],
                "static_check": check,
            }
        )
        print(f"\n  --- {sc['scenario_id']}: {sc['title']} ---")
        print(f"  Rationale  : {sc['rationale']}")
        print(f"  Assumptions: {sc['assumptions']}")
        print(f"  Static chk : safe={check['safe_for_review']}, asserts={check['assert_count']}, flags={check['flags']}")
        print("  Code:")
        for line in sc["test_code"].splitlines():
            print(f"    {line}")

        if check["safe_for_review"]:
            if auto_approve_safe:
                reviewed_safe_ids.append(sc["scenario_id"])
            else:
                ans = input(f"  Approve {sc['scenario_id']} for M04 pytest execution? [y/N]: ").strip().lower()
                if ans == "y":
                    reviewed_safe_ids.append(sc["scenario_id"])
                else:
                    rejected_ids.append(sc["scenario_id"])
        else:
            print(f"  [!] Auto-excluding {sc['scenario_id']} due to safety flags: {check['flags']}")
            rejected_ids.append(sc["scenario_id"])

    if not reviewed_safe_ids:
        rev_resp = client.post(
            f"{base_url}/api/runs/{run_id}/test-review",
            json={
                "approved": False,
                "selected_test_ids": [],
                "reviewer": "marsadh",
                "notes": "No generated scenarios approved during human review.",
            },
        )
        rev_resp.raise_for_status()
        logs = client.get(f"{base_url}/api/runs/{run_id}/logs").json()
        return {
            "label": label,
            "run_id": run_id,
            "state": rev_resp.json()["state"],
            "approved_ids": [],
            "rejected_ids": rejected_ids,
            "scenario_assessments": scenario_assessments,
            "logs": logs,
        }

    t_verify = time.perf_counter()
    rev_resp = client.post(
        f"{base_url}/api/runs/{run_id}/test-review",
        json={
            "approved": True,
            "selected_test_ids": reviewed_safe_ids,
            "reviewer": "marsadh",
            "notes": f"Approved {len(reviewed_safe_ids)} reviewed live-LLM scenario(s); rejected {len(rejected_ids)}.",
        },
    )
    rev_resp.raise_for_status()
    print(f"\n[3] Gate 1 approval -> HTTP {rev_resp.status_code}, selected={reviewed_safe_ids}")

    gate2 = _poll_until(client, base_url, run_id, {"AWAITING_FINAL_APPROVAL", "FAILED"})
    verify_wall_s = round(time.perf_counter() - t_verify, 3)
    ver = gate2.get("verification") or {}
    rep = gate2.get("report") or {}
    print(f"[4] Gate 2 reached in {verify_wall_s}s -> state={gate2['state']}")
    print(
        f"    M04 outcome={ver.get('verification_outcome')}, passed_flag={ver.get('verification_passed')}, "
        f"exit_code={ver.get('exit_code')}, total={ver.get('total_tests')}, "
        f"passed={ver.get('passed')}, failed={ver.get('failed')}, duration_s={ver.get('duration_seconds')}"
    )
    print(f"    Recommendation: {rep.get('recommendation')} ({rep.get('recommendation_label')})")
    print(f"    Allowed actions: {gate2.get('allowed_actions')}")

    blocked_409_status: int | None = None
    blocked_409_code: str | None = None
    if ver.get("verification_passed") is True and expect_pass:
        dec_resp = client.post(
            f"{base_url}/api/runs/{run_id}/decision",
            json={
                "decision": "approve",
                "reviewer": "marsadh",
                "comments": "Live-LLM generated scenarios reviewed and passed on trusted fixture; limitations acknowledged.",
            },
        )
        dec_resp.raise_for_status()
        final_state = dec_resp.json()["state"]
    else:
        if ver.get("verification_passed") is not True:
            blocked = client.post(
                f"{base_url}/api/runs/{run_id}/decision",
                json={
                    "decision": "approve",
                    "reviewer": "marsadh",
                    "comments": "Verifying HTTP 409 block on failed verification.",
                },
            )
            blocked_409_status = blocked.status_code
            blocked_409_code = blocked.json().get("code")
            print(f"[5a] Blocked approval check -> HTTP {blocked_409_status}, code={blocked_409_code}")

        decision_choice = "reject" if (not expect_pass and ver.get("verification_passed") is False) else "needs_review"
        reason = (
            "Rejected after live-LLM verification detected regressive behavior."
            if decision_choice == "reject"
            else "Escalated for manual review (live-LLM tests did not produce clean pass/detection)."
        )
        dec_resp = client.post(
            f"{base_url}/api/runs/{run_id}/decision",
            json={
                "decision": decision_choice,
                "reviewer": "marsadh",
                "comments": reason,
            },
        )
        dec_resp.raise_for_status()
        final_state = dec_resp.json()["state"]

    print(f"[5] Final Gate 2 decision -> state={final_state}")
    logs = client.get(f"{base_url}/api/runs/{run_id}/logs").json()
    print(f"[6] Communication logs: {logs['count']} entries")

    return {
        "label": label,
        "run_id": run_id,
        "final_state": final_state,
        "generation_wall_seconds": gen_wall_s,
        "verification_wall_seconds": verify_wall_s,
        "approved_ids": reviewed_safe_ids,
        "rejected_ids": rejected_ids,
        "blocked_409_status": blocked_409_status,
        "blocked_409_code": blocked_409_code,
        "scenario_assessments": scenario_assessments,
        "gate2": gate2,
        "logs": logs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-live-calls",
        action="store_true",
        help="Required explicit confirmation before transmitting fixture code to Groq API.",
    )
    parser.add_argument(
        "--approve-safe-reviewed",
        action="store_true",
        help="Approve all statically safe scenarios automatically after printing them (for non-interactive runs).",
    )
    parser.add_argument("--port", type=int, default=8000, help="Preferred loopback port (default: 8000).")
    parser.add_argument("--output-json", type=Path, default=None, help="Optional path to save sanitized evidence JSON.")
    args = parser.parse_args()

    if not args.confirm_live_calls:
        print(
            "Refusing to execute external Groq API calls without --confirm-live-calls.\n"
            "This script sends temporary mini_pos fixture source to Groq and consumes API quota.",
            file=sys.stderr,
        )
        return 2

    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    groq_model = os.environ.get("GROQ_MODEL", "").strip()
    if not api_key or not groq_model:
        print(
            "Missing GROQ_API_KEY and/or GROQ_MODEL in process environment.\n"
            "Set them in Windows PowerShell before running:\n"
            '  $env:GROQ_API_KEY = "<your-groq-api-key>"\n'
            '  $env:GROQ_MODEL = "<your-configured-groq-model>"',
            file=sys.stderr,
        )
        return 2

    with tempfile.TemporaryDirectory(prefix="changeguard_p04_live_") as tmp:
        tmp_path = Path(tmp)
        repos_root = tmp_path / "repos"
        repo_a = repos_root / "pos_correct"
        repo_b = repos_root / "pos_regressive"

        meta_a = _prepare_fixture(repo_a, CORRECT_BILLING)
        meta_b = _prepare_fixture(repo_b, REGRESSIVE_BILLING)

        cfg = OrchestratorConfig(
            database_path=tmp_path / "state" / "live_demo.sqlite3",
            workspace_root=tmp_path / "workspaces",
            allowed_repository_roots=[repos_root],
            configured_repositories={
                "pos-correct": repo_a,
                "pos-regressive": repo_b,
            },
            trusted_execution_roots=[repos_root],
            groq_model=groq_model,
            verification_timeout_seconds=60,
            environment="development",
        )
        # Load GROQ_API_KEY via OrchestratorConfig.from_env-equivalent SecretStr without logging it
        from pydantic import SecretStr

        cfg = cfg.model_copy(update={"groq_api_key": SecretStr(api_key)})
        app = create_app(cfg)

        port = _find_port(args.port)
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()

        base_url = f"http://127.0.0.1:{port}"
        evidence: dict[str, Any] = {}
        try:
            with httpx.Client(timeout=120.0) as client:
                for _ in range(50):
                    try:
                        health_res = client.get(f"{base_url}/health")
                        if health_res.status_code == 200:
                            break
                    except httpx.HTTPError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("Live server failed to become healthy")

                health = health_res.json()
                assert health["live_llm_configured"] is True
                assert health["groq_model"] == groq_model
                assert api_key not in health_res.text
                print(f"GET /health OK at {base_url}/health:")
                print(json.dumps(health, indent=2))
                evidence["health"] = health

                evidence["scenario_a"] = _run_live_scenario(
                    client,
                    base_url,
                    label="SCENARIO A — Correct Implementation",
                    repo_id="pos-correct",
                    expect_pass=True,
                    auto_approve_safe=args.approve_safe_reviewed,
                )
                evidence["scenario_b"] = _run_live_scenario(
                    client,
                    base_url,
                    label="SCENARIO B — Incorrect Implementation",
                    repo_id="pos-regressive",
                    expect_pass=False,
                    auto_approve_safe=args.approve_safe_reviewed,
                )
        finally:
            server.should_exit = True
            server_thread.join(timeout=5.0)

        # Verify SQLite file contains zero occurrences of API key & fixtures untouched
        assert api_key.encode("utf-8") not in cfg.database_path.read_bytes()
        assert _git(repo_a, "rev-parse", "HEAD") == meta_a["base_commit"]
        assert _git(repo_b, "rev-parse", "HEAD") == meta_b["base_commit"]
        assert not (repo_a / ".changeguard_generated_tests").exists()
        assert not (repo_b / ".changeguard_generated_tests").exists()

    if args.output_json:
        serialized = json.dumps(evidence, indent=2)
        assert api_key not in serialized
        args.output_json.write_text(serialized, encoding="utf-8")
        print(f"\nSaved sanitized evidence JSON to {args.output_json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
