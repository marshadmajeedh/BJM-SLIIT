"""Repeatable offline end-to-end integration demonstration for ChangeGuard AI (M02).

Demonstrates the real ChangeGuard AI FastAPI backend coordinating:
- M01 CodebaseAdapter (static AST + Git diff + reverse-dependency impact analysis)
- M03 TestingAdapter (unexecuted proposal generation & controlled scenario export)
- M04 VerificationAdapter (real pytest execution on isolated trusted working copies)
- Human Review Gate 1 (test approval before execution)
- Human Review Gate 2 (evidence review & final decision; blocked approval on failure)

Runs two isolated temporary Git fixtures against a live loopback HTTP server:
- Scenario A (Correct software change): 10% discount for premium customers only.
- Scenario B (Regressive software change): erroneously discounts regular customers too.

Leaves the main ChangeGuard repository completely untouched and cleans up only
temporary files and the uvicorn server created by this script.

Usage:
    python scripts/demo_m02_integration.py
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

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
    "customer checkout or breaking dependent refund, dashboard, and report modules."
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
    "    # Regression: applies 10% discount to ALL customers including regular\n"
    "    return total * 0.90\n\n\n"
    "def checkout(price, quantity, customer_type):\n"
    "    total = calculate_total(price, quantity)\n"
    "    return apply_discount(total, customer_type)\n"
)


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
    _git(repo, "config", "user.name", "ChangeGuard Demo")
    _git(repo, "config", "user.email", "demo@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline mini_pos")
    base_commit = _git(repo, "rev-parse", "HEAD")

    (app_dir / "billing.py").write_text(billing_code, encoding="utf-8")
    diff = _git(repo, "diff", "--unified=1", "HEAD")
    return {"base_commit": base_commit, "diff": diff}


def _behavioral_demo_model(prompt: str) -> str:
    """Deterministic behavioral test generator grounded in mini_pos APIs."""
    if "apply_discount" not in prompt:
        raise ValueError("Expected apply_discount context from M01 impact report")

    header = (
        "import sys\n"
        "from pathlib import Path\n"
        "import pytest\n\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mini_pos'))\n"
    )
    return json.dumps(
        {
            "scenarios": [
                {
                    "title": "Premium checkout applies 10% discount",
                    "rationale": "Verify modified billing.apply_discount gives 10% off to premium customers.",
                    "assumptions": "Offline deterministic behavioral test; premium customers receive 10% discount.",
                    "test_code": (
                        header
                        + "from billing import apply_discount, checkout\n\n"
                        + "def test_premium_discount():\n"
                        + "    assert apply_discount(200.0, 'premium') == pytest.approx(180.0)\n"
                        + "    assert checkout(50.0, 4, 'premium') == pytest.approx(180.0)\n"
                    ),
                },
                {
                    "title": "Regular customer checkout stays unchanged",
                    "rationale": "Verify regular customers are not discounted.",
                    "assumptions": "Offline deterministic behavioral test; regular customers pay 100%.",
                    "test_code": (
                        header
                        + "from billing import apply_discount, checkout\n\n"
                        + "def test_regular_customer_unchanged():\n"
                        + "    assert apply_discount(200.0, 'regular') == pytest.approx(200.0)\n"
                        + "    assert checkout(50.0, 4, 'regular') == pytest.approx(200.0)\n"
                    ),
                },
                {
                    "title": "Downstream refund dashboard and sales report preserve customer pricing rules",
                    "rationale": "M01 mapped reverse dependencies: refunds -> customer_service -> dashboard and billing -> reports.",
                    "assumptions": "Offline deterministic behavioral test across impacted modules.",
                    "test_code": (
                        header
                        + "from dashboard import display_refund\n"
                        + "from reports import generate_sales_report\n\n"
                        + "def test_downstream_modules_behavior():\n"
                        + "    assert display_refund(100.0, 'premium') == {'refund_amount': pytest.approx(90.0)}\n"
                        + "    assert display_refund(100.0, 'regular') == {'refund_amount': pytest.approx(100.0)}\n"
                        + "    assert generate_sales_report(100.0, 2, 'premium') == {'total_sales': pytest.approx(180.0)}\n"
                        + "    assert generate_sales_report(100.0, 2, 'regular') == {'total_sales': pytest.approx(200.0)}\n"
                    ),
                },
            ]
        }
    )


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _poll_until(
    client: httpx.Client,
    base_url: str,
    run_id: str,
    stop_states: set[str],
    timeout_s: float = 30.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        detail = client.get(f"{base_url}/api/runs/{run_id}").json()
        if detail["state"] in stop_states:
            return detail
        time.sleep(0.15)
    raise TimeoutError(f"Run {run_id} did not reach {stop_states} within {timeout_s}s")


def _run_scenario(
    client: httpx.Client,
    base_url: str,
    *,
    label: str,
    repo_id: str,
    expect_pass: bool,
) -> None:
    print(f"\n{'=' * 76}")
    print(f"{label} (repository_id={repo_id})")
    print(f"{'=' * 76}")

    create_resp = client.post(
        f"{base_url}/api/runs",
        json={
            "repository_id": repo_id,
            "source_path": "mini_pos",
            "base_ref": "HEAD",
            "requirements": REQUIREMENT,
            "execution_mode": "offline_test",
        },
    )
    create_resp.raise_for_status()
    run_id = create_resp.json()["run_id"]
    print(f"[1] POST /api/runs -> HTTP {create_resp.status_code}, run_id={run_id}")

    gate1 = _poll_until(client, base_url, run_id, {"AWAITING_TEST_REVIEW", "FAILED"})
    print(f"[2] Paused at Gate 1 -> state={gate1['state']}")
    cb = gate1["codebase_analysis"]
    tg = gate1["test_generation"]
    print(f"    M01 changed files   : {cb['changed_files_repo_relative']}")
    print(f"    M01 impacted modules: {cb['impacted_modules_repo_relative']}")
    print(
        f"    M03 scenarios       : {[s['scenario_id'] for s in tg['scenarios']]} "
        f"(executed={tg['executed']}, is_mock_output={tg['is_mock_output']}, model={tg['model_label']})"
    )

    scenario_ids = [s["scenario_id"] for s in tg["scenarios"]]
    rev_resp = client.post(
        f"{base_url}/api/runs/{run_id}/test-review",
        json={
            "approved": True,
            "selected_test_ids": scenario_ids,
            "reviewer": "demo-reviewer",
            "notes": f"Approved behavioral scenarios for {label}.",
        },
    )
    rev_resp.raise_for_status()
    print(f"[3] Gate 1 approval -> HTTP {rev_resp.status_code}, state={rev_resp.json()['state']}")

    gate2 = _poll_until(client, base_url, run_id, {"AWAITING_FINAL_APPROVAL", "FAILED"})
    ver = gate2["verification"]
    rep = gate2["report"]
    print(f"[4] Paused at Gate 2 -> state={gate2['state']}")
    print(
        f"    M04 pytest evidence : outcome={ver['verification_outcome']}, "
        f"passed_flag={ver['verification_passed']}, exit_code={ver['exit_code']}, "
        f"total={ver['total_tests']}, passed={ver['passed']}, failed={ver['failed']}"
    )
    print(f"    Report recommendation: {rep['recommendation']} ({rep['recommendation_label']})")
    print(f"    Allowed Gate 2 actions: {gate2['allowed_actions']}")
    print(
        f"    Traceability status : {rep['traceability'].get('requirement_traceability_status')} "
        f"(baseline={ver['baseline_comparison']['status']})"
    )
    if ver["failures"]:
        print("    Observed failure diagnostics:")
        for fail in ver["failures"]:
            print(f"      - {fail['test_name']} [{fail['error_type']}]: {fail['reason']}")

    if expect_pass:
        dec_resp = client.post(
            f"{base_url}/api/runs/{run_id}/decision",
            json={
                "decision": "approve",
                "reviewer": "demo-reviewer",
                "comments": "Behavioral verification passed on isolated fixture; limitations acknowledged.",
            },
        )
        dec_resp.raise_for_status()
        print(f"[5] Gate 2 final decision -> HTTP {dec_resp.status_code}, state={dec_resp.json()['state']}")
    else:
        blocked = client.post(
            f"{base_url}/api/runs/{run_id}/decision",
            json={
                "decision": "approve",
                "reviewer": "demo-reviewer",
                "comments": "Attempting approval despite regression",
            },
        )
        print(
            f"[5a] Gate 2 blocked approval -> HTTP {blocked.status_code}, "
            f"code={blocked.json()['code']}"
        )
        rej_resp = client.post(
            f"{base_url}/api/runs/{run_id}/decision",
            json={
                "decision": "reject",
                "reviewer": "demo-reviewer",
                "comments": "Rejected due to verified regular-customer discount regression.",
            },
        )
        rej_resp.raise_for_status()
        print(f"[5b] Gate 2 rejection -> HTTP {rej_resp.status_code}, state={rej_resp.json()['state']}")

    logs = client.get(f"{base_url}/api/runs/{run_id}/logs").json()
    print(f"[6] Communication logs ({logs['count']} entries):")
    for entry in logs["logs"]:
        print(
            f"      - [{entry['stage']}] {entry['agent_name']} ({entry['status']}, "
            f"{entry['duration_ms']}ms): {entry['output_summary'][:100]}"
        )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="changeguard_p03_demo_") as tmp:
        tmp_path = Path(tmp)
        repos_root = tmp_path / "repos"
        repo_a = repos_root / "pos_correct"
        repo_b = repos_root / "pos_regressive"

        meta_a = _prepare_fixture(repo_a, CORRECT_BILLING)
        meta_b = _prepare_fixture(repo_b, REGRESSIVE_BILLING)

        print("Prepared isolated trusted Git fixtures:")
        print(f"  - Scenario A (correct)   base={meta_a['base_commit'][:12]}")
        print(f"  - Scenario B (regressive) base={meta_b['base_commit'][:12]}")

        cfg = OrchestratorConfig(
            database_path=tmp_path / "state" / "demo.sqlite3",
            workspace_root=tmp_path / "workspaces",
            allowed_repository_roots=[repos_root],
            configured_repositories={
                "pos-correct": repo_a,
                "pos-regressive": repo_b,
            },
            trusted_execution_roots=[repos_root],
            verification_timeout_seconds=30,
            environment="development",
        )
        app = create_app(
            cfg,
            offline_model_callable=_behavioral_demo_model,
            offline_model_label="deterministic_behavioral_fixture_stub",
        )

        port = _find_free_port()
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()

        base_url = f"http://127.0.0.1:{port}"
        try:
            with httpx.Client(timeout=20.0) as client:
                for _ in range(50):
                    try:
                        health_res = client.get(f"{base_url}/health")
                        if health_res.status_code == 200:
                            break
                    except httpx.HTTPError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("Demo backend failed to become healthy")

                print(f"\nBackend health OK at {base_url}/health:")
                print(json.dumps(health_res.json(), indent=2))

                _run_scenario(
                    client,
                    base_url,
                    label="SCENARIO A — Correct Software Change",
                    repo_id="pos-correct",
                    expect_pass=True,
                )
                _run_scenario(
                    client,
                    base_url,
                    label="SCENARIO B — Regressive Software Change",
                    repo_id="pos-regressive",
                    expect_pass=False,
                )
        finally:
            server.should_exit = True
            server_thread.join(timeout=5.0)

        # Verify original fixtures remained unmutated before cleanup
        assert _git(repo_a, "rev-parse", "HEAD") == meta_a["base_commit"]
        assert _git(repo_b, "rev-parse", "HEAD") == meta_b["base_commit"]
        assert not (repo_a / ".changeguard_generated_tests").exists()
        assert not (repo_b / ".changeguard_generated_tests").exists()

    print("\nDemo completed cleanly; temporary fixtures and server process cleaned up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
