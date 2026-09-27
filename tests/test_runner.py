import json
import re
import subprocess
import sys
from pathlib import Path
import pytest

from harness.runner import (
    REQUIRED_ACTION_FIELDS,
    assert_action_contract,
    compute_metrics,
)

try:
    import app
    has_app = True
except ImportError:
    has_app = False


@pytest.mark.skipif(not has_app, reason="LOUD REASON: app module is not importable, skipping in-process runner test.")
def test_runner_no_llm_e1_e3_e4(tmp_path: Path):
    """Test --no-llm CI gate path over E1, E3, and E4 against in-process app."""
    out_file = tmp_path / "ci_gate_run.json"
    cmd = [
        sys.executable,
        "-m",
        "harness.runner",
        "--set",
        "ALL",
        "--no-llm",
        "--out",
        str(out_file),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path(__file__).parent.parent))
    assert res.returncode == 0, f"Runner failed with exit code {res.returncode}:\n{res.stderr}\n{res.stdout}"
    assert out_file.exists(), "Output file was not created"

    with open(out_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert "summary" in data
    assert "rows" in data
    assert data["summary"]["mode"] == "no_llm"
    assert data["summary"]["ci_gate_passed"] is True
    assert data["summary"]["total_actions"] > 0

    # Ensure sets E1, E2, E3, E4 rows are recorded
    sets_present = {r["set"] for r in data["rows"]}
    assert "E1" in sets_present
    assert "E2" in sets_present
    assert "E3" in sets_present
    assert "E4" in sets_present


def test_min_across_judges_arithmetic():
    """Assert the min-across-judges, mean, and spread arithmetic with fixed scores."""
    fake_scores = {
        "judge_alpha": {
            "total": 85,
            "specificity": 18,
            "category_fit": 17,
            "merchant_fit": 16,
            "decision_quality": 18,
            "engagement_compulsion": 16,
        },
        "judge_beta": {
            "total": 60,
            "specificity": 12,
            "category_fit": 14,
            "merchant_fit": 10,
            "decision_quality": 11,
            "engagement_compulsion": 13,
        },
        "judge_gamma": {
            "total": 75,
            "specificity": 15,
            "category_fit": 16,
            "merchant_fit": 14,
            "decision_quality": 15,
            "engagement_compulsion": 15,
        },
    }

    metrics = compute_metrics(fake_scores)

    # Headline is min across judges
    assert metrics["min"] == 60
    assert metrics["mean"] == pytest.approx(73.33, rel=1e-2)
    assert metrics["spread"] == 25  # 85 - 60

    # Dimensions
    dims = metrics["dims"]
    assert dims["specificity"]["min"] == 12
    assert dims["specificity"]["mean"] == 15.0
    assert dims["specificity"]["spread"] == 6

    assert dims["merchant_fit"]["min"] == 10
    assert dims["merchant_fit"]["mean"] == pytest.approx(13.33, rel=1e-2)
    assert dims["merchant_fit"]["spread"] == 6

    assert dims["category_fit"]["min"] == 14
    assert dims["category_fit"]["mean"] == pytest.approx(15.67, rel=1e-2)
    assert dims["category_fit"]["spread"] == 3


def test_assert_action_contract_valid():
    """Valid action passes contract checks."""
    valid_action = {
        "conversation_id": "conv_123",
        "merchant_id": "m_001",
        "customer_id": "c_001",
        "send_as": "vera",
        "trigger_id": "trg_001",
        "template_name": "tmpl_digest",
        "template_params": ["param1"],
        "body": "Hi Doctor, here is your weekly digest.",
        "cta": "Reply YES to review.",
        "suppression_key": "research:dentists:2026-W17",
        "rationale": "High relevance clinical digest update.",
    }
    seen = {}
    assert_action_contract(valid_action, seen)
    assert "conv_123" in seen
    assert "hi doctor, here is your weekly digest." in seen["conv_123"]


def test_assert_action_contract_missing_field():
    """Missing any of the 11 action fields raises AssertionError."""
    action = {
        "conversation_id": "conv_123",
        "merchant_id": "m_001",
        "customer_id": None,
        "send_as": "vera",
        # missing trigger_id
        "template_name": "tmpl_digest",
        "template_params": [],
        "body": "Some body",
        "cta": "Reply YES",
        "suppression_key": "key_1",
        "rationale": "Rationale",
    }
    seen = {}
    with pytest.raises(AssertionError, match="Action missing required fields"):
        assert_action_contract(action, seen)


def test_assert_action_contract_forbidden_url():
    """URLs in body are strictly prohibited."""
    action = {
        "conversation_id": "conv_123",
        "merchant_id": "m_001",
        "customer_id": None,
        "send_as": "vera",
        "trigger_id": "trg_001",
        "template_name": "tmpl_digest",
        "template_params": [],
        "body": "Check our offers at https://magicpin.in/deals today!",
        "cta": "Click now",
        "suppression_key": "key_1",
        "rationale": "Offer",
    }
    seen = {}
    with pytest.raises(AssertionError, match="URL forbidden in message body"):
        assert_action_contract(action, seen)


def test_assert_action_contract_duplicate_body():
    """Duplicate body in the same conversation raises AssertionError."""
    action1 = {
        "conversation_id": "conv_123",
        "merchant_id": "m_001",
        "customer_id": None,
        "send_as": "vera",
        "trigger_id": "trg_001",
        "template_name": "tmpl_digest",
        "template_params": [],
        "body": "Exact identical body text.",
        "cta": "Reply YES",
        "suppression_key": "key_1",
        "rationale": "First turn",
    }
    action2 = dict(action1)
    action2["trigger_id"] = "trg_002"
    seen = {}
    assert_action_contract(action1, seen)
    with pytest.raises(AssertionError, match="Duplicate body emitted"):
        assert_action_contract(action2, seen)


def test_assert_action_contract_invalid_send_as():
    """Invalid send_as raises AssertionError."""
    action = {
        "conversation_id": "conv_123",
        "merchant_id": "m_001",
        "customer_id": None,
        "send_as": "bot",
        "trigger_id": "trg_001",
        "template_name": "tmpl_digest",
        "template_params": [],
        "body": "Hi there.",
        "cta": "Reply",
        "suppression_key": "key_1",
        "rationale": "Rationale",
    }
    seen = {}
    with pytest.raises(AssertionError, match="Invalid send_as"):
        assert_action_contract(action, seen)
