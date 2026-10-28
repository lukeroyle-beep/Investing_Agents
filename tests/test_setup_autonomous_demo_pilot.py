"""Synthetic key strings and fake read-only /me; never live authorization setup."""
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.fill_agent.pilot_ledger import PilotLedger
from brokers.etoro.client import EtoroCredentials
from execution.domain import canonical_json, payload_hash
from execution.pilot_mandate import StandingPilotMandate
from risk.kill_switch import KillSwitch, RESET_ACKNOWLEDGEMENT
from scripts import setup_autonomous_demo_pilot as setup
from tests.test_pilot_ledger import Account, NOW


@pytest.fixture
def f(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    account = Account()
    ledger = PilotLedger.create(root / "pilot.sqlite3", account, [], NOW)
    marker = {"policy":"demo-pilot-v1", "account_id":account.account_id,
              "inception_hash":payload_hash(ledger.snapshot()["inception"])}
    marker_path = root / "pilot-inception.json"
    marker_path.write_text(canonical_json(marker))
    marker_path.chmod(0o600)
    calls = {"prompt":0, "profile":0}
    credentials = EtoroCredentials("SYNTHETIC_API_KEY", "SYNTHETIC_USER_KEY")
    profile = {"demoCid":123, "scopes":["etoro-public:demo:read", "etoro-public:demo:write"]}
    def reader():
        calls["prompt"] += 1
        return credentials
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["credentials"] == credentials
            assert kwargs["receipt_dir"] == root / "direct-api-captures"
        def get_profile(self):
            calls["profile"] += 1
            return profile
    return SimpleNamespace(root=root, ledger=ledger, calls=calls, credentials=credentials,
                           profile=profile, reader=reader, client=Client)


def run(f, **options):
    values = dict(runtime=f.root, authorization_reference="Synthetic human standing mandate",
        credential_reader=f.reader, client_factory=f.client, clock=lambda:NOW)
    values.update(options)
    return setup.setup(**values)


def switch(f):
    return KillSwitch(state_path=f.root / "kill-switch.json", audit_path=f.root / "kill-switch.jsonl")


def test_default_setup_preserves_inception_and_does_not_activate_or_start_worker(f):
    before = f.ledger.snapshot()
    result = run(f)
    assert result["status"] == "SETUP_SAVED_ACTIVATION_PENDING"
    assert result["worker_started"] is result["order_submitted"] is False
    assert not switch(f).state_path.exists()
    assert not switch(f).audit_path.exists()
    assert f.ledger.snapshot() == before
    credentials_path = f.root / setup.CREDENTIAL_FILENAME
    assert credentials_path.stat().st_mode & 0o777 == 0o600
    assert setup.load_dedicated_credentials(credentials_path) == f.credentials
    assert "SYNTHETIC_API_KEY" not in json.dumps(result)
    assert "SYNTHETIC_USER_KEY" not in json.dumps(result)
    assert f.calls == {"prompt":1, "profile":1}


def test_explicit_activation_binds_new_shared_switch_to_verified_mandate(f):
    result = run(f, activate=True)
    assert result["status"] == "SETUP_SAVED_WRITES_ENABLED"
    assert switch(f).status().writes_allowed
    mandate = StandingPilotMandate.load(f.root / setup.MANDATE_FILENAME, expected_account_id="123")
    assert mandate.mandate_hash in switch(f).status().reason
    assert not f.ledger.snapshot()["submissions"]
    assert f.calls == {"prompt":1, "profile":1}


def test_explicit_resume_uses_existing_secret_file_without_prompt_or_overwrite(f):
    run(f)
    paths = [f.root / setup.CREDENTIAL_FILENAME, f.root / setup.MANDATE_FILENAME]
    before = [p.read_bytes() for p in paths]
    with pytest.raises(setup.PilotSetupError, match="explicit_resume_activation"):
        run(f, activate=True)
    result = run(f, activate=True, resume_activation=True)
    assert result["status"] == "SETUP_SAVED_WRITES_ENABLED"
    assert [p.read_bytes() for p in paths] == before
    assert f.calls == {"prompt":1, "profile":2}


def test_preexisting_engaged_switch_is_never_reset_by_setup(f):
    switch(f).initialize_engaged(operator_id="synthetic-human", reason="prior stop", now=NOW)
    before = switch(f).state_path.read_bytes()
    with pytest.raises(setup.PilotSetupError, match="requires_review"):
        run(f, activate=True)
    assert switch(f).state_path.read_bytes() == before
    assert not (f.root / setup.CREDENTIAL_FILENAME).exists()
    assert not (f.root / setup.MANDATE_FILENAME).exists()
    assert f.calls == {"prompt":0, "profile":0}


def open_switch(f):
    switch(f).initialize_engaged(operator_id="synthetic-human", reason="initial stop", now=NOW)
    switch(f).reset(operator_id="synthetic-human", reason="earlier explicit activation",
                   acknowledgement=RESET_ACKNOWLEDGEMENT, now=NOW)


def test_pending_setup_cannot_inherit_writable_kill_switch(f):
    open_switch(f)
    before = switch(f).state_path.read_bytes()
    with pytest.raises(setup.PilotSetupError, match="requires_explicit_activation"):
        run(f)
    assert switch(f).state_path.read_bytes() == before
    assert not (f.root / setup.CREDENTIAL_FILENAME).exists()
    assert not (f.root / setup.MANDATE_FILENAME).exists()
    assert f.calls == {"prompt":0, "profile":0}


def test_explicit_activation_can_use_existing_unchanged_writable_switch(f):
    open_switch(f)
    before = switch(f).state_path.read_bytes()
    assert run(f, activate=True)["status"] == "SETUP_SAVED_WRITES_ENABLED"
    assert switch(f).state_path.read_bytes() == before


def test_kill_state_is_read_after_acquiring_operator_lock(f, monkeypatch):
    original = setup.fcntl.flock
    def acquire_then_engage(*args):
        original(*args)
        switch(f).initialize_engaged(operator_id="synthetic-human", reason="stop before lock", now=NOW)
    monkeypatch.setattr(setup.fcntl, "flock", acquire_then_engage)
    with pytest.raises(setup.PilotSetupError, match="requires_review"):
        run(f, activate=True)
    assert f.calls == {"prompt":0, "profile":0}
    assert not (f.root / setup.MANDATE_FILENAME).exists()


@pytest.mark.parametrize("previously_open", [False, True])
def test_revocation_during_identity_read_never_saves_or_resets(f, previously_open):
    if previously_open:
        open_switch(f)
    class RevokingClient(f.client):
        def get_profile(self):
            operation = switch(f).engage if previously_open else switch(f).initialize_engaged
            operation(operator_id="synthetic-human", reason="revoked during identity read", now=NOW)
            return super().get_profile()
    with pytest.raises(setup.PilotSetupError, match="requires_review"):
        run(f, activate=True, client_factory=RevokingClient)
    assert switch(f).status().engaged
    assert switch(f).status().reason == "revoked during identity read"
    assert not (f.root / setup.CREDENTIAL_FILENAME).exists()
    assert not (f.root / setup.MANDATE_FILENAME).exists()


def test_revocation_during_artifact_save_remains_engaged(f, monkeypatch):
    open_switch(f)
    original = setup._write_credentials
    def write_then_revoke(*args):
        original(*args)
        switch(f).engage(operator_id="synthetic-human", reason="revoked while saving", now=NOW)
    monkeypatch.setattr(setup, "_write_credentials", write_then_revoke)
    with pytest.raises(setup.PilotSetupError, match="requires_review"):
        run(f, activate=True)
    assert switch(f).status().engaged
    assert switch(f).status().reason == "revoked while saving"
    assert (f.root / setup.CREDENTIAL_FILENAME).exists()
    assert (f.root / setup.MANDATE_FILENAME).exists()


@pytest.mark.parametrize("changes", [{"demoCid":999}, {"scopes":["etoro-public:demo:read"]},
    {"scopes":["etoro-public:real:read", "etoro-public:real:write"]}])
def test_profile_wrong_account_or_missing_demo_scopes_never_activates_or_saves_keys(f, changes):
    f.profile.update(changes)
    with pytest.raises(ValueError, match="identity_read_write_scope_unverified"):
        run(f, activate=True)
    assert not (f.root / setup.CREDENTIAL_FILENAME).exists()
    assert not (f.root / setup.MANDATE_FILENAME).exists()
    assert not switch(f).state_path.exists()


def test_setup_requires_existing_private_runtime_and_inception(f):
    missing = f.root / "missing"
    with pytest.raises(setup.PilotSetupError, match="existing_owned_0700"):
        run(f, runtime=missing)
    assert not missing.exists()
    f.root.chmod(0o755)
    with pytest.raises(setup.PilotSetupError, match="existing_owned_0700"):
        run(f)
    f.root.chmod(0o700)
    (f.root / "pilot-inception.json").unlink()
    with pytest.raises(setup.PilotSetupError, match="private_file_missing"):
        run(f)
    assert f.calls == {"prompt":0, "profile":0}


def test_loader_rejects_public_permissions_symlinks_and_hardlinks(f):
    run(f)
    path = f.root / setup.CREDENTIAL_FILENAME
    path.chmod(0o640)
    with pytest.raises(setup.PilotSetupError, match="0600"):
        setup.load_dedicated_credentials(path)
    path.chmod(0o600)
    link = f.root / "key-link.json"
    link.symlink_to(path)
    with pytest.raises(setup.PilotSetupError):
        setup.load_dedicated_credentials(link)
    link.unlink()
    os.link(path, link)
    with pytest.raises(setup.PilotSetupError, match="0600"):
        setup.load_dedicated_credentials(path)


def test_hidden_entry_refuses_nonterminal_instead_of_echoing(monkeypatch):
    monkeypatch.setattr(setup.sys, "stdin", io.StringIO())
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: pytest.fail("must not prompt"))
    with pytest.raises(setup.PilotSetupError, match="hidden_terminal"):
        setup.read_hidden_credentials()


