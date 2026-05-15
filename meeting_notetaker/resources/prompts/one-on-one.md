You are synthesizing a 1:1 meeting from two sources written in parallel:

1. A live transcript labeled by source -- "Me:" is the user, "Them:" is the other participant.
2. The user's own running notes taken during the meeting (live notes), including any pre-meeting agenda or context.

Merge them. Use the user's live notes as the source of truth for intent and framing; expand with what the transcript supports.

Known attendees: {{attendees}}
When attributing commitments, use the attendee names where possible. Use "Them" only if the transcript identifies the other participant generically.

Produce, in plain ASCII markdown:

# Attendees
- Carry over from live notes; add anyone the transcript reveals.

# Agenda
- Carry over from live notes.

# Topics Covered
- One bullet per topic, ordered as they came up.

# My Commitments
- `[ ] task` lines for things the user committed to.

# Their Commitments
- `[ ] Owner -- task` lines for things the other participant committed to.

# For Next 1:1
- Anything flagged to revisit, can be empty.

# Concerns / Sentiment Shifts
- Any blocker, friction, or notable change in tone. Include the verbatim quote.

# Notes
- The merged narrative, starting from the user's "# Notes" section.

Do not invent or infer beyond what the transcript and live notes support.

Session: {{session_title}}
Date: {{date}}

User's Live Notes:
{{live_notes}}

Transcript:
{{transcript}}
