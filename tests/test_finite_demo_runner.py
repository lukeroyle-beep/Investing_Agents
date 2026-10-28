"""Finite setup/recovery boundaries, using synthetic accounts and no network."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from agents.fill_agent.pilot_ledger import PilotLedger
from execution.domain import canonical_json, payload_hash
from execution.finite_demo_mandate import FiniteDemoMandate
from execution.finite_demo_mandate import FiniteDemoMandateError
from execution.operator import OperatorEvidenceStore
from execution.store import ExecutionStore
from risk.kill_switch import KillSwitch, RESET_ACKNOWLEDGEMENT
from scripts import setup_finite_demo_pilot as setup
from scripts import run_finite_demo_pilot as runner
from tests.test_pilot_ledger import Account, NOW

COMMIT = "c" * 40
SPEC = {"instrument_id": 101, "symbol": "TEST", "exchange": "NYSE", "asset_type": "etf",
    "economic_class": "fixed_income", "currency": "USD", "issuer": "Synthetic Fund",
    "sector": "diversified_funds", "class_source_reference": "synthetic-review"}


@pytest.fixture
def f(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    ledger = PilotLedger.create(root / "pilot.sqlite3", Account(), [], NOW)
    store = ExecutionStore(root / "execution.sqlite3")
    OperatorEvidenceStore(root / "operator.sqlite3")
    marker = root / "pilot-inception.json"
    marker.write_text(canonical_json({"policy": "demo-pilot-v1", "account_id": "123",
        "inception_hash": payload_hash(ledger.snapshot()["inception"])}))
    marker.chmod(0o600)
    switch = KillSwitch(state_path=root / "kill-switch.json", audit_path=root / "kill-switch.jsonl")
    switch.initialize_engaged(operator_id="synthetic", reason="development", now=NOW)
    scope = {"instruments": [deepcopy(SPEC)], "max_submissions": 3, "max_per_day": 3,
        "per_order_cap": "20", "total_commitment": "60", "max_positions": 3,
        "min_cash_fraction": "0.20", "loss_stop": "25", "order_types": ["market"]}
    catalog = [{"instrument_id": 9, "symbol": "LEGACY", "issuer": "Legacy Issuer",
        "sector": "industrials", "asset_type": "equity", "source_reference": "synthetic-baseline"},
        {key: SPEC[key] for key in ("instrument_id", "symbol", "issuer", "sector", "asset_type")} |
        {"source_reference": SPEC["class_source_reference"]}]
    return SimpleNamespace(root=root, ledger=ledger, store=store, switch=switch, scope=scope, catalog=catalog)


def save(f, **updates):
    values = dict(runtime=f.root, authorization_reference="synthetic finite authority",
        reviewed_commit=COMMIT, scope=f.scope, catalog=f.catalog, issued_at=NOW,
        expires_at=NOW + timedelta(hours=1), checkout_verifier=lambda commit: None)
    values.update(updates)
    return setup.setup(**values)


def test_setup_preserves_original_inception_and_never_activates(f):
    before = f.ledger.snapshot()["inception"]
    switch_files = (f.switch.state_path.read_bytes(), f.switch.audit_path.read_bytes())
    result = save(f)
    assert result["worker_started"] is result["order_submitted"] is False
    assert f.ledger.snapshot()["inception"] == before
    assert not f.ledger.snapshot()["submissions"]
    assert (f.switch.state_path.read_bytes(), f.switch.audit_path.read_bytes()) == switch_files
    assert f.switch.status().engaged
    mandate = FiniteDemoMandate.load(f.root / setup.MANDATE_FILE, expected_account_id="123")
    assert mandate.ledger_path == (f.root / "pilot.sqlite3").resolve()
    assert setup.load_catalog(f.root, mandate, f.ledger)[1] == result["catalog_hash"]
    assert not (f.root / "demo-api-credentials.json").exists()


def test_setup_cannot_overwrite_or_reset_existing_allowance(f):
    save(f)
    before = {p.name: p.read_bytes() for p in f.root.iterdir() if p.is_file()}
    with pytest.raises(ValueError, match="exists_never_overwrite"):
        save(f)
    assert {p.name: p.read_bytes() for p in f.root.iterdir() if p.is_file()} == before


def test_setup_missing_original_state_never_recreates_database(f):
    (f.root / "execution.sqlite3").unlink()
    with pytest.raises(ValueError, match="missing_never_initialize"):
        save(f)
    assert not (f.root / "execution.sqlite3").exists()
    assert not (f.root / setup.MANDATE_FILE).exists()


def test_setup_refuses_runtime_or_state_symlinks(f, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(f.root, target_is_directory=True)
    with pytest.raises(ValueError, match="owned_0700"):
        save(f, runtime=alias)
    source = f.root / "operator.sqlite3"
    source.rename(f.root / "saved.sqlite3")
    source.symlink_to(f.root / "saved.sqlite3")
    with pytest.raises(ValueError, match="owned_private_pilot_state"):
        save(f)


def test_setup_requires_stopped_switch_and_never_resets_it(f):
    f.switch.reset(operator_id="synthetic", reason="test only", now=NOW,
        acknowledgement=RESET_ACKNOWLEDGEMENT)
    before = f.switch.state_path.read_bytes()
    with pytest.raises(ValueError, match="existing_engaged"):
        save(f)
    assert f.switch.state_path.read_bytes() == before
    assert not (f.root / setup.MANDATE_FILE).exists()


@pytest.mark.parametrize("mutation", ["missing_legacy", "different_issuer", "duplicate"])
def test_setup_rejects_incomplete_or_conflicting_catalog_before_saving(f, mutation):
    if mutation == "missing_legacy":
        f.catalog.pop(0)
    elif mutation == "different_issuer":
        f.catalog[1]["issuer"] = "Another Issuer"
    else:
        f.catalog.append(deepcopy(f.catalog[0]))
    with pytest.raises(ValueError):
        save(f)
    assert not (f.root / setup.MANDATE_FILE).exists()


def test_saved_catalog_cannot_change_allowlisted_classification(f):
    save(f)
    path = f.root / setup.CATALOG_FILE
    value = json.loads(path.read_text())
    value["catalog"]["classifications"][1]["issuer"] = "Changed Issuer"
    value["sha256"] = payload_hash(value["catalog"])
    path.write_text(canonical_json(value))
    mandate = FiniteDemoMandate.load(f.root / setup.MANDATE_FILE, expected_account_id="123")
    with pytest.raises(ValueError, match="classification_mismatch"):
        setup.load_catalog(f.root, mandate, f.ledger)


def test_worker_cannot_load_credentials_or_make_reads_while_switch_engaged(f, monkeypatch):
    save(f)
    monkeypatch.setattr(runner, "now", lambda: NOW)
    monkeypatch.setattr(runner, "load_dedicated_credentials",
        lambda path: pytest.fail("credentials accessed while stopped"))
    with pytest.raises(FiniteDemoMandateError, match="kill_switch"):
        runner.locked_cycle(SimpleNamespace(reviewed_commit=COMMIT, instrument_id=101,
            amount=Decimal("10")), f.root, f.ledger)


def test_worker_disallows_unlisted_instrument_before_usage_or_credentials(f, monkeypatch):
    save(f)
    f.switch.reset(operator_id="synthetic", reason="test", now=NOW,
        acknowledgement=RESET_ACKNOWLEDGEMENT)
    monkeypatch.setattr(runner, "now", lambda: NOW)
    monkeypatch.setattr(runner, "UsageOnlyHost", lambda root: pytest.fail("usage prompted before scope check"))
    with pytest.raises(ValueError, match="instrument_not_authorized"):
        runner.locked_cycle(SimpleNamespace(reviewed_commit=COMMIT, instrument_id=999,
            amount=Decimal("10")), f.root, f.ledger)


def test_cost_request_keeps_fractional_amount_numeric():
    from brokers.etoro.autonomous_api import build_order_payload
    from brokers.etoro.pilot_reads import _arguments
    _, retained, wire = _arguments("/api/v2/trading/info/demo/costs", {
        "body": build_order_payload(101, Decimal("10.125"))})
    value = json.loads(wire, parse_float=Decimal)
    assert value["amount"] == Decimal("10.125")
    assert not isinstance(value["amount"], str)
    assert json.loads(retained["body"], parse_float=Decimal) == value


def test_pending_journal_blocks_next_candidate_even_when_economically_reconciled():
    old = {"command_id": "old", "reconciled": True}
    finite = {"command_id": "new", "reconciled": True, "mandate_policy": "finite-demo-v2",
        "journal_complete": False}
    assert runner.pending_claim({"submissions": [old, finite]}) == finite
    finite["journal_complete"] = True
    assert runner.pending_claim({"submissions": [old, finite]}) is None


def test_usage_host_cannot_issue_broker_operations(tmp_path):
    host = runner.UsageOnlyHost(tmp_path)
    with pytest.raises(ValueError, match="usage_only"):
        host.call("etoro_place_trade", {"token": "SYNTHETIC"})


def test_worker_arguments_do_not_echo_accidental_secrets(capsys):
    assert runner.main(["--api-key", "SYNTHETIC_SECRET"]) == 2
    assert "SYNTHETIC_SECRET" not in capsys.readouterr().out


def _runner_artifacts(f, root):
    (root / "finite.json").rename(root / setup.MANDATE_FILE)
    (root / "journal.sqlite3").rename(root / setup.JOURNAL_FILE)
    setup._save(root / "pilot-inception.json", {"policy": "demo-pilot-v1", "account_id": "123",
        "inception_hash": payload_hash(f.ledger.snapshot()["inception"])})
    rows = [{"instrument_id": c.instrument_id, "symbol": c.symbol, "issuer": c.issuer,
        "sector": c.sector, "asset_type": c.asset_type, "source_reference": c.source_reference}
        for c in f.catalog.items.values()]
    rows.sort(key=lambda row: row["instrument_id"])
    body = {"schema": "finite-demo-catalog-v2", "mandate_hash": f.mandate.mandate_hash,
        "reviewed_commit": f.mandate.reviewed_commit, "source_reference": f.mandate.source_reference,
        "classifications": rows}
    setup._save(root / setup.CATALOG_FILE, {"catalog": body, "sha256": payload_hash(body)})


@pytest.mark.parametrize("phase", ["operational", "journal"])
def test_worker_recovers_committed_fill_without_credentials_or_network(tmp_path, monkeypatch, phase):
    from execution.domain import CommandState
    from tests.test_finite_demo_operator import workflow, prepare, submit, finish
    f = workflow(tmp_path)
    p = prepare(f)
    submit(p)
    if phase == "journal":
        original = f.journal.record_reconciliation
        monkeypatch.setattr(f.journal, "record_reconciliation",
            lambda command_id: (_ for _ in ()).throw(RuntimeError("synthetic journal crash")))
    else:
        original = f.store.transition_command
        def crash(command_id, state, **kwargs):
            if state == CommandState.FILLED:
                raise RuntimeError("synthetic operational crash")
            return original(command_id, state, **kwargs)
        monkeypatch.setattr(f.store, "transition_command", crash)
    with pytest.raises(RuntimeError, match="synthetic .* crash"):
        finish(p)
    if phase == "journal":
        monkeypatch.setattr(f.journal, "record_reconciliation", original)
    else:
        monkeypatch.setattr(f.store, "transition_command", original)
    assert f.ledger.snapshot()["submissions"][0]["reconciled"]
    _runner_artifacts(f, tmp_path)
    monkeypatch.setattr(runner, "now", lambda: f.clock[0])
    monkeypatch.setattr(runner, "verify_reviewed_checkout", lambda commit: None)
    monkeypatch.setattr(runner, "load_dedicated_credentials",
        lambda path: pytest.fail("committed recovery must not access credentials"))
    monkeypatch.setattr(runner, "DirectBatchReads",
        lambda **kwargs: pytest.fail("committed recovery must not access network"))
    result = runner.run(SimpleNamespace(runtime=tmp_path, reviewed_commit=f.mandate.reviewed_commit,
        instrument_id=f.specs[1]["instrument_id"], amount=Decimal(10)))
    assert not result["unresolved"]
    assert result["submissions"][0]["journal_complete"]
    assert len(f.transport.posts) == 1
    assert len(result["submissions"]) == 1


def test_real_worker_completes_two_synthetic_instruments_and_journals(tmp_path, monkeypatch):
    from brokers.etoro.autonomous_api import DirectDemoClient
    from brokers.etoro.batch_reads import DirectBatchReads
    from execution.usage_reserve import UsageAdmission, UsageWindow
    from tests.test_autonomous_api import KEYS, full_lookup, raw
    from tests.test_batch_assessment import batch_inputs
    from tests.test_batch_reads import MarketTransport
    from tests.test_finite_demo_operator import workflow, account_with_fills
    from tests.test_pilot_assessment import change_response

    f = workflow(tmp_path)
    _runner_artifacts(f, tmp_path)
    fills = []
    posts = []

    class Transport:
        def send(self, request):
            if request.method == "POST":
                payload = json.loads(request.body, parse_float=Decimal)
                assert payload["amount"] == Decimal(10)
                posts.append(request)
                order_id, position_id = 41 + len(posts), 499 + len(posts)
                fills.append((payload["instrumentId"], position_id, "10"))
                self.lookup = full_lookup(request.headers["x-request-id"])
                self.lookup.update(orderId=order_id)
                self.lookup["asset"]["instrumentId"] = payload["instrumentId"]
                self.lookup["positionExecutions"][0]["positionId"] = position_id
                self.lookup["positionExecutions"][0]["openingData"].update(orderId=order_id,
                    executionTime=f.clock[0].isoformat())
                f.clock[0] += timedelta(seconds=1)
                return raw({"orderId": order_id, "referenceId": request.headers["x-request-id"]})
            if request.url.endswith("/api/v1/me"):
                return raw({"demoCid": 123, "realCid": 987,
                    "scopes": ["etoro-public:trade.demo:read", "etoro-public:trade.demo:write"]})
            return raw(self.lookup)

    transport = Transport()

    class Reader:
        def __init__(self, **kwargs):
            self.portfolio_reads = 0
            self.post_count = len(posts)
        def read(self, path, **kwargs):
            if path == "/api/v2/market-data/instruments":
                return {"result": f.catalog.evidence["metadata"]}
            if path.endswith(("/costs", "/eligibility")):
                payload = kwargs["body"]
                payload = json.loads(payload) if isinstance(payload, str) else payload
                iid = payload.get("instrumentId") or payload["instrumentIds"][0]
                spec = next(item for item in f.specs if item["instrument_id"] == iid)
                args = batch_inputs(iid, spec["symbol"], asset_type=spec["asset_type"],
                    economic_class=spec["economic_class"])
                for name in ("cost_receipt", "eligibility_receipt"):
                    args[name]["request_started_at"] = f.clock[0].isoformat()
                    args[name]["response_received_at"] = f.clock[0].isoformat()
                change_response(args, "cost", lambda value: value.update(lastUpdated=f.clock[0].isoformat()))
                if path.endswith("/costs"):
                    assert isinstance(payload["amount"], Decimal)
                    return args["cost_receipt"]
                return args["eligibility_receipt"]
            if path == "/api/v1/me":
                if len(posts) > self.post_count:
                    # Account fixture represents a bounded six-second capture.
                    f.clock[0] += timedelta(seconds=10)
                    self.post_count = len(posts)
                self.portfolio_reads = 0
                name = "profile"
            elif path.endswith("/pnl"):
                name = "pnl"
            else:
                self.portfolio_reads += 1
                name = "first" if self.portfolio_reads == 1 else "last"
            capture = account_with_fills(f.clock[0], fills)
            return deepcopy(capture.raw_sources[name])
        def market(self, instrument, spec):
            market = MarketTransport(f.clock, [{"items": [{"instrumentId": spec["instrument_id"],
                "internalSymbolFull": spec["symbol"], "isCurrentlyTradable": True,
                "isExchangeOpen": True, "isActiveInPlatform": True, "isBuyEnabled": True,
                "isDelisted": False}], "totalItems": 1}, {"results": [{"instrumentId": spec["instrument_id"],
                "quoteType": "realtime", "date": f.clock[0].isoformat(), "ask": 100, "bid": 100}]}])
            return DirectBatchReads(credentials=KEYS, receipt_dir=tmp_path / "market",
                clock=lambda: f.clock[0], transport=market).market(instrument, spec)

    class Usage:
        def __init__(self, root):
            pass
        def usage(self):
            return UsageAdmission(f.clock[0], (UsageWindow("synthetic", Decimal(90), True),),
                ("synthetic",), "synthetic quota", "a" * 64)

    monkeypatch.setattr(runner, "now", lambda: f.clock[0])
    monkeypatch.setattr(runner, "verify_reviewed_checkout", lambda commit: None)
    monkeypatch.setattr(runner, "load_dedicated_credentials", lambda path: KEYS)
    monkeypatch.setattr(runner, "DirectDemoClient", lambda **kwargs: DirectDemoClient(**kwargs, transport=transport))
    monkeypatch.setattr(runner, "DirectBatchReads", Reader)
    monkeypatch.setattr(runner, "UsageOnlyHost", Usage)
    for spec in f.specs:
        result = runner.run(SimpleNamespace(runtime=tmp_path, reviewed_commit=f.mandate.reviewed_commit,
            instrument_id=spec["instrument_id"], amount=Decimal(10)))
        assert not result["unresolved"]
    assert len(posts) == 2
    assert result["position_count"] == 2
    assert len(result["submissions"]) == 2
    assert all(row["journal_complete"] for row in result["submissions"])
