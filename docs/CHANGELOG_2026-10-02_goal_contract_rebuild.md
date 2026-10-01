# 2026-10-02 Goal contract alignment during plan rebuilding

Generic plan rebuilding now uses the active GoalContract as the source of criterion IDs and order. It matches existing tasks by criterion statement when reusing titles and headings, and maps feedback amendments through the original task key so reordered criteria cannot receive another task's change. Old SC prefixes are removed from reused titles.

Candidate validation also checks that each execution task's criterion statement matches the active contract, and rejects placeholder task titles. This prevents a plan from passing coverage solely because its criterion IDs are present while the tasks describe different goals.

Validation: 133 related tests passed locally; the rebuilt candidate passed contract validation in a read-only check. The deployed web health check passed after startup. The change does not approve or apply any project plan.
