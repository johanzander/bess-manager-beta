"""
Official Home Assistant Nordpool integration price source.

This source uses the official HA Nordpool integration's service actions
instead of sensor attributes, providing compatibility with the core integration.
"""

import logging
from datetime import date, timedelta

import requests

from . import time_utils
from .price_manager import PriceSource, normalize_prices_to_quarterly

logger = logging.getLogger(__name__)

_OFFICIAL_NORDPOOL_AREA_CODES = {
    "EE",
    "LT",
    "LV",
    "AT",
    "BE",
    "FR",
    "GER",
    "NL",
    "PL",
    "DK1",
    "DK2",
    "FI",
    "NO1",
    "NO2",
    "NO3",
    "NO4",
    "NO5",
    "SE1",
    "SE2",
    "SE3",
    "SE4",
    "BG",
    "TEL",
    "SYS",
}

_OFFICIAL_NORDPOOL_AREA_ALIASES = {
    "DE": "GER",
    "DE-LU": "GER",
    "DE_LU": "GER",
}


def _normalize_official_area(area: str) -> str | None:
    """Normalize stored/HACS area codes to HA official Nordpool service codes."""
    normalized = area.strip().upper()
    if not normalized:
        return None

    normalized = _OFFICIAL_NORDPOOL_AREA_ALIASES.get(normalized, normalized)
    if normalized in _OFFICIAL_NORDPOOL_AREA_CODES:
        return normalized

    logger.warning(
        "Nordpool area %r is not accepted by the official HA service; "
        "omitting the areas field and using the integration default",
        area,
    )
    return None


def _http_error_details(error: Exception) -> str:
    """Return useful HTTP response details for Home Assistant service failures."""
    if not isinstance(error, requests.HTTPError) or error.response is None:
        return str(error)

    body = (error.response.text or "").strip()
    if body:
        return f"{error} - response: {body[:500]}"
    return str(error)


def _is_bad_request(error: Exception) -> bool:
    return (
        isinstance(error, requests.HTTPError)
        and error.response is not None
        and error.response.status_code == 400
    )


