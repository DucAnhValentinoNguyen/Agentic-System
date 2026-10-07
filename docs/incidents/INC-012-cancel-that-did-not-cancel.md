# INC-012: "I've dropped the booking" while the call stayed on the calendar (the assistant claimed an action it could not do)

**Detection.** Duc-Anh tested the chat as a visitor and sent a screenshot: booked a 90-minute call, wrote "please cancel it", got
"No problem, I've dropped the booking." Then tried to book again and was told "You already hold 3 half-hour slots", asked "when
was it?" and "when is my appointment?" and was sent round the booking flow again, each loop ending in the same limit message.

**Diagnosis.** The calendar was the source of truth and still held the 90-minute event, so the limit message was correct and the
cancel message was false. Twin had no cancel tool at all: the "cancel" branch only cleared its own in-progress notes, which is right
for an unfinished booking and wrong once a call exists. There was also no way to ask about an existing call: that question was read
as a new booking request, which restarts the flow and ends at the limit.

**Fix.**
- A `cancel_booking` tool (MCP): it deletes only an event this assistant created for that email address, is idempotent, and gives
  the allowance back. In the chat it always asks first ("To confirm: cancel your call on ... ?", Yes / No).
- Stopping an unfinished booking now says "nothing new was booked" and, if a call exists, "your call on ... is still booked".
- "When is my appointment?" is answered without the model, including "no call booked" after a cancellation.
- At the limit the message names the call and offers "Cancel that call".
- Tests: the screenshot conversation as a flow test, tool tests (owner only, idempotent, time bookable again), 8 more cases.
- Verified live with a throwaway address (book 90 min, ask when, hit the limit, cancel, calendar shows 0 slots held). The leftover
  call from the report was cancelled with the new tool.

**Not done, deliberately.** Cancelling is limited to calls booked in the same chat; a visitor who returns later is sent to email.
Letting a bare email address cancel a call from another session would let anyone cancel anyone's booking.

**Lesson.** An assistant must never report an action it has no tool for, and its claims need testing against the system of record
(the calendar), not against its own conversation state. This was found by a person using it, not by the 35-case suite, which
tests refusals and injection but not whether "done" is true. Related, found a day earlier: a complaint ("you don't remember my
name") was read as a cancel; the extractor now treats only a clear stop as a cancel.
