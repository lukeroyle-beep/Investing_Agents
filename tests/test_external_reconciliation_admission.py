"""Synthetic source-byte admission tests; never use private broker receipts."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from agents.fill_agent.external_reconciliation import (
    ExternalReconciliationError, SCHEMA, inspect_external_reconciliation,
)
from agents.fill_agent.pilot_ledger import PilotLedger
from execution.domain import canonical_json, payload_hash
from scripts.inspect_external_demo_reconciliation import main
from tests.test_pilot_ledger import Account, Position, NOW

D = Decimal


def save(path, value, raw=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if raw else canonical_json(value).encode())
    path.chmod(0o600)
    return {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def bundle(root, name, path, query, value):
    body = canonical_json(value).encode()
    # Actual transport JSON uses JSON numbers, not normalized Decimal strings.
    request = {"method": "GET", "path": path, "query": query,
        "request_id": str(uuid4()), "request_started_at": (NOW+timedelta(seconds=4)).isoformat()}
    receipt = {"status_code": 200, "read_outcome": "complete", "content_encoding": "identity",
        "response_received_at": (NOW+timedelta(seconds=5)).isoformat(),
        "body_sha256": hashlib.sha256(body).hexdigest(), "body_bytes": len(body)}
    return {"request": save(root/(name+'.request.json'), request),
        "receipt": save(root/(name+'.receipt.json'), receipt),
        "body": save(root/(name+'.response.bin'), body, raw=True)}


def direct_account(root, account):
    sources = deepcopy(account.raw_sources)
    for name, source in sources.items():
        envelope = json.loads(source['result']['content'][0]['text'])
        body = envelope['body'].encode()
        path = root/(name+'.bin')
        save(path, body, raw=True)
        reference = str(uuid4())
        source.update(tool='etoro_direct_read', source_transport='official_demo_api', method='GET',
            request_id=reference, raw_request={'url':'https://public-api.etoro.com'+source['arguments']['path'],
                'method':'GET','request_id':reference,'body_sha256':None},
            raw_response={'status_code':200,'content_encoding':'identity','read_outcome':'complete',
                'body_sha256':hashlib.sha256(body).hexdigest(),'body_bytes':len(body),'body_file':str(path)},
            result={**envelope,'source_transport':'official_demo_api'})
    return save(root/'account.json',sources)


@pytest.fixture
def evidence(tmp_path):
    runtime = tmp_path/'original'
    runtime.mkdir(mode=0o700)
    ledger = PilotLedger.create(runtime/'pilot.sqlite3', Account(), [], NOW)
    for name in ('execution.sqlite3','operator.sqlite3'):
        save(runtime/name,b'existing synthetic store',raw=True)
    state = ledger.snapshot()
    save(runtime/'pilot-inception.json', {'policy':'demo-pilot-v1','account_id':'123',
                                         'inception_hash':payload_hash(state['inception'])})
    root = tmp_path/'proof';root.mkdir(mode=0o700)
    at = NOW+timedelta(seconds=3)
    current = Account(now=at,cash=D('11014'),positions=(Position(position_id='101',instrument_id=2,
        units=D('2'),principal=D('10'),pnl=D('0.09'),exposure=D('10.09')),))
    history = [{'positionId':1,'instrumentId':9,'isBuy':True,'leverage':1,'units':'10',
                'investment':'1000','netProfit':'25','fees':'1','closeTimestamp':(NOW+timedelta(seconds=1)).isoformat()}]
    lookup = {'accountId':123,'orderId':500,'action':'open','transaction':'buy','orderCurrency':'usd',
        'asset':{'instrumentId':2,'leverage':1,'settlementType':'real','side':'long'},'status':{'id':3,'name':'Filled'},'positionExecutions':[
            {'positionId':101,'state':'open','investedAmountCurrency':'10','remainingUnits':'2','addedFunds':'0',
             'openingData':{'orderId':500,'units':'2','fees':'0','taxes':'0',
                            'executionTime':(NOW+timedelta(seconds=2)).isoformat()}}]}
    manifest = {'schema':SCHEMA,'runtime':str(runtime),'inception_hash':payload_hash(state['inception']),
        'account_receipts':direct_account(root,current),
        'history':[bundle(root,'history','/api/v1/trading/info/trade/demo/history',
                         {'minDate':NOW.date().isoformat(),'page':1,'pageSize':100},history)],
        'opening_lookups':[bundle(root,'lookup','/api/v2/trading/info/demo/orders:lookup',{'orderId':'500'},lookup)]}
    path = root/'manifest.json';save(path,manifest)
    return runtime,root,path,manifest,history,lookup


def inspect(evidence, now=NOW+timedelta(seconds=6)):
    runtime,root,path,manifest,_,_=evidence
    save(path,manifest)
    return inspect_external_reconciliation(runtime=runtime,manifest=path,now=now)


def rewrite(evidence, which, value):
    _,root,_,manifest,_,_=evidence
    if which=='history':
        manifest['history']=[bundle(root,'history','/api/v1/trading/info/trade/demo/history',
                            {'minDate':NOW.date().isoformat(),'page':1,'pageSize':100},value)]
    else:
        manifest['opening_lookups']=[bundle(root,'lookup','/api/v2/trading/info/demo/orders:lookup',{'orderId':'500'},value)]


def bytes_under(root):
    return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_balancing_sources_still_cannot_authorize_import(evidence):
    before=bytes_under(evidence[0])
    result=inspect(evidence)
    assert result['cash_residual']=='0'
    assert result['known_baseline_closures_verified']==1
    assert result['external_lookup_positions_matched']==1
    assert result['prospective_external_capital']=='10'
    assert result['prospective_external_pnl']=='0.09'
    assert result['cash_event_coverage']=='unknown'
    assert not result['apply_supported'] and not result['activation_ready']
    assert result['migration_not_applied'] and result['new_submission_claims']==0
    assert not result['legacy_closure_profit_credited_to_pilot']
    assert 'cash_event_source_contract_unqualified' in result['blockers']
    assert bytes_under(evidence[0])==before


def test_current_shape_principal_conflict_and_cash_residual(evidence):
    history,lookup=deepcopy(evidence[4]),deepcopy(evidence[5])
    history[0]['netProfit']='123.70'
    lookup['positionExecutions'][0]['investedAmountCurrency']='1'
    rewrite(evidence,'history',history);rewrite(evidence,'lookup',lookup)
    result=inspect(evidence)
    assert result['cash_residual']=='-98.70'
    assert result['lookup_principal_conflicts']==1
    assert 'lookup_invested_amount_conflicts_with_account_principal' in result['blockers']
    assert 'unexplained_account_cash_difference' in result['blockers']


def test_historical_validation_does_not_claim_current_freshness(evidence):
    result=inspect(evidence,NOW+timedelta(days=1))
    assert result['historical_account_validation']=='passed'
    assert not result['current_account_fresh']
    assert 'current_account_refresh_required' in result['blockers']


@pytest.mark.parametrize('field,value', [('verified',True),('cash_residual','0'),('totals',{})])
def test_caller_assertions_and_totals_rejected(evidence,field,value):
    evidence[3][field]=value
    with pytest.raises(ExternalReconciliationError,match='manifest_shape'):
        inspect(evidence)


def test_original_inception_binding_rejected(evidence):
    evidence[3]['inception_hash']='0'*64
    with pytest.raises(ExternalReconciliationError,match='inception_mismatch'):
        inspect(evidence)


def test_runtime_binding_rejected(evidence):
    evidence[3]['runtime']=str(evidence[1])
    with pytest.raises(ExternalReconciliationError,match='runtime_invalid'):
        inspect(evidence)


def test_source_hash_tampering_rejected(evidence):
    p=evidence[1]/'history.response.bin';p.write_bytes(p.read_bytes()+b' ')
    with pytest.raises(ExternalReconciliationError,match='hash_mismatch'):
        inspect(evidence)


def test_response_byte_binding_rejected_even_with_manifest_hash_replaced(evidence):
    root=evidence[1]
    evidence[3]['history'][0]['body']=save(root/'history.response.bin',b'[]',raw=True)
    with pytest.raises(ExternalReconciliationError,match='provenance_invalid'):
        inspect(evidence)


def test_duplicate_json_keys_rejected(evidence):
    evidence[3]['history'][0]['request']=save(evidence[1]/'bad.json',b'{"method":"GET","method":"POST"}',raw=True)
    with pytest.raises(ExternalReconciliationError,match='json_invalid'):
        inspect(evidence)


@pytest.mark.parametrize('value',['1e100000000','1e-100000000','9'*101])
def test_numeric_amplification_rejected_before_arithmetic(evidence,value):
    history=deepcopy(evidence[4]);history[0]['netProfit']=value;rewrite(evidence,'history',history)
    with pytest.raises(ExternalReconciliationError):
        inspect(evidence)


def test_oversize_identity_rejected(evidence):
    lookup=deepcopy(evidence[5]);lookup['positionExecutions'][0]['positionId']='9223372036854775808'
    rewrite(evidence,'lookup',lookup)
    with pytest.raises(ExternalReconciliationError,match='positive_identity'):
        inspect(evidence)


def test_source_path_escape_rejected(evidence):
    evidence[3]['history'][0]['body']['path']='../original/pilot.sqlite3'
    with pytest.raises(ExternalReconciliationError,match='path_escape'):
        inspect(evidence)


def test_duplicate_closed_identity_rejected(evidence):
    rewrite(evidence,'history',evidence[4]*2)
    with pytest.raises(ExternalReconciliationError,match='duplicate_history'):
        inspect(evidence)


@pytest.mark.parametrize('change,blocker', [({'units':'9'},'closed_position_economics_mismatch'),
    ({'instrumentId':8},'closed_position_economics_mismatch'),
    ({'closeTimestamp':(NOW-timedelta(days=1)).isoformat()},'closed_position_time_outside_interval')])
def test_closed_position_mismatch_remains_missing(evidence,change,blocker):
    history=deepcopy(evidence[4]);history[0].update(change);rewrite(evidence,'history',history)
    result=inspect(evidence)
    assert blocker in result['blockers']
    assert 'missing_baseline_closure_evidence' in result['blockers']


def test_no_history_row_does_not_infer_closed(evidence):
    rewrite(evidence,'history',[])
    assert inspect(evidence)['known_baseline_closures_verified']==0


def test_lookup_wrong_original_identity_rejected(evidence):
    lookup=deepcopy(evidence[5]);lookup['orderId']=501;rewrite(evidence,'lookup',lookup)
    with pytest.raises(ExternalReconciliationError,match='original_identity_mismatch'):
        inspect(evidence)


def test_lookup_wrong_account_rejected(evidence):
    lookup=deepcopy(evidence[5]);lookup['accountId']=999;rewrite(evidence,'lookup',lookup)
    with pytest.raises(ExternalReconciliationError,match='account_or_time'):
        inspect(evidence)


def test_pending_lookup_cannot_become_terminal(evidence):
    lookup=deepcopy(evidence[5]);lookup['status']={'id':5,'name':'PartiallyFilled'};rewrite(evidence,'lookup',lookup)
    result=inspect(evidence)
    assert 'external_opening_terminal_scope_unverified' in result['blockers']
    assert result['external_lookup_positions_matched']==0


@pytest.mark.parametrize('field,value',[('leverage',2),('settlementType','cfd'),('side','short')])
def test_lookup_asset_economics_must_match_account(evidence,field,value):
    lookup=deepcopy(evidence[5]);lookup['asset'][field]=value;rewrite(evidence,'lookup',lookup)
    result=inspect(evidence)
    assert result['external_lookup_positions_matched']==0
    assert 'external_opening_identity_units_or_time_mismatch' in result['blockers']


@pytest.mark.parametrize('carrying',['1','-1'])
def test_carrying_cost_or_credit_cannot_be_silently_excluded(evidence,carrying):
    current=Account(now=NOW+timedelta(seconds=3),cash=D('11014'),positions=(
        Position(position_id='101',instrument_id=2,units=D('2'),principal=D('10'),
                 pnl=D('0.09'),exposure=D('10.09'),carrying_fees=D(carrying)),))
    evidence[3]['account_receipts']=direct_account(evidence[1],current)
    result=inspect(evidence)
    assert 'external_carrying_fee_cash_basis_unqualified' in result['blockers']
    assert result['nonzero_external_carrying_fee_positions']==1
    assert result['reported_external_carrying_fees']==carrying
    assert result['prospective_amounts_and_cash_bridge_exclude_unqualified_carrying_effects']


def test_missing_timezone_rejected(evidence):
    history=deepcopy(evidence[4]);history[0]['closeTimestamp']='2026-10-23T12:00:01';rewrite(evidence,'history',history)
    with pytest.raises(ExternalReconciliationError,match='timezone'):
        inspect(evidence)


def test_seventh_fractional_digit_after_capture_is_not_truncated(evidence):
    history=deepcopy(evidence[4]);history[0]['closeTimestamp']='2026-10-23T12:00:03.0000001Z'
    rewrite(evidence,'history',history)
    assert 'closed_position_time_outside_interval' in inspect(evidence)['blockers']


@pytest.mark.parametrize('kind', ['history','lookup'])
def test_event_cannot_postdate_its_own_response(evidence,kind):
    _,root,_,manifest,_,_=evidence
    item=manifest['history' if kind=='history' else 'opening_lookups'][0]
    request=json.loads((root/item['request']['path']).read_text())
    receipt=json.loads((root/item['receipt']['path']).read_text())
    request['request_started_at']=(NOW+timedelta(microseconds=100000)).isoformat()
    receipt['response_received_at']=(NOW+timedelta(microseconds=200000)).isoformat()
    item['request']=save(root/item['request']['path'],request)
    item['receipt']=save(root/item['receipt']['path'],receipt)
    result=inspect(evidence)
    expected='closed_position_time_outside_interval' if kind=='history' else 'external_opening_identity_units_or_time_mismatch'
    assert expected in result['blockers']


def test_nonempty_wal_requires_stable_snapshot(evidence):
    save(evidence[0]/'pilot.sqlite3-wal',b'live writer',raw=True)
    with pytest.raises(ExternalReconciliationError,match='wal_requires_stopped'):
        inspect(evidence)


def test_no_sqlite_sidecars_created(evidence):
    before=bytes_under(evidence[0]);inspect(evidence)
    assert bytes_under(evidence[0])==before
    assert not list(evidence[0].glob('*-shm'))


def test_arithmetic_exact_under_hostile_decimal_context(evidence):
    history=deepcopy(evidence[4]);history[0]['netProfit']='25.00000000000000000000000000000000000001'
    rewrite(evidence,'history',history)
    with localcontext() as ctx:
        ctx.prec=6
        result=inspect(evidence)
    assert D(result['cash_residual'])==D('-0.00000000000000000000000000000000000001')


def test_cli_apply_flag_is_rejected_without_writes(evidence,capsys):
    runtime,root,path,*_=evidence;before=bytes_under(runtime)
    assert main(['--runtime',str(runtime),'--manifest',str(path),'--apply'])==2
    text=capsys.readouterr().out
    assert 'INVALID_EVIDENCE' in text
    assert str(runtime) not in text
    assert bytes_under(runtime)==before


def test_cli_successful_inspection_is_still_blocked(evidence,capsys):
    assert main(['--runtime',str(evidence[0]),'--manifest',str(evidence[2])])==1
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='EXTERNAL_RECONCILIATION_BLOCKED'
    assert result['apply_supported'] is False
