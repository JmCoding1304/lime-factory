"""§6 Envelope round-trips and the PhaseParams description validator."""

import json

import pytest
from pydantic import ValidationError

from lime import agents
from lime.data_types import (
    AgentCall,
    BuildOutput,
    ChangesOutput,
    DesignOutput,
    DesignVariant,
    DocumentOutput,
    EnvelopeBase,
    GenericOutput,
    Phase,
    PhaseParams,
    PlanOutput,
    ResearchOutput,
    ResearchSource,
    ReviewFinding,
    ReviewOutput,
    ReviewStep,
    ScoutFinding,
    QualityCheckResult,
    ScoutOutput,
    VerifyCheck,
    VerifyOutput,
)

ENVELOPES = [
    GenericOutput(status="success", summary="s"),
    PlanOutput(status="success", summary="planned", artifacts=["plan.md"]),
    BuildOutput(status="success", changed_files=["a.py"], commit_message="Add a.py"),
    ScoutOutput(status="success", findings=[ScoutFinding(file="a.py", note="entry point")]),
    ReviewOutput(
        status="success",
        approved=False,
        findings=[ReviewFinding(requirement="does X", met=True, evidence="a.py:1")],
        blocking=["missing tests"],
        trust_violations=["a.py:1 — reads .env (never: secrets .env*)"],
    ),
    DocumentOutput(status="success", changed_docs=["AGENTS.md"], commit_message="Update docs"),
    ResearchOutput(status="success", sources=[ResearchSource(url="https://x", note="settled")]),
    DesignOutput(
        status="success",
        route_dir="app/proto-hero/",
        url_path="/proto-hero",
        variants=[DesignVariant(id="a", note="bold")],
    ),
    VerifyOutput(status="fail", passed=False, failures=["test: exited 1"]),
    ChangesOutput(status="success", base="main @ abc123 — clean tree", changed_files=["a.py"]),
]


@pytest.mark.parametrize("envelope", ENVELOPES, ids=lambda e: type(e).__name__)
def test_envelope_round_trips_through_json(envelope):
    cls = type(envelope)
    assert cls.model_validate_json(envelope.model_dump_json()) == envelope
    assert isinstance(envelope, EnvelopeBase)


def test_plan_output_has_no_commit_message_field():
    # spec §6: PlanOutput carries no commit_message — no plan commit
    assert "commit_message" not in PlanOutput.model_fields


def test_review_output_has_no_layout_field():
    assert set(ReviewOutput.model_fields) == {
        "status",
        "summary",
        "artifacts",
        "notes_for_next_agent",
        "approved",
        "findings",
        "blocking",
        "trust_violations",
    }
    assert "layout" not in agents.submit_schema(ReviewOutput)["properties"]


def test_a_pre_192_review_envelope_with_layout_still_loads():
    env = ReviewOutput.model_validate_json(
        json.dumps({
            "status": "success", "approved": False,
            "blocking": ["#/cost: overlap: a × b"], "layout": "fail",
        })
    )
    assert env.approved is False
    assert env.blocking == ["#/cost: overlap: a × b"]
    assert "layout" not in env.model_dump()


def test_review_never_approved_while_trust_violations_present_is_a_caller_rule():
    # the type permits it (a gate enforces the rule) — this documents the shape
    output = ReviewOutput(status="success", approved=True, trust_violations=["x"])
    assert output.approved and output.trust_violations


# --- PhaseParams ---


def test_phase_params_requires_a_real_description():
    with pytest.raises(ValueError, match="description is required"):
        PhaseParams(name="build", stage="Build", kind="agent", owner="builder", description="  ")


def test_phase_params_rejects_a_description_that_only_echoes_the_name():
    with pytest.raises(ValueError, match="only restates"):
        PhaseParams(name="build_step", stage="Build", kind="agent", owner="builder", description="build step")


def test_phase_params_accepts_a_real_description():
    params = PhaseParams(
        name="build", stage="Build", kind="agent", owner="builder", description="Implement step 1 of the plan"
    )
    assert params.retries == 0


def test_phase_params_requires_a_stage():
    with pytest.raises(ValidationError, match="stage"):
        PhaseParams(name="build", kind="agent", owner="builder", description="Implement step 1 of the plan")


@pytest.mark.parametrize("stage", ["Other", "Deploy", "build"])
def test_phase_params_rejects_a_stage_outside_the_four(stage):
    with pytest.raises(ValidationError, match="stage"):
        PhaseParams(name="build", kind="agent", stage=stage, owner="builder", description="Implement step 1 of the plan")


@pytest.mark.parametrize("stage", ["Prepare", "Build", "Review", "Ship"])
def test_phase_params_accepts_each_stage(stage):
    params = PhaseParams(
        name="build", kind="agent", stage=stage, owner="builder", description="Implement step 1 of the plan"
    )
    assert params.stage == stage


def test_phase_defaults_to_fail_status():
    phase = Phase(
        phase_id="R1:01",
        run_id="R1",
        seq=1,
        params=PhaseParams(name="build", stage="Build", kind="agent", owner="builder", description="Implement it"),
    )
    assert phase.status == "fail"


# --- AgentCall / ReviewStep (ticket 62) ---


def test_agent_call_max_turns_defaults_to_none():
    call = AgentCall(output_type=BuildOutput, prompt="x")
    assert call.max_turns is None


def test_review_step_has_no_layout_field():
    assert "layout" not in ReviewStep.model_fields


def test_review_step_defaults():
    step = ReviewStep(prompt="p", checklist="c", base="main")
    assert step.previous is None
    assert step.attempt == 1


def test_verify_output_records_checks_and_legacy_json_loads_without_them():
    check = VerifyCheck(name="test", command="pytest", returncode=0, passed=True, duration_seconds=1.5)
    env = VerifyOutput(status="success", passed=True, checks=[check])
    again = VerifyOutput.model_validate_json(env.model_dump_json())
    assert again.checks == [check]
    legacy = VerifyOutput.model_validate_json('{"status":"success","passed":true}')
    assert legacy.checks == []
    assert "output_tail" not in VerifyCheck.model_fields
    assert issubclass(QualityCheckResult, VerifyCheck)


def test_verify_output_carries_head_sha_and_legacy_json_defaults_it_empty():
    env = VerifyOutput(status="success", passed=True, head_sha="a" * 40)
    again = VerifyOutput.model_validate_json(env.model_dump_json())
    assert again.head_sha == "a" * 40
    legacy = VerifyOutput.model_validate_json('{"status":"success","passed":true}')
    assert legacy.head_sha == ""
    # a pre-change envelope carrying a provenance key still loads; the key is ignored
    old = VerifyOutput.model_validate_json(json.dumps({
        "status": "success", "passed": True,
        "verification": {
            "version": 1, "content_fingerprint": "x",
            "head_sha": "a" * 40, "policy": {}, "commands": [],
        },
    }))
    assert old.head_sha == ""
    assert "verification" not in VerifyOutput.model_fields


# --- builder bash deadline (ticket 198) ---


def test_bash_deadline_sits_above_verify_caps_and_well_under_the_session_ceiling():
    from lime import quality
    from lime.data_types import BASH_DEADLINE_S, DEFAULT_TIMEOUT_S

    assert isinstance(BASH_DEADLINE_S, int)
    assert max(quality.TIMEOUTS.values()) < BASH_DEADLINE_S <= DEFAULT_TIMEOUT_S // 2
    assert max(quality.check_timeout({}, name) for name in (*quality.ORDER, "setup", "teardown")) < BASH_DEADLINE_S
    assert quality.check_timeout({}, "test") == 600
