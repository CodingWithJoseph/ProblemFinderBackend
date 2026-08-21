"""Deterministic Markdown and ZIP views of the canonical Build Contract."""

from __future__ import annotations

import io
import json
import re
import zipfile

from app.solution_contract import (
    BuildDocument,
    SolutionCaseDocument,
    SolutionFeature,
    SolutionPrimaryFlow,
    SolutionTask,
    SolutionTestPlan,
)


DOCUMENT_ORDER = (
    "build_contract",
    "features",
    "requirements",
    "tasks",
    "test_plan",
    "decisions",
    "agents",
    "claude",
    "copilot_instructions",
)


def _bullets(values: list[str], *, empty: str = "None specified.") -> str:
    return "\n".join(f"- {value}" for value in values) if values else empty


def _numbered(values: list[str]) -> str:
    return "\n".join(f"{index}. {value}" for index, value in enumerate(values, 1))


def _feature_lines(document: SolutionCaseDocument) -> str:
    sections: list[str] = []
    for feature in document.features:
        sections.append(
            "\n".join([
                f"## {feature.id}: {feature.title}",
                "",
                feature.summary,
                "",
                f"Problem units: {', '.join(feature.problem_unit_ids)}",
                f"Evidence: {', '.join(feature.evidence_ids)}",
                "",
                "Acceptance criteria:",
                _bullets(feature.acceptance_criteria),
            ])
        )
    return "\n\n".join(sections)


def _requirement_lines(document: SolutionCaseDocument) -> str:
    rows = ["| ID | Feature | Requirement |", "| --- | --- | --- |"]
    for feature in document.features:
        for index, criterion in enumerate(feature.acceptance_criteria, 1):
            value = criterion.replace("|", "\\|").replace("\n", " ")
            rows.append(f"| REQ-{feature.id}-{index} | {feature.id} | {value} |")
    return "\n".join(rows)


def _task_lines(document: SolutionCaseDocument) -> str:
    sections: list[str] = []
    for task in document.tasks:
        sections.append(
            "\n".join([
                f"## {task.id}: {task.title}",
                "",
                task.description,
                "",
                f"Features: {', '.join(task.feature_ids)}",
                f"Depends on: {', '.join(task.depends_on) if task.depends_on else 'None'}",
                "",
                "Completion checks:",
                _bullets(task.acceptance_checks),
            ])
        )
    return "\n\n".join(sections)


def _test_section(title: str, values: list[str]) -> str:
    return f"## {title}\n\n{_bullets(values, empty='No checks specified.')}"


def _agent_instructions(document: SolutionCaseDocument, agent_name: str) -> str:
    return f"""# {agent_name} instructions

This repository is being built from the approved Immensity Build Contract.

## Source of truth

Read these files before changing code:

1. `docs/BUILD_CONTRACT.md`
2. `docs/FEATURES.md`
3. `docs/REQUIREMENTS.md`
4. `docs/TASKS.md`
5. `docs/TEST_PLAN.md`
6. `docs/DECISIONS.md`

## Working rules

- Implement only the approved Build scope. Do not invent pricing, growth, positioning, or unrelated product behavior.
- Keep every change traceable to a feature ID and task ID.
- Respect task dependencies and complete one dependency-ready task at a time.
- Record a blocker instead of silently changing scope or architecture.
- Run the relevant checks from `docs/TEST_PLAN.md`; never claim a check passed without running it.
- Record material implementation choices in `docs/DECISIONS.md`.
"""


