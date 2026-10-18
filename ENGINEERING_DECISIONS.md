# Engineering Decisions

The brief leaves a lot to interpretation, so I've written down the assumptions I made first. After that come the technical decisions I thought about most: what else I considered, what I chose, and what each choice costs.

## Assumptions

These are the decisions I made without a product manager to ask. **The first three are the ones I'd check first**, because if they're wrong the data model or the permission rules would have to change, not just the UI.

1. **People pull work; nobody pushes it to them.** New items appear unassigned in a team's queue and someone has to claim them. Leads can't assign work to a person. They can only unassign items that are stuck so someone else can pick them up.

2. **Anyone can raise an item for any team, but only that team works on it.** Support finds payment problems all the time, so they need to be able to raise them. They can follow the item and comment on it, but not edit it.

3. **No one can approve their own work.** Someone else has to approve it, either another Lead or an Admin. This is common in payments and compliance, but it means every team must have at least two Leads (or an Admin who can temporarily stand in).

4. Roles are per team (Viewer, Member, Lead) as the same person usually has different responsibilities in different teams. Admin is one global flag.

5. Nothing is ever deleted. If an item was raised by mistake it gets cancelled and a cancelled item can't come back.

6. The title and description can't be edited after an item is raised. **Only the Lead can change priority**, and the person working on the item can change its due date. Leads see the whole queue, so they own priority. The assignee knows how long the work will take, so they own the timeline.

7. Any change making an item less urgent needs a reason: lowering priority, pushing the due date later, cancelling, sending work back, unassigning, handing off or reopening.

8. SLA targets are 4 hours for P1, 24 hours for P2, 3 days for P3 and 5 days for P4. The clock starts when the current team receives the item and stops when it's resolved. If an item is handed off, the new team receives a fresh clock, and the 'waiting since' date shows how long the requester has been waiting overall.

9. A resolved or closed item can be reopened. If the person who did the work reopens it, it goes straight back to them. If a Lead or the person who raised it reopens it, it goes back to the queue.

10. People only get notified about things affecting them. I didn't want Leads to get a notification every time someone claims something, as people stop reading notifications when there are too many.

## 1. Two people acting on the same item at once

This was the part I cared most about. If two people click Claim at the same time, or save an edit on a page that's been open for ten minutes, the obvious solution (load the item, check it in Python, save it) has a race condition between the check and the save. Both pass the check and the second silently overwrites the first.

I looked at locking the row for the whole request, and at the last-write-wins approach. Locking is safe, but useless in the context of a page that's been open for ten minutes. Last-write-wins is exactly the race condition the brief is warning against.

What I did was **put the check inside the update itself**. Claiming runs `UPDATE ... WHERE id = ? AND status = 'open' AND assignee IS NULL`. Postgres locks the row for the first request, the second waits, re-checks the condition, and updates nothing. **The second person gets a 409** telling them who got there first. For edits, status changes and handoffs, **each request carries the version number the user was looking at**, and the update only applies if it still matches. The database increments it, so I never do the arithmetic in Python.

Something I changed along the way: I originally checked business rules first and the version second. When I tried a stale edit that also lowers the priority without a reason, the page said 'reason required', which is the wrong message when everything on your screen is out of date. Now the service reloads the item and **rejects a stale request before any business rules are checked**. Comments don't bump the version, otherwise someone asking 'any update?' would ruin everyone else's open edits.

**The cost** is that people sometimes have to repeat an edit. To make that less of a pain, the conflict screen shows what's changed since they opened the page, and keeps what they typed. I deliberately don't merge changes, as the user made a decision based on information that's now out of date.

To check that the claim test actually detects the race, I temporarily substituted a naive check-then-save version. All three threads 'won' on every run. With the real version in, exactly one thread wins.

## 2. Keeping history that can't disagree with the item

Every important change needs to be recorded and visible. I could have stored a snapshot of the item after each change, or used database triggers, but snapshots are hard to read as a timeline, and triggers don't know who made the change and why.

So a single `ActivityEvent` table stores the actor, type of change, old and new values, and reason. Every write goes through `services.py`, which saves the change and the event **in the same transaction**. If writing the event fails, the change is rolled back too, and there's a test that simulates exactly that. The model refuses to update or delete events, the admin shows them read-only, and users or teams that have history can't be deleted.

Both the website and the API call the same service functions, so there's only one place where changes and history get written.

**The weakness** is that append-only is only enforced in the app. Someone with direct database access could still edit history. A trigger or a restricted database role would prevent that. The table also grows forever, which is why the history endpoint is paginated.

## 3. Who can see and do what

Permissions had to be enforced on the server, per team, not by hiding buttons. I didn't want a proliferation of checks in every view, and a full object-permissions library seemed like a lot of machinery for a handful of roles.

There's instead one small module, `permissions.py`, that looks up the user's role in the item's team and checks it against a table of which roles can do which actions. Status changes have their own rules in `workflow.py`. Every list starts from a query that only returns items the user is allowed to see. That means filters can only narrow things down, and all filtering happens in the database, not in the browser.

