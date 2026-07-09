from app import models
from app.services.monitoring import schema


def _fk_targets(model, column_name: str) -> set[str]:
    return {fk.target_fullname for fk in model.__table__.c[column_name].foreign_keys}


def test_monitor_schema_names_are_unique_and_canonical():
    logical_names = [table.logical for table in schema.MONITOR_TABLES]
    physical_names = [table.physical for table in schema.MONITOR_TABLES]
    canonical_names = [table.canonical for table in schema.MONITOR_TABLES]

    assert len(logical_names) == len(set(logical_names))
    assert len(physical_names) == len(set(physical_names))
    assert len(canonical_names) == len(set(canonical_names))
    assert all(name.startswith("monitor_") for name in canonical_names)


def test_monitor_models_use_phase4_table_map():
    assert models.MonitorUsageSource.__tablename__ == schema.MONITOR_USAGE_SOURCES.physical
    assert models.MonitorUsageEvent.__tablename__ == schema.MONITOR_USAGE_EVENTS.physical
    assert models.MonitorErrorGroup.__tablename__ == schema.MONITOR_ERROR_GROUPS.physical
    assert models.MonitorErrorEvent.__tablename__ == schema.MONITOR_ERROR_EVENTS.physical
    assert models.MonitorWebVital.__tablename__ == schema.MONITOR_WEB_VITALS.physical
    assert models.MonitorLog.__tablename__ == schema.MONITOR_LOGS.physical
    assert models.MonitorSpan.__tablename__ == schema.MONITOR_SPANS.physical
    assert models.MonitorProblem.__tablename__ == schema.MONITOR_PROBLEMS.physical
    assert models.MonitorInvestigation.__tablename__ == schema.MONITOR_INVESTIGATIONS.physical
    assert models.MonitorInvestigationEntry.__tablename__ == schema.MONITOR_INVESTIGATION_ENTRIES.physical
    assert models.MonitorReport.__tablename__ == schema.MONITOR_REPORTS.physical
    assert models.MonitorAlert.__tablename__ == schema.MONITOR_ALERTS.physical
    assert models.MonitorAlertSettings.__tablename__ == schema.MONITOR_ALERT_SETTINGS.physical


def test_monitor_foreign_keys_follow_phase4_table_map():
    assert _fk_targets(models.MonitorUsageEvent, "source_id") == {schema.MONITOR_USAGE_SOURCES.fk()}
    assert _fk_targets(models.MonitorErrorGroup, "source_id") == {schema.MONITOR_USAGE_SOURCES.fk()}
    assert _fk_targets(models.MonitorErrorEvent, "source_id") == {schema.MONITOR_USAGE_SOURCES.fk()}
    assert _fk_targets(models.MonitorErrorEvent, "group_id") == {schema.MONITOR_ERROR_GROUPS.fk()}
    assert _fk_targets(models.MonitorWebVital, "source_id") == {schema.MONITOR_USAGE_SOURCES.fk()}
    assert _fk_targets(models.MonitorLog, "source_id") == {schema.MONITOR_USAGE_SOURCES.fk()}
    assert _fk_targets(models.MonitorSpan, "source_id") == {schema.MONITOR_USAGE_SOURCES.fk()}
    assert _fk_targets(models.MonitorInvestigationEntry, "investigation_id") == {schema.MONITOR_INVESTIGATIONS.fk()}
    assert _fk_targets(models.MonitorReport, "investigation_id") == {schema.MONITOR_INVESTIGATIONS.fk()}
