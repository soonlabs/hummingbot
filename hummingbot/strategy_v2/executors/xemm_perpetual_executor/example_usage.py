"""
Example Usage of XEMMPerpetualExecutor

This is a demonstration of how to use the perpetual XEMM executor.
DO NOT run this directly - integrate it into your strategy instead.
"""

from decimal import Decimal

from hummingbot.core.data_type.common import PositionMode, TradeType
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.xemm_perpetual_executor.data_types import XEMMPerpetualExecutorConfig
from hummingbot.strategy_v2.executors.xemm_perpetual_executor.xemm_perpetual_executor import XEMMPerpetualExecutor


def create_example_config_buy():
    """Example configuration for BUY side"""
    config = XEMMPerpetualExecutorConfig(
        # ========== Trading Pairs ==========
        maker_connector_pair=ConnectorPair(
            connector_name="your_exchange_perpetual",  # Your exchange
            trading_pair="ETH-USDT"
        ),
        taker_connector_pair=ConnectorPair(
            connector_name="binance_perpetual",  # Binance for hedging
            trading_pair="ETH-USDT"
        ),
        
        # ========== Order Configuration ==========
        side=TradeType.BUY,  # Maker BUY, Taker SELL
        order_amount=Decimal("0.1"),  # 0.1 ETH per order
        min_profitability=Decimal("0.001"),  # 0.1% minimum profit
        max_profitability=Decimal("0.01"),  # 1% maximum profit (optional cap)
        slippage_buffer=Decimal("0.002"),  # 0.2% slippage buffer for Taker
        
        # ========== Leverage Configuration ==========
        maker_leverage=10,  # 10x leverage on your exchange
        taker_leverage=10,  # 10x leverage on Binance
        position_mode=PositionMode.HEDGE,  # Hedge mode (dual direction)
        
        # ========== Order Refresh ==========
        order_refresh_time=10.0,  # Refresh every 10 seconds
        order_refresh_tolerance_pct=Decimal("0.002"),  # Refresh if price moves 0.2%
        
        # ========== Risk Control (Account-Wide Leverage) ==========
        target_leverage=Decimal("10"),  # Target 10x leverage
        
        # Leverage usage thresholds
        leverage_warning_threshold=Decimal("0.60"),  # 60% -> Level 1
        leverage_caution_threshold=Decimal("0.70"),  # 70% -> Level 2
        leverage_critical_threshold=Decimal("0.85"),  # 85% -> Level 3
        leverage_emergency_threshold=Decimal("0.95"),  # 95% -> Level 4
        
        # Risk control parameters
        order_amount_reduction_step=Decimal("0.5"),  # Reduce by 50% each time
        position_reduction_ratio=Decimal("0.3"),  # Emergency reduce 30%
        
        # ========== Other ==========
        max_retries=3,  # Max retries for failed hedge orders
        
        # ========== Alerts (Optional) ==========
        alert_webhook_url="https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN",
        alert_cooldown=300.0,  # 5 minutes cooldown between alerts
    )
    
    return config


def create_example_config_sell():
    """Example configuration for SELL side"""
    config = XEMMPerpetualExecutorConfig(
        # ========== Trading Pairs ==========
        maker_connector_pair=ConnectorPair(
            connector_name="your_exchange_perpetual",
            trading_pair="ETH-USDT"
        ),
        taker_connector_pair=ConnectorPair(
            connector_name="binance_perpetual",
            trading_pair="ETH-USDT"
        ),
        
        # ========== Order Configuration ==========
        side=TradeType.SELL,  # Maker SELL, Taker BUY
        order_amount=Decimal("0.1"),
        min_profitability=Decimal("0.001"),
        
        # ========== Leverage Configuration ==========
        maker_leverage=10,
        taker_leverage=10,
        position_mode=PositionMode.HEDGE,
        
        # ========== Risk Control ==========
        target_leverage=Decimal("10"),
        leverage_warning_threshold=Decimal("0.60"),
        leverage_caution_threshold=Decimal("0.70"),
        leverage_critical_threshold=Decimal("0.85"),
        leverage_emergency_threshold=Decimal("0.95"),
    )
    
    return config


def example_usage_in_strategy():
    """
    Example of how to use in a Strategy V2
    
    In your strategy class:
    ```python
    from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction
    
    class MyXEMMPerpetualStrategy(StrategyV2Base):
        
        async def on_tick(self):
            # Create executor config
            config = create_example_config_buy()
            
            # Create executor action
            action = CreateExecutorAction(
                controller_id="xemm_perp",
                executor_config=config
            )
            
            # Send to orchestrator
            self.executor_orchestrator.execute_action(action)
    ```
    """
    pass


# ==================== Risk Level Explanation ====================

RISK_LEVELS_EXPLAINED = """
Risk Control Tiers:

Level 0 (Normal, <60% leverage usage):
    ✅ Normal operation
    ✅ Full order amount
    ✅ No restrictions

Level 1 (Warning, 60-70%):
    ⚠️  Reduce order amount to 50%
    ⚠️  Send warning alert
    ✅ Continue normal operation

Level 2 (Caution, 70-85%):
    ⚠️  Further reduce order amount (to 25%)
    ⚠️  Adjust Maker strategy based on net position
        - If net long: pause BUY orders
        - If net short: pause SELL orders
    ⚠️  Send caution alert

Level 3 (Critical, 85-95%):
    🚨 Stop all Maker orders
    🚨 Close-only mode (no new positions)
    🚨 Reduce position imbalance if > threshold
    🚨 Send critical alert

Level 4 (Emergency, >95%):
    🔥 Immediately cancel all orders
    🔥 Force reduce 30% of position on critical side
    🔥 Stop all trading
    🔥 Wait for manual intervention
    🔥 Send emergency alert

Leverage Usage Calculation:
    leverage_usage = actual_leverage / target_leverage
    
    Where:
        actual_leverage = total_position_value / account_equity
        
    Example:
        Account equity: $1000
        Position value: $6000
        Target leverage: 10x
        
        actual_leverage = 6000 / 1000 = 6x
        leverage_usage = 6 / 10 = 60% -> Level 1 Warning
"""


if __name__ == "__main__":
    # Example: Print configuration
    config_buy = create_example_config_buy()
    config_sell = create_example_config_sell()
    
    print("=" * 60)
    print("XEMM Perpetual Executor Configuration Examples")
    print("=" * 60)
    
    print("\n[BUY Side Configuration]")
    print(f"Maker: {config_buy.maker_connector_pair.connector_name}")
    print(f"Taker: {config_buy.taker_connector_pair.connector_name}")
    print(f"Side: {config_buy.side.name}")
    print(f"Order Amount: {config_buy.order_amount} ETH")
    print(f"Min Profitability: {config_buy.min_profitability * 100}%")
    print(f"Leverage: {config_buy.maker_leverage}x (Maker) / {config_buy.taker_leverage}x (Taker)")
    print(f"Target Leverage: {config_buy.target_leverage}x")
    
    print("\n[Risk Control Thresholds]")
    print(f"Warning:  {config_buy.leverage_warning_threshold * 100}%")
    print(f"Caution:  {config_buy.leverage_caution_threshold * 100}%")
    print(f"Critical: {config_buy.leverage_critical_threshold * 100}%")
    print(f"Emergency: {config_buy.leverage_emergency_threshold * 100}%")
    
    print("\n" + RISK_LEVELS_EXPLAINED)
    
    print("\n" + "=" * 60)
    print("NOTE: This is just an example. Integrate into your strategy.")
    print("=" * 60)

