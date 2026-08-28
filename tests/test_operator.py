"""The operator UI's write path (OMNI-28).

These tests are about the *gate*, not the widgets. The Panel layer is a
renderer; every rule that matters is asserted here against
:mod:`sis.operator` directly, because that is the level a hand-crafted request
arrives at when the browser is not cooperating.
"""

from __future__ import annotations

import pathlib

import pytest

from sis import config, operator
from sis.config import ConfigTier, FrontendConfig
from sis.operator import Approval, Edit, EditRefused, RefusesToServe
from sis.policy import Justification

# A confirmation that satisfies the write path: a human request with a reason
# long enough to be one. Used wherever a test needs to get *past* the gate so
# it can assert something else.
_APPROVED = Approval.human("deliberately weakening this check for a test")


@pytest.fixture
def config_file(tmp_path: pathlib.Path) -> pathlib.Path:
    """A pristine config.yml the tests may write, away from the real one."""
    path = tmp_path / "config.yml"
    path.write_text(config.render_config_file(), encoding="utf-8")
    return path


def _paths(tier: ConfigTier) -> list[str]:
    return [k.path for k in config.SCHEMA if k.tier is tier]


# --- the tier gate --------------------------------------------------------


def test_every_forbidden_key_is_refused_by_the_write_path(
    config_file: pathlib.Path,
) -> None:
    """Not one of them is editable, and confirming does not unlock it.

    Asserted over the whole tier rather than one example, so a key added to
    SCHEMA as forbidden_ is covered the day it lands.
    """
    for path in _paths(ConfigTier.FORBIDDEN):
        with pytest.raises(EditRefused, match="forbidden_"):
            operator.save_edits(
                [Edit(path, "whatever")], approval=_APPROVED, path=config_file
            )


def test_a_strict_key_needs_an_explicit_confirmation(
    config_file: pathlib.Path,
) -> None:
    with pytest.raises(EditRefused, match="strict_"):
        operator.save_edits([Edit("canary.backend", "serve")], path=config_file)

    operator.save_edits(
        [Edit("canary.backend", "serve")], approval=_APPROVED, path=config_file
    )
    assert config.load_file(config_file)["canary.backend"] == "serve"


def test_a_strict_key_needs_a_justification_not_just_a_confirmation(
    config_file: pathlib.Path,
) -> None:
    """A tick alone is not enough — a strict_ edit has to say why.

    The distinction the `Approval` type exists for: a boolean could record only
    *that* someone clicked, and a key whose edit weakens a check is one where
    the reason is the interesting part.
    """
    with pytest.raises(EditRefused, match="at least"):
        operator.save_edits(
            [Edit("canary.backend", "serve")],
            approval=Approval.human("meh"),
            path=config_file,
        )
    assert config.load_file(config_file)["canary.backend"] != "serve"


def test_whitespace_is_not_a_justification(config_file: pathlib.Path) -> None:
    """Padding to the length threshold with spaces must not satisfy it."""
    with pytest.raises(EditRefused, match="at least"):
        operator.save_edits(
            [Edit("canary.backend", "serve")],
            approval=Approval.human(" " * 40),
            path=config_file,
        )


def test_a_justification_without_a_human_request_is_refused(
    config_file: pathlib.Path,
) -> None:
    """Prose alone does not confirm. The two claims are separate.

    `Justification.EXCEPTION` is what the loop uses; it is not a human at a
    console, so it must not unlock the console's write path however good the
    accompanying note is.
    """
    with pytest.raises(EditRefused, match="human confirmation"):
        operator.save_edits(
            [Edit("canary.backend", "serve")],
            approval=Approval(
                Justification.EXCEPTION, "a thorough and lengthy explanation"
            ),
            path=config_file,
        )


def test_a_soft_key_is_edited_freely(config_file: pathlib.Path) -> None:
    operator.save_edits([Edit("loop.interval_seconds", 90.0)], path=config_file)
    assert config.load_file(config_file)["loop.interval_seconds"] == 90.0