def render_build_documents(document: SolutionCaseDocument) -> list[BuildDocument]:
    if document.status != "ready":
        return []

    flow = document.primary_flow
    architecture = document.architecture
    assert flow is not None
    assert architecture is not None

    build_contract = f"""# Build Contract: {document.project.project_name}

## Objective

{document.mvp_goal}

## Source

- Problem Case version: {document.problem_case_version}
- Delivery targets: {', '.join(document.project.delivery_targets)}

## Constraints

{_bullets(document.constraints)}

## Non-goals

{_bullets(document.non_goals)}

## Primary workflow

- Actor: {flow.actor}
- Trigger: {flow.trigger}

{_numbered(flow.steps)}

Success: {flow.success_outcome}

## Build shape

- Stack: {', '.join(architecture.stack)}
- Components: {', '.join(component.name for component in architecture.components)}
- Integrations: {', '.join(architecture.integrations) if architecture.integrations else 'None specified'}

## Completion boundary

The build is complete only when every required feature meets its acceptance criteria and the relevant checks in `TEST_PLAN.md` have been run and recorded. Anything outside this contract requires an explicit decision before implementation.
"""

    features = f"""# Features

Features describe product behavior. Tasks in `TASKS.md` describe the engineering work needed to deliver it.

{_feature_lines(document)}
"""

    requirements = f"""# Requirements

These testable requirements are derived from the approved feature acceptance criteria.

{_requirement_lines(document)}
"""

    tasks = f"""# Tasks

Complete tasks in dependency order. Use the stable task IDs when reporting progress through MCP.

{_task_lines(document)}
"""

    test_plan = "\n\n".join([
        "# Test Plan",
        "Only report checks that were actually run. Record failures and blockers rather than weakening an acceptance criterion.",
        _test_section("Unit", document.test_plan.unit if document.test_plan else []),
        _test_section("Integration", document.test_plan.integration if document.test_plan else []),
        _test_section("End-to-end", document.test_plan.end_to_end if document.test_plan else []),
    ])

    decisions = """# Decisions

Record only implementation decisions that affect the approved Build Contract. Do not use this file to add product scope.

| Date | Decision | Reason | Affected feature/task |
| --- | --- | --- | --- |
| _Add when needed_ | | | |
"""

    agents = _agent_instructions(document, "Coding agent")
    claude = _agent_instructions(document, "Claude Code")
    copilot = "---\napplyTo: \"**\"\n---\n\n" + _agent_instructions(document, "GitHub Copilot")

    rows = [
        BuildDocument(key="build_contract", title="Build Contract", filename="docs/BUILD_CONTRACT.md", content=build_contract),
        BuildDocument(key="features", title="Features", filename="docs/FEATURES.md", content=features),
        BuildDocument(key="requirements", title="Requirements", filename="docs/REQUIREMENTS.md", content=requirements),
        BuildDocument(key="tasks", title="Tasks", filename="docs/TASKS.md", content=tasks),
        BuildDocument(key="test_plan", title="Test Plan", filename="docs/TEST_PLAN.md", content=test_plan),
        BuildDocument(key="decisions", title="Decisions", filename="docs/DECISIONS.md", content=decisions),
        BuildDocument(key="agents", title="AGENTS.md", filename="AGENTS.md", content=agents, editable=False),
        BuildDocument(key="claude", title="CLAUDE.md", filename="CLAUDE.md", content=claude, editable=False),
        BuildDocument(
            key="copilot_instructions",
            title="Copilot instructions",
            filename=".github/copilot-instructions.md",
            content=copilot,
            editable=False,
        ),
    ]
    return rows


def ensure_build_documents(document: SolutionCaseDocument) -> SolutionCaseDocument:
    if document.status != "ready" or document.documents:
        return document
    return document.model_copy(update={"documents": render_build_documents(document)})


def replace_build_document(
    document: SolutionCaseDocument,
    key: str,
    content: str,
    *,
    next_version: int,
) -> SolutionCaseDocument:
    current = ensure_build_documents(document)
    match = next((item for item in current.documents if item.key == key), None)
    if match is None:
        raise KeyError(key)
    if not match.editable:
        raise PermissionError(key)
    documents = [
        item.model_copy(update={"content": content}) if item.key == key else item
        for item in current.documents
    ]
    updates: dict = {
        "version": next_version,
        "approval_status": "draft",
        "approved_version": None,
        "approved_at": None,
        "documents": documents,
    }
    if key == "build_contract":
        updates.update(_parse_build_contract(content, current))
    elif key == "features":
        updates["features"] = _parse_features(content)
    elif key == "requirements":
        updates["features"] = _parse_requirements(content, current)
    elif key == "tasks":
        updates["tasks"] = _parse_tasks(content, current)
    elif key == "test_plan":
        updates["test_plan"] = _parse_test_plan(content)
    edited = current.model_copy(update=updates)
    validated = SolutionCaseDocument.model_validate(
        edited.model_dump(mode="json", by_alias=True)
    )
    if key == "features":
        return _refresh_document(validated, "requirements")
    if key == "requirements":
        return _refresh_document(validated, "features")
    return validated


def _refresh_document(document: SolutionCaseDocument, key: str) -> SolutionCaseDocument:
    rendered = {item.key: item for item in render_build_documents(document)}
    replacement = rendered[key]
    documents = [
        item.model_copy(update={"content": replacement.content}) if item.key == key else item
        for item in document.documents
    ]
    return document.model_copy(update={"documents": documents})


