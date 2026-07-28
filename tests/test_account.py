from __future__ import annotations

import pytest

from futures_backtest.types import BacktestDataError
from tests.support import make_parts, trading_days, two_contract_tables


def test_opening_costs_margin_not_notional():
    days = trading_days(4)
    dataset, account, _, _ = make_parts(two_contract_tables(days, days[2]), initial_cash=500000)
    account.apply_open("RB2405", "long", 2, 3500, commission=7.0)

    # A cash equity account would have paid 2 * 10 * 3500; a futures account posts margin.
    assert account.cash == pytest.approx(500000 - 7.0)
    assert account.margin_used(days[0]) == pytest.approx(2 * 10 * 3500 * 0.1)
    assert account.available(days[0]) == pytest.approx(500000 - 7.0 - 7000)


def test_short_margin_rate_applies_to_the_short_side():
    days = trading_days(4)
    dataset, account, _, _ = make_parts(two_contract_tables(days, days[2]))
    account.apply_open("RB2405", "short", 1, 3500, commission=0.0)
    assert account.margin_used(days[0]) == pytest.approx(1 * 10 * 3500 * 0.12)


def test_long_and_short_sides_are_tracked_separately():
    days = trading_days(4)
    _, account, _, _ = make_parts(two_contract_tables(days, days[2]))
    account.apply_open("RB2405", "long", 2, 3500, 0.0)
    account.apply_open("RB2405", "short", 1, 3520, 0.0)
    assert account.position("RB2405", "long").lots == 2
    assert account.position("RB2405", "short").lots == 1
    assert account.net_lots("RB") == 1


def test_closing_a_long_realizes_the_move_times_the_multiplier():
    days = trading_days(4)
    _, account, _, _ = make_parts(two_contract_tables(days, days[2]))
    account.apply_open("RB2405", "long", 2, 3500, 0.0)
    pnl = account.apply_close("RB2405", "long", 2, 3510, commission=1.0)
    assert pnl == pytest.approx(10 * 2 * 10)
    assert account.realized_pnl == pytest.approx(200)
    assert account.cash == pytest.approx(500000 + 200 - 1.0)


def test_closing_a_short_realizes_the_inverse_move():
    days = trading_days(4)
    _, account, _, _ = make_parts(two_contract_tables(days, days[2]))
    account.apply_open("RB2405", "short", 1, 3500, 0.0)
    assert account.apply_close("RB2405", "short", 1, 3490, 0.0) == pytest.approx(100)


def test_closing_more_than_held_is_an_error():
    days = trading_days(4)
    _, account, _, _ = make_parts(two_contract_tables(days, days[2]))
    account.apply_open("RB2405", "long", 1, 3500, 0.0)
    with pytest.raises(BacktestDataError, match="only 1 held"):
        account.apply_close("RB2405", "long", 2, 3500, 0.0)


def test_today_lots_become_yesterday_lots_on_the_next_day():
    days = trading_days(4)
    _, account, _, _ = make_parts(two_contract_tables(days, days[2]))
    account.apply_open("RB2405", "long", 3, 3500, 0.0)
    assert account.close_lots_split("RB2405", "long", 3) == (0, 3)

    account.roll_today_into_yesterday()
    assert account.close_lots_split("RB2405", "long", 3) == (3, 0)

    account.apply_open("RB2405", "long", 2, 3510, 0.0)
    assert account.close_lots_split("RB2405", "long", 4) == (3, 1)


def test_closing_today_lots_shrinks_the_today_bucket():
    days = trading_days(4)
    _, account, _, _ = make_parts(two_contract_tables(days, days[2]))
    account.apply_open("RB2405", "long", 2, 3500, 0.0)
    account.roll_today_into_yesterday()
    account.apply_open("RB2405", "long", 2, 3510, 0.0)

    account.apply_close("RB2405", "long", 2, 3520, 0.0, from_today=True)
    position = account.position("RB2405", "long")
    assert (position.lots, position.today_lots, position.yesterday_lots) == (2, 0, 2)


def test_closing_more_today_lots_than_were_opened_today_is_an_error():
    days = trading_days(4)
    _, account, _, _ = make_parts(two_contract_tables(days, days[2]))
    account.apply_open("RB2405", "long", 2, 3500, 0.0)
    account.roll_today_into_yesterday()
    with pytest.raises(BacktestDataError, match="opened today"):
        account.apply_close("RB2405", "long", 1, 3500, 0.0, from_today=True)


def test_settlement_books_the_variation_into_cash_and_resets_the_basis():
    days = trading_days(4)
    dataset, account, _, _ = make_parts(two_contract_tables(days, days[2]))
    day = days[0]
    settle = dataset.settle_price("RB2405", day)
    account.apply_open("RB2405", "long", 2, settle - 10, 0.0)

    outcome = account.settle(day)
    assert outcome["settlement_variation"] == pytest.approx(10 * 2 * 10)
    assert account.cash == pytest.approx(500000 + 200)
    # After settlement the basis is the settlement price, so nothing is unrealized.
    assert account.position("RB2405", "long").average_price == pytest.approx(settle)
    assert account.unrealized_pnl() == pytest.approx(0.0)
    assert account.equity() == pytest.approx(500200)


def test_settlement_of_a_short_moves_the_other_way():
    days = trading_days(4)
    dataset, account, _, _ = make_parts(two_contract_tables(days, days[2]))
    day = days[0]
    settle = dataset.settle_price("RB2405", day)
    account.apply_open("RB2405", "short", 1, settle - 10, 0.0)
    assert account.settle(day)["settlement_variation"] == pytest.approx(-100)


def test_snapshot_reports_only_non_zero_underlyings():
    days = trading_days(4)
    _, account, _, _ = make_parts(two_contract_tables(days, days[2]))
    account.apply_open("RB2405", "long", 2, 3500, 0.0)
    account.apply_close("RB2405", "long", 2, 3500, 0.0)
    assert account.snapshot(days[0]).net_lots == {}
