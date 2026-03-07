<!-- template_id: rpi_feature_list -->
<!-- template_version: 2.2.0 -->

# Feature List Artifact Instruction

Purpose:
- Break down each phase as described in `plan.md` into multiple sub-tasks each with independently verifiable bindings for success.
- Represent phase-to-sub-task mapping where each plan phase can contain multiple sub-tasks.

## Rules
1. Each `sub_task` must contain:
   - `id`
   - `phase_id` (stable link to a phase in `plan.md`)
   - `sub_task_description` (smallest independently testable unit)
   - `verifications` (array of typed verification bindings)
   - `status` (boolean)
2. Order `sub_tasks` by phase order in `plan.md`, then by execution order within each phase.
3. Agents CAN only mutate `status`. All other fields are immutable.
4. Completion is valid only when evidence satisfies every required verification.
5. Use rebaseline workflow for verification-binding changes; do not edit in place.
6. Do not store planning prose in this artifact.
7. Verification bindings must be typed and objectively verifiable.
8. Every `sub_task` must map cleanly to a specific phase boundary from `plan.md`.
9. For test verification, reference linked pytest targets from `.autoeval/instructions/autocheck_map.json`.

## JSON Shape
```json
{
  "schema_version": 1,
    "template": {
      "id": "rpi_feature_list",
      "version": "2.2.0"
    },
  "generated_at": "ISO-8601",
  "sub_tasks": [
    {
      "id": "phase_1_subtask_1",
      "phase_id": "phase_1",
      "sub_task_description": "<smallest testable implementation unit>",
      "verifications": [
        {
          "kind": "pytest",
          "target": "tests/unit/test_example.py::test_ok",
          "required": true
        }
      ],
      "status": false
    }
  ]
}
```
