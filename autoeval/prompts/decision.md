First tool call for every new user request must be mode decision: choose `planning` or `instant`.

Evaluate the user's task request for current repository / parent directory based on complexity, estimated efforts, multi-turn changes spanning across multiple files or simple trivial edits/updates.

- `planning`: Invoke this when user's request is complex, introduces a new functionality, adds independent modules or greatly extends functionality for any existing implementation, relates to architectural decisions or 3+ steps, planning for tasks is required, more context or knowledge for the task execution is required including web search or MCP access.

- `instant`: Invoke for all simple trivial request that do not require much planning, and can be completed with simple changes.
