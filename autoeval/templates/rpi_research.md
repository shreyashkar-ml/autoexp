<!-- template_id: rpi_research -->
<!-- template_version: 2.2.0 -->

# Research Artifact Instruction

Purpose:
- Build a detailed technical documentaiton for the target repository when `autoeval` is initialized there for the first time.
- This artifact is repository-level and not task-specific.
- It is a detailed technical documentation meant for developers and contributors to get a detailed understanding of the entire repository.
- Ignore directories/files mentioned inside `.gitignore`.

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
- When `/autoeval/instructions/research.md` already exists, when behavior changes, update only impacted sections with concrete details of the current functionality of the codebase, don't maintain reference to old implementation, that's not needed at all.
