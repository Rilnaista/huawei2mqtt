"""Status code and alarm bit tables (Huawei "SUN2000 Modbus Interface Definitions").

Keys are our own snake_case English identifiers. Unknown values are reported as
``unknown_0xXXXX`` (status) or ``alarm_<n>_bit_<b>`` (alarm bit).
"""

from __future__ import annotations

# Register 32089 (device status)
STATUS: dict[int, str] = {
    0x0000: "standby_initializing",
    0x0001: "standby_detecting_insulation_resistance",
    0x0002: "standby_detecting_irradiation",
    0x0003: "standby_grid_detecting",
    0x0100: "starting",
    0x0200: "on_grid",
    0x0201: "on_grid_power_limited",
    0x0202: "on_grid_self_derating",
    0x0203: "off_grid_running",
    0x0300: "shutdown_fault",
    0x0301: "shutdown_command",
    0x0302: "shutdown_ovgr",
    0x0303: "shutdown_communication_disconnected",
    0x0304: "shutdown_power_limited",
    0x0305: "shutdown_manual_startup_required",
    0x0306: "shutdown_dc_switches_disconnected",
    0x0307: "shutdown_rapid_cutoff",
    0x0308: "shutdown_input_underpowered",
    0x030C: "shutdown_ess_end_of_discharge",
    0x0401: "grid_scheduling_cos_phi_p_curve",
    0x0402: "grid_scheduling_q_u_curve",
    0x0403: "grid_scheduling_pf_u_curve",
    0x0404: "grid_scheduling_dry_contact",
    0x0405: "grid_scheduling_q_p_curve",
    0x0500: "spot_check_ready",
    0x0501: "spot_checking",
    0x0600: "inspecting",
    0x0700: "afci_self_check",
    0x0800: "iv_scanning",
    0x0900: "dc_input_detection",
    0x0A00: "running_off_grid_charging",
    0xA000: "standby_no_irradiation",
}


def status_key(code: int | None) -> str | None:
    if code is None:
        return None
    return STATUS.get(code, f"unknown_0x{code:04X}")


def is_running(code: int | None) -> bool | None:
    key = status_key(code)
    return None if key is None else key.startswith("on_grid")


# Registers 32008..32010 (alarm 1..3): bit -> key
ALARMS: dict[int, dict[int, str]] = {
    1: {
        0: "high_string_input_voltage",
        1: "dc_arc_fault",
        2: "string_reverse_connection",
        3: "string_current_backfeed",
        4: "abnormal_string_power",
        5: "afci_self_check_fail",
        6: "phase_wire_short_circuited_to_pe",
        7: "grid_loss",
        8: "grid_undervoltage",
        9: "grid_overvoltage",
        10: "grid_voltage_imbalance",
        11: "grid_overfrequency",
        12: "grid_underfrequency",
        13: "unstable_grid_frequency",
        14: "output_overcurrent",
        15: "output_dc_component_overhigh",
    },
    2: {
        0: "abnormal_residual_current",
        1: "abnormal_grounding",
        2: "low_insulation_resistance",
        3: "overtemperature",
        4: "device_fault",
        5: "upgrade_failed_or_version_mismatch",
        6: "license_expired",
        7: "faulty_monitoring_unit",
        8: "faulty_power_collector",
        9: "battery_abnormal",
        10: "active_islanding",
        11: "passive_islanding",
        12: "transient_ac_overvoltage",
        13: "peripheral_port_short_circuit",
        14: "churn_output_overload",
        15: "abnormal_pv_module_configuration",
    },
    3: {
        0: "optimizer_fault",
        1: "builtin_pid_operation_abnormal",
        2: "high_input_string_voltage_to_ground",
        3: "external_fan_abnormal",
        4: "battery_reverse_connection",
        5: "on_off_grid_controller_abnormal",
        6: "pv_string_loss",
        7: "internal_fan_abnormal",
        8: "dc_protection_unit_abnormal",
        9: "el_unit_abnormal",
        10: "active_adjustment_instruction_abnormal",
        11: "reactive_adjustment_instruction_abnormal",
        12: "ct_wiring_abnormal",
        13: "dc_arc_fault_manual_clear",
        14: "dc_switch_abnormal",
        15: "battery_discharge_capacity_low",
    },
}


def decode_alarms(words: dict[int, int | None]) -> list[str] | None:
    """words: {1: alarm_1, 2: alarm_2, 3: alarm_3}. None if nothing is known."""
    if all(v is None for v in words.values()):
        return None
    out: list[str] = []
    for n in (1, 2, 3):
        val = words.get(n)
        if not val:
            continue
        for bit in range(16):
            if val & (1 << bit):
                out.append(ALARMS[n].get(bit, f"alarm_{n}_bit_{bit}"))
    return out
