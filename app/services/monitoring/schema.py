"""Monitor storage naming map.

Phase 4 starts with compatibility, not a physical table rename. The ORM still
points at today's deployed tables, while this module records the Monitor-owned
logical table and the future canonical storage name in one place.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MonitorTable:
    logical: str
    physical: str
    canonical: str

    @property
    def uses_legacy_portfolio_prefix(self) -> bool:
        return self.physical.startswith("portfolio_")

    def fk(self, column: str = "id") -> str:
        return f"{self.physical}.{column}"


MONITOR_USAGE_SOURCES = MonitorTable("usage_sources", "portfolio_usage_sources", "monitor_usage_sources")
MONITOR_USAGE_EVENTS = MonitorTable("usage_events", "portfolio_usage_events", "monitor_usage_events")
MONITOR_ERROR_GROUPS = MonitorTable("error_groups", "portfolio_error_groups", "monitor_error_groups")
MONITOR_ERROR_EVENTS = MonitorTable("error_events", "portfolio_error_events", "monitor_error_events")
MONITOR_WEB_VITALS = MonitorTable("web_vitals", "portfolio_web_vitals", "monitor_web_vitals")
MONITOR_LOGS = MonitorTable("logs", "portfolio_logs", "monitor_logs")
MONITOR_SPANS = MonitorTable("spans", "portfolio_spans", "monitor_spans")
MONITOR_PROBLEMS = MonitorTable("problems", "monitoring_problems", "monitor_problems")
MONITOR_INVESTIGATIONS = MonitorTable("investigations", "monitoring_investigations", "monitor_investigations")
MONITOR_INVESTIGATION_ENTRIES = MonitorTable(
    "investigation_entries",
    "monitoring_investigation_entries",
    "monitor_investigation_entries",
)
MONITOR_REPORTS = MonitorTable("reports", "monitoring_reports", "monitor_reports")
MONITOR_ALERTS = MonitorTable("alerts", "portfolio_alerts", "monitor_alerts")
MONITOR_ALERT_SETTINGS = MonitorTable("alert_settings", "portfolio_alert_settings", "monitor_alert_settings")

MONITOR_TABLES = (
    MONITOR_USAGE_SOURCES,
    MONITOR_USAGE_EVENTS,
    MONITOR_ERROR_GROUPS,
    MONITOR_ERROR_EVENTS,
    MONITOR_WEB_VITALS,
    MONITOR_LOGS,
    MONITOR_SPANS,
    MONITOR_PROBLEMS,
    MONITOR_INVESTIGATIONS,
    MONITOR_INVESTIGATION_ENTRIES,
    MONITOR_REPORTS,
    MONITOR_ALERTS,
    MONITOR_ALERT_SETTINGS,
)

MONITOR_TABLES_BY_LOGICAL_NAME = {table.logical: table for table in MONITOR_TABLES}
LEGACY_PORTFOLIO_MONITOR_TABLES = tuple(
    table for table in MONITOR_TABLES if table.uses_legacy_portfolio_prefix
)
