# Phase 1 Requirements: Fix Real Teacher Classroom Assignments and Screen Visibility

## Critical Production Issue
The teacher account `sameh` logs in successfully but sees:
- My classrooms: 0
- Screens: 0
- Screens online: 0
- "You are not assigned to any classrooms yet. Please ask an administrator."

Meanwhile, the administrator account `hazem` sees two classrooms, two registered screens, four teacher accounts, and two offline screens.

## Functional Requirements

### Core Authorization Model
1. **Teacher must see all classrooms explicitly assigned to that teacher**, along with the registered screens belonging to those classrooms.
2. **Teacher must not see classrooms outside their authorization.**
3. **Administrator must be able to assign teachers to multiple classrooms using the existing administration workflow.**

### Data Integrity Rules
4. Never assign every classroom to every teacher to make the dashboard non-empty.
5. Never invent assignments or expose all school classrooms as a workaround.
6. Never delete or recreate the existing two classrooms or two registered screens.
7. Preserve all existing competition and question history.
8. If the actual teacher has no classroom assignments, determine whether this is missing administrative data or an application defect. Report the evidence and fix the appropriate cause.
9. Do not silently assign `sameh` to classrooms without explicit authorization.

### Consistency Requirements
Make the authorization model consistent across:
10. Teacher dashboard.
11. Teacher classroom list.
12. Teacher screen list.
13. Question bank.
14. Competition creation.
15. Competition detail and start confirmation.
16. Reports.
17. Live competition controls.

### Screen Visibility Requirements
18. The teacher must see the registered screens assigned to their authorized classrooms. Display screen ID, classroom, status, and last-seen information when available.
19. Offline screens must be identified as offline, not treated as nonexistent. An offline screen count must not be confused with an unregistered screen count.
20. Keep screen creation, editing, deletion, and Screen ID management administrator-only.

### Data Model Requirements
21. If the current data model cannot express one teacher assigned to several classrooms, and several teachers assigned to one classroom, propose the smallest safe migration.
22. Do not add a duplicate relation if one already exists.

## Test Requirements

Add or update tests proving:
23. A teacher assigned to two classrooms sees both.
24. The same teacher sees the registered screens assigned to those classrooms.
25. An unassigned teacher sees no unauthorized classroom.
26. A teacher cannot access another teacher's classroom by changing a URL or submitting a forged ID.
27. An administrator retains the correct school-wide view.
28. Offline screens remain visible with an accurate offline status.
29. Competition targeting uses the same authorization scope.
30. No screen-management permissions are accidentally granted to teachers.

## Runtime Acceptance (Mandatory)

31. Run the production tests and deploy the permanent source fix through the normal Docker workflow.
32. Then use the real production login and browser session for `sameh`.
33. Verify:
    - Dashboard displays the actual authorized classroom count.
    - The teacher can open each assigned classroom.
    - The associated screens are displayed.
    - The question bank and competition creation use the same classroom authorization.
    - Direct requests for unauthorized classrooms are denied.
    - The administrator still sees the existing classrooms and screens.
    - Existing school data remains intact.

34. If `sameh` genuinely has no assignments, demonstrate the exact administrator workflow for assigning the correct classrooms, then verify the teacher sees them after assignment. Do not manufacture assignment records for the test.

## Stop Condition
Do not start Phase 2.