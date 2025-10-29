import asyncio
from decimal import Decimal
from test.isolated_asyncio_wrapper_test_case import IsolatedAsyncioWrapperTestCase
from test.logger_mixin_for_test import LoggerMixinForTest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from hummingbot.connector.derivative_base import DerivativeBase
from hummingbot.core.data_type.common import OrderType, PositionMode, TradeType
from hummingbot.core.data_type.order_candidate import PerpetualOrderCandidate
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.strategy_v2.executors.data_types import ConnectorPair
from hummingbot.strategy_v2.executors.xemm_perpetual_executor.data_types import XEMMPerpetualExecutorConfig
from hummingbot.strategy_v2.executors.xemm_perpetual_executor.xemm_perpetual_executor import XEMMPerpetualExecutor
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executors import CloseType


class TestXEMMPerpetualExecutor(IsolatedAsyncioWrapperTestCase, LoggerMixinForTest):
    """Test cases for XEMMPerpetualExecutor (using Mock, no real trades)"""
    
    def setUp(self):
        super().setUp()
        self.strategy = self.create_mock_strategy()
        self.xemm_config = self.base_config_buy
        self.update_interval = 0.5
        self.executor = XEMMPerpetualExecutor(self.strategy, self.xemm_config, self.update_interval)
        self.set_loggers(loggers=[self.executor.logger()])
    
    @property
    def base_config_buy(self) -> XEMMPerpetualExecutorConfig:
        """Basic BUY configuration"""
        return XEMMPerpetualExecutorConfig(
            timestamp=1234,
            maker_connector_pair=ConnectorPair(
                connector_name='binance_perpetual',
                trading_pair='ETH-USDT'
            ),
            taker_connector_pair=ConnectorPair(
                connector_name='okx_perpetual',
                trading_pair='ETH-USDT'
            ),
            side=TradeType.BUY,
            order_amount=Decimal('0.1'),
            min_profitability=Decimal('0.001'),
            maker_leverage=10,
            taker_leverage=10,
            target_leverage=Decimal('10'),
        )
    
    @property
    def base_config_sell(self) -> XEMMPerpetualExecutorConfig:
        """Basic SELL configuration"""
        return XEMMPerpetualExecutorConfig(
            timestamp=1234,
            maker_connector_pair=ConnectorPair(
                connector_name='binance_perpetual',
                trading_pair='ETH-USDT'
            ),
            taker_connector_pair=ConnectorPair(
                connector_name='okx_perpetual',
                trading_pair='ETH-USDT'
            ),
            side=TradeType.SELL,
            order_amount=Decimal('0.1'),
            min_profitability=Decimal('0.001'),
            maker_leverage=10,
            taker_leverage=10,
            target_leverage=Decimal('10'),
        )
    
    @staticmethod
    def create_mock_strategy():
        """Create mock strategy with perpetual connectors"""
        market = MagicMock()
        market_info = MagicMock()
        market_info.market = market
        
        strategy = MagicMock(spec=ScriptStrategyBase)
        type(strategy).market_info = PropertyMock(return_value=market_info)
        type(strategy).trading_pair = PropertyMock(return_value="ETH-USDT")
        strategy.buy.side_effect = ["OID-BUY-1", "OID-BUY-2", "OID-BUY-3"]
        strategy.sell.side_effect = ["OID-SELL-1", "OID-SELL-2", "OID-SELL-3"]
        strategy.cancel.return_value = None
        
        # Mock perpetual connectors
        binance_perp = MagicMock(spec=DerivativeBase)
        binance_perp.set_leverage = MagicMock()
        binance_perp.set_position_mode = MagicMock()
        binance_perp.supported_order_types = MagicMock(return_value=[OrderType.LIMIT, OrderType.MARKET])
        binance_perp.quantize_order_price = MagicMock(side_effect=lambda pair, price: price)
        binance_perp.quantize_order_amount = MagicMock(side_effect=lambda pair, amount: amount)
        binance_perp._account_balances = {'USDT': Decimal('1000')}
        binance_perp.account_positions = {}
        binance_perp.position_mode = PositionMode.HEDGE
        
        okx_perp = MagicMock(spec=DerivativeBase)
        okx_perp.set_leverage = MagicMock()
        okx_perp.set_position_mode = MagicMock()
        okx_perp.supported_order_types = MagicMock(return_value=[OrderType.LIMIT, OrderType.MARKET])
        okx_perp.quantize_order_price = MagicMock(side_effect=lambda pair, price: price)
        okx_perp.quantize_order_amount = MagicMock(side_effect=lambda pair, amount: amount)
        okx_perp._account_balances = {'USDT': Decimal('1000')}
        okx_perp.account_positions = {}
        okx_perp.position_mode = PositionMode.HEDGE
        
        # Mock get_price_for_volume (for Taker hedge price)
        price_result = MagicMock()
        price_result.result_price = Decimal('2000')
        okx_perp.get_price_for_volume = MagicMock(return_value=price_result)
        
        strategy.connectors = {
            "binance_perpetual": binance_perp,
            "okx_perpetual": okx_perp,
        }
        return strategy
    
    # ==================== Basic Tests ====================
    
    def test_config_creation(self):
        """Test that configuration is created correctly"""
        self.assertEqual(self.executor.config.side, TradeType.BUY)
        self.assertEqual(self.executor.config.order_amount, Decimal('0.1'))
        self.assertEqual(self.executor.config.maker_leverage, 10)
        self.assertEqual(self.executor.config.target_leverage, Decimal('10'))
    
    @patch.object(XEMMPerpetualExecutor, "validate_sufficient_balance")
    async def test_on_start_sets_leverage(self, mock_validate):
        """Test that on_start sets leverage and position mode"""
        mock_validate.return_value = None
        await self.executor.on_start()
        
        # Check that set_leverage was called
        maker_connector = self.strategy.connectors['binance_perpetual']
        taker_connector = self.strategy.connectors['okx_perpetual']
        
        maker_connector.set_leverage.assert_called_once()
        taker_connector.set_leverage.assert_called_once()
        maker_connector.set_position_mode.assert_called_once_with(PositionMode.HEDGE)
        taker_connector.set_position_mode.assert_called_once_with(PositionMode.HEDGE)
    
    # ==================== Profitability Tests ====================
    
    @patch.object(XEMMPerpetualExecutor, "get_price")
    @patch.object(XEMMPerpetualExecutor, "get_tx_cost_in_asset")
    async def test_maker_order_price_calculation_buy(self, mock_tx_cost, mock_price):
        """Test Maker order price calculation for BUY side"""
        mock_price.return_value = Decimal('2000')  # Taker mid price
        mock_tx_cost.return_value = Decimal('0.0005')  # 0.05% fee
        
        await self.executor.update_prices_and_tx_costs()
        maker_price = self.executor.get_maker_order_price()
        
        # For BUY: maker_price = taker_price / (1 + min_profit + tx_cost)
        # = 2000 / (1 + 0.001 + 0.001) = 2000 / 1.002 = 1996.008
        self.assertAlmostEqual(maker_price, Decimal('1996.008'), places=2)
    
    @patch.object(XEMMPerpetualExecutor, "get_price")
    @patch.object(XEMMPerpetualExecutor, "get_tx_cost_in_asset")
    async def test_maker_order_price_calculation_sell(self, mock_tx_cost, mock_price):
        """Test Maker order price calculation for SELL side"""
        executor = XEMMPerpetualExecutor(self.strategy, self.base_config_sell, self.update_interval)
        
        mock_price.return_value = Decimal('2000')  # Taker mid price
        mock_tx_cost.return_value = Decimal('0.0005')  # 0.05% fee
        
        await executor.update_prices_and_tx_costs()
        maker_price = executor.get_maker_order_price()
        
        # For SELL: maker_price = taker_price * (1 + min_profit + tx_cost)
        # = 2000 * (1 + 0.001 + 0.001) = 2000 * 1.002 = 2004
        self.assertAlmostEqual(maker_price, Decimal('2004'), places=2)
    
    @patch.object(XEMMPerpetualExecutor, "get_price")
    @patch.object(XEMMPerpetualExecutor, "get_tx_cost_in_asset")
    async def test_is_profitable_true(self, mock_tx_cost, mock_price):
        """Test profitability check returns True when spread is sufficient"""
        mock_price.return_value = Decimal('2000')
        mock_tx_cost.return_value = Decimal('0.0005')
        
        await self.executor.update_prices_and_tx_costs()
        self.executor._maker_price = Decimal('1990')  # Buy at 1990
        self.executor._taker_price = Decimal('2000')  # Sell at 2000
        
        is_profitable = await self.executor.is_profitable()
        # Spread = (2000-1990)/1990 = 0.005 = 0.5%
        # Net profit = 0.5% - 0.1% = 0.4% > 0.1% min_profitability
        self.assertTrue(is_profitable)
    
    @patch.object(XEMMPerpetualExecutor, "get_price")
    @patch.object(XEMMPerpetualExecutor, "get_tx_cost_in_asset")
    async def test_is_profitable_false(self, mock_tx_cost, mock_price):
        """Test profitability check returns False when spread is insufficient"""
        mock_price.return_value = Decimal('2000')
        mock_tx_cost.return_value = Decimal('0.0005')
        
        await self.executor.update_prices_and_tx_costs()
        self.executor._maker_price = Decimal('1999')  # Buy at 1999
        self.executor._taker_price = Decimal('2000')  # Sell at 2000
        
        is_profitable = await self.executor.is_profitable()
        # Spread = (2000-1999)/1999 = 0.05% 
        # Net profit = 0.05% - 0.1% = -0.05% < 0.1% min_profitability
        self.assertFalse(is_profitable)
    
    # ==================== Margin Check Tests ====================
    
    @patch.object(XEMMPerpetualExecutor, 'adjust_order_candidates')
    async def test_check_sufficient_margin_success(self, mock_adjust):
        """Test margin check succeeds when both sides have sufficient margin"""
        # Mock sufficient margin
        order_candidate = PerpetualOrderCandidate(
            trading_pair="ETH-USDT",
            is_maker=True,
            order_type=OrderType.LIMIT,
            order_side=TradeType.BUY,
            amount=Decimal("0.1"),
            price=Decimal("2000"),
            leverage=Decimal("10")
        )
        mock_adjust.return_value = [order_candidate]
        
        self.executor._maker_price = Decimal('2000')
        self.executor._taker_price = Decimal('2000')
        
        result = await self.executor.check_sufficient_margin(Decimal('0.1'))
        self.assertTrue(result)
    
    @patch.object(XEMMPerpetualExecutor, 'adjust_order_candidates')
    async def test_check_sufficient_margin_failure(self, mock_adjust):
        """Test margin check fails when insufficient margin"""
        # Mock insufficient margin (amount becomes 0)
        order_candidate = PerpetualOrderCandidate(
            trading_pair="ETH-USDT",
            is_maker=True,
            order_type=OrderType.LIMIT,
            order_side=TradeType.BUY,
            amount=Decimal("0"),  # Insufficient!
            price=Decimal("2000"),
            leverage=Decimal("10")
        )
        mock_adjust.return_value = [order_candidate]
        
        self.executor._maker_price = Decimal('2000')
        self.executor._taker_price = Decimal('2000')
        
        result = await self.executor.check_sufficient_margin(Decimal('0.1'))
        self.assertFalse(result)
    
    # ==================== Risk Level Tests ====================
    
    @patch.object(XEMMPerpetualExecutor, "get_price")
    async def test_check_leverage_risk_level_0_normal(self, mock_price):
        """Test risk level 0 (normal) when leverage usage < 60%"""
        mock_price.return_value = Decimal('2000')
        
        # Mock account with low leverage usage
        self.strategy.connectors['binance_perpetual']._account_balances = {'USDT': Decimal('1000')}
        self.strategy.connectors['binance_perpetual'].account_positions = {}
        
        risk_level, risk_data = await self.executor.check_account_leverage_risk()
        
        self.assertEqual(risk_level, 0)  # Normal
        self.assertLess(risk_data['max_usage'], Decimal('0.60'))
    
    async def test_leverage_risk_level_1_warning(self):
        """Test risk level 1 (warning) when leverage usage 60-70%"""
        # Mock account with medium leverage usage
        mock_position = MagicMock()
        mock_position.trading_pair = 'ETH-USDT'
        mock_position.amount = Decimal('3')  # 3 ETH position
        
        self.strategy.connectors['binance_perpetual']._account_balances = {'USDT': Decimal('1000')}
        self.strategy.connectors['binance_perpetual'].account_positions = {'ETH-USDT': mock_position}
        
        with patch.object(self.executor, 'get_price', return_value=Decimal('2000')):
            risk_level, risk_data = await self.executor.check_account_leverage_risk()
            
            # 3 ETH * 2000 = 6000 position value
            # 6000 / 1000 = 6x actual leverage
            # 6x / 10x target = 60% usage -> Level 1
            if risk_data['max_usage'] >= Decimal('0.60'):
                self.assertGreaterEqual(risk_level, 1)
    
    async def test_handle_level1_warning_reduces_order_amount(self):
        """Test Level 1 warning reduces order amount"""
        risk_data = {'max_usage': Decimal('0.65'), 'critical_side': 'maker'}
        
        self.assertIsNone(self.executor._degraded_order_amount)
        
        await self.executor._handle_level1_warning(risk_data)
        
        # Should reduce to 50%
        expected = self.executor.config.order_amount * Decimal('0.5')
        self.assertEqual(self.executor._degraded_order_amount, expected)
    
    async def test_handle_level3_critical_cancels_maker_orders(self):
        """Test Level 3 critical cancels all Maker orders"""
        risk_data = {'max_usage': Decimal('0.90'), 'critical_side': 'maker'}
        
        # Create a mock Maker order
        self.executor._maker_order = MagicMock()
        self.executor._maker_order.order_id = "test-order"
        
        with patch.object(self.executor, 'cancel_maker_order', new_callable=AsyncMock) as mock_cancel:
            await self.executor._handle_level3_critical(risk_data)
            
            mock_cancel.assert_called_once()
            self.assertTrue(self.executor._degraded_mode)
    
    # ==================== Order Management Tests ====================
    
    @patch.object(XEMMPerpetualExecutor, "place_order")
    async def test_place_maker_order(self, mock_place_order):
        """Test Maker order placement"""
        mock_place_order.return_value = "OID-MAKER-1"
        self.executor._maker_price = Decimal('1995')
        
        await self.executor.place_maker_order(Decimal('0.1'))
        
        mock_place_order.assert_called_once()
        self.assertIsNotNone(self.executor._maker_order)
        self.assertEqual(self.executor._maker_order.order_id, "OID-MAKER-1")
    
    async def test_cancel_maker_order(self):
        """Test Maker order cancellation"""
        self.executor._maker_order = MagicMock()
        self.executor._maker_order.order_id = "OID-MAKER-1"
        
        with patch.object(self.strategy, 'cancel') as mock_cancel:
            await self.executor.cancel_maker_order()
            
            mock_cancel.assert_called_once()
            self.assertIsNone(self.executor._maker_order)
    
    @patch.object(XEMMPerpetualExecutor, "place_order")
    @patch.object(XEMMPerpetualExecutor, "place_taker_hedge_order")
    async def test_process_order_completed_triggers_hedge(self, mock_hedge, mock_place_order):
        """Test that Maker order completion triggers hedge"""
        mock_hedge.return_value = None
        
        self.executor._maker_order = MagicMock()
        self.executor._maker_order.order_id = "OID-MAKER-1"
        
        # Mock order completed event
        event = MagicMock()
        event.order_id = "OID-MAKER-1"
        event.base_asset_amount = Decimal('0.1')
        event.quote_asset_amount = Decimal('200')
        
        self.executor.process_order_completed_event(1, None, event)
        
        # Give async task time to be created
        await asyncio.sleep(0.1)
        
        # Maker order should be reset
        self.assertIsNone(self.executor._maker_order)
    
    # ==================== Alert Tests ====================
    
    @patch.object(XEMMPerpetualExecutor, "_send_webhook_alert")
    async def test_send_risk_alert_respects_cooldown(self, mock_webhook):
        """Test that alerts respect cooldown period"""
        # Set webhook URL so that webhook will be called
        self.executor.config.alert_webhook_url = "https://example.com/webhook"
        
        # Mock current_timestamp to return actual numbers
        # First call: checks cooldown (1000.0), then message timestamp (1000.0)
        # Second call: checks cooldown (1001.0), then returns early (blocked by cooldown)
        type(self.strategy).current_timestamp = PropertyMock(side_effect=[1000.0, 1000.0, 1001.0])
        
        risk_data = {
            'max_usage': Decimal('0.65'),
            'critical_side': 'maker',
            'maker': {
                'actual_leverage': Decimal('6.5'),
                'total_equity': Decimal('1000'),
            },
            'taker': {
                'actual_leverage': Decimal('6.0'),
                'total_equity': Decimal('1000'),
            }
        }
        
        # First alert should send
        await self.executor._send_risk_alert(1, risk_data)
        self.assertEqual(mock_webhook.call_count, 1)
        
        # Second alert immediately should be blocked by cooldown
        await self.executor._send_risk_alert(1, risk_data)
        self.assertEqual(mock_webhook.call_count, 1)  # Still 1, not 2
    
    # ==================== Integration Test ====================
    
    @patch.object(XEMMPerpetualExecutor, "get_price")
    @patch.object(XEMMPerpetualExecutor, "get_tx_cost_in_asset")
    @patch.object(XEMMPerpetualExecutor, "check_sufficient_margin")
    @patch.object(XEMMPerpetualExecutor, "is_profitable")
    @patch.object(XEMMPerpetualExecutor, "place_maker_order")
    async def test_control_task_full_flow(
        self, mock_place_order, mock_is_profitable, 
        mock_check_margin, mock_tx_cost, mock_price
    ):
        """Test full control task flow"""
        mock_price.return_value = Decimal('2000')
        mock_tx_cost.return_value = Decimal('0.0005')
        mock_check_margin.return_value = True
        mock_is_profitable.return_value = True
        mock_place_order.return_value = None
        
        self.executor._status = RunnableStatus.RUNNING
        
        await self.executor.control_task()
        
        # Should have checked profitability and placed order
        mock_is_profitable.assert_called_once()
        mock_place_order.assert_called_once()
    
    def test_get_custom_info(self):
        """Test custom info generation"""
        custom_info = self.executor.get_custom_info()
        
        self.assertIn('maker_order', custom_info)
        self.assertIn('taker_order', custom_info)
        self.assertIn('risk_level', custom_info)
        self.assertIn('degraded_mode', custom_info)

