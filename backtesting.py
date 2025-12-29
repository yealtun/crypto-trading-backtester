from __future__ import annotations

from argparse import ArgumentParser
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import pandas as pd
import pytz
from ta.trend import PSARIndicator


DEFAULT_TIME_ZONE = "Europe/Istanbul"


@dataclass
class BacktestConfig:
    """Runtime configuration for the backtest."""

    csv_name: str
    leverage: float = 0
    initial_capital: float = 100
    stop_loss_pct: float = 0.5
    take_profit_pct: float = 0.25
    time_zone: str = DEFAULT_TIME_ZONE


@dataclass
class TradeResult:
    """Represents the lifecycle of a single trade."""

    entry_time: datetime
    exit_time: datetime
    side: str
    entry_price: float
    exit_price: float
    exit_reason: str


@dataclass
class BacktestState:
    """Aggregated statistics and ongoing capital state."""

    capital: float
    min_capital: float
    max_capital: float
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0
    trades: List[TradeResult] = field(default_factory=list)

    def register_trade(self, profit_loss_pct: float, trade: TradeResult) -> None:
        """Update statistics after closing a trade."""
        self.capital += (self.capital / 100) * profit_loss_pct
        self.capital *= 0.9998 if profit_loss_pct > 0 else 0.9995

        self.min_capital = min(self.min_capital, self.capital)
        self.max_capital = max(self.max_capital, self.capital)

        self.trade_count += 1
        if profit_loss_pct > 0:
            self.win_count += 1
        else:
            self.loss_count += 1

        self.win_rate = (self.win_count / self.trade_count) * 100 if self.trade_count else 0
        self.trades.append(trade)


@dataclass
class Position:
    """Represents an open position."""

    side: str
    entry_price: float
    entry_time: datetime
    stop_loss: float
    take_profit: float


def to_local_time(timestamp_ms: int, time_zone: str) -> datetime:
    """Convert an epoch millisecond timestamp to a timezone-aware datetime."""
    return (
        datetime.utcfromtimestamp(timestamp_ms / 1000)
        .replace(tzinfo=pytz.utc)
        .astimezone(pytz.timezone(time_zone))
    )


def load_dataset(csv_name: str) -> pd.DataFrame:
    """Load candlestick data and append PSAR indicator values."""
    attributes = ["timestamp", "open", "high", "low", "close", "volume", "1", "2", "3", "4", "5", "6"]
    dataframe = pd.read_csv(csv_name, names=attributes)
    numeric_columns = ["timestamp", "open", "high", "low", "close", "volume"]
    dataframe[numeric_columns] = dataframe[numeric_columns].apply(pd.to_numeric, errors="coerce")
    dataframe = dataframe.dropna(subset=["timestamp", "open", "high", "low", "close"])
    psar_indicator = PSARIndicator(high=dataframe["high"], low=dataframe["low"], close=dataframe["close"], step=0.02, max_step=0.2)
    dataframe["psar"] = psar_indicator.psar()
    return dataframe


def open_long(row: pd.Series, config: BacktestConfig) -> Position:
    entry_price = float(row["close"])
    return Position(
        side="long",
        entry_price=entry_price,
        entry_time=to_local_time(int(row["timestamp"]), config.time_zone),
        stop_loss=entry_price - (entry_price * config.stop_loss_pct / 100),
        take_profit=entry_price + (entry_price * config.take_profit_pct / 100),
    )


def open_short(row: pd.Series, config: BacktestConfig) -> Position:
    entry_price = float(row["close"])
    return Position(
        side="short",
        entry_price=entry_price,
        entry_time=to_local_time(int(row["timestamp"]), config.time_zone),
        stop_loss=entry_price + (entry_price * config.stop_loss_pct / 100),
        take_profit=entry_price - (entry_price * config.take_profit_pct / 100),
    )


def close_position(
    position: Position,
    exit_price: float,
    exit_time: datetime,
    exit_reason: str,
    config: BacktestConfig,
    state: BacktestState,
) -> None:
    """Calculate profit/loss and update state when exiting a trade."""
    if position.side == "long":
        profit_loss_pct = ((exit_price - position.entry_price) / position.entry_price) * 100 * config.leverage
    else:
        profit_loss_pct = ((position.entry_price - exit_price) / position.entry_price) * 100 * config.leverage

    state.register_trade(
        profit_loss_pct,
        TradeResult(
            entry_time=position.entry_time,
            exit_time=exit_time,
            side=position.side.capitalize(),
            entry_price=position.entry_price,
            exit_price=exit_price,
            exit_reason=exit_reason,
        ),
    )


