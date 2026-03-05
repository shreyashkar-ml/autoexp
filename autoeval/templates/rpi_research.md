<!-- template_id: rpi_research -->
<!-- template_version: 2.2.0 -->

# Research Artifact Instruction

Purpose:
- Build repository familiarity context for a target repository when `autoeval` is initialized there for the first time.
- This artifact is repository-level and not task-specific.
- It should remain reusable across future tasks in the same target repository.

## Structure and Guidelines
1. Cover whole-repository architecture and module/component relationships.
2. Provide directory/module overview with one-line purpose notes.
3. Organize into sections for major components or execution paths.
4. Include concrete code references and flow notes that justify understanding.
5. Capture end-to-end traces for key user/system paths.
6. Include runtime/testing/dependency setup plus known risks or gaps.
7. Follow repository `.gitignore` boundaries unless ignored paths are directly relevant.

## Update Rules
- Keep file references current and actionable.
- When behavior changes, update only impacted sections with concrete deltas.
