from freqtrade.strategy import IStrategy
from pandas import DataFrame
from freqtrade.persistence import Trade
import logging

logger = logging.getLogger(__name__)


class WebhookStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = '4h'

    # Static 10% under entry until the trade is +12%.
    # After that, trail 10% under the high. The stop only moves up.
    # 0.12 > 0.10, so the first trailed stop is still about +0.8% over entry.
    stoploss = -0.10
    trailing_stop = True
    trailing_stop_positive = 0.10
    trailing_stop_positive_offset = 0.12
    trailing_only_offset_is_reached = True

    minimal_roi = {"0": 100}
    process_only_new_candles = False
    position_adjustment_enable = True
    max_entry_position_adjustment = -1

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe['enter_long'] = 0
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe['exit_long'] = 0
        return dataframe

    def adjust_trade_position(self, trade: Trade, current_time, current_rate: float,
                              current_profit: float, min_stake: float,
                              max_stake: float, current_entry_rate: float,
                              current_exit_rate: float, current_entry_profit: float,
                              current_exit_profit: float, **kwargs):
        return None

    def custom_exit(self, pair: str, trade: Trade, current_time, current_rate: float,
                    current_profit: float, **kwargs):
        return None
