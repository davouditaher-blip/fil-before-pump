"""Deterministic tests for the Zerion historical wallet-history provider.

Every fixture is a hand-written payload and every transport is a fake, so each
test pins one rule of the integration rather than whatever the live API happens
to return. Nothing here touches the network, and ``requests`` is not imported at
all: ``zerion_layer`` imports it lazily, so this suite runs in a bare checkout.

The properties under test are the ones the integration has to get right to be
safe to merge into the existing GMGN history:

* the documented endpoint, auth-free request construction and filter mapping;
* cursor pagination that follows the opaque ``links.next`` token verbatim;
* retries on 429/500/503 and no retry on 400/401/422;
* normalization that matches the existing wallet-history row contract and
  fabricates nothing (no price_change, no peak_multiple, no guessed open/close);
* an additive merge that can never delete or rewrite a GMGN row.

Live trading, scoring and candidate selection are not touched by this module and
are not exercised here.
"""
import base64
import json
import time

import wallet_history_validation as whv
import zerion_layer as zl
import zerion_smoke_test as zst

DAY = 86400
T0 = 1_700_000_000  # 2023-11-14T22:13:20Z
T0_ISO = "2023-11-14T22:13:20+00:00"
WALLET = "0x3D457D0B79EFAC77ed38F37870C713D0244479EA"


def _next_url(token: str) -> str:
    """A distinct opaque ``links.next`` URL, as the provider would return."""
    return (
        f"https://api.zerion.io/v1/wallets/{WALLET}"
        f"/transactions/?currency=usd&page%5Bsize%5D=100&page%5Bafter%5D={token}"
    )


