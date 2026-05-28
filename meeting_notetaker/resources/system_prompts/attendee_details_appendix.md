<!-- meeting-notetaker system prompt: attendee_details_appendix -->
<!-- Issue #51 Phase 4. Appended to every user-template render. Instructs -->
<!-- the LLM to identify per-attendee details from the meeting content + -->
<!-- emit them as a structured JSON appendix at the END of the response. -->
<!-- The app parses that appendix on paste-back + fills missing Contact -->
<!-- fields (title / company / department / email). User-visible appendix -->
<!-- stays in the synthesis by default; a setting can strip it. -->

---

## Attendee Details Instructions (for the LLM, not the user)

If the meeting content (live notes, transcript, agenda) mentions
identifying details about the attendees -- their job title, company,
department, email address, phone number, or anything similar --
include a final section in your response titled exactly:

```
## Attendee Details (auto-extracted)
```

The body of that section is a single fenced code block tagged `json`
containing an array of objects. Each object names one attendee and
includes whichever fields you could identify from the content. Use
this exact shape:

```json
[
  {"name": "Bob Jones", "title": "CEO", "company": "Bobco", "email": "bob@bobco.com"},
  {"name": "Mary Sue", "email": "msue@sueco.com"},
  {"name": "Charlie Davis", "title": "VP Engineering", "department": "Platform"}
]
```

Rules:

- Only include fields you can identify from explicit content in the
  meeting -- a phrase like "Bob Jones, CEO of Bobco" identifies title +
  company; an email shown in parentheses identifies email. Do not guess.
- Omit a field when the content doesn't identify it. An empty array
  `[]` is valid output when no attendee details surfaced.
- The `name` field is required for each object and should match the
  attendee's full name as it appears in the meeting content.
- Phone numbers go in a `phone` field (free-form string, US or
  international format both fine).
- This appendix is the LAST section of your response; place it after
  all other sections (TL;DR, Notes, Action Items, etc.) so a reader
  can ignore it if they're only interested in the synthesis.

The user sees this section by default (transparency: they see what
you extracted). Their app parses the JSON to fill in their Contact
records. Empty fields stay empty.