def test_a_soft_key_needs_no_approval_at_all(
    config_file: pathlib.Path,
) -> None:
    """The approval gates strict_ only — it must not become a blanket demand.

    A console that asked for a written justification to change a poll interval
    would train its operators to type "x" into the box, which is exactly how
    the requirement stops meaning anything where it does matter.
    """
    operator.save_edits(
        [Edit("frontend.port", 9000)],
        approval=operator.UNJUSTIFIED,
        path=config_file,
    )
    assert config.load_file(config_file)["frontend.port"] == 9000


def test_the_ui_cannot_grant_itself_access(config_file: pathlib.Path) -> None:
    """The allowlist and the auth mode are not editable from inside a session.

    The escalation this blocks is specific: an authenticated operator appending
    a login, or switching auth off, through the very UI those settings guard.
    """
    for path in ("frontend.allowed_logins", "frontend.auth", "frontend.bind"):
        with pytest.raises(EditRefused):
            operator.save_edits(
                [Edit(path, "attacker")], approval=_APPROVED, path=config_file
            )


def test_naming_the_config_as_a_target_does_not_make_it_editable(
    config_file: pathlib.Path,
) -> None:
    """The guardrail-precedence trick from tests/test_config.py, UI side.

    `policy.target_paths` is itself a config key, so "name the config as a
    target, then rewrite the spend cap" has to be inert here too. It is, for a
    blunter reason than in the policy: the target list is forbidden_, so the
    first half of the move is refused outright.
    """
    with pytest.raises(EditRefused, match="forbidden_"):
        operator.save_edits(
            [Edit("policy.target_paths", "config.yml")],
            approval=_APPROVED,
            path=config_file,
        )


# --- atomicity and validation ---------------------------------------------


def test_a_rejected_edit_leaves_the_file_untouched(
    config_file: pathlib.Path,
) -> None:
    """No partial writes: one bad edit in a batch cancels the whole batch."""
    before = config_file.read_text(encoding="utf-8")
    with pytest.raises(EditRefused):
        operator.save_edits(
            [
                Edit("loop.interval_seconds", 90.0),   # fine on its own
                Edit("brakes.budget_usd", 999.0),      # forbidden_
            ],
            approval=_APPROVED,
            path=config_file,
        )
    assert config_file.read_text(encoding="utf-8") == before


def test_an_edit_inherits_the_schemas_own_validation(
    config_file: pathlib.Path,
) -> None:
    """The UI must not grow a second, quietly different set of checks."""
    with pytest.raises(ValueError):  # not in `choices`
        operator.save_edits(
            [Edit("canary.backend", "sevre")], approval=_APPROVED, path=config_file
        )
    with pytest.raises(ValueError):  # `positive` — a 0 port is not a weaker port
        operator.save_edits([Edit("frontend.port", 0)], path=config_file)


def test_an_unknown_key_is_rejected_rather_than_written(
    config_file: pathlib.Path,
) -> None:
    with pytest.raises(KeyError):
        operator.save_edits([Edit("loop.intervall", 5)], path=config_file)


# --- what the file looks like afterwards ----------------------------------


def test_a_saved_file_still_matches_the_renderer(
    config_file: pathlib.Path,
) -> None:
    """An operator edit changes a value and nothing else about the file."""
    operator.save_edits([Edit("loop.interval_seconds", 90.0)], path=config_file)
    rendered = config.render_config_file(config.load_file(config_file))
    assert config_file.read_text(encoding="utf-8") == rendered


def test_a_saved_edit_does_not_disturb_any_other_key(
    config_file: pathlib.Path,
) -> None:
    before = config.load_file(config_file)
    operator.save_edits([Edit("loop.interval_seconds", 90.0)], path=config_file)
    after = config.load_file(config_file)

    assert after["loop.interval_seconds"] == 90.0
    del before["loop.interval_seconds"], after["loop.interval_seconds"]
    assert before == after


