Also, alongside the synthesis above, please pull out any per-attendee
details the meeting content reveals -- job title, company, department,
email address, phone number -- and put them in a final section titled
exactly:

```
## Attendee Details (auto-extracted)
```

The body of that section should be a single fenced JSON code block
containing an array of objects, one per attendee, with whichever
fields the content actually identifies. The shape I want:

```json
[
  {"name": "Bob Jones", "title": "CEO", "company": "Bobco", "email": "bob@bobco.com"},
  {"name": "Mary Sue", "email": "msue@sueco.com"},
  {"name": "Charlie Davis", "title": "VP Engineering", "department": "Platform"}
]
```

A couple of ground rules I care about:

- Only include fields the content explicitly identifies -- "Bob Jones,
  CEO of Bobco" gives me title + company; an email in parentheses
  gives me email. Don't guess and don't infer from naming conventions.
- Skip a field when the content doesn't identify it. An empty array
  `[]` is fine when nothing surfaced.
- The `name` field is required on every object. Use the attendee's
  full name as it appears in the meeting.
- Phone numbers go in a `phone` field, any format is fine.
- Put this section LAST in your response so I can scan the synthesis
  without scrolling past it.

This is for my own contact records -- I review what you extracted and
my app fills in any details I don't already have. Thanks.
