from pathlib import Path

from scripts.migrate import (
    Migration,
    discover_migrations,
    pending_migrations,
    migrations_up_to,
)


def _mig(version):
    return Migration(version=version, path=Path(f"{version}.sql"))


def test_discover_sorts_by_filename(tmp_path):
    for name in ("0011_c.sql", "0009_a.sql", "0010_b.sql"):
        (tmp_path / name).write_text("select 1;")
    (tmp_path / "notes.txt").write_text("ignore me")
    versions = [m.version for m in discover_migrations(tmp_path)]
    assert versions == ["0009_a", "0010_b", "0011_c"]


def test_pending_excludes_applied_preserving_order():
    allm = [_mig("0009_a"), _mig("0010_b"), _mig("0011_c")]
    pending = pending_migrations(allm, applied={"0009_a"})
    assert [m.version for m in pending] == ["0010_b", "0011_c"]


def test_pending_empty_when_all_applied():
    allm = [_mig("0009_a"), _mig("0010_b")]
    assert pending_migrations(allm, {"0009_a", "0010_b"}) == []


def test_migration_number_is_leading_id():
    assert _mig("0045_project_journey_goals").number == "0045"


def test_migrations_up_to_baseline_is_inclusive():
    allm = [_mig("0044_x"), _mig("0045_y"), _mig("0046_z")]
    upto = [m.version for m in migrations_up_to(allm, "0045")]
    assert upto == ["0044_x", "0045_y"]


def test_real_migrations_dir_discovers_in_order():
    from scripts.migrate import MIGRATIONS_DIR
    versions = [m.version for m in discover_migrations(MIGRATIONS_DIR)]
    assert versions == sorted(versions)
    assert all(v[:4].isdigit() for v in versions)