def _heading_sections(content: str) -> dict[str, str]:
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", content, re.MULTILINE))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        sections[match.group(1).strip()] = content[match.end():end].strip()
    return sections


def _require_section(sections: dict[str, str], title: str) -> str:
    value = sections.get(title, "").strip()
    if not value:
        raise ValueError(f"{title} section is required")
    return value


def _section_bullets(body: str) -> list[str]:
    return [line[2:].strip() for line in body.splitlines() if line.startswith("- ")]


def _labeled_value(body: str, label: str, *, bullet: bool = False) -> str:
    prefix = r"-\s*" if bullet else ""
    match = re.search(rf"^{prefix}{re.escape(label)}:\s*(.+?)\s*$", body, re.MULTILINE)
    if match is None:
        raise ValueError(f"{label} line is required")
    return match.group(1).strip()


def _parse_build_contract(
    content: str,
    current: SolutionCaseDocument,
) -> dict:
    title = re.search(r"^#\s+Build Contract:\s*(.+?)\s*$", content, re.MULTILINE)
    if title is None or title.group(1).strip() != current.project.project_name:
        raise ValueError("Build Contract title must keep the project name")
    sections = _heading_sections(content)
    objective = _require_section(sections, "Objective")
    source = _require_section(sections, "Source")
    constraints_body = _require_section(sections, "Constraints")
    non_goals = _section_bullets(_require_section(sections, "Non-goals"))
    flow_body = _require_section(sections, "Primary workflow")
    build_shape = _require_section(sections, "Build shape")
    if not non_goals:
        raise ValueError("Non-goals requires at least one bullet")
    if _labeled_value(source, "Problem Case version", bullet=True) != str(current.problem_case_version):
        raise ValueError("Problem Case version cannot be changed in Build")
    try:
        raw_targets = _labeled_value(source, "Delivery targets", bullet=True)
    except ValueError:
        raw_targets = _labeled_value(source, "Delivery target", bullet=True)
    if _csv(raw_targets) != current.project.delivery_targets:
        raise ValueError("Delivery targets must be changed through project setup")

    steps = [
        match.group(1).strip()
        for match in re.finditer(r"^\d+\.\s+(.+?)\s*$", flow_body, re.MULTILINE)
    ]
    if not steps:
        raise ValueError("Primary workflow requires at least one numbered step")
    flow = SolutionPrimaryFlow(
        actor=_labeled_value(flow_body, "Actor", bullet=True),
        trigger=_labeled_value(flow_body, "Trigger", bullet=True),
        steps=steps,
        success_outcome=_labeled_value(flow_body, "Success"),
    )

    architecture = current.architecture
    if architecture is None:
        raise ValueError("Build shape requires architecture data")
    stack = _csv(_labeled_value(build_shape, "Stack", bullet=True))
    if not stack or stack == ["None specified"]:
        raise ValueError("Build shape requires at least one stack item")
    component_names = _csv(_labeled_value(build_shape, "Components", bullet=True))
    if len(component_names) != len(architecture.components):
        raise ValueError("Build shape must keep the same number of architecture components")
    integrations_value = _labeled_value(build_shape, "Integrations", bullet=True)
    integrations = [] if integrations_value.lower() == "none specified" else _csv(integrations_value)
    return {
        "mvp_goal": objective,
        "constraints": _section_bullets(constraints_body),
        "non_goals": non_goals,
        "primary_flow": flow,
        "architecture": architecture.model_copy(update={
            "stack": stack,
            "components": [
                component.model_copy(update={"name": name})
                for component, name in zip(architecture.components, component_names, strict=True)
            ],
            "integrations": integrations,
        }),
    }


