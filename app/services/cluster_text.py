"""Shared SQL eligibility for complete opportunity text."""
from sqlalchemy import func


def has_nonblank_text(column):
    # Unlike trim(), this also excludes tabs and line breaks.
    return func.regexp_replace(column, r"\s", "", "g") != ""
