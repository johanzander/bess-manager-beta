"""Solis inverter controller using Pho3niX90/solis_modbus entities.

Solis hybrid inverters expose separate grid time-of-use charge and discharge
periods through Home Assistant ``time``, ``number`` and ``switch`` entities.
This controller maps BESS strategic intents onto those persistent Modbus slots.
"""

import logging
from datetime import datetime
from typing import ClassVar

from .dp_schedule import DPSchedule
from .growatt_sph_controller import GrowattSphController
from .inverter_controller import InverterController
from .settings import BatterySettings

logger = logging.getLogger(__name__)


class SolisModbusController(InverterController):
    """Solis controller backed by the ``solis_modbus`` Home Assistant integration."""

    supports_charge_rate_control: ClassVar[bool] = True

    MAX_CHARGE_PERIODS = 6
    MAX_DISCHARGE_PERIODS = 6

    CHARGE_INTENTS: ClassVar[frozenset[str]] = frozenset({"GRID_CHARGING"})
    DISCHARGE_INTENTS: ClassVar[frozenset[str]] = frozenset(
        {"LOAD_SUPPORT", "BATTERY_EXPORT"}
    )

    def __init__(self, battery_settings: BatterySettings) -> None:
        super().__init__(battery_settings)
        self._charge_periods: list[dict] = []
        self._discharge_periods: list[dict] = []

    @property
    def active_tou_intervals(self) -> list[dict]:
        return self.tou_intervals

    def create_schedule(
        self,
        schedule: DPSchedule,
        current_period: int = 0,
        previous_tou_intervals: list[dict] | None = None,
    ) -> None:
        logger.info("Creating Solis Modbus TOU schedule from strategic intents")
        self.strategic_intents = schedule.original_dp_results["strategic_intent"]
        self.current_schedule = schedule
        self._build_periods()
        logger.info(
            "Solis schedule created: %d charge period(s), %d discharge period(s)",
            len(self._charge_periods),
            len(self._discharge_periods),
        )

    def _build_periods(self) -> None:
        charge_blocks, discharge_blocks = GrowattSphController._group_sph_periods(self)  # type: ignore[arg-type]
        charge_blocks = GrowattSphController._enforce_period_limit(  # type: ignore[arg-type]
            self, charge_blocks, self.MAX_CHARGE_PERIODS
        )
        discharge_blocks = GrowattSphController._enforce_period_limit(  # type: ignore[arg-type]
            self, discharge_blocks, self.MAX_DISCHARGE_PERIODS
        )

        self._charge_periods = GrowattSphController._blocks_to_period_dicts(  # type: ignore[arg-type]
            self, charge_blocks
        )
        self._discharge_periods = GrowattSphController._blocks_to_period_dicts(  # type: ignore[arg-type]
            self, discharge_blocks
        )

        self.tou_intervals = []
        for p in self._charge_periods:
            self.tou_intervals.append(
                {
                    "start_time": p["start_time"],
                    "end_time": p["end_time"],
                    "batt_mode": "battery_first",
                    "enabled": True,
                    "is_default": False,
                    "strategic_intent": "GRID_CHARGING",
                }
            )
        for p in self._discharge_periods:
            self.tou_intervals.append(
                {
                    "start_time": p["start_time"],
                    "end_time": p["end_time"],
                    "batt_mode": "grid_first",
                    "enabled": True,
                    "is_default": False,
                    "strategic_intent": "LOAD_SUPPORT/BATTERY_EXPORT",
                }
            )
        self.tou_intervals.sort(key=lambda x: x["start_time"])
        for idx, interval in enumerate(self.tou_intervals):
            interval["segment_id"] = idx + 1

    def write_schedule_to_hardware(
        self,
        controller,
        effective_period: int,
        current_tou: list,
    ) -> tuple[int, int]:
        charge_stop_soc = int(self.battery_settings.max_soc)
        discharge_stop_soc = int(self.battery_settings.min_soc)

        logger.info(
            "SOLIS HARDWARE: Writing TOU schedule charge=%s discharge=%s",
            self._charge_periods,
            self._discharge_periods,
        )
        controller.write_solis_tou_schedule(
            charge_periods=self._charge_periods,
            discharge_periods=self._discharge_periods,
            charge_rate_pct=100,
            discharge_rate_pct=100,
            charge_stop_soc=charge_stop_soc,
            discharge_stop_soc=discharge_stop_soc,
            max_slots=self.MAX_CHARGE_PERIODS,
        )
        return 2, 0

    def _write_period_to_hardware(
        self, controller, grid_charge: bool, discharge_rate: int
    ) -> tuple[bool, str]:
        errors = []
        try:
            controller.set_grid_charge(grid_charge)
        except Exception as e:
            logger.error("FAILED: set Solis grid_charge(%s): %s", grid_charge, e)
            errors.append(str(e))

        try:
            controller.set_solis_discharge_rate(discharge_rate)
        except Exception as e:
            logger.error("FAILED: set Solis discharge current: %s", e)
            errors.append(str(e))

        if errors:
            return False, "; ".join(errors)
        return True, ""

    def sync_soc_limits(self, controller) -> None:
        controller.set_solis_soc_limits(
            charge_stop_soc=int(self.battery_settings.max_soc),
            discharge_stop_soc=int(self.battery_settings.min_soc),
        )

    def initialize_hardware(self, controller) -> None:
        self.sync_soc_limits(controller)

    def read_and_initialize_from_hardware(self, controller, current_hour: int) -> None:
        result = controller.read_solis_tou_schedule(max_slots=self.MAX_CHARGE_PERIODS)
        self._charge_periods = result.get("charge_periods", [])
        self._discharge_periods = result.get("discharge_periods", [])
        self.tou_intervals = []
        for p in self._charge_periods:
            self.tou_intervals.append(
                {
                    "start_time": p["start_time"],
                    "end_time": p["end_time"],
                    "batt_mode": "battery_first",
                    "enabled": p.get("enabled", True),
                    "is_default": False,
                    "strategic_intent": "existing_schedule",
                }
            )
        for p in self._discharge_periods:
            self.tou_intervals.append(
                {
                    "start_time": p["start_time"],
                    "end_time": p["end_time"],
                    "batt_mode": "grid_first",
                    "enabled": p.get("enabled", True),
                    "is_default": False,
                    "strategic_intent": "existing_schedule",
                }
            )
        self.tou_intervals.sort(key=lambda x: x["start_time"])
        for idx, interval in enumerate(self.tou_intervals):
            interval["segment_id"] = idx + 1

    def compare_schedules(
        self, other_schedule: "InverterController", from_period: int = 0
    ) -> tuple[bool, str]:
        if not isinstance(other_schedule, SolisModbusController):
            return True, "different controller type"
        if self._charge_periods != other_schedule._charge_periods:
            return True, "Solis charge periods differ"
        if self._discharge_periods != other_schedule._discharge_periods:
            return True, "Solis discharge periods differ"
        return False, ""

    def get_daily_TOU_settings(self) -> list[dict]:
        return list(self.tou_intervals)

    def get_all_tou_segments(self) -> list[dict]:
        if not self.tou_intervals:
            return [
                {
                    "segment_id": 0,
                    "start_time": "00:00",
                    "end_time": "23:59",
                    "batt_mode": "load_first",
                    "enabled": False,
                    "is_default": True,
                }
            ]
        return list(self.tou_intervals)

    def log_current_TOU_schedule(self, header: str = "") -> None:
        if header:
            logger.info(header)
        logger.info("Solis charge periods: %s", self._charge_periods)
        logger.info("Solis discharge periods: %s", self._discharge_periods)

    def log_detailed_schedule(self, header: str = "") -> None:
        if header:
            logger.info(header)
        groups = self.get_detailed_period_groups()
        for group in groups:
            logger.info(
                "Solis %s-%s %s charge=%s discharge=%s",
                group["start_time"],
                group["end_time"],
                group["intent"],
                group["charge_rate"],
                group["discharge_rate"],
            )

    def check_health(self, controller) -> list:
        checks = []
        required = [
            "solis_tou_mode",
            "solis_self_use_mode",
            "grid_charge",
            "battery_charging_power_rate",
            "battery_discharging_power_rate",
            "solis_tou_charge_start_1",
            "solis_tou_charge_end_1",
            "solis_tou_discharge_start_1",
            "solis_tou_discharge_end_1",
            "solis_tou_charge_enabled_1",
            "solis_tou_discharge_enabled_1",
        ]
        ok = True
        for key in required:
            try:
                entity_id, _ = controller._resolve_entity_id(key)
                checks.append(
                    {
                        "component": key,
                        "status": "OK",
                        "message": f"Configured as {entity_id}",
                    }
                )
            except Exception as e:
                ok = False
                checks.append(
                    {"component": key, "status": "ERROR", "message": str(e)}
                )
        return [
            {
                "name": "Battery Control (Solis Modbus)",
                "description": "Controls Solis grid TOU charge/discharge periods via Modbus entities",
                "required": True,
                "status": "OK" if ok else "ERROR",
                "checks": checks,
                "last_run": datetime.now().isoformat(),
            }
        ]
