from bridge import codes
from bridge import registers as rg


def reg(field):
    return rg.BY_FIELD[field]


def test_fields_unique_and_all_in_groups():
    assert len({r.field for r in rg.REGISTERS}) == len(rg.REGISTERS)
    listed = {f for fields in rg.GROUP_FIELDS.values() for f in fields}
    for r in rg.REGISTERS:
        assert r.field in listed or r.group == "internal"  # internal: never published
    for f in listed:
        assert f in rg.BY_FIELD or f in rg.DERIVED


def test_gain_and_rounding():
    assert rg.decode(reg("pv1_voltage_v"), [3005]) == (3005, 300.5)
    assert rg.decode(reg("pv1_current_a"), [512]) == (512, 5.12)
    assert rg.decode(reg("frequency_hz"), [4999]) == (4999, 49.99)
    assert rg.decode(reg("power_factor"), [950]) == (950, 0.95)
    assert rg.decode(reg("current_l1_a"), [0, 5123])[1] == 5.123
    assert rg.decode(reg("efficiency_pct"), [9857])[1] == 98.6
    assert rg.decode(reg("total_yield_kwh"), [2130789 >> 16, 2130789 & 0xFFFF])[1] == 21307.89


def test_signed_values():
    assert rg.decode(reg("pv1_voltage_v"), [0xFFF6])[1] == -1.0
    assert rg.decode(reg("reactive_power_var"), [0xFFFF, 0xFF9C])[1] == -100
    assert rg.decode(reg("internal_temperature_c"), [0xFFEC])[1] == -2.0


def test_invalid_markers_are_none():
    assert rg.decode(reg("active_power_w"), [0x7FFF, 0xFFFF])[1] is None
    assert rg.decode(reg("insulation_resistance_mohm"), [0xFFFF])[1] is None
    assert rg.decode(reg("pv1_voltage_v"), [0x7FFF])[1] is None
    assert rg.decode(reg("total_yield_kwh"), [0xFFFF, 0xFFFF])[1] is None


def test_bitfields_keep_all_bits():
    assert rg.decode(reg("alarm_1"), [0xFFFF]) == (0xFFFF, 0xFFFF)


def test_string_and_epoch():
    words = [int.from_bytes(b"SU", "big"), int.from_bytes(b"N\x00", "big")] + [0] * 13
    assert rg.decode(reg("model"), words) == ("SUN", "SUN")
    assert rg.decode(reg("startup_time"), [0x6553, 0xF100])[1] == "2023-11-14T22:13:20Z"
    assert rg.decode(reg("startup_time"), [0, 0])[1] is None


def test_blocks_respect_limits_and_cover_registers():
    for klass in (rg.LIVE, rg.STATIC):
        regs = [r for r in rg.REGISTERS if r.klass == klass]
        blocks = rg.blocks_for(klass=klass)
        assert all(1 <= b.count <= 64 for b in blocks)
        covered = [r for b in blocks for r in b.regs]
        assert sorted(r.address for r in covered) == sorted(r.address for r in regs)
        for b in blocks:
            assert all(b.start <= r.address and r.end <= b.start + b.count for r in b.regs)


def test_block_splitting():
    regs = [rg.Reg(100 + i * 10, f"f{i}", "g", "U16") for i in range(10)]
    blocks = rg.build_blocks(regs, max_words=25, max_gap=16)
    assert len(blocks) > 1 and all(b.count <= 25 for b in blocks)
    far = [rg.Reg(1, "a", "g", "U16"), rg.Reg(500, "b", "g", "U16")]
    assert len(rg.build_blocks(far)) == 2


def test_status_mapping():
    assert codes.status_key(0xA000) == "standby_no_irradiation"
    assert codes.status_key(0x0200) == "on_grid"
    assert codes.status_key(0x0301) == "shutdown_command"
    assert codes.status_key(0x1234) == "unknown_0x1234"
    assert codes.status_key(None) is None
    assert codes.is_running(0x0200) and codes.is_running(0x0201)
    assert codes.is_running(0xA000) is False


def test_alarm_decoding():
    assert codes.decode_alarms({1: 0, 2: 0, 3: 0}) == []
    assert codes.decode_alarms({1: 0x0080, 2: 0x0004, 3: None}) == \
        ["grid_loss", "low_insulation_resistance"]
    assert codes.decode_alarms({1: None, 2: None, 3: None}) is None


def test_epoch_is_local_time_winter_and_summer():
    from datetime import datetime
    ber = rg.local_tz("Europe/Berlin")

    def words(local):  # wall clock as "epoch" seconds
        n = int((datetime.fromisoformat(local) - datetime(1970, 1, 1)).total_seconds())
        return [n >> 16, n & 0xFFFF]
    r = reg("startup_time")
    assert rg.decode(r, words("2026-01-15T10:00:00"), ber)[1] == "2026-01-15T09:00:00Z"  # CET +1
    assert rg.decode(r, words("2026-07-15T10:00:00"), ber)[1] == "2026-07-15T08:00:00Z"  # CEST +2


def test_epoch_fallback_to_device_offset_when_tz_invalid():
    from datetime import datetime
    from bridge.cache import Cache
    assert rg.local_tz("Nowhere/Invalid") is None
    c = Cache(tz_name="Nowhere/Invalid")
    n = int((datetime(2026, 1, 15, 10) - datetime(1970, 1, 1)).total_seconds())
    blk = rg.Block(43006, 1, (rg.BY_FIELD["timezone_offset_min"],))
    c.update_block(blk, [60])
    assert c.value("timezone_offset_min") == 60
    blk2 = rg.Block(32091, 2, (reg("startup_time"),))
    c.update_block(blk2, [n >> 16, n & 0xFFFF])
    assert c.value("startup_time") == "2026-01-15T09:00:00Z"