def test_partial_write_preserves_mandate_and_requires_review(f, monkeypatch):
    def fail(*args):
        raise OSError("synthetic interrupted write")
    monkeypatch.setattr(setup, "_write_credentials", fail)
    with pytest.raises(OSError):
        run(f, activate=True)
    mandate = f.root / setup.MANDATE_FILENAME
    before = mandate.read_bytes()
    with pytest.raises(setup.PilotSetupError, match="partial_setup_preserved"):
        run(f, activate=True, resume_activation=True)
    assert mandate.read_bytes() == before
    assert not switch(f).state_path.exists()


def test_interrupted_activation_keeps_engaged_switch_and_never_resets_on_resume(f, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("synthetic interrupted activation")
    monkeypatch.setattr(KillSwitch, "reset", fail)
    with pytest.raises(OSError):
        run(f, activate=True)
    assert switch(f).status().engaged
    before = switch(f).state_path.read_bytes()
    with pytest.raises(setup.PilotSetupError, match="requires_review"):
        run(f, activate=True, resume_activation=True)
    assert switch(f).state_path.read_bytes() == before
    assert f.calls == {"prompt":1, "profile":1}


def test_cli_never_echoes_unexpected_secret_exception_or_argv(monkeypatch, capsys):
    def fail(**kwargs):
        raise RuntimeError("SYNTHETIC_SECRET_MUST_NOT_ECHO")
    monkeypatch.setattr(setup, "setup", fail)
    assert setup.main(["--runtime", "/synthetic", "--authorization-reference", "synthetic"]) == 1
    assert "SYNTHETIC_SECRET_MUST_NOT_ECHO" not in capsys.readouterr().err
    assert setup.main(["--api-key", "SYNTHETIC_SECRET_MUST_NOT_ECHO"]) == 1
    assert "SYNTHETIC_SECRET_MUST_NOT_ECHO" not in capsys.readouterr().err
