from decimal import Decimal
from typing import Literal, Optional

from pydantic import Field, field_validator

from hummingbot.core.data_type.common import PositionMode, TradeType
from hummingbot.strategy_v2.executors.data_types import ConnectorPair, ExecutorConfigBase


class XEMMPerpetualExecutorConfig(ExecutorConfigBase):
    """
    Configuration for Perpetual Cross-Exchange Market Making Executor

    Core Logic:
    1. Place limit orders on Maker side (your perpetual exchange)
    2. Immediately hedge with market orders on Taker side (e.g., Binance) when filled
    3. Opposite direction orders automatically close previous positions
    4. Monitor account-wide leverage ratio with tiered risk control
    """

    type: Literal["xemm_perpetual_executor"] = Field(
        default="xemm_perpetual_executor",
        json_schema_extra={"client_data": None},
    )

    # ========== Trading Pair Configuration ==========
    maker_connector_pair: ConnectorPair = Field(
        ...,
        description="Maker side trading pair (your perpetual exchange)"
    )
    taker_connector_pair: ConnectorPair = Field(
        ...,
        description="Taker side trading pair (hedge exchange, e.g., Binance Perpetual)"
    )

    # ========== Order Parameters ==========
    side: TradeType = Field(
        ...,
        description="Maker order side (BUY or SELL)"
    )
    order_amount: Decimal = Field(
        ...,
        gt=0,
        description="Order amount per trade"
    )
    min_profitability: Decimal = Field(
        default=Decimal("0.001"),
        ge=0,
        description="Minimum profitability (0.1% = 0.001) for calculating Maker order price"
    )
    max_profitability: Decimal = Field(
        default=Decimal("0.01"),
        ge=0,
        description="Maximum profitability (1% = 0.01), optional limit for excessive spreads"
    )
    slippage_buffer: Decimal = Field(
        default=Decimal("0.002"),
        ge=0,
        description="Slippage buffer for Taker hedge orders (0.2% = 0.002)"
    )

    # ========== Leverage Configuration ==========
    maker_leverage: int = Field(
        default=10,
        ge=1,
        le=125,
        description="Leverage multiplier for Maker side"
    )
    taker_leverage: int = Field(
        default=10,
        ge=1,
        le=125,
        description="Leverage multiplier for Taker side"
    )
    position_mode: PositionMode = Field(
        default=PositionMode.HEDGE,
        description="Position mode (HEDGE=dual direction, ONEWAY=single direction)"
    )

    # ========== Order Refresh ==========
    order_refresh_time: float = Field(
        default=10.0,
        gt=0,
        description="Order refresh interval in seconds"
    )
    order_refresh_tolerance_pct: Decimal = Field(
        default=Decimal("0.002"),
        ge=0,
        description="Price change tolerance (0.2% = 0.002) before refreshing order"
    )

    # ========== Risk Control (Based on Account-Wide Leverage) ==========
    target_leverage: Decimal = Field(
        default=Decimal("10"),
        gt=0,
        description="Target leverage (actual leverage changes dynamically with positions)"
    )

    # Leverage usage ratio thresholds
    leverage_warning_threshold: Decimal = Field(
        default=Decimal("0.60"),
        ge=0,
        le=1,
        description="Leverage warning threshold (60% -> Level 1 risk control)"
    )
    leverage_caution_threshold: Decimal = Field(
        default=Decimal("0.70"),
        ge=0,
        le=1,
        description="Leverage caution threshold (70% -> Level 2 risk control)"
    )
    leverage_critical_threshold: Decimal = Field(
        default=Decimal("0.85"),
        ge=0,
        le=1,
        description="Leverage critical threshold (85% -> Level 3 risk control)"
    )
    leverage_emergency_threshold: Decimal = Field(
        default=Decimal("0.95"),
        ge=0,
        le=1,
        description="Leverage emergency threshold (95% -> Level 4 risk control)"
    )

    # Risk control action parameters
    order_amount_reduction_step: Decimal = Field(
        default=Decimal("0.5"),
        gt=0,
        le=1,
        description="Order amount reduction step (0.5 = reduce by 50% each time)"
    )
    position_reduction_ratio: Decimal = Field(
        default=Decimal("0.3"),
        gt=0,
        le=1,
        description="Emergency position reduction ratio (0.3 = reduce 30% of position)"
    )

    # ========== Other Risk Controls ==========
    max_retries: int = Field(
        default=3,
        ge=0,
        description="Maximum retries for failed hedge orders"
    )

    # ========== Alert Notifications ==========
    alert_webhook_url: Optional[str] = Field(
        default=None,
        description="Alert webhook URL (supports DingTalk, Slack, etc.)"
    )
    alert_cooldown: float = Field(
        default=300.0,
        ge=0,
        description="Alert cooldown period in seconds to avoid spam"
    )

    @field_validator('position_mode', mode='before')
    @classmethod
    def validate_position_mode(cls, v):
        """Validate position mode"""
        if isinstance(v, str):
            v_upper = v.upper()
            if v_upper == "HEDGE":
                return PositionMode.HEDGE
            elif v_upper == "ONEWAY":
                return PositionMode.ONEWAY
        return v

    @field_validator('leverage_caution_threshold')
    @classmethod
    def validate_caution_threshold(cls, v, info):
        """Ensure caution > warning"""
        if 'leverage_warning_threshold' in info.data:
            if v <= info.data['leverage_warning_threshold']:
                raise ValueError("leverage_caution_threshold must be greater than leverage_warning_threshold")
        return v

    @field_validator('leverage_critical_threshold')
    @classmethod
    def validate_critical_threshold(cls, v, info):
        """Ensure critical > caution"""
        if 'leverage_caution_threshold' in info.data:
            if v <= info.data['leverage_caution_threshold']:
                raise ValueError("leverage_critical_threshold must be greater than leverage_caution_threshold")
        return v

    @field_validator('leverage_emergency_threshold')
    @classmethod
    def validate_emergency_threshold(cls, v, info):
        """Ensure emergency > critical"""
        if 'leverage_critical_threshold' in info.data:
            if v <= info.data['leverage_critical_threshold']:
                raise ValueError("leverage_emergency_threshold must be greater than leverage_critical_threshold")
        return v

    @field_validator('max_profitability')
    @classmethod
    def validate_max_profitability(cls, v, info):
        """Ensure max >= min"""
        if 'min_profitability' in info.data:
            if v < info.data['min_profitability']:
                raise ValueError("max_profitability must be greater than or equal to min_profitability")
        return v
