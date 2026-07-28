from __future__ import annotations

from datetime import date

from .dataset import MarketDataset
from .types import AccountSnapshot, BacktestDataError, Position


class Account:
    """Contract-level futures book: cash, margin, and settlement mark-to-market.

    Unlike a cash equity account, buying does not spend the notional. Cash only
    moves for commissions, realized PnL, and the daily settlement variation; the
    notional shows up as margin that must stay covered by equity.
    """

    def __init__(self, dataset: MarketDataset, initial_cash: float, default_margin_rate: float):
        self.dataset = dataset
        self.cash = float(initial_cash)
        self.initial_cash = float(initial_cash)
        self.default_margin_rate = float(default_margin_rate)
        self.positions: dict[tuple[str, str], Position] = {}
        self.realized_pnl = 0.0
        self.total_commission = 0.0

    # -- position helpers -------------------------------------------------

    def position(self, symbol: str, direction: str) -> Position:
        key = (symbol, direction)
        existing = self.positions.get(key)
        if existing is None:
            info = self.dataset.contracts[symbol]
            existing = Position(symbol=symbol, underlying=info.underlying, direction=direction)
            self.positions[key] = existing
        return existing

    def open_positions(self) -> list[Position]:
        return [item for item in self.positions.values() if item.lots > 0]

    def net_lots(self, underlying: str) -> int:
        return sum(
            item.signed_lots()
            for item in self.positions.values()
            if item.underlying == underlying and item.lots > 0
        )

    def symbol_net_lots(self, symbol: str) -> int:
        return sum(
            item.signed_lots()
            for key, item in self.positions.items()
            if key[0] == symbol and item.lots > 0
        )

    def net_lots_by_underlying(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for item in self.positions.values():
            if item.lots:
                result[item.underlying] = result.get(item.underlying, 0) + item.signed_lots()
        return {key: value for key, value in result.items() if value}

    # -- trading ----------------------------------------------------------

    def apply_open(
        self, symbol: str, direction: str, lots: int, price: float, commission: float
    ) -> None:
        position = self.position(symbol, direction)
        total = position.lots + lots
        position.average_price = (position.average_price * position.lots + price * lots) / total
        position.lots = total
        position.today_lots += lots
        position.last_price = price
        self.cash -= commission
        self.total_commission += commission

    def apply_close(
        self,
        symbol: str,
        direction: str,
        lots: int,
        price: float,
        commission: float,
        from_today: bool = False,
    ) -> float:
        """Close ``lots`` of an existing side and return the realized PnL.

        ``from_today`` says which bucket shrinks, because the exchange prices
        close-today separately from close-yesterday.
        """
        position = self.position(symbol, direction)
        if lots > position.lots:
            raise BacktestDataError(
                f"cannot close {lots} lots of {symbol} {direction}: only {position.lots} held"
            )
        if from_today and lots > position.today_lots:
            raise BacktestDataError(
                f"cannot close {lots} lots of {symbol} {direction} opened today: "
                f"only {position.today_lots} were"
            )
        multiplier = self.dataset.contracts[symbol].multiplier
        sign = 1.0 if direction == "long" else -1.0
        pnl = sign * (price - position.average_price) * lots * multiplier
        position.lots -= lots
        if from_today:
            position.today_lots -= lots
        else:
            position.today_lots = min(position.today_lots, position.lots)
        position.last_price = price
        if position.lots == 0:
            position.average_price = 0.0
        self.cash += pnl - commission
        self.realized_pnl += pnl
        self.total_commission += commission
        return pnl

    def close_lots_split(self, symbol: str, direction: str, lots: int) -> tuple[int, int]:
        """Split a close request into (yesterday_lots, today_lots).

        Exchanges charge close-today separately, and yesterday's lots are cheaper
        to shut, so they go first.
        """
        position = self.position(symbol, direction)
        yesterday = min(lots, position.yesterday_lots)
        return yesterday, lots - yesterday

    def mark(self, symbol: str, price: float) -> None:
        for direction in ("long", "short"):
            item = self.positions.get((symbol, direction))
            if item is not None and item.lots:
                item.last_price = price

    def roll_today_into_yesterday(self) -> None:
        for item in self.positions.values():
            item.today_lots = 0

    # -- valuation --------------------------------------------------------

    def margin_used(self, day: date) -> float:
        total = 0.0
        for item in self.open_positions():
            info = self.dataset.contracts[item.symbol]
            rate = self.dataset.margin_rate(
                item.symbol, day, item.direction, self.default_margin_rate
            )
            price = item.last_price or item.average_price
            total += item.lots * info.multiplier * price * rate
        return total

    def unrealized_pnl(self) -> float:
        total = 0.0
        for item in self.open_positions():
            info = self.dataset.contracts[item.symbol]
            sign = 1.0 if item.direction == "long" else -1.0
            total += sign * (item.last_price - item.average_price) * item.lots * info.multiplier
        return total

    def equity(self) -> float:
        return self.cash + self.unrealized_pnl()

    def available(self, day: date) -> float:
        return self.equity() - self.margin_used(day)

    def margin_for(self, symbol: str, direction: str, lots: int, price: float, day: date) -> float:
        info = self.dataset.contracts[symbol]
        rate = self.dataset.margin_rate(symbol, day, direction, self.default_margin_rate)
        return lots * info.multiplier * price * rate

    def settle(self, day: date) -> dict[str, float]:
        """Daily mark-to-market against settlement prices.

        Settlement variation is booked into cash so tomorrow's available funds
        reflect today's move, and ``average_price`` is reset to the settlement
        price the way a futures clearing house does it.
        """
        variation = 0.0
        for item in self.open_positions():
            info = self.dataset.contracts[item.symbol]
            settle = self.dataset.settle_price(item.symbol, day, fallback=item.last_price or None)
            sign = 1.0 if item.direction == "long" else -1.0
            variation += sign * (settle - item.average_price) * item.lots * info.multiplier
            item.average_price = settle
            item.last_price = settle
        self.cash += variation
        return {"settlement_variation": variation}

    def snapshot(self, day: date) -> AccountSnapshot:
        return AccountSnapshot(
            trading_day=day,
            cash=self.cash,
            margin=self.margin_used(day),
            available=self.available(day),
            equity=self.equity(),
            unrealized_pnl=self.unrealized_pnl(),
            net_lots=self.net_lots_by_underlying(),
        )
