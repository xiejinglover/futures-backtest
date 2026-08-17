from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from futures_backtest import RoutingConfig, TargetPosition
from futures_backtest.types import BacktestDataError
from tests.support import make_parts, trading_days, two_contract_tables

TIMESTAMP = datetime(2024, 4, 10, 15, 0)


def _prices(dataset, day):
    return {
        symbol: dataset.last_bar_of_day(symbol, day).open
        for symbol in dataset.contracts
        if dataset.last_bar_of_day(symbol, day) is not None
    }


def test_dominant_lag_of_one_delays_the_switch_by_a_day():
    days = trading_days(6)
    switch = days[3]
    dataset, _, router, _ = make_parts(two_contract_tables(days, switch))

    assert router.trading_symbol("RB", days[2]) == "RB2405"
    # The map already says RB2410 on the switch day, but a desk deciding that day
    # only knows the previous day's confirmation.
    assert dataset.dominant_symbol("RB", switch) == "RB2410"
    assert router.trading_symbol("RB", switch) == "RB2405"
    assert router.trading_symbol("RB", days[4]) == "RB2410"

    assert not router.is_roll_day("RB", switch)
    assert router.is_roll_day("RB", days[4])


def test_lookahead_dominant_trades_the_same_day_decision():
    days = trading_days(6)
    switch = days[3]
    tables = two_contract_tables(days, switch)
    dataset, _, _, _ = make_parts(tables)
    from futures_backtest import Router

    router = Router(dataset, RoutingConfig(dominant_lag=0, lookahead_dominant=True))
    assert router.trading_symbol("RB", switch) == "RB2410"


def test_lookahead_must_be_declared_explicitly():
    with pytest.raises(ValueError, match="lookahead_dominant=true"):
        RoutingConfig(dominant_lag=0)
    with pytest.raises(ValueError, match="requires routing.dominant_lag=0"):
        RoutingConfig(dominant_lag=1, lookahead_dominant=True)


def test_signal_orders_open_on_the_routed_contract():
    days = trading_days(6)
    dataset, account, router, _ = make_parts(two_contract_tables(days, days[3]))
    prices = _prices(dataset, days[1])
    orders = router.signal_orders(TargetPosition("RB", 3), days[1], TIMESTAMP, account, prices)
    assert [(order.symbol, order.side, order.offset, order.lots) for order in orders] == [
        ("RB2405", "buy", "open", 3)
    ]


def test_signal_orders_preserve_stop_limit_and_gtc_fields():
    days = trading_days(6)
    dataset, account, router, _ = make_parts(two_contract_tables(days, days[3]))
    orders = router.signal_orders(
        TargetPosition(
            "RB",
            3,
            limit_price=3510,
            stop_price=3500,
            time_in_force="GTC",
        ),
        days[1],
        TIMESTAMP,
        account,
        _prices(dataset, days[1]),
    )
    assert len(orders) == 1
    assert orders[0].limit_price == 3510
    assert orders[0].stop_price == 3500
    assert orders[0].time_in_force == "GTC"


def test_a_reversal_closes_before_it_opens():
    days = trading_days(6)
    dataset, account, router, matcher = make_parts(two_contract_tables(days, days[3]))
    day = days[1]
    prices = _prices(dataset, day)
    bars = {symbol: dataset.last_bar_of_day(symbol, day) for symbol in dataset.contracts}

    for order in router.signal_orders(TargetPosition("RB", 2), day, TIMESTAMP, account, prices):
        matcher.execute(order, bars[order.symbol], account)
    assert account.net_lots("RB") == 2

    orders = router.signal_orders(TargetPosition("RB", -1), day, TIMESTAMP, account, prices)
    assert [(order.side, order.offset, order.lots) for order in orders] == [
        ("sell", "close", 2),
        ("sell", "open", 1),
    ]


