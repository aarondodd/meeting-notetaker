Also, alongside the synthesis above, please pull out the meeting's
main topics. Think of these as the section headings or themes a
reader scanning the meeting summary would want to see -- the things
the meeting was actually *about*. Put them in a section titled
exactly:

```
## Suggested Topics (auto-extracted)
```

The body of that section should be a single fenced JSON code block
containing an array of short topic strings:

```json
[
  "Q3 hiring plan",
  "Backend migration cutover",
  "Customer onboarding redesign"
]
```

A few ground rules:

- Keep each topic short (a noun phrase, 2-6 words). Skip generic
  fillers like "Updates" or "Discussion".
- Only include topics the meeting actually substantively discussed --
  passing mentions don't count.
- Skip people's names, project codenames already obvious from
  context, and one-off jokes.
- An empty array `[]` is fine when the meeting didn't have clear
  topical structure (a casual catch-up, say).
- Put this section after the Attendee Details appendix if both are
  present, so the synthesis body stays at the top.

This feeds my classification dropdown -- I review which ones to
accept and the app remembers my choices for future meetings on the
same topic.