If a user can't see an item at all they get a **404**, the same response as for an item that doesn't exist, so outsiders can't probe IDs. If they can see it but aren't allowed to do something, they get a **403**. The 404 is less helpful when debugging, but it doesn't give away which items exist, so I think it's the safer choice. The permission check itself is the real protection either way.

## 4. The workflow

The allowed status changes live in one dictionary that maps each move (from, to) to who can make it and whether a reason is needed. **Anything not in the dictionary is rejected**, so jumping from Open straight to Closed, or reopening a cancelled item, fails without any special code. Claiming isn't in the table at all, because it's the only way an item goes from Open to In Progress, and it has its own atomic update.

I thought about using a state machine library, but the states aren't the interesting part, the 'who can do this' rules are, and a dictionary keeps them all on one screen. The same rules decide which buttons a user sees, but the server checks every request anyway.

Adding a new status means touching the model, the table and the button labels. That's three places, but they're all obvious, which matters if I'm asked to add one live.

## 5. Double clicks and retries

A double click or a network retry shouldn't create two items. Disabling the button helps in the browser, but does nothing for API clients, and checking 'have I seen this key before?' first has the same race as claiming.

The idempotency key gets **inserted first, under a unique constraint, in the same transaction as the action**. If two copies arrive at the same time, Postgres makes the second insert wait for the first, then it fails and returns the saved response. If the same key comes in with a different request body, it's rejected with a 422 instead of returning someone else's answer. **Only successful requests are saved**, so if a request fails validation, the user can fix it and retry with the same key. The API uses an `Idempotency-Key` header, the web forms send a hidden key, and buttons disable themselves when clicked.

A nice side effect: without this, someone who double-clicks Claim would be told 'someone else has already claimed this item', and that someone would be them.

Keys never expire in this version. In production I'd clear them out after a day or so.

## 6. Notifications in the background

Notifications shouldn't slow down or break the request that triggered them. Sending them inline makes the user wait. Saving the change and then putting a job on Redis has a gap: if the process dies in between, the notification is lost, and it also adds Redis and Celery to a setup I wanted to keep to one command.

I used an **outbox table in Postgres**. The job row is written **in the same transaction as the change**, so a job exists only if the change actually happened. A failed claim leaves no job behind. A separate worker process picks up jobs with `SELECT ... FOR UPDATE SKIP LOCKED`, so more than one worker could run without taking the same job. Each job runs in its own transaction, so if the worker crashes halfway through, the job just stays pending. Failures retry after 10s, 20s, 40s and so on, up to 10 minutes, and after 5 attempts the job is marked failed with the error kept. Each notification is tied to exactly one job, so **processing a job twice still sends only one notification**.

Notifications show up a second or two late because the worker polls. At much higher volume I'd add a dedicated queue and use the outbox to feed it, so the 'written with the change' guarantee doesn't go away.

## 7. Big lists

With tens of thousands of items, everything has to be filtered and paginated on the server. Page numbers with OFFSET are familiar, but the database still reads and throws away every row before the page you asked for, and if new items arrive while you're browsing, rows shift and you see duplicates.

I used **cursor pagination**. Lists are sorted **most urgent first**: priority, then the nearest SLA deadline, then ID so the order is always unique. The cursor remembers the last row on the page, and the next page asks for rows after it, so every page costs the same. I fetch one extra row to know whether there's another page, instead of running a count. Page size is capped at 100.

The indexes match the queries the app actually runs: team and status in the same order as the sort, assignee and status for 'Assigned to me', status and deadline for 'Overdue', creator for 'Raised by me', and item and time for history. Search uses Postgres full-text search with a GIN index on the title and description.

**The downside** is that you can only go forward, or back to the start, and you can't jump to page 50. For a work queue that's fine. Postgres search won't handle typos the way a real search engine would, and the dashboard counts are live queries that I'd cache first at 10x the data.

## 8. Django templates instead of React

The suggested stack used React, but the brief says to use what you know best, and I know Django much better. I'll also be asked to explain and change the code live, and I'd rather do that in code I'm confident in.

The pages are server-rendered Django templates with plain CSS and a few lines of JavaScript to disable buttons on submit. They call the same service functions as the API, so the rules can't drift apart. Instead of optimistic updates, a conflict re-renders the page with a 'your change wasn't saved' panel showing what changed, with the user's input still filled in. I kept the design calm and warm, with a small mascot, because this is a tool people open when something is already going wrong.

**What I give up** is instant updates. Every action reloads the page and nothing updates live. The JSON API is still there if someone wants to build a richer frontend later.

## What I chose not to build

- Live updates. The version check catches stale edits, so this would be a convenience.

- Email or Slack. Notifications are in-app only, and the outbox handler is where a new channel would go.

- Assigning work to a named person, because of the pull model. If it's needed, it'd be a Lead-only action using the same conditional update as claim.

- Editing titles and descriptions, and deleting anything.

- SSO, sign-up, password reset and token expiry. The brief allows seeded users and simple auth.

- Business-hours SLAs, pausing the clock while waiting for approval, attachments, reporting and a mobile app. These are in the README under 'Next week I would…'.
