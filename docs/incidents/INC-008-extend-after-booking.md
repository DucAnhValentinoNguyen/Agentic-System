# INC-008: "extend my meeting" restarted the booking from scratch

**Detection.** A screenshot of a real chat. After a successful 30-minute booking the visitor wrote "can I extend the
meeting to 1 hour"; the assistant offered the booking page again and asked for the name, email and topic. The visitor
answered "same name, address and topic, just extend...", and it asked again, three times.

**Cause.** After a booking the flow threw everything away (`booking = {}`). There was no notion of "the meeting I just
made", no way to change a booking's length, and no way to reuse earlier details. Separately, every booking was a fixed
30 minutes and one per email, so a visitor could not have more time at all.

**Fix.** The conversation remembers the last booking. Visitors can ask for a length (30/60/90 minutes) when booking, and
extend a booking afterwards in plain language or with a chip ("Extend to 60 min"); "Book another time" reuses the saved
details. A visitor can hold **3 half-hour slots in total** (three calls, one 90-minute call, or 60 + 30), enforced from
the calendar itself so it holds across chats and restarts. A recurring Wednesday and Friday 13:00-14:00 block is mirrored
from the Google schedule, so a run of slots never crosses it. Extending patches the same calendar event. Only a booking
made in the same chat can be extended. 19 rule/tool tests with a fake calendar and 8 end-to-end conversation tests,
including this screenshot's conversation.

**Confirmation on the live service and the real calendar.** Book 30 minutes, "can I extend the meeting to 1 hour",
then "Extend to 90 min": one event, 14:30 to 16:00, `twin_slots = 3` (deleted afterwards).

**Two more bugs found by that live test, both fixed.** The offered times listed a slot twice when a day's first free
slot was after 14:00 (older than today's changes). And "extend it to 2 hours" fell back to asking for the name again
because the extractor only reported 60 or 90; it now reports any requested length and the assistant explains the
90-minute limit.

**Limits.** The Google booking page's own availability cannot be read by the API, so its weekly pattern is a copy in
`app/slots.py`; if it changes, the copy must too. Someone using Google's own page directly is not capped (Google has no
per-guest limit). Extending works only into the slots right after the call, not before it.
