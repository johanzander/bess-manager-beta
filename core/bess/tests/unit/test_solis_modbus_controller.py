"""Behavioral tests for Solis Modbus schedule control."""

from unittest.mock import MagicMock

import pytest

from core.bess.settings import BatterySettings
from core.bess.solis_modbus_controller import SolisModbusController


def make_intents(hourly: dict[int, str], default: str = "IDLE") -> list[str]:
    quarterly = [default] * 96
    for hour, intent in hourly.items():
        for period in range(hour * 4, (hour + 1) * 4):
            quarterly[period] = intent
    return quarterly


def make_schedule_mock(intents: list[str]) -> MagicMock:
    schedule = MagicMock()
    schedule.original_dp_results = {"strategic_intent": intents}
    schedule.actions = [0.0] * len(intents)
    return schedule


@pytest.fixture
def controller() -> SolisModbusController:
    settings = BatterySettings(
        total_capacity=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        min_soc=15.0,
        max_soc=95.0,
    )
    return SolisModbusController(settings)


def test_grid_charging_writes_solis_charge_period(
    controller: SolisModbusController,
) -> None:
    controller.create_schedule(make_schedule_mock(make_intents({2: "GRID_CHARGING"})))

    ha = MagicMock()
    writes, disables = controller.write_schedule_to_hardware(ha, 0, [])

    assert writes == 2
    assert disables == 0
    ha.write_solis_tou_schedule.assert_called_once()
    kwargs = ha.write_solis_tou_schedule.call_args.kwargs
    assert kwargs["charge_periods"] == [
        {"start_time": "02:00", "end_time": "02:59", "enabled": True}
    ]
    assert kwargs["discharge_periods"] == []
    assert kwargs["charge_stop_soc"] == 95


def test_battery_export_writes_solis_discharge_period(
    controller: SolisModbusController,
) -> None:
    controller.create_schedule(make_schedule_mock(make_intents({18: "BATTERY_EXPORT"})))

    ha = MagicMock()
    controller.write_schedule_to_hardware(ha, 0, [])

    kwargs = ha.write_solis_tou_schedule.call_args.kwargs
    assert kwargs["charge_periods"] == []
    assert kwargs["discharge_periods"] == [
        {"start_time": "18:00", "end_time": "18:59", "enabled": True}
    ]
    assert kwargs["discharge_stop_soc"] == 15


def test_apply_period_updates_grid_charge_and_discharge_current(
    controller: SolisModbusController,
) -> None:
    ha = MagicMock()

    success, error = controller.apply_period(ha, grid_charge=False, discharge_rate=50)

    assert success is True
    assert error == ""
    ha.set_grid_charge.assert_called_once_with(False)
    ha.set_solis_discharge_rate.assert_called_once_with(50)