def test_roll_moves_the_whole_net_position_to_the_new_contract():
    days = trading_days(6)
    dataset, account, router, matcher = make_parts(two_contract_tables(days, days[3]))
    entry_day = days[1]
    prices = _prices(dataset, entry_day)
    bars = {symbol: dataset.last_bar_of_day(symbol, entry_day) for symbol in dataset.contracts}
    for order in router.signal_orders(
        TargetPosition("RB", 2), entry_day, TIMESTAMP, account, prices
    ):
        matcher.execute(order, bars[order.symbol], account)

    roll_day = days[4]
    roll_prices = _prices(dataset, roll_day)
    orders = router.roll_orders("RB", roll_day, TIMESTAMP, account, roll_prices)
    assert [(order.symbol, order.side, order.offset, order.reason) for order in orders] == [
        ("RB2405", "sell", "close", "roll_out"),
        ("RB2410", "buy", "open", "roll_in"),
    ]

    roll_bars = {symbol: dataset.last_bar_of_day(symbol, roll_day) for symbol in dataset.contracts}
    for order in orders:
        matcher.execute(order, roll_bars[order.symbol], account)
    assert account.symbol_net_lots("RB2405") == 0
    assert account.symbol_net_lots("RB2410") == 2
    assert account.net_lots("RB") == 2


def test_roll_is_a_no_op_without_a_position():
    days = trading_days(6)
    dataset, account, router, _ = make_parts(two_contract_tables(days, days[3]))
    assert router.roll_orders("RB", days[4], TIMESTAMP, account, _prices(dataset, days[4])) == []


def test_a_target_change_unwinds_the_retired_contract_first():
    days = trading_days(6)
    dataset, account, router, matcher = make_parts(two_contract_tables(days, days[3]))
    entry_day = days[1]
    bars = {symbol: dataset.last_bar_of_day(symbol, entry_day) for symbol in dataset.contracts}
    for order in router.signal_orders(
        TargetPosition("RB", 2), entry_day, TIMESTAMP, account, _prices(dataset, entry_day)
    ):
        matcher.execute(order, bars[order.symbol], account)

    # Still holding RB2405 while the router has moved on to RB2410.
    roll_day = days[4]
    orders = router.signal_orders(
        TargetPosition("RB", 0), roll_day, TIMESTAMP, account, _prices(dataset, roll_day)
    )
    assert [(order.symbol, order.offset, order.lots) for order in orders] == [
        ("RB2405", "close", 2)
    ]


def test_dominant_history_before_the_window_is_used_for_the_first_day():
    days = trading_days(6)
    tables = two_contract_tables(days, days[3])
    earlier = tables["dominant_map"].head(1).copy()
    earlier["trading_day"] = days[0] - timedelta(days=1)
    earlier["dominant_symbol"] = "RB2410"
    tables["dominant_map"] = pd.concat([earlier, tables["dominant_map"]], ignore_index=True)

    _, _, router, _ = make_parts(tables)
    assert router.trading_symbol("RB", days[0]) == "RB2410"
    assert router.warmup_fallbacks == set()


def test_a_window_without_earlier_history_flags_the_first_day():
    days = trading_days(6)
    _, _, router, _ = make_parts(two_contract_tables(days, days[3]))
    assert router.trading_symbol("RB", days[0]) == "RB2405"
    assert router.warmup_fallbacks == {("RB", days[0])}
    # Only the first day needs the compromise.
    router.trading_symbol("RB", days[1])
    assert router.warmup_fallbacks == {("RB", days[0])}


def test_missing_dominant_coverage_is_reported_with_the_lag():
    days = trading_days(6)
    tables = two_contract_tables(days, days[3])
    tables["dominant_map"] = tables["dominant_map"][
        tables["dominant_map"]["trading_day"] >= days[2]
    ]
    _, _, router, _ = make_parts(tables)
    with pytest.raises(BacktestDataError, match="dominant_lag=1"):
        router.trading_symbol("RB", days[1])


def test_expiry_buffer_flattens_a_contract_near_its_last_trading_day():
    days = trading_days(6)
    tables = two_contract_tables(days, days[3])
    tables["contracts"].loc[0, "expire_date"] = days[4]
    dataset, account, router, matcher = make_parts(tables)
    from futures_backtest import Router

    router = Router(dataset, RoutingConfig(force_close_before_expiry_days=2))
    entry_day = days[1]
    bars = {symbol: dataset.last_bar_of_day(symbol, entry_day) for symbol in dataset.contracts}
    for order in router.signal_orders(
        TargetPosition("RB", 2), entry_day, TIMESTAMP, account, _prices(dataset, entry_day)
    ):
        matcher.execute(order, bars[order.symbol], account)

    orders = router.expiry_orders("RB", days[3], TIMESTAMP, account, _prices(dataset, days[3]))
    assert [(order.symbol, order.offset, order.reason) for order in orders] == [
        ("RB2405", "close", "expiry")
    ]
