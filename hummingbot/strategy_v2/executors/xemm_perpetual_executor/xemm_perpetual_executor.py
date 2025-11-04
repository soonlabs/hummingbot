import asyncio
import logging
from decimal import Decimal
from typing import Dict, Optional, Tuple

from hummingbot.connector.derivative_base import DerivativeBase
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, PriceType, TradeType
from hummingbot.core.data_type.order_candidate import PerpetualOrderCandidate
from hummingbot.strategy_v2.executors.executor_base import ExecutorBase
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType, TrackedOrder

from .data_types import XEMMPerpetualExecutorConfig


class XEMMPerpetualExecutor(ExecutorBase):
    """
    Perpetual Cross-Exchange Market Making Executor

    Core Logic:
    1. Place limit orders on Maker side (your exchange)
    2. Immediately hedge with market orders on Taker side (Binance) when filled
    3. Opposite direction orders automatically close previous positions
    4. Monitor account-wide leverage ratio and position limits
    """

    _logger = None

    @classmethod
    def logger(cls) -> logging.Logger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    def __init__(self, strategy, config: XEMMPerpetualExecutorConfig, update_interval: float = 1.0, max_retries: int = 15):
        super().__init__(
            strategy=strategy,
            connectors=[
                config.maker_connector_pair.connector_name,
                config.taker_connector_pair.connector_name
            ],
            config=config,
            update_interval=update_interval
        )

        self.config: XEMMPerpetualExecutorConfig = config

        # Retry configuration
        self._max_retries = max_retries

        # Order tracking
        self._maker_order: Optional[TrackedOrder] = None
        self._taker_order: Optional[TrackedOrder] = None

        # Price cache
        self._maker_price: Optional[Decimal] = None
        self._taker_price: Optional[Decimal] = None
        self._total_tx_cost_pct: Decimal = Decimal("0")

        # Retry tracking
        self._hedge_retries: int = 0

        # Order timing
        self._last_order_timestamp: float = 0

        # Risk control state
        self._current_risk_level: int = 0  # 0=normal, 1-4=Level 1-4
        self._degraded_order_amount: Optional[Decimal] = None  # Reduced order amount
        self._degraded_mode: bool = False  # Degraded operation mode
        self._last_alert_time: float = 0  # Last alert timestamp

    # ==================== Lifecycle ====================

    async def on_start(self):
        """Initialize leverage and position mode on start"""
        await self._set_leverage_and_position_mode()
        await self.validate_sufficient_balance()

    async def _set_leverage_and_position_mode(self):
        """Set leverage and position mode for both sides"""
        maker_mode = self._configure_connector_leverage_and_mode(
            connector_name=self.config.maker_connector_pair.connector_name,
            trading_pair=self.config.maker_connector_pair.trading_pair,
            leverage=self.config.maker_leverage,
        )
        taker_mode = self._configure_connector_leverage_and_mode(
            connector_name=self.config.taker_connector_pair.connector_name,
            trading_pair=self.config.taker_connector_pair.trading_pair,
            leverage=self.config.taker_leverage,
        )

        mode_message = (
            maker_mode.name if maker_mode and maker_mode == taker_mode
            else f"{self.config.maker_connector_pair.connector_name}:{maker_mode.name if maker_mode else 'unknown'}, "
                 f"{self.config.taker_connector_pair.connector_name}:{taker_mode.name if taker_mode else 'unknown'}"
        )

        self.logger().info(
            f"Set leverage - Maker: {self.config.maker_leverage}x, "
            f"Taker: {self.config.taker_leverage}x, Mode: {mode_message}"
        )

    def _configure_connector_leverage_and_mode(
            self,
            connector_name: str,
            trading_pair: str,
            leverage: int,
    ) -> Optional[PositionMode]:
        connector: DerivativeBase = self.connectors[connector_name]
        connector.set_leverage(trading_pair, leverage)

        desired_mode = self.config.position_mode
        supported_modes = []
        applied_mode: Optional[PositionMode] = None

        # Check current position mode first to avoid unnecessary API calls
        current_mode = None
        if hasattr(connector, "position_mode"):
            try:
                current_mode = connector.position_mode
                if current_mode == desired_mode:
                    self.logger().info(
                        f"{connector_name} is already in {desired_mode.name} mode. Skipping mode change."
                    )
                    return desired_mode
            except Exception:  # pragma: no cover
                self.logger().debug(
                    f"Unable to fetch current position mode for {connector_name}."
                )

        if hasattr(connector, "supported_position_modes"):
            try:
                supported_modes = connector.supported_position_modes()
            except Exception:  # pragma: no cover - defensive logging only
                self.logger().warning(
                    f"Unable to fetch supported position modes for {connector_name}. Attempting desired mode {desired_mode.name}."
                )

        if desired_mode in supported_modes:
            try:
                connector.set_position_mode(desired_mode)
                self.logger().info(
                    f"Set {connector_name} position mode to {desired_mode.name}."
                )
                applied_mode = desired_mode
            except Exception as e:
                self.logger().error(
                    f"Failed to set {connector_name} position mode to {desired_mode.name}: {str(e)}"
                )
                applied_mode = current_mode
        elif supported_modes:
            fallback_mode = supported_modes[0]
            if fallback_mode != desired_mode:
                self.logger().warning(
                    f"{connector_name} does not support {desired_mode.name} mode. Falling back to {fallback_mode.name}."
                )
            try:
                connector.set_position_mode(fallback_mode)
                applied_mode = fallback_mode
            except Exception as e:
                self.logger().error(
                    f"Failed to set {connector_name} position mode to {fallback_mode.name}: {str(e)}"
                )
                applied_mode = current_mode
        else:
            try:
                connector.set_position_mode(desired_mode)
                applied_mode = desired_mode
            except Exception as e:  # pragma: no cover - defensive logging only
                self.logger().warning(
                    f"Failed to set position mode {desired_mode.name} on {connector_name}: {str(e)}"
                )
                applied_mode = current_mode

        return applied_mode

    # ==================== Main Control Loop ====================

    async def control_task(self):
        """Main control loop"""
        if self.status == RunnableStatus.RUNNING:

            # ========== Risk Control Checks ==========
            risk_level, risk_data = await self.check_account_leverage_risk()

            if risk_level != self._current_risk_level:
                # Risk level changed, send alert and handle risk
                await self._send_risk_alert(risk_level, risk_data)
                await self.handle_leverage_risk(risk_level, risk_data)
                self._current_risk_level = risk_level

            # ========== Normal Business Logic ==========
            # Only continue placing orders if risk level < 4 (Level 4 = emergency, force reduce positions)
            if risk_level < 4:
                await self.control_maker_order()

        elif self.status == RunnableStatus.SHUTTING_DOWN:
            await self.control_shutdown_process()

    async def control_maker_order(self):
        """Maker order control logic"""
        # Use reduced order amount if in degraded mode
        effective_order_amount = self._degraded_order_amount or self.config.order_amount

        self.logger().debug(
            "Control loop tick | risk_level=%s | degraded_mode=%s | effective_order_amount=%s",
            self._current_risk_level,
            self._degraded_mode,
            effective_order_amount,
        )

        # If in degraded mode and not allowed to open new positions, return
        if self._degraded_mode and self._current_risk_level >= 3:
            return

        # Update prices and transaction costs
        await self.update_prices_and_tx_costs()

        # Check margin sufficiency (similar to spot balance check)
        if not await self.check_sufficient_margin(effective_order_amount):
            if self._maker_order:
                await self.cancel_maker_order()
            return

        # Check profitability
        if not await self.is_profitable():
            if self._maker_order:
                await self.cancel_maker_order()
            return

        # Manage orders (using effective order amount)
        if self._maker_order:
            if self._maker_order.order:
                # Existing order - check if refresh needed
                if await self.should_refresh_order():
                    await self.cancel_maker_order()
                    await asyncio.sleep(0.5)  # Wait for cancellation confirmation
                    await self.place_maker_order(effective_order_amount)
            else:
                # Order placement in-flight, wait for exchange confirmation
                return
        else:
            # No order - create new one
            await self.place_maker_order(effective_order_amount)

    # ==================== Profitability Check ====================

    async def update_prices_and_tx_costs(self):
        """Update prices and transaction costs"""
        # Taker price (hedge price with slippage)
        self._taker_price = await self.get_taker_hedge_price()

        # Transaction costs (both sides fees)
        maker_fee = await self.get_tx_cost_in_asset(
            self.config.maker_connector_pair.connector_name,
            self.config.maker_connector_pair.trading_pair,
            is_maker=True
        )
        taker_fee = await self.get_tx_cost_in_asset(
            self.config.taker_connector_pair.connector_name,
            self.config.taker_connector_pair.trading_pair,
            is_maker=False
        )
        self._total_tx_cost_pct = maker_fee + taker_fee

        # Maker price (our limit order price)
        self._maker_price = self.get_maker_order_price(reference_price=self._taker_price)

        self.logger().debug(
            "Pricing update | maker_price=%s | taker_price=%s | maker_fee_pct=%s | taker_fee_pct=%s | total_fee_pct=%s",
            self._maker_price,
            self._taker_price,
            maker_fee,
            taker_fee,
            self._total_tx_cost_pct,
        )

    def get_maker_order_price(self, reference_price: Optional[Decimal] = None) -> Decimal:
        """
        Calculate Maker order price
        Based on Taker price, ensuring min_profitability
        """
        # Get Taker market reference price
        taker_price_reference = reference_price
        if taker_price_reference is None:
            taker_price_reference = self.get_price(
                self.config.taker_connector_pair.connector_name,
                self.config.taker_connector_pair.trading_pair,
                price_type=PriceType.MidPrice
            )

        if taker_price_reference is None:
            return Decimal("0")

        if getattr(taker_price_reference, "is_nan", None) and taker_price_reference.is_nan():
            return Decimal("0")

        if taker_price_reference <= 0:
            return Decimal("0")

        # Calculate Maker price based on direction
        if self.config.side == TradeType.BUY:
            # Maker buy -> Taker sell
            # maker_price = taker_price / (1 + min_profitability + tx_cost)
            maker_price = taker_price_reference / (
                Decimal("1") + self.config.min_profitability + self._total_tx_cost_pct
            )
        else:
            # Maker sell -> Taker buy
            # maker_price = taker_price * (1 + min_profitability + tx_cost)
            maker_price = taker_price_reference * (
                Decimal("1") + self.config.min_profitability + self._total_tx_cost_pct
            )

        # Quantize price
        maker_connector = self.connectors[self.config.maker_connector_pair.connector_name]
        return maker_connector.quantize_order_price(
            self.config.maker_connector_pair.trading_pair,
            maker_price
        )

    async def get_taker_hedge_price(self) -> Decimal:
        """Get Taker hedge price (market price + slippage buffer)"""
        taker_connector = self.connectors[self.config.taker_connector_pair.connector_name]

        # Hedge direction opposite to Maker
        hedge_side = TradeType.SELL if self.config.side == TradeType.BUY else TradeType.BUY
        is_buy = (hedge_side == TradeType.BUY)

        # Get hedge price with slippage
        price_result = taker_connector.get_price_for_volume(
            self.config.taker_connector_pair.trading_pair,
            is_buy,
            self.config.order_amount
        )

        hedge_price = price_result.result_price

        # Add slippage buffer
        if is_buy:
            hedge_price *= (Decimal("1") + self.config.slippage_buffer)
        else:
            hedge_price *= (Decimal("1") - self.config.slippage_buffer)

        return hedge_price

    async def get_tx_cost_in_asset(
        self,
        connector_name: str,
        trading_pair: str,
        is_maker: bool = False
    ) -> Decimal:
        """
        Get transaction cost as a percentage for a connector

        Args:
            connector_name: Name of the connector
            trading_pair: Trading pair
            is_maker: Whether this is a maker order (limit) or taker order (market)

        Returns:
            Transaction cost as a percentage (e.g., 0.001 for 0.1%)
        """
        connector = self.connectors.get(connector_name)
        if not connector:
            return Decimal("0")

        # Determine order type and side
        order_type = OrderType.LIMIT if is_maker else OrderType.MARKET
        order_side = self.config.side if connector_name == self.config.maker_connector_pair.connector_name else (
            TradeType.SELL if self.config.side == TradeType.BUY else TradeType.BUY
        )

        # Get current price for fee calculation
        current_price = self.get_price(connector_name, trading_pair, PriceType.MidPrice)

        # Get fee from connector
        base_currency = trading_pair.split("-")[0]
        quote_currency = trading_pair.split("-")[1]

        position_action = PositionAction.OPEN
        fee = connector.get_fee(
            base_currency=base_currency,
            quote_currency=quote_currency,
            order_type=order_type,
            order_side=order_side,
            position_action=position_action,
            amount=self.config.order_amount,
            price=current_price,
            is_maker=is_maker,
        )

        # Return fee as percentage
        # For perpetuals, fee is typically calculated as percentage of position value
        return fee.percent

    async def is_profitable(self) -> bool:
        """
        Check if current spread is profitable
        Formula: spread - tx_cost >= min_profitability
        """
        if not self._maker_price or not self._taker_price:
            return False

        # Calculate spread profit ratio
        if self.config.side == TradeType.BUY:
            # Maker buy @ maker_price, Taker sell @ taker_price
            spread_pct = (self._taker_price - self._maker_price) / self._maker_price
        else:
            # Maker sell @ maker_price, Taker buy @ taker_price
            spread_pct = (self._maker_price - self._taker_price) / self._taker_price

        # Net profit = spread - transaction costs
        net_profit = spread_pct - self._total_tx_cost_pct

        is_profitable = net_profit >= self.config.min_profitability

        self.logger().debug(
            "Profit check | maker_price=%s | taker_price=%s | spread_pct=%s | net_profit_pct=%s | min_required=%s | result=%s",
            self._maker_price,
            self._taker_price,
            spread_pct,
            net_profit,
            self.config.min_profitability,
            is_profitable,
        )

        return is_profitable

    # ==================== Margin Check (Similar to Spot Balance Check) ====================

    async def check_sufficient_margin(self, order_amount: Decimal) -> bool:
        """
        Check if both sides have sufficient margin (similar to spot balance check)

        Check:
        1. Maker side can open position with order_amount
        2. Taker side can hedge with order_amount
        """
        # Check Maker side
        maker_ok = await self._check_single_side_margin(
            connector_name=self.config.maker_connector_pair.connector_name,
            trading_pair=self.config.maker_connector_pair.trading_pair,
            side=self.config.side,
            amount=order_amount,
            price=self._maker_price if self._maker_price else Decimal("0"),
            leverage=self.config.maker_leverage
        )

        # Check Taker side
        taker_side = TradeType.SELL if self.config.side == TradeType.BUY else TradeType.BUY
        taker_ok = await self._check_single_side_margin(
            connector_name=self.config.taker_connector_pair.connector_name,
            trading_pair=self.config.taker_connector_pair.trading_pair,
            side=taker_side,
            amount=order_amount,
            price=self._taker_price if self._taker_price else Decimal("0"),
            leverage=self.config.taker_leverage
        )

        if not maker_ok:
            self.logger().warning("Maker side insufficient margin")
        if not taker_ok:
            self.logger().warning("Taker side insufficient margin")

        return maker_ok and taker_ok

    async def _check_single_side_margin(
        self,
        connector_name: str,
        trading_pair: str,
        side: TradeType,
        amount: Decimal,
        price: Decimal,
        leverage: int
    ) -> bool:
        """Check if single side has sufficient margin"""
        if price == 0:
            return False

        # Use PerpetualOrderCandidate to check
        order_candidate = PerpetualOrderCandidate(
            trading_pair=trading_pair,
            is_maker=True,
            order_type=OrderType.LIMIT,
            order_side=side,
            amount=amount,
            price=price,
            leverage=Decimal(leverage)
        )

        # Let exchange's budget_checker adjust
        adjusted = self.adjust_order_candidates(connector_name, [order_candidate])

        # If adjusted amount becomes 0, insufficient margin
        return adjusted[0].amount > 0

    # ==================== Order Management ====================

    async def place_maker_order(self, amount: Decimal):
        """Place Maker limit order"""
        if self._maker_price is None:
            return

        order_id = self.place_order(
            connector_name=self.config.maker_connector_pair.connector_name,
            trading_pair=self.config.maker_connector_pair.trading_pair,
            order_type=OrderType.LIMIT,
            side=self.config.side,
            amount=amount,
            price=self._maker_price,
            position_action=PositionAction.OPEN
        )

        self._maker_order = TrackedOrder(order_id)
        self._last_order_timestamp = self._strategy.current_timestamp

        self.logger().info(
            f"Placed Maker {self.config.side.name} order: {amount} @ {self._maker_price}"
        )

    async def cancel_maker_order(self):
        """Cancel Maker order"""
        if self._maker_order and self._maker_order.order_id:
            self._strategy.cancel(
                connector_name=self.config.maker_connector_pair.connector_name,
                trading_pair=self.config.maker_connector_pair.trading_pair,
                order_id=self._maker_order.order_id
            )
            self._maker_order = None

    async def should_refresh_order(self) -> bool:
        """Check if order refresh is needed"""
        if not self._maker_order or not self._maker_order.order:
            return False

        # Check time interval
        time_since_last_order = self._strategy.current_timestamp - self._last_order_timestamp
        if time_since_last_order < self.config.order_refresh_time:
            return False

        # Check price change
        current_ideal_price = self.get_maker_order_price(reference_price=self._taker_price)
        existing_price = self._maker_order.order.price

        price_diff_pct = abs(current_ideal_price - existing_price) / existing_price

        return price_diff_pct > self.config.order_refresh_tolerance_pct

    # ==================== Hedge Logic ====================

    def process_order_completed_event(self, event_tag: int, market, event):
        """Maker order filled -> trigger hedge"""
        if self._maker_order and event.order_id == self._maker_order.order_id:
            self.logger().info(
                f"Maker order filled: {event.base_asset_amount} @ "
                f"{event.quote_asset_amount / event.base_asset_amount}"
            )

            # Immediately hedge
            asyncio.create_task(self.place_taker_hedge_order(event.base_asset_amount))

            # Reset Maker order
            self._maker_order = None

    async def place_taker_hedge_order(self, amount: Decimal):
        """Place Taker market order to hedge"""
        # Hedge direction opposite
        hedge_side = TradeType.SELL if self.config.side == TradeType.BUY else TradeType.BUY

        try:
            order_id = self.place_order(
                connector_name=self.config.taker_connector_pair.connector_name,
                trading_pair=self.config.taker_connector_pair.trading_pair,
                order_type=OrderType.MARKET,
                side=hedge_side,
                amount=amount,
                price=Decimal("NaN"),
                position_action=PositionAction.OPEN  # Open position (opposite direction auto-closes)
            )

            self._taker_order = TrackedOrder(order_id)
            self._hedge_retries = 0  # Reset retry counter

            self.logger().info(f"Placed Taker {hedge_side.name} hedge order: {amount}")

        except Exception as e:
            self.logger().error(f"Hedge order failed: {e}")
            self._hedge_retries += 1

            if self._hedge_retries < self.config.max_retries:
                # Retry
                await asyncio.sleep(1)
                await self.place_taker_hedge_order(amount)
            else:
                # Max retries reached, emergency stop
                self.logger().error("Max hedge retries reached. Emergency stop!")
                self.close_type = CloseType.FAILED
                self._status = RunnableStatus.SHUTTING_DOWN

    # ==================== Position Management ====================

    async def get_net_position(self) -> Decimal:
        """Get net position (Maker + Taker)"""
        maker_position = self.get_position_size(
            self.config.maker_connector_pair.connector_name,
            self.config.maker_connector_pair.trading_pair
        )
        taker_position = self.get_position_size(
            self.config.taker_connector_pair.connector_name,
            self.config.taker_connector_pair.trading_pair
        )

        return maker_position + taker_position

    def get_position_size(self, connector_name: str, trading_pair: str) -> Decimal:
        """Get position size for specific trading pair (signed)"""
        connector: DerivativeBase = self.connectors[connector_name]

        if hasattr(connector, 'account_positions'):
            position = connector.account_positions.get(trading_pair)
            if position:
                return position.amount  # Positive=long, negative=short

        return Decimal("0")

    # ==================== Leverage Risk Control ====================

    async def check_account_leverage_risk(self) -> Tuple[int, dict]:
        """
        Check account-wide leverage risk

        Returns:
            (risk_level, risk_data)
            risk_level: 0=normal, 1-4=Level 1-4
            risk_data: Risk details
        """
        # Get both sides account info
        maker_account = await self._get_account_leverage_info(
            self.config.maker_connector_pair.connector_name
        )
        taker_account = await self._get_account_leverage_info(
            self.config.taker_connector_pair.connector_name
        )

        # Calculate total leverage usage ratio (take the higher side)
        maker_usage = maker_account['leverage_usage']
        taker_usage = taker_account['leverage_usage']
        max_usage = max(maker_usage, taker_usage)

        # Prepare risk data
        risk_data = {
            'maker': maker_account,
            'taker': taker_account,
            'max_usage': max_usage,
            'critical_side': 'maker' if maker_usage > taker_usage else 'taker'
        }

        # Determine risk level
        if max_usage >= self.config.leverage_emergency_threshold:
            return (4, risk_data)  # Level 4: Emergency (95%+)
        elif max_usage >= self.config.leverage_critical_threshold:
            return (3, risk_data)  # Level 3: Critical (85-95%)
        elif max_usage >= self.config.leverage_caution_threshold:
            return (2, risk_data)  # Level 2: Caution (70-85%)
        elif max_usage >= self.config.leverage_warning_threshold:
            return (1, risk_data)  # Level 1: Warning (60-70%)
        else:
            return (0, risk_data)  # Level 0: Normal (<60%)

    async def _get_account_leverage_info(self, connector_name: str) -> dict:
        """
        Get leverage info for single account

        Returns:
            {
                'total_equity': Decimal,          # Account equity
                'total_position_value': Decimal,  # Total position value
                'actual_leverage': Decimal,       # Actual leverage
                'leverage_usage': Decimal,        # Leverage usage ratio
                'available_margin': Decimal,      # Available margin
            }
        """
        connector: DerivativeBase = self.connectors[connector_name]

        # Get account info (different for each exchange)
        # Example using Binance Perpetual structure
        if hasattr(connector, '_account_balances'):
            # Get USDT balance
            usdt_balance = connector._account_balances.get('USDT', Decimal("0"))
            total_equity = usdt_balance  # Account equity

            # Calculate total position value
            total_position_value = Decimal("0")
            if hasattr(connector, 'account_positions'):
                for position in connector.account_positions.values():
                    if position.amount != 0:
                        # Position value = |amount| * mark_price
                        mark_price = self.get_price(
                            connector_name,
                            position.trading_pair,
                            price_type=PriceType.MidPrice
                        )
                        position_value = abs(position.amount) * mark_price
                        total_position_value += position_value

            # Calculate actual leverage
            if total_equity > 0:
                actual_leverage = total_position_value / total_equity
            else:
                actual_leverage = Decimal("0")

            # Calculate leverage usage ratio
            if self.config.target_leverage > 0:
                leverage_usage = actual_leverage / self.config.target_leverage
            else:
                leverage_usage = Decimal("0")

            # Available margin
            available_margin = total_equity - (total_position_value / self.config.target_leverage)

            return {
                'connector': connector_name,
                'total_equity': total_equity,
                'total_position_value': total_position_value,
                'actual_leverage': actual_leverage,
                'leverage_usage': leverage_usage,
                'available_margin': available_margin,
            }

        # If failed to get info, return safe values
        return {
            'connector': connector_name,
            'total_equity': Decimal("0"),
            'total_position_value': Decimal("0"),
            'actual_leverage': Decimal("0"),
            'leverage_usage': Decimal("0"),
            'available_margin': Decimal("0"),
        }

    # ==================== Tiered Risk Handling ====================

    async def handle_leverage_risk(self, risk_level: int, risk_data: dict):
        """
        Handle leverage risk (tiered response)

        Level 1 (60-70%): Reduce order_amount
        Level 2 (70-85%): Further reduce + adjust Maker strategy
        Level 3 (85-95%): Stop new positions, close-only mode
        Level 4 (95%+):   Force reduce positions
        """
        if risk_level == 1:
            await self._handle_level1_warning(risk_data)
        elif risk_level == 2:
            await self._handle_level2_caution(risk_data)
        elif risk_level == 3:
            await self._handle_level3_critical(risk_data)
        elif risk_level == 4:
            await self._handle_level4_emergency(risk_data)

    # ========== Level 1: Warning (60-70%) ==========

    async def _handle_level1_warning(self, risk_data: dict):
        """
        Level 1: Primary risk control

        Actions:
        1. Reduce order_amount to 50%
        2. Send warning alert
        3. Continue normal operation
        """
        max_usage = risk_data['max_usage']

        self.logger().warning(f"Level 1 Warning: Leverage usage at {max_usage:.1%}")

        # Reduce order amount (if not already reduced)
        if self._degraded_order_amount is None:
            self._degraded_order_amount = self.config.order_amount * self.config.order_amount_reduction_step
            self.logger().info(
                f"Reducing order amount: {self.config.order_amount} -> {self._degraded_order_amount}"
            )

    # ========== Level 2: Caution (70-85%) ==========

    async def _handle_level2_caution(self, risk_data: dict):
        """
        Level 2: Intermediate risk control

        Actions:
        1. Further reduce order_amount (reduce by 50% again)
        2. Adjust Maker strategy: only place orders in less risky direction
        3. Send warning alert
        """
        max_usage = risk_data['max_usage']
        critical_side = risk_data['critical_side']

        self.logger().warning(
            f"Level 2 Caution: Leverage usage at {max_usage:.1%}, critical side: {critical_side}"
        )

        # Further reduce order amount
        if self._degraded_order_amount is None:
            self._degraded_order_amount = self.config.order_amount * Decimal("0.25")  # Reduce to 25%
        else:
            self._degraded_order_amount = self._degraded_order_amount * self.config.order_amount_reduction_step

        self.logger().info(f"Further reducing order amount to: {self._degraded_order_amount}")

        # Adjust Maker strategy: reduce position in direction of net exposure
        await self._adjust_maker_strategy_for_risk(risk_data)

    async def _adjust_maker_strategy_for_risk(self, risk_data: dict):
        """
        Adjust Maker strategy to control leverage

        Logic:
        - If current net position is long -> prefer sell orders (reduce position)
        - If current net position is short -> prefer buy orders (reduce position)
        """
        # Get net position
        maker_position = self.get_position_size(
            self.config.maker_connector_pair.connector_name,
            self.config.maker_connector_pair.trading_pair
        )
        taker_position = self.get_position_size(
            self.config.taker_connector_pair.connector_name,
            self.config.taker_connector_pair.trading_pair
        )
        net_position = maker_position + taker_position

        # Adjust strategy based on net position
        if abs(net_position) > self.config.order_amount:
            # Net position is significant, need adjustment
            if net_position > 0:
                # Net long -> if originally buy orders, pause
                if self.config.side == TradeType.BUY:
                    self.logger().info("Pausing BUY orders due to long net position")
                    if self._maker_order:
                        await self.cancel_maker_order()
                    return  # Don't place buy orders
            else:
                # Net short -> if originally sell orders, pause
                if self.config.side == TradeType.SELL:
                    self.logger().info("Pausing SELL orders due to short net position")
                    if self._maker_order:
                        await self.cancel_maker_order()
                    return  # Don't place sell orders

    # ========== Level 3: Critical (85-95%) ==========

    async def _handle_level3_critical(self, risk_data: dict):
        """
        Level 3: High-level risk control

        Actions:
        1. Stop all Maker orders
        2. Only allow closing positions (close-only mode)
        3. Send critical alert
        """
        max_usage = risk_data['max_usage']

        self.logger().error(f"Level 3 Critical: Leverage usage at {max_usage:.1%}!")

        # Cancel all Maker orders
        if self._maker_order:
            await self.cancel_maker_order()
            self.logger().info("Cancelled all Maker orders")

        # Mark as degraded mode: no new positions
        self._degraded_mode = True

        # Check and reduce position imbalance if exists
        await self._check_and_reduce_imbalance()

    async def _check_and_reduce_imbalance(self):
        """Check and reduce position imbalance"""
        net_position = await self.get_net_position()

        # If net position exceeds threshold, actively close part of it
        imbalance_threshold = self.config.order_amount * Decimal("2")

        if abs(net_position) > imbalance_threshold:
            # Close excess
            excess = abs(net_position) - imbalance_threshold
            close_side = TradeType.SELL if net_position > 0 else TradeType.BUY

            self.logger().warning(
                f"Reducing imbalanced position: {net_position} -> closing {excess}"
            )

            self.place_order(
                connector_name=self.config.taker_connector_pair.connector_name,
                trading_pair=self.config.taker_connector_pair.trading_pair,
                order_type=OrderType.MARKET,
                side=close_side,
                amount=excess,
                price=Decimal("NaN"),
                position_action=PositionAction.CLOSE
            )

    # ========== Level 4: Emergency (95%+) ==========

    async def _handle_level4_emergency(self, risk_data: dict):
        """
        Level 4: Emergency risk control

        Actions:
        1. Immediately force reduce positions (reduce 30% of total positions)
        2. Stop all Maker orders
        3. Send emergency alert
        4. Wait for manual intervention
        """
        max_usage = risk_data['max_usage']
        critical_side = risk_data['critical_side']

        self.logger().error(
            f"Level 4 EMERGENCY: Leverage usage at {max_usage:.1%}! Reducing positions now!"
        )

        # Cancel all orders
        if self._maker_order:
            await self.cancel_maker_order()

        # Force reduce positions (on the higher risk side)
        await self._emergency_reduce_position(critical_side, risk_data)

        # Enter degraded mode, wait for manual intervention
        self._degraded_mode = True

    async def _emergency_reduce_position(self, critical_side: str, risk_data: dict):
        """
        Emergency position reduction

        Strategy: Reduce 30% of position on the higher risk side
        """
        if critical_side == 'maker':
            connector_name = self.config.maker_connector_pair.connector_name
            trading_pair = self.config.maker_connector_pair.trading_pair
        else:
            connector_name = self.config.taker_connector_pair.connector_name
            trading_pair = self.config.taker_connector_pair.trading_pair

        # Get current position
        position_size = self.get_position_size(connector_name, trading_pair)

        if position_size != 0:
            # Reduce 30%
            reduce_amount = abs(position_size) * self.config.position_reduction_ratio
            close_side = TradeType.SELL if position_size > 0 else TradeType.BUY

            self.logger().error(
                f"Emergency reducing {critical_side} position by {self.config.position_reduction_ratio:.0%}: "
                f"{position_size} -> closing {reduce_amount}"
            )

            self.place_order(
                connector_name=connector_name,
                trading_pair=trading_pair,
                order_type=OrderType.MARKET,
                side=close_side,
                amount=reduce_amount,
                price=Decimal("NaN"),
                position_action=PositionAction.CLOSE
            )

            # Send emergency alert
            await self._send_emergency_alert(
                f"Emergency reduced {critical_side} position by {reduce_amount}. "
                f"Leverage usage was {risk_data['max_usage']:.1%}"
            )

    # ==================== Alert System ====================

    async def _send_risk_alert(self, risk_level: int, risk_data: dict):
        """
        Send risk alert (all levels)

        Alert content:
        - Risk level
        - Leverage usage
        - Actions taken
        - Account details
        """
        # Check cooldown
        current_time = self._strategy.current_timestamp
        if current_time - self._last_alert_time < self.config.alert_cooldown:
            return  # In cooldown, don't spam

        max_usage = risk_data['max_usage']
        critical_side = risk_data['critical_side']

        # Build alert message
        level_names = {
            0: "Normal",
            1: "Level 1 Warning",
            2: "Level 2 Caution",
            3: "Level 3 Critical",
            4: "Level 4 EMERGENCY"
        }

        level_actions = {
            0: "No action needed",
            1: f"Reduced order amount to {self._degraded_order_amount}",
            2: "Further reduced order amount + adjusted Maker strategy",
            3: "Stopped all Maker orders, close-only mode",
            4: f"Emergency reduced {critical_side} position by {self.config.position_reduction_ratio:.0%}"
        }

        message = f"""
XEMM Perpetual Risk Alert

Risk Level: {level_names[risk_level]}
Leverage Usage: {max_usage:.1%} (Target: {self.config.target_leverage}x)
Critical Side: {critical_side}

Action Taken: {level_actions[risk_level]}

Account Details:
- Maker: {risk_data['maker']['actual_leverage']:.2f}x (Equity: {risk_data['maker']['total_equity']:.2f} USDT)
- Taker: {risk_data['taker']['actual_leverage']:.2f}x (Equity: {risk_data['taker']['total_equity']:.2f} USDT)

Timestamp: {self._strategy.current_timestamp}
        """.strip()

        self.logger().info(f"Sending alert: {message}")

        # Send to webhook
        if self.config.alert_webhook_url:
            await self._send_webhook_alert(message)

        # Update last alert time
        self._last_alert_time = current_time

    async def _send_webhook_alert(self, message: str):
        """Send webhook alert (DingTalk/Slack/custom)"""
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "msgtype": "text",
                    "text": {"content": message}
                }
                async with session.post(
                    self.config.alert_webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status == 200:
                        self.logger().info("Alert sent successfully")
                    else:
                        self.logger().error(f"Alert failed: {response.status}")
        except Exception as e:
            self.logger().error(f"Failed to send alert: {e}")

    async def _send_emergency_alert(self, message: str):
        """Send emergency alert (immediate, no cooldown)"""
        emergency_message = f"""
EMERGENCY ALERT

{message}

Immediate action required!
Please check the account and add margin if needed.
        """.strip()

        if self.config.alert_webhook_url:
            await self._send_webhook_alert(emergency_message)

    # ==================== Shutdown Process ====================

    async def control_shutdown_process(self):
        """Shutdown process"""
        # Cancel Maker orders
        if self._maker_order:
            await self.cancel_maker_order()

        await self._sleep(5)
        self._status = RunnableStatus.TERMINATED

    # ==================== Other Methods ====================

    async def validate_sufficient_balance(self):
        """Validate sufficient margin for both sides"""
        # Use PerpetualOrderCandidate to check
        for connector_name, trading_pair, side, leverage in [
            (self.config.maker_connector_pair.connector_name,
             self.config.maker_connector_pair.trading_pair,
             self.config.side,
             self.config.maker_leverage),
            (self.config.taker_connector_pair.connector_name,
             self.config.taker_connector_pair.trading_pair,
             TradeType.SELL if self.config.side == TradeType.BUY else TradeType.BUY,
             self.config.taker_leverage)
        ]:
            price = self.get_price(connector_name, trading_pair)
            if price == 0:
                continue

            order_candidate = PerpetualOrderCandidate(
                trading_pair=trading_pair,
                is_maker=True,
                order_type=OrderType.LIMIT,
                order_side=side,
                amount=self.config.order_amount,
                price=price,
                leverage=Decimal(leverage)
            )

            adjusted = self.adjust_order_candidates(connector_name, [order_candidate])

            if adjusted[0].amount == 0:
                self.logger().error(f"Insufficient balance on {connector_name}")
                self.close_type = CloseType.INSUFFICIENT_BALANCE
                self._status = RunnableStatus.TERMINATED

    def get_custom_info(self) -> Dict:
        """Get custom info for display"""
        return {
            "maker_order": self._maker_order.order_id if self._maker_order else None,
            "taker_order": self._taker_order.order_id if self._taker_order else None,
            "maker_price": float(self._maker_price) if self._maker_price else None,
            "taker_price": float(self._taker_price) if self._taker_price else None,
            "risk_level": self._current_risk_level,
            "degraded_order_amount": float(self._degraded_order_amount) if self._degraded_order_amount else None,
            "degraded_mode": self._degraded_mode,
        }

    # ==================== Required ExecutorBase Methods ====================

    def early_stop(self, keep_position: bool = False):
        """
        Stop the executor early and cancel any open orders.

        For perpetual XEMM, we close any open maker orders but leave positions
        as they should be hedged on both sides.
        """
        # Cancel maker order if it's still open
        if self._maker_order and self._maker_order.order and self._maker_order.order.is_open:
            self.logger().info(f"Cancelling maker order {self._maker_order.order_id}")
            self._strategy.cancel(
                self.config.maker_connector_pair.connector_name,
                self.config.maker_connector_pair.trading_pair,
                self._maker_order.order_id
            )

        # Cancel taker order if somehow still open (shouldn't normally be)
        if self._taker_order and self._taker_order.order and self._taker_order.order.is_open:
            self.logger().info(f"Cancelling taker order {self._taker_order.order_id}")
            self._strategy.cancel(
                self.config.taker_connector_pair.connector_name,
                self.config.taker_connector_pair.trading_pair,
                self._taker_order.order_id
            )

        self.close_type = CloseType.EARLY_STOP
        self.stop()
        self.logger().info("XEMM Perpetual executor stopped early")

    def get_cum_fees_quote(self) -> Decimal:
        """
        Calculate cumulative fees in quote currency.

        Returns the sum of fees from both maker and taker orders.
        """
        total_fees = Decimal("0")

        if self._maker_order:
            total_fees += self._maker_order.cum_fees_quote

        if self._taker_order:
            total_fees += self._taker_order.cum_fees_quote

        return total_fees

    def get_net_pnl_quote(self) -> Decimal:
        """
        Calculate net PnL in quote currency.

        For XEMM Perpetual:
        - PnL = Taker order value - Maker order value - Fees
        - This represents the profit from the arbitrage after fees
        """
        if not (self._maker_order and self._taker_order):
            return Decimal("0")

        # Only calculate PnL if both orders are done
        if not (self._maker_order.is_done and self._taker_order.is_done):
            return Decimal("0")

        # Calculate order values
        maker_value = self._maker_order.executed_amount_base * self._maker_order.average_executed_price
        taker_value = self._taker_order.executed_amount_base * self._taker_order.average_executed_price

        # For perpetuals, the PnL calculation depends on the direction
        if self.config.side == TradeType.BUY:
            # Maker BUY, Taker SELL (hedge)
            # PnL = SELL price - BUY price
            pnl = taker_value - maker_value
        else:
            # Maker SELL, Taker BUY (hedge)
            # PnL = SELL price - BUY price
            pnl = maker_value - taker_value

        # Subtract fees
        net_pnl = pnl - self.get_cum_fees_quote()

        return net_pnl

    def get_net_pnl_pct(self) -> Decimal:
        """
        Calculate net PnL as a percentage.

        Returns PnL as a percentage of the total traded value.
        """
        if not (self._maker_order and self._maker_order.is_done):
            return Decimal("0")

        # Use maker order value as the base for percentage calculation
        maker_value = self._maker_order.executed_amount_base * self._maker_order.average_executed_price

        if maker_value == Decimal("0"):
            return Decimal("0")

        return self.get_net_pnl_quote() / maker_value
