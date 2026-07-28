from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from futures_backtest import Matcher
from futures_backtest.config import ExecutionConfig
from futures_backtest.types import Order
from tests.support import bar_rows, make_parts, trading_days, two_contract_tables

TIMESTAMP = datetime(2024, 4, 2, 15, 0)


def _order(day, side="buy", offset="open", lots=1, price=3500.0, symbol="RB2405"):
    return Order(
        trading_day=day,
        timestamp=TIMESTAMP,
        underlying="RB",
        symbol=symbol,
        side=side,
        offset=offset,
        lots=lots,
        reference_price=price,
        reason="signal",
    )


def test_slippage_is_charged_against_the_trader_and_snapped_to_the_tick():
    days = trading_days(4)
    dataset, account, _, matcher = make_parts(two_contract_tables(days, days[2]))
    bar = dataset.last_bar_of_day("RB2405", days[0])

    buy = matcher.execute(_order(days[0], "buy", price=3500.0), bar, account)[0]
    assert buy.price == pytest.approx(3501.0)
    assert buy.slippage_ticks == pytest.approx(1.0)

    sell = matcher.execute(_order(days[0], "sell", price=3500.0), bar, account)[0]
    assert sell.price == pytest.approx(3499.0)


def test_a_price_off_the_tick_grid_is_snapped_the_expensive_way():
    days = trading_days(4)
    dataset, account, _, _ = make_parts(two_contract_tables(days, days[2]))
    matcher = Matcher(dataset, ExecutionConfig(slippage_ticks=0))
    bar = dataset.last_bar_of_day("RB2405", days[0])

    assert matcher.execute(_order(days[0], "buy", price=3500.4), bar, account)[0].price == 3501
    assert matcher.execute(_order(days[0], "sell", price=3500.4), bar, account)[0].price == 3500


def test_a_buy_at_the_upper_limit_is_rejected():
    days = trading_days(4)
    tables = two_contract_tables(days, days[2])
    tables["bars"] = pd.DataFrame(
        bar_rows(
            "RB2405", "RB", days, [3500 + 10 * i for i in range(len(days))], limits=(3500, 3200)
        )
        + bar_rows("RB2410", "RB", days, [3560 + 10 * i for i in range(len(days))])
    )
    dataset, account, _, matcher = make_parts(tables)
    bar = dataset.last_bar_of_day("RB2405", days[0])

    rejected = matcher.execute(_order(days[0], "buy", price=3500.0), bar, account)[0]
    assert rejected.status == "rejected"
    assert rejected.reject_reason == "limit_up"
    # Selling into a limit-up bar is still allowed.
    sold = matcher.execute(_order(days[0], "sell", price=3490.0), bar, account)[0]
    assert sold.status == "filled"


def test_a_sell_at_the_lower_limit_is_rejected():
    days = trading_days(4)
    tables = two_contract_tables(days, days[2])
    tables["bars"] = pd.DataFrame(
        bar_rows(
            "RB2405", "RB", days, [3500 + 10 * i for i in range(len(days))], limits=(3800, 3500)
        )
        + bar_rows("RB2410", "RB", days, [3560 + 10 * i for i in range(len(days))])
    )
    dataset, account, _, matcher = make_parts(tables)
    bar = dataset.last_bar_of_day("RB2405", days[0])
    rejected = matcher.execute(_order(days[0], "sell", price=3500.0), bar, account)[0]
    assert rejected.reject_reason == "limit_down"


def test_slippage_cannot_push_a_fill_past_the_price_limit():
    days = trading_days(4)
    tables = two_contract_tables(days, days[2])
    tables["bars"] = pd.DataFrame(
        bar_rows(
            "RB2405", "RB", days, [3500 + 10 * i for i in range(len(days))], limits=(3499.5, 3200)
        )
        + bar_rows("RB2410", "RB", days, [3560 + 10 * i for i in range(len(days))])
    )
    dataset, account, _, matcher = make_parts(tables)
    bar = dataset.last_bar_of_day("RB2405", days[0])
    # One tick of slippage off 3499 would reach 3500, past the 3499.5 ceiling.
    fill = matcher.execute(_order(days[0], "buy", price=3499.0), bar, account)[0]
    assert fill.price == pytest.approx(3499.5)