def test_an_operator_edit_can_never_move_a_guardrail(
    config_file: pathlib.Path,
) -> None:
    """The invariant tests/test_config.py pins for the committed file.

    Saving through the UI must keep it true, or deleting config.yml would stop
    being a safe way back to the shipped guardrails.
    """
    operator.save_edits(
        [Edit("loop.interval_seconds", 90.0), Edit("frontend.port", 9000)],
        path=config_file,
    )
    saved = config.load_file(config_file)
    for key in config.SCHEMA:
        if key.tier is ConfigTier.FORBIDDEN:
            assert saved[key.path] == key.default, key.path


# --- the audit log --------------------------------------------------------


def test_a_save_records_what_changed_and_why(config_file: pathlib.Path) -> None:
    """The justification is written down, not just demanded and discarded."""
    operator.save_edits(
        [Edit("canary.backend", "serve")], approval=_APPROVED, path=config_file
    )
    entries = operator.read_audit(operator.audit_path_for(config_file))

    assert len(entries) == 1
    entry = entries[0]
    assert entry.path == "canary.backend"
    assert entry.tier == ConfigTier.STRICT.value
    assert entry.after == "serve"
    assert entry.justification == Justification.HUMAN_REQUEST.value
    assert entry.note == _APPROVED.reason


def test_the_audit_records_the_value_it_replaced(
    config_file: pathlib.Path,
) -> None:
    """`before` makes the log a diff rather than a list of assertions."""
    original = config.load_file(config_file)["loop.interval_seconds"]
    operator.save_edits([Edit("loop.interval_seconds", 90.0)], path=config_file)
    entry = operator.read_audit(operator.audit_path_for(config_file))[0]

    assert entry.before == original
    assert entry.after == 90.0


def test_a_soft_edit_is_audited_too(config_file: pathlib.Path) -> None:
    """Every committed change is recorded, not only the ones needing approval.

    A log that held strict_ edits alone could not answer "what changed on this
    box last week", which is the question it will actually be asked.
    """
    operator.save_edits([Edit("loop.interval_seconds", 90.0)], path=config_file)
    entry = operator.read_audit(operator.audit_path_for(config_file))[0]

    assert entry.tier == ConfigTier.SOFT.value
    assert entry.justification == Justification.NONE.value
    assert entry.note == ""


def test_the_audit_log_is_append_only(config_file: pathlib.Path) -> None:
    """History accumulates; a later save must not overwrite an earlier one."""
    operator.save_edits([Edit("loop.interval_seconds", 90.0)], path=config_file)
    operator.save_edits([Edit("frontend.port", 9000)], path=config_file)
    entries = operator.read_audit(operator.audit_path_for(config_file))

    assert [e.path for e in entries] == ["loop.interval_seconds", "frontend.port"]


def test_every_key_in_a_batch_gets_its_own_entry(
    config_file: pathlib.Path,
) -> None:
    operator.save_edits(
        [Edit("loop.interval_seconds", 90.0), Edit("frontend.port", 9000)],
        path=config_file,
    )
    entries = operator.read_audit(operator.audit_path_for(config_file))

    assert len(entries) == 2
    assert len({e.at for e in entries}) == 1  # one save, one timestamp


def test_a_refused_edit_is_never_audited(config_file: pathlib.Path) -> None:
    """The log records what happened. A refusal did not change anything."""
    with pytest.raises(EditRefused):
        operator.save_edits(
            [Edit("brakes.budget_usd", 999.0)],
            approval=_APPROVED,
            path=config_file,
        )
    assert operator.read_audit(operator.audit_path_for(config_file)) == []


def test_a_corrupt_line_does_not_make_the_history_unreadable(
    tmp_path: pathlib.Path,
) -> None:
    """A crash mid-append must cost one entry, not the whole log.

    The log is most needed on the machine where something already went wrong,
    so it fails soft in exactly the way the episodic store's state file does.
    """
    log = tmp_path / "operator_audit.jsonl"
    log.write_text(
        '{"at": "2026-08-28T00:00:00+00:00", "path": "loop.interval_seconds", '
        '"tier": "soft", "before": 60.0, "after": 90.0, '
        '"justification": "none", "note": ""}\n'
        "{not json\n",
        encoding="utf-8",
    )
    entries = operator.read_audit(log)

    assert len(entries) == 1
    assert entries[0].path == "loop.interval_seconds"


