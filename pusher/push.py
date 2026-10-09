#!/usr/bin/env python3
"""Fetch battery status for all cars on a Polestar ID account and push it to a TRMNL webhook.

Environment:
  POLESTAR_USERNAME, POLESTAR_PASSWORD  Polestar ID login
  TRMNL_WEBHOOK_URL                     https://trmnl.com/api/custom_plugins/<uuid>
  CAR_NAMES (optional)                  "VIN1=Polestar 2,VIN2=Polestar 3" to override display names
  STATE_FILE (optional)                 where the last payload is cached (default ./last_payload.json)

Pushes only when something changed, or at least once an hour, to stay under TRMNL's
webhook rate limit (12 requests/hour). Run it every 10 minutes from cron or a systemd timer.
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from pypolestar import PolestarApi

TZ = ZoneInfo("Europe/Stockholm")
FORCE_PUSH_AFTER = 3600
log = logging.getLogger("polestar-trmnl")

STATUS_SV = {
    "CHARGING_STATUS_CHARGING": "Laddar",
    "CHARGING_STATUS_SMART_CHARGING": "Smartladdar",
    "CHARGING_STATUS_SMART_CHARGING_PAUSED": "Smartladdning pausad",
    "CHARGING_STATUS_SCHEDULED": "Schemalagd",
    "CHARGING_STATUS_DONE": "Fulladdad",
    "CHARGING_STATUS_FAULT": "Laddfel",
    "CHARGING_STATUS_ERROR": "Laddfel",
    "CHARGING_STATUS_DISCHARGING": "Urladdning",
}


SERVICE_SV = {
    "SERVICE_WARNING_SERVICE_REQUIRED": "Service behövs",
    "SERVICE_WARNING_REGULAR_MAINTENANCE_ALMOST_TIME_FOR_SERVICE": "Snart dags för service",
    "SERVICE_WARNING_DISTANCE_DRIVEN_ALMOST_TIME_FOR_SERVICE": "Snart dags för service",
    "SERVICE_WARNING_REGULAR_MAINTENANCE_TIME_FOR_SERVICE": "Dags för service",
    "SERVICE_WARNING_DISTANCE_DRIVEN_TIME_FOR_SERVICE": "Dags för service",
    "SERVICE_WARNING_REGULAR_MAINTENANCE_OVERDUE_FOR_SERVICE": "Service försenad",
    "SERVICE_WARNING_DISTANCE_DRIVEN_OVERDUE_FOR_SERVICE": "Service försenad",
}
FLUID_SV = {
    "brake_fluid_level_warning": "Bromsvätska låg",
    "engine_coolant_level_warning": "Kylvätska låg",
    "oil_level_warning": "Kontrollera olja",
}
TARGET_TYPE_SV = {"DAILY": "vardag", "LONG_TRIP": "långresa", "CUSTOM": "anpassad"}


def enum_name(value):
    return getattr(value, "name", None) if value is not None else None


def car_names():
    names = {}
    for pair in filter(None, os.environ.get("CAR_NAMES", "").split(",")):
        vin, _, name = pair.partition("=")
        names[vin.strip()] = name.strip()
    return names


def km(value):
    """48213 -> "48 213" (Swedish thousands separator); None stays None."""
    return f"{round(value):,}".replace(",", "\u00a0") if value is not None else None


def health_alerts(health):
    """Swedish warning strings for anything on the car that needs attention."""
    if not health:
        return []
    alerts = []
    service = enum_name(health.service_warning)
    if service in SERVICE_SV:
        alerts.append(SERVICE_SV[service])
    for field, text in FLUID_SV.items():
        name = enum_name(getattr(health, field, None))
        if name and not name.endswith(("_NO_WARNING", "_UNSPECIFIED")):
            alerts.append(text)
    return alerts


def describe(vin, info, grpc, battery, names, health=None, odometer=None, target=None):
    src = grpc or battery
    soc = src.battery_charge_level_percentage if src else None
    status = enum_name(grpc.charging_status if grpc else battery.charging_status if battery else None)
    connection = enum_name(grpc.charger_connection_status if grpc else battery.charger_connection_status if battery else None)
    charging = status in ("CHARGING_STATUS_CHARGING", "CHARGING_STATUS_SMART_CHARGING")
    connected = None if connection in (None, "CHARGER_CONNECTION_STATUS_UNSPECIFIED") else connection == "CHARGER_CONNECTION_STATUS_CONNECTED"

    if status in STATUS_SV:
        status_text = STATUS_SV[status]
    elif connected is True:
        status_text = "Inkopplad"
    elif connected is False:
        status_text = "Ej inkopplad"
    else:
        status_text = "Parkerad"

    minutes_to_full = src.estimated_charging_time_to_full_minutes if src and charging else None
    ready_at = (datetime.now(TZ) + timedelta(minutes=minutes_to_full)).strftime("%H:%M") if minutes_to_full else None
    power = getattr(src, "charging_power_watts", None) if charging else None
    stamp = (grpc.timestamp if grpc else battery.event_updated_timestamp if battery else None)

    return {
        "name": names.get(vin) or (info.model_name if info and info.model_name else vin[-6:]),
        "reg": info.registration_no if info else None,
        "soc": round(soc) if soc is not None else None,
        "range_km": src.estimated_distance_to_empty_km if src else None,
        "charging": charging,
        "connected": connected,
        "status": status_text,
        "minutes_to_full": minutes_to_full,
        "ready_at": ready_at,
        "power_kw": round(power / 1000, 1) if power else None,
        "seen": stamp.astimezone(TZ).strftime("%-d/%-m %H:%M") if stamp else None,
        "alerts": health_alerts(health),
        "service_days": health.days_to_service if health else None,
        "service_km": km(health.distance_to_service_km) if health else None,
        "odometer_km": km(odometer.odometer_meters / 1000) if odometer and odometer.odometer_meters else None,
        "target_soc": target.battery_charge_target_level if target else None,
        "target_type": TARGET_TYPE_SV.get(enum_name(target.charge_target_level_setting_type)) if target else None,
    }


async def collect():
    names = car_names()
    cars = []
    async with httpx.AsyncClient(timeout=30) as client:
        api = PolestarApi(
            username=os.environ["POLESTAR_USERNAME"],
            password=os.environ["POLESTAR_PASSWORD"],
            client_session=client,
        )
        await api.async_init()
        try:
            for vin in sorted(api.get_available_vins()):
                try:
                    await api.update_latest_data(vin=vin, update_vehicle=True, update_telematics=True, update_grpc=True)
                except Exception as exc:  # one car failing shouldn't blank the other
                    log.warning("update failed for %s: %s", vin[-6:], exc)
                telematics = api.get_car_telematics(vin)
                cars.append(describe(
                    vin,
                    api.get_car_information(vin),
                    api.get_grpc_battery(vin),
                    telematics.battery if telematics else None,
                    names,
                    health=telematics.health if telematics else None,
                    odometer=telematics.odometer if telematics else None,
                    target=api.get_grpc_target_soc(vin),  # None where unsupported (Polestar 2)
                ))
        finally:
            await api.async_logout()
    # Polestar 2 before Polestar 3 etc.
    return sorted(cars, key=lambda c: c["name"])


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state_file = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("last_payload.json")))
    cars = asyncio.run(collect())
    if not cars:
        log.error("no cars found on account")
        return 1

    payload = {"cars": cars}
    last = json.loads(state_file.read_text()) if state_file.exists() else {}
    stale = time.time() - last.get("pushed_at", 0) > FORCE_PUSH_AFTER
    if last.get("payload") == payload and not stale:
        log.info("unchanged, not pushing")
        return 0

    payload_out = {**payload, "updated": datetime.now(TZ).strftime("%H:%M")}
    resp = httpx.post(os.environ["TRMNL_WEBHOOK_URL"], json={"merge_variables": payload_out}, timeout=30)
    log.info("pushed %d cars -> %s %s", len(cars), resp.status_code, resp.text[:200])
    resp.raise_for_status()
    state_file.write_text(json.dumps({"payload": payload, "pushed_at": time.time()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