def test_a_bar_without_volume_cannot_fill():
    days = trading_days(4)
    tables = two_contract_tables(days, days[2])
    tables["bars"] = pd.DataFrame(
        bar_rows("RB2405", "RB", days, [3500 + 10 * i for i in range(len(days))], volume=0)
        + bar_rows("RB2410", "RB", days, [3560 + 10 * i for i in range(len(days))])
    )
    dataset, account, _, matcher = make_parts(tables)
    bar = dataset.last_bar_of_day("RB2405", days[0])
    assert matcher.execute(_order(days[0], "buy"), bar, account)[0].reject_reason == "no_volume"


def test_insufficient_margin_is_rejected_by_default():
    days = trading_days(4)
    # 5 lots need 5 * 10 * 3501 * 0.1 = 17505 of margin.
    dataset, account, _, matcher = make_parts(
        two_contract_tables(days, days[2]), initial_cash=10000
    )
    bar = dataset.last_bar_of_day("RB2405", days[0])
    fill = matcher.execute(_order(days[0], "buy", lots=5, price=3500.0), bar, account)[0]
    assert fill.reject_reason == "insufficient_margin"
    assert account.net_lots("RB") == 0


def test_scale_mode_trims_the_order_to_what_margin_allows():
    days = trading_days(4)
    dataset, account, _, _ = make_parts(two_contract_tables(days, days[2]), initial_cash=10000)
    matcher = Matcher(dataset, ExecutionConfig(on_margin_short="scale"))
    bar = dataset.last_bar_of_day("RB2405", days[0])
    fill = matcher.execute(_order(days[0], "buy", lots=5, price=3500.0), bar, account)[0]
    assert fill.status == "partial"
    assert fill.filled_lots == 2  # floor(10000 / 3501)
    assert account.net_lots("RB") == 2


def test_closing_without_a_position_is_rejected():
    days = trading_days(4)
    dataset, account, _, matcher = make_parts(two_contract_tables(days, days[2]))
    bar = dataset.last_bar_of_day("RB2405", days[0])
    fill = matcher.execute(_order(days[0], "sell", offset="close"), bar, account)[0]
    assert fill.reject_reason == "insufficient_position"


def test_a_close_splits_into_yesterday_and_today_at_their_own_fee_rates():
    days = trading_days(4)
    dataset, account, _, matcher = make_parts(two_contract_tables(days, days[2]))
    bar = dataset.last_bar_of_day("RB2405", days[0])
    matcher.execute(_order(days[0], "buy", lots=2, price=3500.0), bar, account)
    account.roll_today_into_yesterday()
    matcher.execute(_order(days[0], "buy", lots=1, price=3500.0), bar, account)

    closing = _order(days[0], "sell", offset="close", lots=3, price=3500.0)
    fills = matcher.execute(closing, bar, account)
    assert [(fill.offset, fill.filled_lots) for fill in fills] == [("close", 2), ("close_today", 1)]
    # close_today is priced at 0.001 here versus 0.0001 for close.
    per_lot_close = fills[0].commission / 2
    per_lot_close_today = fills[1].commission
    assert per_lot_close_today == pytest.approx(per_lot_close * 10)
    assert all(fill.status == "filled" for fill in fills)


def test_zero_lot_orders_are_rejected_rather_than_silently_dropped():
    days = trading_days(4)
    dataset, account, _, matcher = make_parts(two_contract_tables(days, days[2]))
    bar = dataset.last_bar_of_day("RB2405", days[0])
    rejected = matcher.execute(_order(days[0], lots=0), bar, account)[0]
    assert rejected.reject_reason == "non_positive_lots"
