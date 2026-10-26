import json
from decimal import Decimal

import pytest

from brokers.etoro.client import EtoroClientError, EtoroConfigurationError
from brokers.etoro.connector_read import EtoroConnectorReadOnlyClient
from brokers.etoro.endpoints import GET_DEMO_PNL_PORTFOLIO, GET_DEMO_POSITION_CURRENCIES, SUBMIT_DEMO_ORDER_V3
from tests.contracts.etoro.test_etoro_market_evidence import payload


def reader(response):
    calls = []
    def execute(**kwargs):
        calls.append(kwargs)
        return response
    return EtoroConnectorReadOnlyClient(execute_read=execute), calls


def test_raw_text_decodes_exact_decimals_without_credentials():
    client, calls = reader({'content': [{'type': 'text', 'text':
        '{"statusCode":200,"body":{"cash":123.123456789123456789}}'}]})
    result = client._read(GET_DEMO_PNL_PORTFOLIO, {})
    assert result['cash'] == Decimal('123.123456789123456789')
    assert calls == [{'path': '/api/v1/trading/info/demo/pnl'}]
    assert not hasattr(client, '_credentials')


def test_live_eligibility_reuses_strict_parser_and_exact_read_semantics_body():
    client, calls = reader(json.dumps({'statusCode': 200, 'body': payload()}))
    assert client.get_demo_eligibility((1001,)).items[0].real_long_one_available
    assert json.loads(calls[0]['body']) == {'instrumentIds': [1001], 'currency': 'USD'}


@pytest.mark.parametrize('endpoint', [SUBMIT_DEMO_ORDER_V3, GET_DEMO_POSITION_CURRENCIES])
def test_mutation_and_missing_header_capability_fail_before_send(endpoint):
    client, calls = reader(None)
    with pytest.raises(EtoroConfigurationError):
        client._read(endpoint, {})
    assert not calls


@pytest.mark.parametrize('response', [
    {'statusCode': 403, 'body': {}}, {'statusCode': 429, 'body': {}},
    {'statusCode': 0, 'body': {}}, {'statusCode': 200, 'body': {}, 'bodyTruncated': True},
    {'statusCode': 200, 'body': {'cash': 1.23456789}},
])
def test_failures_never_retry_or_convert_binary_floats(response):
    client, calls = reader(response)
    with pytest.raises(EtoroClientError):
        client._read(GET_DEMO_PNL_PORTFOLIO, {})
    assert len(calls) == 1


@pytest.mark.parametrize("response", [
    '{"statusCode":200,"body":{broken private-response-text}',
    b"\xffprivate-response-text",
    '{"statusCode":200,"body":{"cash":NaN}}',
    {"statusCode": 429, "body": {"private-response-text": "rate limit"}},
    {"statusCode": 200, "body": {"cash": 1.23456789}},
    {"isError": True, "content": [{"type": "text", "text": "private-response-text"}]},
    {"content": [None]},
])
def test_raw_failure_receipt_precedes_parsing_or_error_checks(response):
    receipts = []
    calls = []
    def execute(**arguments):
        calls.append(arguments)
        return response
    client = EtoroConnectorReadOnlyClient(execute_read=execute, receipt_sink=receipts.append)
    with pytest.raises(EtoroClientError) as error:
        client._read(GET_DEMO_PNL_PORTFOLIO, {})
    assert len(calls) == len(receipts) == 1
    assert receipts[0]["raw_response"] == response
    assert receipts[0]["operation"] == GET_DEMO_PNL_PORTFOLIO.operation
    assert receipts[0]["request"] == calls[0]
    assert receipts[0]["started_at"] <= receipts[0]["completed_at"]
    assert "private-response-text" not in str(error.value)


def test_raw_success_receipt_keeps_original_decimal_text():
    response = '{"statusCode":200,"body":{"cash":123.123456789123456789}}'
    receipts = []
    client = EtoroConnectorReadOnlyClient(execute_read=lambda **_: response, receipt_sink=receipts.append)
    result = client._read(GET_DEMO_PNL_PORTFOLIO, {})
    assert result["cash"] == Decimal("123.123456789123456789")
    assert receipts[0]["raw_response"] == response
