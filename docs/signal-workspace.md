# Signal workspace

Signal turns a saved problem cluster into a versioned, evidence-backed problem
case. The same case powers three frontend screens:

1. **Overview** explains the problem thesis, atomic problem units, audiences,
   alternatives, assumptions and the recommended validation focus.
2. **Evidence** exposes the source records behind every generated claim.
3. **Conversation** lets the user interrogate the case and accept explicit,
   reviewable proposals. Chat never silently rewrites the case.

## Lifecycle

Adding a problem to Pipeline creates a Signal case and queues its initial
analysis. The Pipeline request returns immediately. A worker prepares evidence,
calls the configured model once, validates the complete `signal-case.v1`
document and saves an immutable version. At most one schema-repair call is
allowed.

Opening Signal reads the current case; it does not regenerate it. When the
source fingerprint changes, the case becomes `stale` and the user may request a
refresh. A failed refresh preserves the last valid version and reports a safe
error separately.

## Model and non-model work

The initial analysis and each Ask Signal assistant turn call a model. Metrics,
evidence browsing, filters, user overrides, citation navigation, history and
handoffs are deterministic application operations and do not call a model.

Generated claims must cite stored evidence IDs. Inferred and observed claims
without citations, dangling references and duplicate object IDs fail validation
and cannot become current.

## Source fingerprint

The fingerprint is a SHA-256 hash of the ordered evidence identity and update
state. It is stable when only display order changes and changes when evidence is
added, removed or materially updated. The job records the fingerprint it
analyzed; the case compares that value with the current source fingerprint to
detect staleness.

## Version and override policy

Generated versions are immutable. User edits are stored as patches in
`signal_case_overrides`, keyed by case, object kind and object ID. Regeneration
creates a new generated version, then reapplies compatible user patches. It
never overwrites user-authored confirmation, rejection, pinning or notes.

