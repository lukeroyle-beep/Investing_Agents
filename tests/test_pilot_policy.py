"""Pilot exceptions never permit adding to a breached issuer or losing costs."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from execution.pilot_policy import PilotCatalog, PilotClassification, PilotPolicyError, evaluate_pilot, preview_costs
from tests.test_demo_pilot_operator import NOW, account_at


def catalog(same_issuer=False):
    return PilotCatalog((
        PilotClassification(1001,"AAPL","legacy" if same_issuer else "Apple","technology","equity","synthetic-reviewed"),
        PilotClassification(1002,"MSFT","legacy","technology","equity","synthetic-reviewed")),
        {"results":[{"instrumentId":1001,"symbol":"AAPL","type":"Stocks"},
                    {"instrumentId":1002,"symbol":"MSFT","type":"Stocks"}],"pagination":{"hasNext":False}})


def decision(*, amount=D(10), cost=D('.01'), same=False, **state):
    ledger = {"account_id":"123","committed_capital":D(0),"position_count":0,
              "pilot_pnl":D(0),"loss_stopped":False,"unresolved":False,"submissions":[],**state}
    return evaluate_pilot(account=account_at(NOW),ledger=ledger,catalog=catalog(same),
        instrument_id=1001,amount=amount,expected_costs=cost,now=NOW)


def test_legacy_breach_recorded_but_new_issuer_can_enter():
    result = decision()
    assert result['eligible']
    assert any(x['kind']=='issuer:legacy' for x in result['legacy_breaches'])
    assert result['historical_daily_loss'] is None and result['qualification_credit'] is False


def test_other_share_class_of_over_limit_issuer_rejected():
    assert not decision(same=True)['checks']['issuer_cap']


@pytest.mark.parametrize('amount,cost,passed', [(D('99.99'),D('.01'),True),(D(100),D('1e-40'),False),
    (D(100),D(0),True),(D('100.01'),D(0),False)])
def test_order_boundaries_include_exact_tiny_cost(amount,cost,passed):
    assert decision(amount=amount,cost=cost)['checks']['order_cap'] is passed


@pytest.mark.parametrize('state,check', [({'committed_capital':D(490)},'capital_cap'),
    ({'position_count':5},'position_cap'),({'pilot_pnl':D(-25)},'loss_stop'),
    ({'loss_stopped':True},'loss_stop'),({'unresolved':True},'reconciled'),
    ({'submissions':[{'london_day':'2026-10-26'}]},'daily_submission')])
def test_ledger_limits_hold_new_admission(state,check):
    assert not decision(**state)['checks'][check]


def preview():
    return {'confirmation':{'costs':[{'type':'transactionFee','value':'0.01','currency':'USD'},
        {'type':'overnightFee','value':'0','currency':'USD'}], 'totalUpfrontCost':'0.01',
        'estimatedUnits':'0.1','quote':{'ask':'100','bid':'99.99'}}}


def test_spread_reserved_without_repeating_cash_fee():
    assert preview_costs(preview()) == (D('.01'),D('.011'))


@pytest.mark.parametrize('kind',['wrong_currency','negative','unknown','mismatched_sum','recurring'])
def test_unverified_costs_cannot_pass(kind):
    p=preview(); c=p['confirmation']
    if kind=='wrong_currency': c['costs'][0]['currency']='GBP'
    if kind=='negative': c['costs'][0]['value']='-0.01'
    if kind=='unknown': c['costs'][0]['type']='unknownFee'
    if kind=='mismatched_sum': c['totalUpfrontCost']='0'
    if kind=='recurring': c['costs'][1]['value']='0.01'
    with pytest.raises(PilotPolicyError): preview_costs(p)