class OfficialNordpoolSource(PriceSource):
    """Price source that uses the official Home Assistant Nordpool integration.

    Uses the nordpool.get_prices_for_date service action instead of sensor attributes.
    The official integration was added to HA Core and provides different API than custom components.
    """

    def __init__(
        self, ha_controller, config_entry_id: str, vat_multiplier: float, area: str = ""
    ) -> None:
        """Initialize with Home Assistant controller and config entry ID.

        Args:
            ha_controller: Controller with access to Home Assistant services
            config_entry_id: Configuration entry ID for the Nordpool integration
            vat_multiplier: VAT multiplier (prices from official integration are VAT-exclusive)
            area: Market area code (e.g. "SE4", "NO1"). When provided it is passed
                  to the service call and used as the response key for an exact lookup.
                  When empty the first list in the response is used as a fallback.
        """
        self.ha_controller = ha_controller
        self.config_entry_id = config_entry_id
        self.vat_multiplier = vat_multiplier
        self.area = area

    def get_prices_for_date(self, target_date: date) -> list[float]:
        """Get prices from official Nordpool integration for the specified date.

        Uses the nordpool.get_prices_for_date service action.

        Args:
            target_date: The date to get prices for

        Returns:
            List of hourly prices per kWh (VAT-exclusive)

        Raises:
            ValueError: If prices cannot be fetched
        """
        if not self.config_entry_id:
            raise ValueError(
                "Nordpool integration not configured: config_entry_id is missing. "
                "Run the setup wizard to configure the Nordpool integration."
            )

        logger.info(
            f"Fetching Nordpool prices for {target_date} using official integration"
        )

        # Only support today and tomorrow (official integration limitation)
        current_date = time_utils.today()
        tomorrow_date = current_date + timedelta(days=1)

        if target_date not in (current_date, tomorrow_date):
            raise ValueError(
                f"Official Nordpool integration only supports today and tomorrow, not {target_date}"
            )

        last_error: Exception | None = None
        try:
            # Call the nordpool.get_prices_for_date service
            date_str = target_date.strftime("%Y-%m-%d")

            service_data: dict = {
                "config_entry": self.config_entry_id,
                "date": date_str,
            }
            service_area = _normalize_official_area(self.area) if self.area else None
            if service_area:
                service_data["areas"] = [service_area]

            # Make service call. Some HA/Nordpool versions are strict about
            # area selector values, so fall back to the integration's configured
            # default area if an explicit area is rejected.
            call_attempts = [service_data]
            if "areas" in service_data:
                fallback_data = {
                    "config_entry": self.config_entry_id,
                    "date": date_str,
                }
                call_attempts.append(fallback_data)

            response = None
            for attempt_data in call_attempts:
                try:
                    response = self.ha_controller._service_call_with_retry(
                        "nordpool",
                        "get_prices_for_date",
                        **attempt_data,
                        return_response=True,
                    )
                    break
                except Exception as e:
                    last_error = e
                    if attempt_data is service_data and _is_bad_request(e):
                        logger.warning(
                            "Nordpool service rejected explicit area payload %s; "
                            "retrying with integration default area. Error: %s",
                            attempt_data,
                            _http_error_details(e),
                        )
                        continue
                    raise

            if not response or "service_response" not in response:
                raise ValueError(
                    f"No response from nordpool.get_prices_for_date for {target_date}"
                )

            service_response = response["service_response"]

            # Response is keyed by area code (e.g. {"SE4": [...]}).
            # Use the configured area for an exact lookup; fall back to the first
            # list in the response for installs where area is not yet configured.
            price_entries: list = []
            lookup_areas: list[str] = []
            if service_area:
                lookup_areas.append(service_area)
            if self.area:
                lookup_areas.append(self.area.upper())
            for lookup_area in dict.fromkeys(lookup_areas):
                price_entries = service_response.get(lookup_area, [])
                if price_entries:
                    logger.debug(f"Found price data under area key: {lookup_area}")
                    break
            if not price_entries:
                for key, value in service_response.items():
                    if isinstance(value, list) and value:
                        price_entries = value
                        logger.debug(f"Found price data under key: {key} (fallback)")
                        break

            if not price_entries:
                raise ValueError(
                    f"No price entries returned for {target_date}. Available keys: {list(service_response.keys())}"
                )

            # Convert price entries to hourly list
            prices = []
            for entry in price_entries:
                # Official integration returns prices in [Currency]/MWh
                price_mwh = float(entry["price"])
                # Convert from per MWh to per kWh
                price_kwh = price_mwh / 1000.0
                prices.append(price_kwh)

            logger.info(
                f"Successfully fetched {len(prices)} prices from official Nordpool integration"
            )
            logger.debug(f"Price range: {min(prices):.3f} - {max(prices):.3f} per kWh")

            return normalize_prices_to_quarterly(prices)

        except Exception as e:
            if isinstance(e, ValueError):
                raise
            if last_error is not None:
                e = last_error
            raise ValueError(
                f"Failed to get prices from official integration for {target_date}: "
                f"{_http_error_details(e)}"
            ) from e

    def perform_health_check(self):
        """Perform health check for official Nordpool integration.

        Returns:
            dict: Health check results
        """
        health_check = {
            "component_name": "Official Nordpool Integration",
            "description": "Official Home Assistant Nordpool price source",
            "is_required": True,
            "status": "OK",
            "checks": [],
        }

        # Check config entry ID first — no point calling the API without it
        config_check = {
            "name": "Configuration Entry",
            "status": "OK",
            "error": None,
            "value": f"ID: {self.config_entry_id}",
        }

        if not self.config_entry_id:
            config_check.update(
                {
                    "status": "ERROR",
                    "error": "No config entry ID configured",
                    "value": "Missing",
                }
            )
            health_check["status"] = "ERROR"
            health_check["checks"] = [config_check]
            return health_check

        # Test the service call with today's date
        today = time_utils.today()

        service_check = {
            "name": "Nordpool Service Call",
            "status": "OK",
            "error": None,
            "value": "Available",
        }

        try:
            prices = self.get_prices_for_date(today)
            service_check.update(
                {"status": "OK", "value": f"{len(prices)} hourly prices available"}
            )
        except Exception as e:
            service_check.update(
                {
                    "status": "ERROR",
                    "error": f"Service call failed: {e!s}",
                    "value": "N/A",
                }
            )
            health_check["status"] = "ERROR"

        health_check["checks"] = [service_check, config_check]
        return health_check
