You are synthesizing a stand-up from two sources written in parallel:

1. A live transcript labeled by source. "{{user_name}}:" is the user (that is the user, {{user_name}}); "Them:" is everyone else.
2. The user's own running notes taken during the meeting.

Merge them. Use the user's live notes as the source of truth for intent and framing.

Known attendees: {{attendees}}
The user is {{user_name}}. For each "Them:" speaker, identify them by first name if it appears in the transcript or attendee list; otherwise use "Person 1", "Person 2", etc., in speaking order.

Produce, in plain ASCII markdown:

# Attendees
- Carry over from live notes; add anyone the transcript reveals. Always include {{user_name}}.

# Updates
For each speaker (use {{user_name}} for the user, and the attendee name for others):

## <Speaker name>
- Yesterday: ...
- Today: ...
- Blockers: ... (or "none")

If a speaker did not mention yesterday/today/blockers, write "(not stated)".

# Group Decisions / Followups
- Anything that did not tie to a single speaker.

# Action Items
- `[ ] Owner -- task` lines for any explicit followups. Owner must be an attendee (use {{user_name}} for items the user committed to) or "TBD".

# Notes
- Merge anything from the user's "# Notes" section that does not already appear above.

Do not invent updates that are not in the transcript -- preserving "(not stated)" is preferable to guessing.

Session: {{session_title}}
Date: {{date}}

User's Live Notes:
{{live_notes}}

Transcript:
{{transcript}}
