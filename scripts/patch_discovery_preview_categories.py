"""One-time development data patch; independent of request/pipeline processing.

Supply the IDs/categories already returned by the preview dashboard. Credentials
come from the named env file or process environment, never from command arguments.
The backup contains only IDs/categories and can restore this patch with compare-
and-set updates, preserving any subsequent category changes.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


CATEGORIES = {"build_and_tooling", "data_and_apis", "workflow_and_automation", "uncategorized"}


def read_environment(path: Path | None) -> dict[str, str]:
    values = {}
    if path:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return {**values, **os.environ}


def validate_assignments(rows: list[dict]) -> dict[int, str]:
    result = {}
    for row in rows:
        identifier, category = row["id"], row["category"]
        if type(identifier) is not int or identifier <= 0 or category not in CATEGORIES - {"uncategorized"}:
            raise ValueError("Assignments require positive IDs and a known preview category")
        if identifier in result and result[identifier] != category:
            raise ValueError(f"Conflicting assignments for cluster {identifier}")
        result[identifier] = category
    if not result:
        raise ValueError("No preview memberships supplied")
    return result


def plan_patch(assignments: dict[int, str], records: list[dict]) -> list[dict]:
    by_id = {record["id"]: record for record in records}
    if set(assignments) != set(by_id):
        raise ValueError("Some preview IDs no longer exist; refresh the assignment list")
    changes = []
    for identifier, category in assignments.items():
        row = by_id[identifier]
        previous = row["discovery_category"]
        if row["pipeline_version"] != "v2" or not (row.get("name") or "").strip() or not (row.get("summary") or "").strip():
            raise ValueError(f"Cluster {identifier} no longer meets the opportunity contract")
        if previous not in ("uncategorized", category):
            raise ValueError(f"Cluster {identifier} already has a different category; preserving it")
        if previous != category:
            changes.append(dict(id=identifier, previous=previous, category=category))
    return changes


class CategoryStore:
    def __init__(self, origin: str, secret: str, expected_project: str):
        parsed = urlparse(origin)
        if parsed.scheme != "https" or parsed.hostname != expected_project or parsed.path not in ("", "/"):
            raise ValueError("Supabase origin does not match the explicitly selected project")
        if not secret:
            raise ValueError("SUPABASE_SERVICE_ROLE_KEY is missing")
        self.origin = origin.rstrip("/")
        self.secret = secret

    def request(self, method: str, query: dict, body: dict | None = None) -> list[dict]:
        request = Request(
            f"{self.origin}/rest/v1/clusters?{urlencode(query)}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"apikey": self.secret, "Authorization": f"Bearer {self.secret}",
                     "Content-Type": "application/json", "Prefer": "return=representation"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"Supabase category request failed (HTTP {error.code})") from None

    def read(self, identifiers: list[int]) -> list[dict]:
        return self.request("GET", {"select": "id,discovery_category,name,summary,pipeline_version",
                                    "id": f"in.({','.join(map(str, identifiers))})"})

    def update(self, changes: list[dict], restore: bool = False) -> int:
        count = 0
        for category in CATEGORIES:
            group = [row for row in changes if row["category"] == category]
            if not group:
                continue
            target = "uncategorized" if restore else category
            expected = category if restore else "uncategorized"
            rows = self.request("PATCH", {
                "id": f"in.({','.join(str(row['id']) for row in group)})",
                "pipeline_version": "eq.v2", "discovery_category": f"eq.{expected}",
                "select": "id,discovery_category",
            }, {"discovery_category": target})
            if not restore and len(rows) != len(group):
                raise RuntimeError("Category data changed during the patch; use the backup to restore and recheck")
            count += len(rows)
        return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--expected-project", required=True, help="Exact Supabase hostname")
    parser.add_argument("--assignments", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--restore", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    env = read_environment(args.env_file)
    store = CategoryStore(env.get("SUPABASE_URL", ""), env.get("SUPABASE_SERVICE_ROLE_KEY", ""), args.expected_project)
    if args.restore:
        backup = json.loads(args.restore.read_text())
        if backup["origin"] != store.origin:
            raise ValueError("Backup belongs to a different project")
        if args.apply:
            print(f"Restored {store.update(backup['changes'], restore=True)} unchanged patch assignments")
        else:
            print(f"Restore plan: {len(backup['changes'])} assignments; pass --apply to restore")
        return
    if not args.assignments:
        parser.error("--assignments is required when not restoring")
    assignments = validate_assignments(json.loads(args.assignments.read_text()))
    changes = plan_patch(assignments, store.read(list(assignments)))
    print(f"Preview plan: {len(changes)} updates, {len(assignments) - len(changes)} already assigned")
    if args.apply and changes:
        if not args.backup:
            parser.error("--backup is required before applying")
        args.backup.parent.mkdir(parents=True, exist_ok=True)
        with args.backup.open("x", encoding="utf-8") as file:
            json.dump({"origin": store.origin, "createdAt": datetime.now(timezone.utc).isoformat(), "changes": changes}, file, indent=2)
        print(f"Applied {store.update(changes)} temporary preview category assignments")
        verified = store.read(list(assignments))
        if any(row["discovery_category"] != assignments[row["id"]] for row in verified):
            raise RuntimeError("Category verification failed; backup is available for restoration")
        print("Verified stored categories match every supplied preview membership")


if __name__ == "__main__":
    main()