def evaluate_signals(dataframe: pd.DataFrame, config: BacktestConfig) -> BacktestState:
    """Iterate through the dataset and execute trades based on PSAR signals."""
    state = BacktestState(
        capital=config.initial_capital,
        min_capital=config.initial_capital,
        max_capital=config.initial_capital,
    )
    position: Optional[Position] = None

    for i in range(1, dataframe.shape[0]):
        row = dataframe.iloc[i]
        current_price = float(row["open"])

        if position:
            if position.side == "long" and (current_price <= position.stop_loss or current_price >= position.take_profit):
                close_position(
                    position=position,
                    exit_price=current_price,
                    exit_time=to_local_time(int(row["timestamp"]), config.time_zone),
                    exit_reason="stop_loss" if current_price <= position.stop_loss else "take_profit",
                    config=config,
                    state=state,
                )
                position = None
            elif position.side == "short" and (current_price >= position.stop_loss or current_price <= position.take_profit):
                close_position(
                    position=position,
                    exit_price=current_price,
                    exit_time=to_local_time(int(row["timestamp"]), config.time_zone),
                    exit_reason="stop_loss" if current_price >= position.stop_loss else "take_profit",
                    config=config,
                    state=state,
                )
                position = None

        sar_crossover = row["close"] > row["psar"]
        sar_crossunder = row["close"] < row["psar"]
        prev_row = dataframe.iloc[i - 1]
        prev_crossover = prev_row["close"] > prev_row["psar"]
        prev_crossunder = prev_row["close"] < prev_row["psar"]

        if sar_crossover and not prev_crossover:
            if position and position.side == "short":
                close_position(
                    position=position,
                    exit_price=float(row["close"]),
                    exit_time=to_local_time(int(row["timestamp"]), config.time_zone),
                    exit_reason="signal_flip",
                    config=config,
                    state=state,
                )
                position = None
            if position is None:
                position = open_long(row, config)

        if sar_crossunder and not prev_crossunder:
            if position and position.side == "long":
                close_position(
                    position=position,
                    exit_price=float(row["close"]),
                    exit_time=to_local_time(int(row["timestamp"]), config.time_zone),
                    exit_reason="signal_flip",
                    config=config,
                    state=state,
                )
                position = None
            if position is None:
                position = open_short(row, config)

    return state


def print_summary(config: BacktestConfig, state: BacktestState) -> None:
    """Display a concise report of the backtest results."""
    print("BACKTEST COMPLETED. TRADE RESULTS:")
    for trade in state.trades:
        print(
            f"Entry Time: {trade.entry_time.strftime('%Y-%m-%d %H:%M:%S')}, "
            f"Exit Time: {trade.exit_time.strftime('%Y-%m-%d %H:%M:%S')}, "
            f"Type: {trade.side}, Entry Price: {trade.entry_price}, Exit Price: {trade.exit_price}, "
            f"Exit Type: {trade.exit_reason}"
        )

    print("/////////////////////////////////////////////////////////////////////////////////")
    print("BACKTEST COMPLETED. RESULTS:")
    print(config.csv_name)
    print("Total Capital: ", round(state.capital, 2))
    print("Minimum Capital: ", round(state.min_capital, 2))
    print("Maximum Capital: ", round(state.max_capital, 2))
    print("Completed Trades Count: ", state.trade_count)
    print("Wins: ", state.win_count, " Losses: ", state.loss_count, " Win Rate: ", round(state.win_rate, 2))


def build_config_from_args() -> BacktestConfig:
    """Parse CLI arguments into a BacktestConfig."""
    parser = ArgumentParser(description="Backtest a simple PSAR-based strategy.")
    parser.add_argument("--csv", dest="csv_name", default="ETHUSDT-2023-2024-15m.csv", help="Path to the CSV file with candlestick data.")
    parser.add_argument("--leverage", dest="leverage", type=float, default=0, help="Leverage multiplier (set to 0 for spot).")
    parser.add_argument("--initial-capital", dest="initial_capital", type=float, default=100, help="Starting capital for the simulation.")
    parser.add_argument("--stop-loss", dest="stop_loss_pct", type=float, default=0.5, help="Stop-loss percentage.")
    parser.add_argument("--take-profit", dest="take_profit_pct", type=float, default=0.25, help="Take-profit percentage.")
    parser.add_argument("--timezone", dest="time_zone", default=DEFAULT_TIME_ZONE, help="Timezone for printing timestamps.")
    args = parser.parse_args()

    return BacktestConfig(
        csv_name=args.csv_name,
        leverage=args.leverage,
        initial_capital=args.initial_capital,
        stop_loss_pct=args.stop_loss_pct,
        take_profit_pct=args.take_profit_pct,
        time_zone=args.time_zone,
    )


def main() -> None:
    config = build_config_from_args()
    print("PREPARING FOR BACKTEST...")
    dataframe = load_dataset(config.csv_name)
    state = evaluate_signals(dataframe, config)
    print_summary(config, state)


if __name__ == "__main__":
    main()