NEXT_URL = _next_url("OPAQUE_CURSOR_TOKEN")


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = dict(headers or {})
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeTransport:
    """Replays a fixed script of responses and records every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, params, headers, auth, timeout):
        self.calls.append({
            "url": url, "params": params, "headers": headers,
            "auth": auth, "timeout": timeout,
        })
        if not self.responses:
            raise AssertionError(f"unexpected extra request: {url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def urls(self):
        return [call["url"] for call in self.calls]

    @property
    def auths(self):
        return [call["auth"] for call in self.calls]


def no_sleep(_seconds):
    return None


# ---------------------------------------------------------------------------
# Zerion payload fixtures
# ---------------------------------------------------------------------------
def _transfer(direction, symbol, address, quantity=100.0, price=1.0, value=None,
              chain_id="ethereum", native=False):
    implementations = [{"chain_id": chain_id, "address": address, "decimals": 18}]
    if native:
        implementations = [{"chain_id": chain_id, "address": None, "decimals": 18}]
    if value is None and price is not None:
        value = round(quantity * price, 6)
    transfer = {
        "fungible_info": {
            "id": f"id-{symbol}",
            "name": symbol.title(),
            "symbol": symbol,
            "flags": {"verified": True},
            "implementations": implementations,
        },
        "direction": direction,
        "quantity": {
            "int": str(int(quantity)), "decimals": 18,
            "float": quantity, "numeric": str(quantity),
        },
        "price": price,
        "value": value,
        "sender": WALLET if direction == "out" else "0xrouter",
        "recipient": "0xrouter" if direction == "out" else WALLET,
        "act_id": "1",
    }
    return transfer


def _transaction(transfers, mined_at=T0_ISO, chain="ethereum", operation="trade",
                 status="confirmed", tx_hash="0xabc", is_trash=False):
    attributes = {
        "address": WALLET,
        "operation_type": operation,
        "hash": tx_hash,
        "mined_at_block": 18_500_000,
        "mined_at": mined_at,
        "sent_from": WALLET,
        "sent_to": "0xrouter",
        "status": status,
        "nonce": 7,
        "fee": {"quantity": {"int": "1", "decimals": 18, "float": 0.0004},
                "price": 2000.0, "value": 0.8},
        "transfers": transfers,
        "approvals": [],
    }
    if is_trash:
        attributes["flags"] = {"is_trash": True}
    return {
        "type": "transactions",
        "id": tx_hash,
        "attributes": attributes,
        "relationships": {"chain": {"data": {"type": "chains", "id": chain}}},
    }


def _page(data, next_url=None):
    links = {"self": NEXT_URL}
    if next_url:
        links["next"] = next_url
    return {"links": links, "data": data}


# A realistic buy/sell swap: the wallet sends AAA and receives BBB.
SWAP = _transaction([
    _transfer("out", "AAA", "0xaaa", quantity=1000.0, price=2.0),
    _transfer("in", "BBB", "0xbbb", quantity=400.0, price=5.0),
], tx_hash="0xswap")


# ---------------------------------------------------------------------------
# 1. Request construction
# ---------------------------------------------------------------------------
def test_transactions_url_is_the_documented_wallet_transactions_endpoint():
    url = zl.transactions_url(WALLET)
    assert url == (
        "https://api.zerion.io/v1/wallets/0x3D457D0B79EFAC77ed38F37870C713D0244479EA"
        "/transactions/"
    )
    # The path segment is escaped, so an address can never break out of the URL.
    assert zl.transactions_url("a/b?x=1#f").endswith("/wallets/a%2Fb%3Fx%3D1%23f/transactions/")


def test_transactions_url_requires_an_address():
    for empty in ("", "   ", None):
        try:
            zl.transactions_url(empty)
        except ValueError:
            continue
        raise AssertionError("an empty address must be rejected")


def test_chain_names_map_between_project_and_zerion_spellings():
    assert zl.zerion_chain("eth") == "ethereum"
    assert zl.zerion_chain("BSC") == "binance-smart-chain"
    assert zl.zerion_chain("sol") == "solana"
    assert zl.repo_chain("binance-smart-chain") == "bsc"
    assert zl.repo_chain("solana") == "sol"
    assert zl.repo_chain("ethereum") == "eth"
    # Round-trips, so a Zerion row and a GMGN row share one asset identity.
    for name in zl.SUPPORTED_CHAINS:
        assert zl.repo_chain(zl.zerion_chain(name)) == name


def test_build_params_uses_zerion_chain_ids_and_page_size():
    params = zl.build_params(address=WALLET, chains=["eth", "base"], page_size=50)
    assert params["filter[chain_ids]"] == "ethereum,base"
    assert params["page[size]"] == "50"
    assert params["currency"] == "usd"


def test_build_params_accepts_a_comma_separated_chain_string():
    params = zl.build_params(address=WALLET, chains="eth, base")
    assert params["filter[chain_ids]"] == "ethereum,base"
    sol_wallet = "FjE5mmR3B45T49LsJx75cMPkW6TGnRMHbhucWT9wQpee"
    assert zl.build_params(address=sol_wallet, chains="sol,solana")[
        "filter[chain_ids]"] == "solana"


def test_build_params_deduplicates_chains():
    assert zl.build_params(address=WALLET, chains=["eth", "ethereum"])["filter[chain_ids]"] == "ethereum"


def test_build_params_rejects_an_unsupported_chain_locally():
    """A bad chain costs a 400 and a request; it is refused before it is sent."""
    try:
        zl.build_params(address=WALLET, chains=["dogecoin"])
    except ValueError as exc:
        assert "unsupported Zerion chain" in str(exc)
    else:
        raise AssertionError("an unsupported chain must be rejected")


def test_build_params_rejects_a_chain_the_wallet_address_family_cannot_have():
    """Zerion 400s an EVM chain filter for a Solana address; do not spend it."""
    sol_wallet = "FjE5mmR3B45T49LsJx75cMPkW6TGnRMHbhucWT9wQpee"
    try:
        zl.build_params(address=sol_wallet, chains=["eth"])
    except ValueError as exc:
        assert "not compatible with a solana wallet address" in str(exc)
    else:
        raise AssertionError("a cross-family chain filter must be rejected")
    # The right family is accepted.
    assert zl.build_params(address=sol_wallet, chains=["sol"])["filter[chain_ids]"] == "solana"


def test_trade_filter_defaults_to_trade_only():
    params = zl.build_params(address=WALLET)
    assert params["filter[operation_types]"] == "trade"


def test_trade_filter_is_configurable_and_validated():
    assert zl.build_params(address=WALLET, operation_types=["trade", "send"])[
        "filter[operation_types]"] == "trade,send"
    try:
        zl.build_params(address=WALLET, operation_types=["nonsense"])
    except ValueError as exc:
        assert "unsupported Zerion operation type" in str(exc)
    else:
        raise AssertionError("an unknown operation type must be rejected")


def test_spam_filtering_is_explicit_and_defaults_to_excluding_trash():
    assert zl.build_params(address=WALLET)["filter[trash]"] == "only_non_trash"
    assert zl.build_params(address=WALLET, include_trash=True)["filter[trash]"] == "no_filter"


def test_historical_date_range_is_sent_as_13_digit_milliseconds():
    params = zl.build_params(address=WALLET, start=T0, end=T0 + 30 * DAY)
    assert params["filter[min_mined_at]"] == str(T0 * 1000)
    assert params["filter[max_mined_at]"] == str((T0 + 30 * DAY) * 1000)
    assert len(params["filter[min_mined_at]"]) == 13


def test_historical_date_range_accepts_iso_and_date_strings():
    by_iso = zl.build_params(address=WALLET, start=T0_ISO)["filter[min_mined_at]"]
    assert by_iso == str(T0 * 1000)
    by_date = zl.build_params(address=WALLET, start="2023-11-14")["filter[min_mined_at]"]
    assert by_date == "1699920000000"
    by_millis = zl.build_params(address=WALLET, start=T0 * 1000)["filter[min_mined_at]"]
    assert by_millis == str(T0 * 1000)


def test_unusable_date_range_is_rejected_before_the_request():
    for bad in ("not-a-date", 123, "0000-00-00"):
        try:
            zl.build_params(address=WALLET, start=bad)
        except ValueError as exc:
            assert "unusable history start date" in str(exc)
        else:
            raise AssertionError(f"unusable start date {bad!r} must be rejected")


def test_inverted_date_range_is_rejected():
    try:
        zl.build_params(address=WALLET, start=T0 + DAY, end=T0)
    except ValueError as exc:
        assert "start date is after the end date" in str(exc)
    else:
        raise AssertionError("an inverted range must be rejected")


def test_page_size_is_bounded_to_the_documented_range():
    assert zl.build_params(address=WALLET, page_size=1)["page[size]"] == "1"
    assert zl.build_params(address=WALLET, page_size=100)["page[size]"] == "100"
    for bad in (0, 101, "many"):
        try:
            zl.build_params(address=WALLET, page_size=bad)
        except ValueError:
            continue
        raise AssertionError(f"page_size {bad!r} must be rejected")


# ---------------------------------------------------------------------------
# 2. Retries and rate-limit handling
# ---------------------------------------------------------------------------
def test_missing_api_key_issues_no_request():
    """An unkeyed run must fail loudly, never look like an empty wallet."""
    transport = FakeTransport()
    page = zl.fetch_page(zl.transactions_url(WALLET), {"page[size]": "100"},
                         api_key="", http_get=transport)
    assert page["ok"] is False
    assert page["error"] == "missing_api_key"
    assert transport.calls == []


def test_basic_auth_sends_the_key_as_username_with_an_empty_password():
    transport = FakeTransport(FakeResponse(200, _page([])))
    zl.fetch_page(zl.transactions_url(WALLET), {}, api_key="secret-key",
                  http_get=transport, sleep=no_sleep)
    key, password = transport.auths[0]
    assert key == "secret-key"
    assert password == ""


def test_429_is_retried_and_then_succeeds():
    transport = FakeTransport(
        FakeResponse(429, {"errors": [{"title": "Too many requests"}]},
                     headers={"RateLimit-Org-Second-Reset": "1"}),
        FakeResponse(200, _page([SWAP])),
    )
    waits = []
    page = zl.fetch_page(zl.transactions_url(WALLET), {}, api_key="k",
                         http_get=transport, sleep=waits.append)
    assert page["ok"] is True
    assert page["attempts"] == 2
    # Zerion documents backoff for 429, unlike GMGN where retrying extends a ban.
    assert waits and waits[0] >= 1.0


def test_retry_after_header_is_honoured():
    transport = FakeTransport(
        FakeResponse(503, {"errors": [{"detail": "still preparing"}]},
                     headers={"Retry-After": "7"}),
        FakeResponse(200, _page([])),
    )
    waits = []
    page = zl.fetch_page(zl.transactions_url(WALLET), {}, api_key="k",
                         http_get=transport, sleep=waits.append)
    assert page["ok"] is True
    assert waits[0] == 7.0


def test_backoff_grows_exponentially_and_stays_capped():
    transport = FakeTransport(*[FakeResponse(500, {"errors": [{"detail": "boom"}]})
                                for _ in range(3)],
                              FakeResponse(200, _page([])))
    waits = []
    page = zl.fetch_page(zl.transactions_url(WALLET), {}, api_key="k",
                         http_get=transport, sleep=waits.append, attempts=4)
    assert page["ok"] is True
    assert waits == [1.0, 2.0, 4.0]
    assert max(waits) <= zl.BACKOFF_CAP_SECONDS


def test_retry_attempts_are_bounded():
    transport = FakeTransport(*[FakeResponse(500, {}) for _ in range(10)])
    page = zl.fetch_page(zl.transactions_url(WALLET), {}, api_key="k",
                         http_get=transport, sleep=no_sleep, attempts=3)
    assert page["ok"] is False
    assert len(transport.calls) == 3


def test_400_401_and_422_are_never_retried():
    """Zerion states these return the same answer however often they are sent."""
    for status in (400, 401, 422):
        transport = FakeTransport(FakeResponse(
            status, {"errors": [{"title": "no", "detail": f"status {status}"}]},
        ))
        page = zl.fetch_page(zl.transactions_url(WALLET), {}, api_key="k",
                             http_get=transport, sleep=no_sleep, attempts=4)
        assert page["ok"] is False
        assert page["status"] == status
        assert len(transport.calls) == 1
        assert f"http_{status}" in page["error"]
        assert str(status) in page["error"]


def test_402_and_403_are_terminal_credential_rejections():
    """402 and 403 are what the live API returns for a rejected credential.

    Measured against api.zerion.io: an unauthenticated request answers 402
    ("Provide an API key via Authorization: Basic <base64(api_key:)>"), and a
    request the edge refuses answers 403. Neither is transient, so neither is
    retried, and both are labelled as a credential fault so a caller can tell
    them apart from a wallet that genuinely has no history.
    """
    for status in (402, 403):
        transport = FakeTransport(FakeResponse(
            status, {"errors": [{"detail": "Provide an API key"}]},
        ))
        page = zl.fetch_page(zl.transactions_url(WALLET), {}, api_key="k",
                             http_get=transport, sleep=no_sleep, attempts=4)
        assert len(transport.calls) == 1, f"{status} must not be retried"
        assert page["terminal"] is True
        assert page["reason"] == "CREDENTIAL_REJECTED"
    assert 402 in zl.TERMINAL_STATUS and 403 in zl.TERMINAL_STATUS


def test_a_malformed_request_is_terminal_but_not_a_credential_fault():
    transport = FakeTransport(FakeResponse(
        400, {"errors": [{"detail": "chain tron does not support transactions"}]},
    ))
    page = zl.fetch_page(zl.transactions_url(WALLET), {}, api_key="k",
                         http_get=transport, sleep=no_sleep)
    assert page["terminal"] is True
    assert page["reason"] == "REQUEST_NOT_SERVABLE"
    assert "does not support transactions" in page["error"]


def test_the_real_error_envelope_is_parsed():
    """The shape Zerion actually returns, captured from the live API."""
    response = FakeResponse(401, {"errors": [{
        "title": "Unauthorized Error",
        "detail": "The API key is invalid, please, make sure that you are using a valid key",
    }]})
    transport = FakeTransport(response)
    page = zl.fetch_page(zl.transactions_url(WALLET), {}, api_key="k",
                         http_get=transport, sleep=no_sleep)
    assert page["error"] == (
        "http_401: The API key is invalid, please, make sure that you are using a valid key"
    )


def test_terminal_reason_reaches_the_public_fetch_result():
    transport = FakeTransport(FakeResponse(401, {"errors": [{"detail": "bad key"}]}))
    result = zl.fetch_wallet_transactions(WALLET, api_key="k", http_get=transport,
                                          sleep=no_sleep)
    assert result["ok"] is False
    assert result["terminal"] is True
    assert result["reason"] == "CREDENTIAL_REJECTED"
    assert result["rows"] == []


def test_requests_carry_an_explicit_user_agent():
    """The default Python agent is refused at the Cloudflare edge with a 403.

    Measured against the live API: a request whose agent is
    `Python-urllib/x.y` never reaches Zerion and answers
    "Error 1010: The site owner has blocked access based on your browser's
    signature". Relying on a library's default agent to stay allowed is a
    hidden dependency, so the header is set explicitly.
    """
    transport = FakeTransport(FakeResponse(200, _page([])))
    zl.fetch_page(zl.transactions_url(WALLET), {}, api_key="k",
                  http_get=transport, sleep=no_sleep)
    headers = transport.calls[0]["headers"]
    assert headers["User-Agent"] == zl.USER_AGENT
    assert "Python-urllib" not in headers["User-Agent"]
    assert "python-requests" not in headers["User-Agent"]
    assert headers["Accept"] == "application/json"


def test_request_headers_carry_no_credential():
    text = json.dumps(zl.request_headers())
    assert "Authorization" not in text
    assert "api_key" not in text.lower()


def test_transport_failure_is_retried_then_reported():
    transport = FakeTransport(
        TimeoutError("read timed out"),
        ConnectionResetError("reset by peer"),
    )
    page = zl.fetch_page(zl.transactions_url(WALLET), {}, api_key="k",
                         http_get=transport, sleep=no_sleep, attempts=2)
    assert page["ok"] is False
    assert "transport_error" in page["error"]
    assert len(transport.calls) == 2


def test_exhausted_daily_quota_stops_the_run_instead_of_burning_retries():
    """A spent day/month quota cannot be waited out, so it is terminal."""
    zl._clear_rate_limit()
    try:
        transport = FakeTransport(FakeResponse(
            429, {"errors": [{"detail": "throttled"}]},
            headers={"RateLimit-Org-Day-Remaining": "0",
                     "RateLimit-Org-Second-Remaining": "0"},
        ))
        page = zl.fetch_page(zl.transactions_url(WALLET), {}, api_key="k",
                             http_get=transport, sleep=no_sleep, attempts=5)
        assert page["ok"] is False
        assert "daily quota exhausted" in page["error"]
        assert len(transport.calls) == 1
        assert zl.is_rate_limited() is True
    finally:
        zl._clear_rate_limit()


def test_a_spent_per_second_window_is_retryable_not_terminal():
    """The per-second limit resets within a second; abandoning the page is wrong."""
    zl._clear_rate_limit()
    transport = FakeTransport(
        FakeResponse(429, {"errors": [{"detail": "slow down"}]},
                     headers={"RateLimit-Org-Second-Remaining": "0",
                              "RateLimit-Org-Second-Reset": "1"}),
        FakeResponse(200, _page([])),
    )
    page = zl.fetch_page(zl.transactions_url(WALLET), {}, api_key="k",
                         http_get=transport, sleep=no_sleep)
    assert page["ok"] is True
    assert zl.is_rate_limited() is False


def test_provider_state_never_contains_the_api_key():
    zl.ZERION_API_KEY = "super-secret-key"
    try:
        state = json.dumps(zl.provider_state())
        assert "super-secret-key" not in state
        assert "api_key" not in state
    finally:
        zl.ZERION_API_KEY = ""


# ---------------------------------------------------------------------------
# 3. Cursor pagination
# ---------------------------------------------------------------------------
def test_pagination_follows_the_opaque_next_cursor_verbatim():
    transport = FakeTransport(
        FakeResponse(200, _page([SWAP], next_url=NEXT_URL)),
        FakeResponse(200, _page([])),
    )
    result = zl.fetch_wallet_transactions(WALLET, api_key="k", http_get=transport,
                                          sleep=no_sleep, now_ts=T0)
    assert result["ok"] is True
    assert result["pages"] == 2
    assert transport.urls[0] == zl.transactions_url(WALLET)
    assert transport.urls[1] == NEXT_URL
    # The first page carries the filter query string; the cursor page must not be
    # given one, because the provider's own URL already carries it.
    assert transport.calls[0]["params"]["page[size]"] == "100"
    assert transport.calls[1]["params"] is None


def test_pagination_stops_when_links_next_is_absent():
    transport = FakeTransport(FakeResponse(200, _page([SWAP])))
    result = zl.fetch_wallet_transactions(WALLET, api_key="k", http_get=transport,
                                          sleep=no_sleep, now_ts=T0)
    assert result["ok"] is True
    assert result["pages"] == 1
    assert result["truncated"] is False
    assert len(transport.calls) == 1


def test_pagination_sends_the_date_range_and_trade_filter():
    transport = FakeTransport(FakeResponse(200, _page([])))
    zl.fetch_wallet_transactions(WALLET, chains=["eth"], start=T0, end=T0 + 7 * DAY,
                                 api_key="k", http_get=transport, sleep=no_sleep)
    params = transport.calls[0]["params"]
    assert params["filter[chain_ids]"] == "ethereum"
    assert params["filter[operation_types]"] == "trade"
    assert params["filter[min_mined_at]"] == str(T0 * 1000)
    assert params["filter[max_mined_at]"] == str((T0 + 7 * DAY) * 1000)


def test_pagination_stops_at_max_pages_and_reports_truncation():
    pages = [FakeResponse(200, _page([SWAP], next_url=_next_url(f"cursor{i}")))
             for i in range(10)]
    transport = FakeTransport(*pages)
    result = zl.fetch_wallet_transactions(WALLET, max_pages=3, api_key="k",
                                          http_get=transport, sleep=no_sleep)
    assert result["pages"] == 3
    assert result["truncated"] is True
    assert result["error"] == "max_pages_reached"
    assert result["ok"] is False
    assert len(transport.calls) == 3


def test_pagination_refuses_a_cursor_that_leaves_the_zerion_host():
    """The cursor is followed verbatim, and the key rides along with it."""
    transport = FakeTransport(
        FakeResponse(200, _page([SWAP], next_url="https://evil.example/v1/wallets/x/transactions/")),
    )
    result = zl.fetch_wallet_transactions(WALLET, api_key="k", http_get=transport,
                                          sleep=no_sleep)
    assert result["ok"] is False
    assert result["error"] == "untrusted_next_cursor"
    assert len(transport.calls) == 1
    # And the page helper refuses such a URL on its own.
    assert zl.fetch_page("http://api.zerion.io/x", api_key="k", http_get=transport,
                         sleep=no_sleep)["error"] == "untrusted_url"
    assert zl.fetch_page("https://api.zerion.io.evil.test/x", api_key="k",
                         http_get=transport, sleep=no_sleep)["error"] == "untrusted_url"


def test_pagination_detects_a_repeating_cursor_instead_of_looping():
    """A provider that keeps handing back one cursor must not spin the run."""
    transport = FakeTransport(*[FakeResponse(200, _page([SWAP], next_url=NEXT_URL))
                               for _ in range(6)])
    result = zl.fetch_wallet_transactions(WALLET, max_pages=50, api_key="k",
                                          http_get=transport, sleep=no_sleep)
    assert result["error"] == "cursor_loop_detected"
    assert result["ok"] is False
    assert len(transport.calls) < 6


def test_a_failed_page_keeps_the_pages_already_fetched():
    transport = FakeTransport(
        FakeResponse(200, _page([SWAP], next_url=NEXT_URL)),
        FakeResponse(401, {"errors": [{"detail": "The API key is invalid"}]}),
    )
    result = zl.fetch_wallet_transactions(WALLET, api_key="k", http_get=transport,
                                          sleep=no_sleep, now_ts=T0)
    # Partial data is never thrown away, but the result is still not "ok":
    # unavailable must stay distinguishable from empty.
    assert result["ok"] is False
    assert "http_401" in result["error"]
    assert result["pages"] == 1
    assert len(result["rows"]) == 2


def test_an_empty_wallet_is_ok_with_no_rows_not_an_error():
    transport = FakeTransport(FakeResponse(200, _page([])))
    result = zl.fetch_wallet_transactions(WALLET, api_key="k", http_get=transport,
                                          sleep=no_sleep)
    assert result["ok"] is True
    assert result["rows"] == []
    assert result["error"] is None


def test_request_budget_stops_a_runaway_fan_out():
    transport = FakeTransport(*[FakeResponse(200, _page([SWAP], next_url=_next_url(f"c{i}")))
                               for i in range(10)])
    result = zl.fetch_wallet_transactions(WALLET, request_budget=2, api_key="k",
                                          http_get=transport, sleep=no_sleep)
    assert result["error"] == "request_budget_exhausted"
    assert len(transport.calls) == 2


def test_an_over_long_request_url_is_refused_before_it_is_sent():
    transport = FakeTransport()
    result = zl.fetch_wallet_transactions(WALLET, chains="eth", search_query="q" * 3000,
                                          api_key="k", http_get=transport, sleep=no_sleep)
    assert result["ok"] is False
    assert "2000-character" in result["error"]
    assert transport.calls == []


# ---------------------------------------------------------------------------
# 4. Normalization
# ---------------------------------------------------------------------------
def test_a_swap_normalizes_into_one_buy_and_one_sell_row():
    rows = zl.normalize_transaction(SWAP, now_ts=T0 + 60)
    assert len(rows) == 2
    by_side = {row["side"]: row for row in rows}
    assert set(by_side) == {"buy", "sell"}
    # direction=in is the wallet acquiring the token; direction=out is exiting it.
    assert by_side["buy"]["symbol"] == "BBB"
    assert by_side["buy"]["address"] == "0xbbb"
    assert by_side["buy"]["amount_usd"] == 2000.0
    assert by_side["buy"]["price_usd"] == 5.0
    assert by_side["sell"]["symbol"] == "AAA"
    assert by_side["sell"]["amount_usd"] == 2000.0
    assert by_side["sell"]["price_usd"] == 2.0
    for row in rows:
        assert row["source"] == "zerion"
        assert row["transaction_hash"] == "0xswap"
        assert row["trade_timestamp"] == T0
        assert row["timestamp"] == T0 + 60


def test_normalized_rows_use_the_repository_short_chain_names():
    for chain_id, expected in (("ethereum", "eth"), ("base", "base"),
                               ("binance-smart-chain", "bsc"), ("solana", "sol")):
        tx = _transaction([_transfer("in", "AAA", "0xaaa", chain_id=chain_id)], chain=chain_id)
        assert zl.normalize_transaction(tx)[0]["chain"] == expected


def test_the_chain_specific_implementation_address_wins():
    """A multi-chain fungible must be keyed on this chain's contract, not another's."""
    info = {
        "symbol": "AAA",
        "implementations": [
            {"chain_id": "ethereum", "address": "0xeth", "decimals": 18},
            {"chain_id": "base", "address": "0xbase", "decimals": 18},
        ],
    }
    transfer = _transfer("in", "AAA", "0xaaa")
    transfer["fungible_info"] = info
    on_base = _transaction([transfer], chain="base")
    assert zl.normalize_transaction(on_base)[0]["address"] == "0xbase"
    on_eth = _transaction([transfer], chain="ethereum")
    assert zl.normalize_transaction(on_eth)[0]["address"] == "0xeth"


def test_native_asset_transfers_are_dropped_for_having_no_token_identity():
    tx = _transaction([_transfer("in", "ETH", None, chain_id="ethereum", native=True)])
    assert zl.normalize_transaction(tx) == []
    # A swap that also moved a token keeps the token leg only.
    mixed = _transaction([
        _transfer("out", "ETH", None, chain_id="ethereum", native=True),
        _transfer("in", "AAA", "0xaaa"),
    ])
    rows = zl.normalize_transaction(mixed)
    assert len(rows) == 1 and rows[0]["symbol"] == "AAA"


def test_self_transfers_are_not_trades():
    tx = _transaction([_transfer("self", "AAA", "0xaaa")])
    assert zl.normalize_transaction(tx) == []


def test_failed_and_pending_transactions_are_dropped():
    for status in ("failed", "pending"):
        tx = _transaction([_transfer("in", "AAA", "0xaaa")], status=status)
        assert zl.normalize_transaction(tx) == []


def test_spam_flagged_transactions_are_dropped():
    tx = _transaction([_transfer("in", "SCAM", "0xscam")], is_trash=True)
    assert zl.normalize_transaction(tx) == []


def test_price_is_reconstructed_from_value_and_quantity_when_absent():
    transfer = _transfer("in", "AAA", "0xaaa", quantity=250.0, price=None, value=1000.0)
    row = zl.normalize_transaction(_transaction([transfer]))[0]
    assert row["price_usd"] == 4.0
    assert row["amount_usd"] == 1000.0


def test_value_is_reconstructed_from_price_and_quantity_when_absent():
    transfer = _transfer("in", "AAA", "0xaaa", quantity=50.0, price=3.0, value=None)
    row = zl.normalize_transaction(_transaction([transfer]))[0]
    assert row["amount_usd"] == 150.0


def test_an_unpriced_transfer_is_not_reported_as_a_free_trade():
    """Zero price would read as a real entry at no cost; it is left honest."""
    transfer = _transfer("in", "AAA", "0xaaa", quantity=10.0, price=None, value=None)
    row = zl.normalize_transaction(_transaction([transfer]))[0]
    assert row["price_usd"] == 0.0
    assert row["amount_usd"] == 0.0
    # And the reconstruction refuses to call it an entry.
    assert whv.reconstruct_asset([row]) is None


def test_normalization_fabricates_no_peak_or_price_change_evidence():
    """Zerion has no current/entry ratio, so the keys must simply be absent."""
    rows = zl.normalize_transaction(SWAP)
    for row in rows:
        for key in ("price_change", "peak_multiple", "first_1_2x_timestamp",
                    "first_1_5x_timestamp", "first_2x_timestamp"):
            assert key not in row
    # open/close is genuinely unknown to Zerion and is not guessed.
    assert rows[0]["is_open_or_close"] is None
    assert rows[0]["maker_tags"] == []


def test_a_transaction_without_a_usable_timestamp_is_dropped():
    assert zl.normalize_transaction(_transaction([_transfer("in", "AAA", "0xaaa")], mined_at="")) == []
    assert zl.normalize_transaction("not a dict") == []


def test_a_malformed_transfer_is_skipped_not_fatal():
    tx = _transaction([_transfer("in", "AAA", "0xaaa"), "junk", {"direction": "in"}])
    rows = zl.normalize_transaction(tx)
    assert len(rows) == 1 and rows[0]["symbol"] == "AAA"


def test_solana_base58_case_is_preserved():
    """Lowercasing a base58 address is data corruption, not normalisation."""
    address = "DgACXKn6kSopTrFAyAwvGaeTKicrqWzLvXRYuvKGpump"
    tx = _transaction([_transfer("in", "PUMP", address, chain_id="solana")], chain="solana")
    row = zl.normalize_transaction(tx)[0]
    assert row["address"] == address
    assert whv.wallet_identity(address) == address


def test_repeated_transactions_across_pages_are_deduplicated():
    rows = zl.normalize_rows([SWAP, SWAP, SWAP])
    assert len(rows) == 2


def test_two_identical_trades_in_the_same_second_stay_two_events():
    """Same token, same second, same size, different transaction = two entries.

    The transaction hash is part of the identity for exactly this reason. Drop
    it and the key collapses to (asset, time, side, size, price), which would
    quietly delete a real second entry and understate a wallet's history.
    """
    first = _transaction([_transfer("in", "AAA", "0xaaa", quantity=100.0, price=1.0)],
                         tx_hash="0xfirst")
    second = _transaction([_transfer("in", "AAA", "0xaaa", quantity=100.0, price=1.0)],
                          tx_hash="0xsecond")
    rows = zl.normalize_rows([first, second], now_ts=T0)
    assert len(rows) == 2
    assert {row["transaction_hash"] for row in rows} == {"0xfirst", "0xsecond"}
    # The same holds for the consumer's own dedupe, or the merge would disagree.
    assert len(whv._dedupe(rows)) == 2
    history = {}
    merged = zl.merge_into_history(WALLET, rows, history)
    assert merged["added"] == 2
    assert merged["total"] == 2


# ---------------------------------------------------------------------------
# 5. Compatibility with the existing wallet-history format
# ---------------------------------------------------------------------------
def test_event_identity_matches_the_wallet_history_validation_dedupe_rule():
    """If these two drift apart, a merge would double-count one on-chain event."""
    sample = zl.normalize_transaction(SWAP, now_ts=T0) + zl.normalize_transaction(
        _transaction([_transfer("in", "CCC", "0xccc")], mined_at="2023-11-15T00:00:00+00:00",
                     tx_hash="0xother"),
        now_ts=T0,
    )
    duplicate = zl.normalize_transaction(SWAP, now_ts=T0)
    rows = sample + duplicate
    deduped = whv._dedupe(rows)
    assert len(deduped) == len(sample)
    keys = {zl.event_identity(row) for row in deduped}
    assert keys == {zl.event_identity(row) for row in sample}


def test_normalized_rows_feed_the_offline_history_reconstruction_unchanged():
    """The compatibility contract: a Zerion buy/sell pair is a real position."""
    entry = _transaction([_transfer("in", "AAA", "0xaaa", quantity=100.0, price=1.0)],
                         mined_at="2023-11-14T22:13:20+00:00", tx_hash="0xentry")
    # The wallet's own later trade of the same contract is the forward observation.
    later = _transaction([_transfer("in", "AAA", "0xaaa", quantity=100.0, price=2.5)],
                         mined_at="2023-11-16T00:00:00+00:00", tx_hash="0xlater")
    rows = zl.normalize_rows([entry, later], now_ts=T0)
    profile = whv.reconstruct_wallet(WALLET, rows, chain="eth")
    assert profile["opportunities"] == 1
    record = profile["all_examples"][0]
    assert record["address"] == "0xaaa"
    assert record["entry_price"] == 1.0
    assert record["peak_multiple"] == 2.5
    assert record["target_reached"] is True
    assert record["outcome"] == whv.OUTCOME_SUCCESS


def test_an_unobserved_zerion_entry_stays_unknown_never_zero_percent():
    entry = _transaction([_transfer("in", "AAA", "0xaaa", quantity=100.0, price=1.0)],
                         mined_at="2023-11-14T22:13:20+00:00", tx_hash="0xentry")
    profile = whv.reconstruct_wallet(WALLET, zl.normalize_rows([entry], now_ts=T0))
    assert profile["opportunities"] == 1
    assert profile["all_examples"][0]["outcome"] == whv.OUTCOME_UNKNOWN
    assert profile["unknown_opportunities"] == 1
    assert profile["pre_pump_win_rate"] is None
    assert profile["history_class"] == whv.ACTIVITY_BUT_UNPROVEN


# ---------------------------------------------------------------------------
# 6. Additive merge
# ---------------------------------------------------------------------------
def _gmgn_row(**extra):
    row = {
        "timestamp": T0, "trade_timestamp": T0, "chain": "sol",
        "address": "0xgmgnasset", "symbol": "GMGN", "side": "buy",
        "amount_usd": 5000.0, "price_usd": 1.0, "transaction_hash": "gmgntx",
    }
    row.update(extra)
    return row


def test_merge_adds_zerion_rows_without_touching_gmgn_rows():
    history = {WALLET: [_gmgn_row()]}
    gmgn_before = json.dumps(history, sort_keys=True)
    later = zl.normalize_transaction(
        _transaction([_transfer("in", "BBB", "0xbbb", quantity=400.0, price=5.0)],
                     mined_at="2023-11-15T22:13:20+00:00", tx_hash="0xlater"),
        now_ts=T0 + DAY,
    )
    result = zl.merge_into_history(WALLET, later, history)
    assert result["ok"] is True
    assert result["added"] == 1
    assert result["gmgn_rows"] == 1
    assert result["total"] == 2
    # The GMGN row is untouched, byte for byte, and still first in time.
    assert history[WALLET][0] == json.loads(gmgn_before)[WALLET][0]
    assert history[WALLET][0]["transaction_hash"] == "gmgntx"
    assert history[WALLET][0]["chain"] == "sol"
    assert [r["trade_timestamp"] for r in history[WALLET]] == [T0, T0 + DAY]
    assert history[WALLET][1]["source"] == "zerion"


def test_merge_writes_one_record_when_both_providers_already_agree():
    shared = _gmgn_row(trade_timestamp=T0, chain="eth", address="0xbbb",
                       symbol="BBB", side="buy", amount_usd=2000.0, price_usd=5.0,
                       transaction_hash="0xswap")
    history = {WALLET: [shared]}
    rows = zl.normalize_transaction(SWAP, now_ts=T0)
    result = zl.merge_into_history(WALLET, rows, history)
    assert result["added"] == 1
    assert result["skipped"] == 1
    assert result["total"] == 2
    # The GMGN record itself is untouched; only the extra sell leg was added.
    assert history[WALLET][0] == shared


def test_merge_is_idempotent():
    history = {}
    rows = zl.normalize_transaction(SWAP, now_ts=T0)
    first = zl.merge_into_history(WALLET, rows, history)
    second = zl.merge_into_history(WALLET, rows, history)
    assert first["added"] == 2
    assert second["added"] == 0
    assert second["total"] == 2


def test_merge_with_no_rows_deletes_nothing():
    history = {WALLET: [_gmgn_row(), _gmgn_row(transaction_hash="other")]}
    before = json.dumps(history, sort_keys=True)
    result = zl.merge_into_history(WALLET, [], history)
    assert result["added"] == 0
    assert result["total"] == 2
    assert json.dumps(history, sort_keys=True) == before


def test_merge_keeps_rows_with_no_usable_identity_out():
    result = zl.merge_into_history(WALLET, [{"side": "buy"}, "junk", None], {})
    assert result["added"] == 0
    assert result["skipped"] == 3
    assert result["total"] == 0


def test_merge_reuses_the_stored_key_for_a_differently_cased_wallet():
    """A checksummed address must not fork one wallet into two half-histories."""
    stored = whv.wallet_identity(WALLET.lower())
    history = {stored: [_gmgn_row()]}
    result = zl.merge_into_history(
        WALLET, zl.normalize_transaction(SWAP, now_ts=T0), history
    )
    assert list(history) == [stored]
    assert result["history_key"] == stored


def test_merge_requires_a_wallet_and_a_mapping():
    assert zl.merge_into_history("", [], {})["error"] == "wallet address is required"
    assert zl.merge_into_history(WALLET, [], None)["error"] == "history must be a mapping"


def test_merge_does_not_mutate_the_caller_row_list():
    rows = zl.normalize_transaction(SWAP, now_ts=T0)
    snapshot = json.dumps(rows, sort_keys=True)
    zl.merge_into_history(WALLET, rows, {})
    assert json.dumps(rows, sort_keys=True) == snapshot


def test_merged_history_still_reconstructs_across_both_providers():
    """The point of the integration: both sources feed one reconstruction."""
    gmgn_entry = _gmgn_row(trade_timestamp=T0, chain="eth", address="0xaaa",
                           symbol="AAA", side="buy", amount_usd=2000.0,
                           price_usd=5.0, transaction_hash="0xswap")
    zerion_later = zl.normalize_transaction(
        _transaction([_transfer("in", "AAA", "0xaaa", quantity=100.0, price=12.5)],
                     mined_at="2023-11-17T00:00:00+00:00", tx_hash="0xlater"),
        now_ts=T0,
    )
    history = {WALLET: [gmgn_entry]}
    zl.merge_into_history(WALLET, zerion_later, history)
    rows = whv.rows_for_wallet(history, WALLET)
    assert len(rows) == 2
    record = whv.reconstruct_asset(whv._dedupe(rows))
    assert record["entry_price"] == 5.0
    assert record["peak_multiple"] == 2.5
    assert record["target_reached"] is True


# ---------------------------------------------------------------------------
# 6b. Cross-provider event identity (Zerion vs GMGN)
#
# The defect these cover: a strict identity key includes transaction_hash, but a
# large share of stored GMGN rows carry no hash at all. A Zerion row and a
# hashless GMGN row for one on-chain event therefore produced two different keys,
# so the merge stored the event twice and every derived count was inflated.
# ---------------------------------------------------------------------------
def test_same_event_is_true_when_both_rows_share_a_transaction_hash():
    a = _gmgn_row(transaction_hash="0xsame")
    b = _gmgn_row(transaction_hash="0xsame")
    assert zl.same_event(a, b) is True
    assert zl.event_identity(a) == zl.event_identity(b)


def test_same_event_is_true_when_gmgn_has_no_transaction_hash():
    """The case that produced the duplicates.

    GMGN stores no hash, Zerion always has one, and both describe one event.
    """
    hashless = _gmgn_row(transaction_hash="")
    hashed = _gmgn_row(transaction_hash="0xfromZerion")
    assert zl.event_hash(hashless) == ""
    assert zl.event_hash(hashed) == "0xfromZerion"
    # The strict key cannot see the match ...
    assert zl.event_identity(hashless) != zl.event_identity(hashed)
    # ... which is exactly why the merge does not use it.
    assert zl.same_event(hashless, hashed) is True
    assert zl.same_event(hashed, hashless) is True


def test_same_event_is_false_for_a_different_transaction_hash():
    """Two real trades in the same second, same token, same size and price.

    When both providers know the hash, the hash decides and nothing else can
    merge them. Dropping the hash here would delete a genuine second record.
    """
    a = _gmgn_row(transaction_hash="0xfirst")
    b = _gmgn_row(transaction_hash="0xsecond")
    assert zl.same_event(a, b) is False


def test_same_event_is_false_for_different_events_of_the_same_token():
    """The hashless case must still refuse events that differ on a real field."""
    base = _gmgn_row(transaction_hash="")
    for field, other in (
        ("amount_usd", 5001.0),
        ("price_usd", 1.5),
        ("side", "sell"),
        ("trade_timestamp", T0 + 1),
        ("address", "0xdifferentasset"),
        ("chain", "eth"),
    ):
        assert zl.same_event(base, _gmgn_row(**{field: other})) is False, field
    # A row with no usable identity never matches anything.
    assert zl.same_event(base, {"side": "buy"}) is False
    assert zl.same_event(base, "junk") is False


def test_merge_does_not_duplicate_an_event_gmgn_stored_without_a_hash():
    """End to end: the historical count must not grow for a known event."""
    hashless = _gmgn_row(transaction_hash="", amount_usd=200.0, price_usd=2.0,
                         address="0xaaa", symbol="AAA", side="buy", chain="eth")
    history = {WALLET: [hashless]}
    before = len(history[WALLET])
    zerion_same_event = zl.normalize_transaction(
        _transaction([_transfer("in", "AAA", "0xaaa", quantity=100.0, price=2.0)],
                     mined_at="2023-11-14T22:13:20+00:00", tx_hash="0xonchain"),
        now_ts=T0,
    )
    result = zl.merge_into_history(WALLET, zerion_same_event, history)
    assert result["added"] == 0
    assert result["skipped"] == 1
    assert result["total"] == before
    # The GMGN record is still exactly as stored.
    assert history[WALLET] == [hashless]


def test_merge_still_adds_a_zerion_only_event_alongside_a_hashless_gmgn_row():
    history = {WALLET: [_gmgn_row(transaction_hash="", amount_usd=200.0,
                                  price_usd=2.0, address="0xaaa", symbol="AAA",
                                  side="buy", chain="eth")]}
    other_event = zl.normalize_transaction(
        _transaction([_transfer("in", "AAA", "0xaaa", quantity=100.0, price=2.0)],
                     mined_at="2023-11-15T22:13:20+00:00", tx_hash="0xnewer"),
        now_ts=T0,
    )
    result = zl.merge_into_history(WALLET, other_event, history)
    assert result["added"] == 1
    assert result["total"] == 2


def test_merge_keeps_two_different_hashless_events_that_really_differ():
    """The hashless fallback must not collapse genuinely distinct trades."""
    history = {WALLET: [_gmgn_row(transaction_hash="", amount_usd=200.0,
                                  price_usd=2.0, address="0xaaa", symbol="AAA",
                                  side="buy", chain="eth")]}
    different_size = zl.normalize_transaction(
        _transaction([_transfer("in", "AAA", "0xaaa", quantity=250.0, price=2.0)],
                     mined_at="2023-11-14T22:13:20+00:00", tx_hash="0xother"),
        now_ts=T0,
    )
    result = zl.merge_into_history(WALLET, different_size, history)
    assert result["added"] == 1
    assert result["total"] == 2


def test_repeated_ingestion_of_identical_zerion_data_never_grows_history():
    """Replaying the same Zerion fetch any number of times is a no-op."""
    history = {}
    rows = zl.normalize_rows([
        _transaction([_transfer("in", "AAA", "0xaaa", quantity=100.0, price=1.0)],
                     tx_hash="0xone"),
        _transaction([_transfer("in", "BBB", "0xbbb", quantity=50.0, price=4.0)],
                     tx_hash="0xtwo"),
    ], now_ts=T0)
    first = zl.merge_into_history(WALLET, rows, history)
    assert first["added"] == 2
    snapshot = json.dumps(history, sort_keys=True)
    for _ in range(4):
        again = zl.merge_into_history(WALLET, rows, history)
        assert again["added"] == 0
        assert again["total"] == 2
    assert json.dumps(history, sort_keys=True) == snapshot


def test_repeated_ingestion_against_a_hashless_history_is_also_a_no_op():
    """The real replay shape: a history of hashless GMGN rows plus a replayed
    Zerion fetch. The first pass may add the rows GMGN genuinely lacks; every
    pass after that must add nothing."""
    history = {WALLET: [_gmgn_row(transaction_hash="", amount_usd=200.0,
                                  price_usd=2.0, address="0xaaa", symbol="AAA",
                                  side="buy", chain="eth")]}
    rows = zl.normalize_rows([
        _transaction([_transfer("in", "AAA", "0xaaa", quantity=100.0, price=2.0)],
                     mined_at="2023-11-14T22:13:20+00:00", tx_hash="0xonchain"),
        _transaction([_transfer("in", "BBB", "0xbbb", quantity=50.0, price=4.0)],
                     mined_at="2023-11-16T00:00:00+00:00", tx_hash="0xbrandnew"),
    ], now_ts=T0)
    first = zl.merge_into_history(WALLET, rows, history)
    assert first["added"] == 1        # only the event GMGN did not have
    assert first["skipped"] == 1      # the shared event was recognised
    assert first["total"] == 2
    for _ in range(3):
        assert zl.merge_into_history(WALLET, rows, history)["added"] == 0
    assert len(history[WALLET]) == 2


def test_stored_gmgn_history_row_count_is_unchanged_by_a_replay():
    """A replay of Zerion data must not inflate the committed GMGN dataset.

    Guards the actual regression this change exists to prevent, using the real
    artifact rather than a fixture.
    """
    with open("gmgn_wallet_history.json") as fh:
        stored = json.load(fh)
    wallet = next(k for k, v in stored.items() if isinstance(v, list) and v)
    sample = stored[wallet]
    before_wallets = len(stored)
    before_rows = sum(len(v) for v in stored.values() if isinstance(v, list))
    before_bucket = json.dumps(sample, sort_keys=True)

    # Replay each stored row back through the merge as if it came from Zerion.
    # Whatever the hash situation, replaying what is already stored cannot add.
    history = {wallet: json.loads(before_bucket)}
    for _ in range(3):
        summary = zl.merge_into_history(wallet, json.loads(before_bucket), history)
        assert summary["added"] == 0, "replaying stored rows must add nothing"
        assert summary["total"] == len(sample)

    assert len(stored) == before_wallets
    assert sum(len(v) for v in stored.values() if isinstance(v, list)) == before_rows
    assert json.dumps(stored[wallet], sort_keys=True) == before_bucket
    # The file on disk is never touched by any of this.
    with open("gmgn_wallet_history.json") as fh:
        assert json.dumps(json.load(fh)[wallet], sort_keys=True) == before_bucket


def test_a_zerion_row_with_no_hash_does_not_match_a_hashed_gmgn_row_of_another_event():
    """Both sides hashed but different, and one side hashless, are distinct."""
    hashed = _gmgn_row(transaction_hash="0xknown", amount_usd=200.0, price_usd=2.0,
                       address="0xaaa", symbol="AAA", side="buy", chain="eth")
    # Same fields, different hash on the Zerion side: a genuinely later trade.
    different = dict(hashed, transaction_hash="0xdifferent")
    assert zl.same_event(hashed, different) is False


# ---------------------------------------------------------------------------
# 7. Archive
# ---------------------------------------------------------------------------
def test_archive_records_the_fetch_without_any_credential(tmp_path=None):
    import tempfile
    from pathlib import Path

    directory = Path(tempfile.mkdtemp()) if tmp_path is None else tmp_path
    payload = {
        "wallet": WALLET, "endpoint": zl.TRANSACTIONS_PATH, "fetched_at": T0,
        "coverage": {"from": T0, "to": T0 + DAY}, "ok": True, "error": None,
        "truncated": False, "pages": 2, "transactions": 1,
        "rows": zl.normalize_transaction(SWAP, now_ts=T0),
    }
    zl.ZERION_API_KEY = "super-secret-key"
    try:
        path = zl.write_archive(payload, directory)
        assert path.is_file()
        text = path.read_text()
        assert "super-secret-key" not in text
        stored = json.loads(text)
        assert stored["source"] == "zerion"
        assert stored["summary"]["normalized_rows"] == 2
        assert stored["coverage"] == {"from": T0, "to": T0 + DAY}
        assert len(stored["records"]) == 2
    finally:
        zl.ZERION_API_KEY = ""


def test_archive_keeps_the_undecoded_provider_payload(tmp_path=None):
    """A normalizer change must be re-derivable without spending quota again."""
    import tempfile
    from pathlib import Path

    directory = Path(tempfile.mkdtemp()) if tmp_path is None else tmp_path
    payload = {
        "wallet": WALLET, "endpoint": zl.TRANSACTIONS_PATH, "fetched_at": T0,
        "coverage": None, "ok": True, "error": None, "truncated": False,
        "pages": 1, "transactions": 1,
        "rows": zl.normalize_transaction(SWAP, now_ts=T0),
        "raw_transactions": [SWAP],
    }
    path = zl.write_archive(payload, directory)
    stored = json.loads(path.read_text())
    assert stored["raw_transactions"] == [SWAP]
    assert stored["summary"]["raw_transactions"] == 1
    assert stored["schema_version"] == 2
    # still no credential, even with the raw payload attached
    zl.ZERION_API_KEY = "super-secret-key"
    try:
        assert "super-secret-key" not in zl.write_archive(payload, directory).read_text()
    finally:
        zl.ZERION_API_KEY = ""


def test_a_second_fetch_of_one_wallet_does_not_overwrite_the_first(tmp_path=None):
    import tempfile
    from pathlib import Path

    directory = Path(tempfile.mkdtemp()) if tmp_path is None else tmp_path
    def fetch(at):
        return {"wallet": WALLET, "endpoint": zl.TRANSACTIONS_PATH, "fetched_at": at,
                "coverage": None, "ok": True, "error": None, "truncated": False,
                "pages": 1, "transactions": 0, "rows": [], "raw_transactions": []}
    first = zl.write_archive(fetch(T0), directory)
    second = zl.write_archive(fetch(T0 + 3600), directory)
    assert first != second
    assert first.read_text(), "the earlier snapshot was replaced"
    assert json.loads(first.read_text())["fetched_at"] == T0
    assert len(list(Path(directory).glob("*.json"))) == 2


def test_the_fetch_result_carries_its_raw_transactions():
    """The paginator collects them, so the archive can keep them."""
    transport = FakeTransport(FakeResponse(200, _page([SWAP])))
    result = zl.fetch_wallet_transactions(WALLET, api_key="k",
                                          http_get=transport, sleep=no_sleep)
    assert result["ok"] is True, result.get("error")
    assert result["raw_transactions"] == [SWAP]
    assert result["rows"]


def test_module_does_not_enable_trading():
    """Belt and braces: this module ships no order path of any kind."""
    import inspect

    source = inspect.getsource(zl)
    for forbidden in ("create_order", "place_order", "submit_order",
                      "ccxt", "orders_enabled", "POST /v1/orders"):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# The live smoke-test script, driven offline
#
# The workflow that talks to the real API must never be the first time this
# code runs. Every path below is exercised with a fake transport, so a change
# that would break the probe is caught by the offline suite.
# ---------------------------------------------------------------------------
def test_smoke_without_a_key_fails_without_sending_anything():
    transport = FakeTransport()
    report = zst.run_smoke(WALLET, api_key="", http_get=transport, sleep=no_sleep)
    assert report["ok"] is False
    assert report["error"] == "missing_api_key"
    assert transport.calls == []


def test_smoke_probes_one_small_page():
    transport = FakeTransport(FakeResponse(200, _page([SWAP])))
    report = zst.run_smoke(WALLET, api_key="k", http_get=transport, sleep=no_sleep,
                           now_ts=T0)
    assert report["ok"] is True
    assert report["http_status"] == 200
    assert len(transport.calls) == 1
    params = transport.calls[0]["params"]
    assert params["page[size]"] == "2"
    assert params["filter[operation_types]"] == "trade"
    assert transport.calls[0]["auth"] == ("k", "")


def test_smoke_passes_on_a_200_with_no_records():
    """An empty window is a working request, not a failure.

    Reporting this as a failure would make the probe useless for a quiet
    wallet, and would invite someone to "fix" it by widening the request.
    """
    transport = FakeTransport(FakeResponse(200, _page([])))
    report = zst.run_smoke(WALLET, api_key="k", http_get=transport, sleep=no_sleep,
                           now_ts=T0)
    assert report["ok"] is True
    assert report["normalized_rows"] == 0
    assert all(c["pass"] for c in report["checks"])


def test_smoke_follows_one_cursor_and_proves_it_advanced():
    other = _transaction([
        _transfer("in", "CCC", "0xccc", quantity=5.0, price=3.0),
    ], tx_hash="0xdef", mined_at="2023-11-15T00:00:00+00:00")
    transport = FakeTransport(
        FakeResponse(200, _page([SWAP, other], next_url=NEXT_URL)),
        FakeResponse(200, _page([_transaction([
            _transfer("out", "DDD", "0xddd", quantity=1.0, price=4.0),
        ], tx_hash="0xghi", mined_at="2023-11-16T00:00:00+00:00")])),
    )
    report = zst.run_smoke(WALLET, api_key="k", http_get=transport, sleep=no_sleep,
                           now_ts=T0)
    assert report["ok"] is True
    assert len(transport.calls) == 2
    # The second call must reuse the provider's own cursor URL with no params.
    assert transport.calls[1]["url"] == NEXT_URL
    assert transport.calls[1]["params"] is None
    assert [p["transactions"] for p in report["page_trace"]] == [2, 1]
    assert all(c["pass"] for c in report["checks"])


def test_smoke_cannot_paginate_forever():
    """max_pages caps the probe so a busy wallet cannot become a backfill."""
    # Distinct cursors each time: an identical repeated cursor is a provider
    # loop, which the provider layer already detects and stops on its own.
    transport = FakeTransport(*[
        FakeResponse(200, _page([SWAP], next_url=_next_url(f"CURSOR_{i}")))
        for i in range(10)
    ])
    report = zst.run_smoke(WALLET, api_key="k", http_get=transport, sleep=no_sleep,
                           now_ts=T0)
    assert len(transport.calls) == zst.MAX_PAGES == 2
    assert report["truncated"] is True
    assert report["error"] == "max_pages_reached"
    assert report["transactions"] <= 4


def test_smoke_stops_on_a_repeating_cursor_instead_of_looping():
    transport = FakeTransport(*[
        FakeResponse(200, _page([SWAP], next_url=NEXT_URL)) for _ in range(10)
    ])
    report = zst.run_smoke(WALLET, api_key="k", http_get=transport, sleep=no_sleep,
                           now_ts=T0)
    assert report["error"] == "cursor_loop_detected"
    assert len(transport.calls) == 2


def test_smoke_reports_a_rejected_credential_without_retrying():
    transport = FakeTransport(*[
        FakeResponse(401, {"errors": [{"detail": "The API key is invalid"}]})
        for _ in range(3)
    ])
    report = zst.run_smoke(WALLET, api_key="a-realistic-looking-key-0001",
                           http_get=transport, sleep=no_sleep, now_ts=T0)
    assert report["ok"] is False
    assert report["http_status"] == 401
    assert len(transport.calls) == 1
    assert report["reason"] == "CREDENTIAL_REJECTED"
    # Provider prose must survive verbatim; it is the diagnosis the operator
    # needs. Only the credential itself may be replaced.
    assert "The API key is invalid" in report["error"]


def test_smoke_redaction_does_not_corrupt_ordinary_error_text():
    """A short or absent key must not shred the words in the report."""
    for weak in ("k", "abc", ""):
        assert zst._secret_variants(weak) == ()
        kept = zst._redact("The API key is invalid: basic trade data", zst._secret_variants(weak))
        assert kept == "The API key is invalid: basic trade data"
    # An assigned credential is still removed, with or without a short value.
    assert "<redacted>" in zst._redact("Authorization: Basic ZZZZZZZZ",
                                       zst._secret_variants("long-enough-key"))


def test_smoke_reports_an_untrackable_wallet_distinctly_from_a_bad_key():
    transport = FakeTransport(FakeResponse(
        400, {"errors": [{"detail": "The address is not tracked by Zerion"}]}))
    report = zst.run_smoke(WALLET, api_key="k", http_get=transport, sleep=no_sleep,
                           now_ts=T0)
    assert report["ok"] is False
    assert report["http_status"] == 400
    assert report["reason"] == "REQUEST_NOT_SERVABLE"
    assert report["reason"] != "CREDENTIAL_REJECTED"
    assert "not tracked" in report["error"]


def test_smoke_output_never_contains_the_credential():
    """A provider that echoes the key back must still not leak it to the log."""
    secret = "ZERION_TEST_KEY_DO_NOT_PRINT_1234567890"
    echoed = FakeResponse(401, {"errors": [{
        "detail": f"invalid key: {secret} / "
                  f"{base64.b64encode((secret + ':').encode()).decode()}"
    }]})
    transport = FakeTransport(echoed)
    report = zst.run_smoke(WALLET, api_key=secret, http_get=transport,
                           sleep=no_sleep, now_ts=T0)
    rendered = zst.render(report)
    for form in (secret, base64.b64encode((secret + ":").encode()).decode()):
        assert form not in json.dumps(report)
        assert form not in rendered
    assert "<redacted>" in rendered
    assert secret not in rendered


def test_smoke_render_is_readable_and_states_the_outcome():
    transport = FakeTransport(FakeResponse(200, _page([SWAP])))
    report = zst.run_smoke(WALLET, api_key="k", http_get=transport, sleep=no_sleep,
                           now_ts=T0)
    text = zst.render(report)
    for expected in ("ZERION LIVE API SMOKE TEST", "HTTP status", "page 1:",
                     "RESULT: PASS", WALLET):
        assert expected in text


def test_smoke_default_wallet_is_real_and_not_the_exchange_hot_wallet():
    """The Binance hot wallet from the Nansen workflow is the wrong target.

    Zerion declines to track high-volume exchange addresses, so probing it would
    return 400 and prove nothing about the credential.
    """
    assert zst.DEFAULT_WALLET == WALLET
    assert zst.DEFAULT_WALLET != "0x28C6c06298d514Db089934071355E5743bF21d60"
    with open("gmgn_wallet_history.json") as fh:
        stored = json.load(fh)
    assert WALLET.lower() in json.dumps(stored).lower()


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    started = time.time()
    for test in TESTS:
        test()
        print(f"  ok  {test.__name__}")
    print(
        f"Zerion provider tests: PASS ({len(TESTS)} tests) "
        f"in {time.time() - started:.2f}s"
    )
