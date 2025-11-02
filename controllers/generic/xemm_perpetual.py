"""
XEMM Perpetual Controller for Hummingbot

This controller manages XEMMPerpetualExecutor instances for cross-exchange
perpetual market making with advanced risk management.
"""

import time
from decimal import Decimal
from typing import Dict, List, Set

import pandas as pd
from pydantic import Field, field_validator

from hummingbot.client.ui.interface_utils import format_df_for_printout
from hummingbot.core.data_type.common import PositionMode, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers.controller_base import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.xemm_perpetual_executor import XEMMPerpetualExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction


class XEMMPerpetualConfig(ControllerConfigBase):
    """Configuration for XEMM Perpetual Controller"""

    controller_name: str = "xemm_perpetual"
    controller_type: str = "generic"
    candles_config: List[CandlesConfig] = []

    # ========== Trading Pair Configuration ==========
    maker_connector: str = Field(
        default="binance_perpetual",
        json_schema_extra={
            "prompt": "Enter the maker connector (your perpetual exchange): ",
            "prompt_on_new": True
        }
    )
    maker_trading_pair: str = Field(
        default="BTC-USDT",
        json_schema_extra={
            "prompt": "Enter the maker trading pair: ",
            "prompt_on_new": True
        }
    )
    taker_connector: str = Field(
        default="binance_perpetual",
        json_schema_extra={
            "prompt": "Enter the taker connector (hedge exchange): ",
            "prompt_on_new": True
        }
    )
    taker_trading_pair: str = Field(
        default="BTC-USDT",
        json_schema_extra={
            "prompt": "Enter the taker trading pair: ",
            "prompt_on_new": True
        }
    )

    # ========== Order Parameters ==========
    side: str = Field(
        default="BUY",
        json_schema_extra={
            "prompt": "Enter the maker order side (BUY or SELL): ",
            "prompt_on_new": True
        }
    )
    order_amount: Decimal = Field(
        default=Decimal("0.001"),
        json_schema_extra={
            "prompt": "Enter the order amount per trade: ",
            "prompt_on_new": True
        }
    )
    min_profitability: Decimal = Field(
        default=Decimal("0.001"),
        json_schema_extra={
            "prompt": "Enter the minimum profitability (e.g., 0.001 for 0.1%): ",
            "prompt_on_new": True
        }
    )
    max_profitability: Decimal = Field(
        default=Decimal("0.01"),
        json_schema_extra={
            "prompt": "Enter the maximum profitability (e.g., 0.01 for 1%): ",
            "prompt_on_new": True
        }
    )
    slippage_buffer: Decimal = Field(
        default=Decimal("0.002"),
        json_schema_extra={
            "prompt": "Enter the slippage buffer (e.g., 0.002 for 0.2%): ",
            "prompt_on_new": False
        }
    )

    # ========== Leverage Configuration ==========
    maker_leverage: int = Field(
        default=10,
        json_schema_extra={
            "prompt": "Enter the maker leverage (1-125): ",
            "prompt_on_new": True
        }
    )
    taker_leverage: int = Field(
        default=10,
        json_schema_extra={
            "prompt": "Enter the taker leverage (1-125): ",
            "prompt_on_new": True
        }
    )
    position_mode: str = Field(
        default="HEDGE",
        json_schema_extra={
            "prompt": "Enter the position mode (HEDGE or ONEWAY): ",
            "prompt_on_new": True
        }
    )

    # ========== Order Refresh ==========
    order_refresh_time: float = Field(
        default=10.0,
        json_schema_extra={
            "prompt": "Enter the order refresh time in seconds: ",
            "prompt_on_new": False
        }
    )
    order_refresh_tolerance_pct: Decimal = Field(
        default=Decimal("0.002"),
        json_schema_extra={
            "prompt": "Enter the order refresh tolerance (e.g., 0.002 for 0.2%): ",
            "prompt_on_new": False
        }
    )

    # ========== Risk Control ==========
    target_leverage: Decimal = Field(
        default=Decimal("10"),
        json_schema_extra={
            "prompt": "Enter the target account leverage: ",
            "prompt_on_new": False
        }
    )
    leverage_warning_threshold: Decimal = Field(
        default=Decimal("0.60"),
        json_schema_extra={
            "prompt": "Enter the leverage warning threshold (e.g., 0.60 for 60%): ",
            "prompt_on_new": False
        }
    )
    leverage_caution_threshold: Decimal = Field(
        default=Decimal("0.70"),
        json_schema_extra={
            "prompt": "Enter the leverage caution threshold (e.g., 0.70 for 70%): ",
            "prompt_on_new": False
        }
    )
    leverage_critical_threshold: Decimal = Field(
        default=Decimal("0.85"),
        json_schema_extra={
            "prompt": "Enter the leverage critical threshold (e.g., 0.85 for 85%): ",
            "prompt_on_new": False
        }
    )
    leverage_emergency_threshold: Decimal = Field(
        default=Decimal("0.95"),
        json_schema_extra={
            "prompt": "Enter the leverage emergency threshold (e.g., 0.95 for 95%): ",
            "prompt_on_new": False
        }
    )
    order_amount_reduction_step: Decimal = Field(
        default=Decimal("0.5"),
        json_schema_extra={
            "prompt": "Enter the order amount reduction step (e.g., 0.5 for 50% reduction): ",
            "prompt_on_new": False
        }
    )

    # ========== Other ==========
    max_retries: int = Field(
        default=3,
        json_schema_extra={
            "prompt": "Enter the maximum retries for failed hedge orders: ",
            "prompt_on_new": False
        }
    )
    alert_webhook_url: str = Field(
        default="",
        json_schema_extra={
            "prompt": "Enter alert webhook URL (optional): ",
            "prompt_on_new": False
        }
    )
    alert_cooldown: float = Field(
        default=300.0,
        json_schema_extra={
            "prompt": "Enter alert cooldown in seconds: ",
            "prompt_on_new": False
        }
    )

    @field_validator("side", mode="before")
    @classmethod
    def validate_side(cls, v):
        """Validate and normalize side"""
        if isinstance(v, str):
            v = v.upper()
            if v not in ["BUY", "SELL"]:
                raise ValueError("Side must be 'BUY' or 'SELL'")
        return v

    @field_validator("position_mode", mode="before")
    @classmethod
    def validate_position_mode(cls, v):
        """Validate and normalize position mode"""
        if isinstance(v, str):
            v = v.upper()
            if v not in ["HEDGE", "ONEWAY"]:
                raise ValueError("Position mode must be 'HEDGE' or 'ONEWAY'")
        return v

    def update_markets(self, markets: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
        """Register required trading pairs"""
        if self.maker_connector not in markets:
            markets[self.maker_connector] = set()
        markets[self.maker_connector].add(self.maker_trading_pair)

        if self.taker_connector not in markets:
            markets[self.taker_connector] = set()
        markets[self.taker_connector].add(self.taker_trading_pair)

        return markets


class XEMMPerpetual(ControllerBase):
    """
    XEMM Perpetual Controller

    Manages XEMMPerpetualExecutor instances for cross-exchange perpetual market making.
    Monitors executors and creates new ones as needed based on configuration.
    """

    def __init__(self, config: XEMMPerpetualConfig, *args, **kwargs):
        self.config = config
        super().__init__(config, *args, **kwargs)

        # Convert side string to TradeType
        self._trade_side = TradeType.BUY if config.side == "BUY" else TradeType.SELL

        # Track active executor
        self._active_executor_id: str = None

    async def update_processed_data(self):
        """Update any processed data if needed"""
        pass

    def determine_executor_actions(self) -> List[ExecutorAction]:
        """
        Determine what executor actions to take

        Creates a new XEMMPerpetualExecutor if there is no active one.
        """
        executor_actions = []

        # Check for active executors
        active_executors = self.filter_executors(
            executors=self.executors_info,
            filter_func=lambda e: not e.is_done
        )

        total_executors = len(self.executors_info)
        done_executors = len([e for e in self.executors_info if e.is_done])

        # Log executor status periodically
        if int(time.time()) % 30 == 0:  # Every 30 seconds
            self.logger().info(
                f"[XEMM Controller {self.config.id}] "
                f"Total Executors: {total_executors}, "
                f"Active: {len(active_executors)}, "
                f"Completed: {done_executors}"
            )

        # If no active executor, create one
        if len(active_executors) == 0:
            executor_config = self._create_executor_config()
            executor_actions.append(
                CreateExecutorAction(
                    executor_config=executor_config,
                    controller_id=self.config.id
                )
            )
            self.logger().info(
                f"[XEMM Controller {self.config.id}] Creating new executor: "
                f"{self.config.maker_connector}/{self.config.maker_trading_pair} -> "
                f"{self.config.taker_connector}/{self.config.taker_trading_pair}, "
                f"Side: {self.config.side}, Amount: {self.config.order_amount}, "
                f"Target Profit: {self.config.min_profitability * 100:.2f}%-{self.config.max_profitability * 100:.2f}%"
            )
        else:
            # Log info about active executors every 10 seconds
            if int(time.time()) % 10 == 0:
                for executor in active_executors:
                    self.logger().info(
                        f"[XEMM Controller {self.config.id}] Active Executor {executor.id[:8]}: "
                        f"Status={executor.status.name}, "
                        f"Net PnL={executor.net_pnl:.2f}"
                    )

        return executor_actions

    def _create_executor_config(self) -> XEMMPerpetualExecutorConfig:
        """Create executor configuration from controller config"""

        # Convert position mode string to enum
        position_mode = PositionMode.HEDGE if self.config.position_mode == "HEDGE" else PositionMode.ONEWAY

        config = XEMMPerpetualExecutorConfig(
            controller_id=self.config.id,
            timestamp=time.time(),
            maker_connector_pair=ConnectorPair(
                connector_name=self.config.maker_connector,
                trading_pair=self.config.maker_trading_pair
            ),
            taker_connector_pair=ConnectorPair(
                connector_name=self.config.taker_connector,
                trading_pair=self.config.taker_trading_pair
            ),
            side=self._trade_side,
            order_amount=self.config.order_amount,
            min_profitability=self.config.min_profitability,
            max_profitability=self.config.max_profitability,
            slippage_buffer=self.config.slippage_buffer,
            maker_leverage=self.config.maker_leverage,
            taker_leverage=self.config.taker_leverage,
            position_mode=position_mode,
            order_refresh_time=self.config.order_refresh_time,
            order_refresh_tolerance_pct=self.config.order_refresh_tolerance_pct,
            target_leverage=self.config.target_leverage,
            leverage_warning_threshold=self.config.leverage_warning_threshold,
            leverage_caution_threshold=self.config.leverage_caution_threshold,
            leverage_critical_threshold=self.config.leverage_critical_threshold,
            leverage_emergency_threshold=self.config.leverage_emergency_threshold,
            order_amount_reduction_step=self.config.order_amount_reduction_step,
            max_retries=self.config.max_retries,
            alert_webhook_url=self.config.alert_webhook_url if self.config.alert_webhook_url else None,
            alert_cooldown=self.config.alert_cooldown,
        )

        return config

    def to_format_status(self) -> List[str]:
        """Format controller status for display"""

        if not self.executors_info:
            return ["No active executors"]

        # Gather executor information
        executor_data = []
        for executor_info in self.executors_info:
            try:
                data = {
                    "ID": executor_info.id[:8],
                    "Status": executor_info.status.name,
                    "Side": executor_info.config.side.name if hasattr(executor_info.config, 'side') else "N/A",
                    "Amount": f"{executor_info.config.order_amount:.8f}" if hasattr(executor_info.config, 'order_amount') else "N/A",
                    "Filled": f"{executor_info.filled_amount_quote:.2f}" if hasattr(executor_info, 'filled_amount_quote') else "0",
                    "Net PnL": f"{executor_info.net_pnl:.2f}" if hasattr(executor_info, 'net_pnl') else "0",
                    "Close Type": executor_info.close_type.name if hasattr(executor_info, 'close_type') and executor_info.close_type else "N/A",
                }
                executor_data.append(data)
            except Exception as e:
                self.logger().error(f"Error formatting executor info: {e}")
                continue

        if not executor_data:
            return ["No executor data available"]

        df = pd.DataFrame(executor_data)

        # Add summary statistics
        summary = [
            "\n📊 XEMM Perpetual Controller Status",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"Maker: {self.config.maker_connector}/{self.config.maker_trading_pair}",
            f"Taker: {self.config.taker_connector}/{self.config.taker_trading_pair}",
            f"Side: {self.config.side}",
            f"Leverage: Maker {self.config.maker_leverage}x, Taker {self.config.taker_leverage}x",
            f"Target Profit: {self.config.min_profitability * 100:.2f}% - {self.config.max_profitability * 100:.2f}%",
            f"\nActive Executors: {len([e for e in self.executors_info if not e.is_done])}",
            f"Completed Executors: {len([e for e in self.executors_info if e.is_done])}",
            f"\n{format_df_for_printout(df, table_format='psql')}",
        ]

        return summary
