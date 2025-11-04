import os
from decimal import Decimal
from typing import Dict, List, Optional, Set

from hummingbot.client.hummingbot_application import HummingbotApplication
from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.event.events import MarketOrderFailureEvent
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy.strategy_v2_base import StrategyV2Base, StrategyV2ConfigBase
from hummingbot.strategy_v2.models.base import RunnableStatus
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction


class V2WithControllersConfig(StrategyV2ConfigBase):
    script_file_name: str = os.path.basename(__file__)
    candles_config: List[CandlesConfig] = []
    markets: Dict[str, Set[str]] = {}
    max_global_drawdown_quote: Optional[float] = None
    max_controller_drawdown_quote: Optional[float] = None


class V2WithControllers(StrategyV2Base):
    """
    This script runs a generic strategy with cash out feature. Will also check if the controllers configs have been
    updated and apply the new settings.
    The cash out of the script can be set by the time_to_cash_out parameter in the config file. If set, the script will
    stop the controllers after the specified time has passed, and wait until the active executors finalize their
    execution.
    The controllers will also have a parameter to manually cash out. In that scenario, the main strategy will stop the
    specific controller and wait until the active executors finalize their execution. The rest of the executors will
    wait until the main strategy stops them.
    """
    performance_report_interval: int = 1

    def __init__(self, connectors: Dict[str, ConnectorBase], config: V2WithControllersConfig):
        super().__init__(connectors, config)
        self.config = config
        self.max_pnl_by_controller = {}
        self.max_global_pnl = Decimal("0")
        self.drawdown_exited_controllers = []
        self.closed_executors_buffer: int = 30
        self._last_performance_report_timestamp = 0

    def on_tick(self):
        super().on_tick()
        if not self._is_stop_triggered:
            # Log tick activity every 10 seconds
            if int(self.current_timestamp) % 10 == 0:
                active_executors = self.filter_executors(
                    executors=self.get_all_executors(),
                    filter_func=lambda executor: executor.status == RunnableStatus.RUNNING
                )
                self.logger().info(f"[Strategy Tick] Controllers: {len(self.controllers)}, Active Executors: {len(active_executors)}")
                for controller_id, controller in self.controllers.items():
                    perf = self.get_performance_report(controller_id)
                    self.logger().info(
                        f"[Controller {controller_id}] Status: {controller.status.name}, "
                        f"PnL: {perf.global_pnl_quote:.2f}, "
                        f"Active Executors: {len(self.get_executors_by_controller(controller_id))}"
                    )

            self.check_manual_kill_switch()
            self.control_max_drawdown()
            self.send_performance_report()

    def control_max_drawdown(self):
        if self.config.max_controller_drawdown_quote:
            self.check_max_controller_drawdown()
        if self.config.max_global_drawdown_quote:
            self.check_max_global_drawdown()

    def check_max_controller_drawdown(self):
        for controller_id, controller in self.controllers.items():
            if controller.status != RunnableStatus.RUNNING:
                continue
            controller_pnl = self.get_performance_report(controller_id).global_pnl_quote
            last_max_pnl = self.max_pnl_by_controller[controller_id]
            if controller_pnl > last_max_pnl:
                self.max_pnl_by_controller[controller_id] = controller_pnl
                self.logger().info(f"[Drawdown Check] Controller {controller_id} new max PnL: {controller_pnl:.2f}")
            else:
                current_drawdown = last_max_pnl - controller_pnl
                if current_drawdown > self.config.max_controller_drawdown_quote:
                    self.logger().warning(f"[Drawdown Alert] Controller {controller_id} reached max drawdown: {current_drawdown:.2f} > {self.config.max_controller_drawdown_quote}. Stopping the controller.")
                    controller.stop()
                    executors_order_placed = self.filter_executors(
                        executors=self.get_executors_by_controller(controller_id),
                        filter_func=lambda x: x.is_active and not x.is_trading,
                    )
                    self.executor_orchestrator.execute_actions(
                        actions=[StopExecutorAction(controller_id=controller_id, executor_id=executor.id) for executor in executors_order_placed]
                    )
                    self.drawdown_exited_controllers.append(controller_id)

    def check_max_global_drawdown(self):
        current_global_pnl = sum([self.get_performance_report(controller_id).global_pnl_quote for controller_id in self.controllers.keys()])
        if current_global_pnl > self.max_global_pnl:
            self.max_global_pnl = current_global_pnl
            if int(self.current_timestamp) % 30 == 0:  # Log every 30 seconds
                self.logger().info(f"[Global PnL] New max: {self.max_global_pnl:.2f}")
        else:
            current_global_drawdown = self.max_global_pnl - current_global_pnl
            if current_global_drawdown > self.config.max_global_drawdown_quote:
                self.drawdown_exited_controllers.extend(list(self.controllers.keys()))
                self.logger().warning(f"[Global Drawdown Alert] Reached max drawdown: {current_global_drawdown:.2f} > {self.config.max_global_drawdown_quote}. Stopping the strategy.")
                self._is_stop_triggered = True
                HummingbotApplication.main_application().stop()

    def send_performance_report(self):
        if self.current_timestamp - self._last_performance_report_timestamp >= self.performance_report_interval and self._pub:
            performance_reports = {controller_id: self.get_performance_report(controller_id).dict() for controller_id in self.controllers.keys()}
            self._pub(performance_reports)
            self._last_performance_report_timestamp = self.current_timestamp

    def check_manual_kill_switch(self):
        for controller_id, controller in self.controllers.items():
            if controller.config.manual_kill_switch and controller.status == RunnableStatus.RUNNING:
                self.logger().warning(f"[Manual Kill Switch] Stopping controller {controller_id}.")
                controller.stop()
                executors_to_stop = self.get_executors_by_controller(controller_id)
                self.logger().info(f"[Manual Kill Switch] Stopping {len(executors_to_stop)} executors for controller {controller_id}")
                self.executor_orchestrator.execute_actions(
                    [StopExecutorAction(executor_id=executor.id,
                                        controller_id=executor.controller_id) for executor in executors_to_stop])
            if not controller.config.manual_kill_switch and controller.status == RunnableStatus.TERMINATED:
                if controller_id in self.drawdown_exited_controllers:
                    continue
                self.logger().info(f"[Controller Restart] Restarting controller {controller_id}.")
                controller.start()

    def check_executors_status(self):
        active_executors = self.filter_executors(
            executors=self.get_all_executors(),
            filter_func=lambda executor: executor.status == RunnableStatus.RUNNING
        )
        if not active_executors:
            self.logger().info("All executors have finalized their execution. Stopping the strategy.")
            HummingbotApplication.main_application().stop()
        else:
            non_trading_executors = self.filter_executors(
                executors=active_executors,
                filter_func=lambda executor: not executor.is_trading
            )
            self.executor_orchestrator.execute_actions(
                [StopExecutorAction(executor_id=executor.id,
                                    controller_id=executor.controller_id) for executor in non_trading_executors])

    def create_actions_proposal(self) -> List[CreateExecutorAction]:
        return []

    def stop_actions_proposal(self) -> List[StopExecutorAction]:
        return []

    def apply_initial_setting(self):
        self.logger().info("[Initial Setup] Applying initial settings...")
        connectors_position_mode = {}
        for controller_id, controller in self.controllers.items():
            self.max_pnl_by_controller[controller_id] = Decimal("0")
            config_dict = controller.config.model_dump()
            self.logger().info(f"[Initial Setup] Controller {controller_id}: {config_dict.get('connector_name', 'Unknown')} - {config_dict.get('trading_pair', 'Unknown')}")
            if "connector_name" in config_dict:
                if self.is_perpetual(config_dict["connector_name"]):
                    if "position_mode" in config_dict:
                        connectors_position_mode[config_dict["connector_name"]] = config_dict["position_mode"]
                        self.logger().info(f"[Initial Setup] Setting position mode for {config_dict['connector_name']}: {config_dict['position_mode']}")
                    if "leverage" in config_dict:
                        self.logger().info(f"[Initial Setup] Setting leverage for {config_dict['connector_name']} {config_dict['trading_pair']}: {config_dict['leverage']}x")
                        self.connectors[config_dict["connector_name"]].set_leverage(leverage=config_dict["leverage"],
                                                                                    trading_pair=config_dict["trading_pair"])
        for connector_name, position_mode in connectors_position_mode.items():
            connector = self.connectors[connector_name]
            # Check if already in desired mode to avoid unnecessary API calls
            if hasattr(connector, 'position_mode') and connector.position_mode == position_mode:
                self.logger().info(f"[Initial Setup] {connector_name} is already in {position_mode} mode. Skipping.")
            else:
                self.logger().info(f"[Initial Setup] Setting {connector_name} position mode to {position_mode}.")
                connector.set_position_mode(position_mode)
        self.logger().info("[Initial Setup] Initial settings applied successfully!")

    def did_fail_order(self, order_failed_event: MarketOrderFailureEvent):
        """
        Handle order failure events by logging the error and stopping the strategy if necessary.
        """
        if "position side" in order_failed_event.error_message.lower():
            connectors_position_mode = {}
            for controller_id, controller in self.controllers.items():
                config_dict = controller.config.model_dump()
                if "connector_name" in config_dict:
                    if self.is_perpetual(config_dict["connector_name"]):
                        if "position_mode" in config_dict:
                            connectors_position_mode[config_dict["connector_name"]] = config_dict["position_mode"]
            for connector_name, position_mode in connectors_position_mode.items():
                connector = self.connectors[connector_name]
                # Check if already in desired mode before retrying
                if hasattr(connector, 'position_mode') and connector.position_mode == position_mode:
                    self.logger().info(f"[Error Recovery] {connector_name} is already in {position_mode} mode.")
                else:
                    self.logger().info(f"[Error Recovery] Retrying to set {connector_name} position mode to {position_mode}.")
                    connector.set_position_mode(position_mode)