def test_a_save_to_a_temporary_config_does_not_touch_the_real_log(
    config_file: pathlib.Path,
) -> None:
    """The isolation the rest of these tests depend on, asserted directly."""
    operator.save_edits([Edit("loop.interval_seconds", 90.0)], path=config_file)

    assert operator.audit_path_for(config_file) != operator.OPERATOR_AUDIT_JSONL
    assert operator.audit_path_for(config.CONFIG_FILE) == operator.OPERATOR_AUDIT_JSONL


# --- shadowing: the way this tool could most easily lie -------------------


def test_a_key_supplied_by_the_environment_is_flagged_as_shadowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIS_LOOP_INTERVAL", "17")
    views = {v.key.path: v for v in operator.review()}

    shadowed = views["loop.interval_seconds"]
    assert shadowed.shadowed
    assert shadowed.shadow_note is not None
    assert "SIS_LOOP_INTERVAL" in shadowed.shadow_note

    assert not views["frontend.port"].shadowed
    assert views["frontend.port"].shadow_note is None


def test_a_file_supplied_value_is_not_shadowed() -> None:
    """Only a *higher*-precedence layer shadows; the file itself does not."""
    for view in operator.review(env={}):
        assert not view.shadowed


def test_review_reports_the_tier_as_editability() -> None:
    for view in operator.review(env={}):
        assert view.editable is (view.key.tier is not ConfigTier.FORBIDDEN)
        assert view.needs_confirmation is (view.key.tier is ConfigTier.STRICT)


# --- refusing to serve ----------------------------------------------------


def _frontend(**kwargs: object) -> FrontendConfig:
    base: dict[str, object] = {
        "auth": "github",
        "allowed_logins": ("ozumpe",),
        "bind": "127.0.0.1",
        "port": 8080,
    }
    base.update(kwargs)
    return FrontendConfig(**base)  # type: ignore[arg-type]


def test_unauthenticated_on_a_public_bind_refuses_to_start() -> None:
    with pytest.raises(RefusesToServe, match="reachable beyond this machine"):
        operator.check_servable(_frontend(auth="none", bind="0.0.0.0"))


def test_unauthenticated_on_loopback_is_allowed() -> None:
    """Local development must not need a registered OAuth app."""
    operator.check_servable(_frontend(auth="none", bind="127.0.0.1"))
    operator.check_servable(_frontend(auth="none", bind="localhost"))


def test_github_auth_with_nobody_allowlisted_refuses_to_start() -> None:
    with pytest.raises(RefusesToServe, match="allowed_logins is empty"):
        operator.check_servable(_frontend(allowed_logins=()))


def test_a_wildcard_bind_is_not_treated_as_loopback() -> None:
    """The whole auth rule rests on this distinction."""
    assert operator.is_loopback("127.0.0.1")
    assert operator.is_loopback("::1")
    assert not operator.is_loopback("0.0.0.0")
    assert not operator.is_loopback("::")
    assert not operator.is_loopback("10.0.0.5")


# --- the allowlist --------------------------------------------------------


def test_an_empty_allowlist_denies_everyone() -> None:
    """Fails closed: an allowlist defaulting to 'anyone' only looks like one."""
    empty = _frontend(allowed_logins=())
    assert not operator.is_authorized("ozumpe", empty)
    assert not operator.is_authorized("", empty)


def test_the_allowlist_ignores_capitalisation() -> None:
    """GitHub logins are case-insensitive; locking someone out on case is a bug."""
    frontend = _frontend(allowed_logins=("OzUmPe",))
    assert operator.is_authorized("ozumpe", frontend)
    assert operator.is_authorized("OZUMPE", frontend)
    assert not operator.is_authorized("someone-else", frontend)


def test_a_blank_login_is_never_authorized() -> None:
    assert not operator.is_authorized("   ", _frontend(allowed_logins=("",)))
