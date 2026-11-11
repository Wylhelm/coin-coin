"""The diagnostic harness must distinguish missing contact data from no contacts."""

import json
from pathlib import Path

import pytest

from tests.integration.nav_repro import free_route_failures, read_contact_events, summarize


def test_summary_reports_contacts_only_when_measured() -> None:
    row = {"err": 0.1, "yaw_err": 2.0, "contacts": 0}
    assert summarize([row])["contacts"] == 0
    assert summarize([row, {**row, "contacts": 2}])["contacts"] == 1
    assert summarize([row, {**row, "contacts": None}])["contacts"] is None


def test_summary_preserves_unrounded_confident_error_during_the_route() -> None:
    rows = [
        {"err": 0.2, "yaw_err": 2.0, "contacts": 0, "converged": True},
        {"err": 0.200001, "yaw_err": 1.0, "contacts": 0, "converged": True},
        {"err": 0.4, "yaw_err": 1.0, "contacts": 0, "converged": False},
    ]
    assert summarize(rows)["max_converged_m"] == 0.200001
    result = {"step": "salon", "ok": True, "final_m": 0.1, "contact_events": 0, **summarize(rows)}
    assert free_route_failures([result])


def test_free_route_assessment_checks_real_arrival_and_expected_skating_refusal() -> None:
    rows = [
        {"step": "mode:roller", "ok": True, "contact_events": 0},
        {"step": "roller:salon", "ok": False, "reason": "on skates", "contact_events": 0},
        {"step": "mode:walk", "ok": True, "contact_events": 0},
        {
            "step": "ma chambre",
            "ok": True,
            "final_m": 0.2,
            "contact_events": 0,
            "after": {"converged": True, "err": 0.1},
        },
    ]
    assert free_route_failures(rows) == []
    assert free_route_failures(
        [{"step": "salon", "ok": True, "final_m": 0.200001, "contact_events": 0}]
    )
    assert free_route_failures(
        [{"step": "salon", "ok": True, "final_m": float("nan"), "contact_events": 0}]
    )
    assert free_route_failures(
        [
            {
                "step": "salon",
                "ok": True,
                "final_m": 0.1,
                "contact_events": 0,
                "after": {"converged": False, "err": 0.1},
            }
        ]
    )
    for error in (None, float("nan"), -0.1, True):
        assert free_route_failures(
            [
                {
                    "step": "status",
                    "ok": True,
                    "contact_events": 0,
                    "after": {"converged": True, "err": error},
                }
            ]
        )
    assert free_route_failures(
        [{"step": "salon", "ok": False, "reason": "lost", "contact_events": 0}]
    )
    assert free_route_failures(
        [{"step": "stand", "ok": False, "reason": "not standing", "contact_events": 0}]
    )
    assert free_route_failures([{"step": "roller:salon", "ok": True, "contact_events": 0}])
    assert free_route_failures(
        [{"step": "roller:salon", "ok": False, "reason": "unknown place", "contact_events": 0}]
    )
    assert free_route_failures([{"step": "salon", "ok": True, "final_m": 0.1}])
    assert free_route_failures([{"step": "salon", "ok": True, "final_m": 0.1, "contact_events": 1}])
    assert free_route_failures(
        [
            {
                "step": "status",
                "ok": True,
                "detail": "position: salon",
                "contact_events": 0,
                "after": {"converged": True, "err": 0.200001},
            }
        ]
    )
    assert free_route_failures(
        [
            {
                "step": "status",
                "ok": True,
                "detail": "position: incertaine",
                "contact_events": 0,
                "after": {"converged": True, "err": 1.0},
            }
        ]
    )


def test_contact_event_log_requires_a_ready_contiguous_probe(tmp_path: Path) -> None:
    path = tmp_path / "contacts.jsonl"
    path.write_text(json.dumps({"ready": True}) + "\n")
    assert read_contact_events(path) == 0
    with path.open("a") as out:
        out.write(json.dumps({"event": 1}) + "\n")
    assert read_contact_events(path) == 1
    with path.open("a") as out:
        out.write(json.dumps({"event": 3}) + "\n")
    with pytest.raises(RuntimeError, match="incomplete"):
        read_contact_events(path)