def _sections(content: str) -> list[tuple[str, str, str]]:
    matches = list(re.finditer(r"^##\s+([^:\n]+):\s*(.+?)\s*$", content, re.MULTILINE))
    rows: list[tuple[str, str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        rows.append((match.group(1).strip(), match.group(2).strip(), content[match.end():end].strip()))
    return rows


def _required_line(body: str, label: str) -> str:
    match = re.search(rf"^{re.escape(label)}:\s*(.+?)\s*$", body, re.MULTILINE)
    if match is None:
        raise ValueError(f"{label} line is required")
    return match.group(1).strip()


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _bullets_after(body: str, label: str) -> list[str]:
    match = re.search(
        rf"^{re.escape(label)}:\s*\n(?P<rows>(?:- .+(?:\n|$))+)",
        body,
        re.MULTILINE,
    )
    if match is None:
        raise ValueError(f"{label} bullets are required")
    return [line[2:].strip() for line in match.group("rows").splitlines() if line.startswith("- ")]


def _description_before(body: str, label: str) -> str:
    marker = re.search(rf"^{re.escape(label)}:", body, re.MULTILINE)
    if marker is None:
        raise ValueError(f"{label} line is required")
    description = body[:marker.start()].strip()
    if not description:
        raise ValueError("section description is required")
    return description


def _parse_features(content: str) -> list[SolutionFeature]:
    rows = _sections(content)
    if not rows:
        raise ValueError("FEATURES.md requires '## feature-id: title' sections")
    return [
        SolutionFeature(
            id=feature_id,
            title=title,
            summary=_description_before(body, "Problem units"),
            problem_unit_ids=_csv(_required_line(body, "Problem units")),
            evidence_ids=_csv(_required_line(body, "Evidence")),
            acceptance_criteria=_bullets_after(body, "Acceptance criteria"),
        )
        for feature_id, title, body in rows
    ]


def _parse_requirements(
    content: str,
    current: SolutionCaseDocument,
) -> list[SolutionFeature]:
    rows: list[tuple[str, str, str]] = []
    for line in content.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [
            cell.strip().replace("\\|", "|")
            for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))
        ]
        if len(cells) != 3 or cells[0] == "ID" or set(cells[0]) <= {"-", ":"}:
            continue
        if not all(cells):
            raise ValueError("every requirement row needs an ID, feature ID, and requirement")
        rows.append((cells[0], cells[1], cells[2]))
    if not rows:
        raise ValueError("REQUIREMENTS.md requires requirement table rows")
    requirement_ids = [row[0] for row in rows]
    if len(requirement_ids) != len(set(requirement_ids)):
        raise ValueError("requirement IDs must be unique")

    known_features = {feature.id: feature for feature in current.features}
    criteria: dict[str, list[str]] = {feature_id: [] for feature_id in known_features}
    for _, feature_id, requirement in rows:
        if feature_id not in known_features:
            raise ValueError(f"requirement references unknown feature {feature_id}")
        criteria[feature_id].append(requirement)
    missing = [feature_id for feature_id, values in criteria.items() if not values]
    if missing:
        raise ValueError(f"requirements missing for feature(s): {', '.join(missing)}")
    return [
        feature.model_copy(update={"acceptance_criteria": criteria[feature.id]})
        for feature in current.features
    ]


def _parse_tasks(content: str, current: SolutionCaseDocument) -> list[SolutionTask]:
    rows = _sections(content)
    if not rows:
        raise ValueError("TASKS.md requires '## task-id: title' sections")
    statuses = {task.id: task.status for task in current.tasks}
    tasks: list[SolutionTask] = []
    for task_id, title, body in rows:
        dependencies = _required_line(body, "Depends on")
        tasks.append(SolutionTask(
            id=task_id,
            title=title,
            description=_description_before(body, "Features"),
            feature_ids=_csv(_required_line(body, "Features")),
            depends_on=[] if dependencies.lower() == "none" else _csv(dependencies),
            acceptance_checks=_bullets_after(body, "Completion checks"),
            status=statuses.get(task_id, "todo"),
        ))
    return tasks


def _parse_test_plan(content: str) -> SolutionTestPlan:
    values: dict[str, list[str]] = {}
    matches = list(re.finditer(r"^##\s+(Unit|Integration|End-to-end)\s*$", content, re.MULTILINE))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        body = content[match.end():end]
        values[match.group(1)] = [
            line[2:].strip()
            for line in body.splitlines()
            if line.startswith("- ")
        ]
    if set(values) != {"Unit", "Integration", "End-to-end"}:
        raise ValueError("TEST_PLAN.md requires Unit, Integration, and End-to-end sections")
    return SolutionTestPlan(
        unit=values["Unit"],
        integration=values["Integration"],
        end_to_end=values["End-to-end"],
    )


def build_export_zip(document: SolutionCaseDocument) -> tuple[str, bytes]:
    current = ensure_build_documents(document)
    safe_name = re.sub(r"[^a-z0-9]+", "-", current.project.project_name.lower()).strip("-")
    filename = f"{safe_name or 'immensity'}-build-v{current.version}.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in current.documents:
            archive.writestr(item.filename, item.content.rstrip() + "\n")
        archive.writestr(
            "build.json",
            json.dumps(current.model_dump(mode="json", by_alias=True), indent=2) + "\n",
        )
    return filename, buffer.getvalue()
