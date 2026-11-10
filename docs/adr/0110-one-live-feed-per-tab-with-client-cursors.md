# One live feed per browser tab, with the client owning each Run's cursor

Status: accepted

Every open tab needs live Run data: the Focus list, the Runs now running, and each open Run tab's header and events. Browsers allow only about six HTTP/1.1 connections per origin across all tabs, and the local server speaks HTTP/1.1, so a stream per Run tab runs out once two windows are open. The feed therefore uses one stream per tab, and the state a reconnect needs travels with the client.

1. **One stream.** Each tab opens one `EventSource` on `GET /api/live`, carrying `focus`, `running`, `run` and `events` frames.
2. **Cursors ride in the URL.** Each open Run tab sends `run=<project>:<run_id>:<seq>`, where `seq` is the last event the tab has shown. The server keeps no subscription state: the URL is the subscription, and the server replays each Run's events after its cursor.
3. **No `id:` lines.** One SSE event id cannot carry several Runs' cursors, so the client's own cursors replace `Last-Event-ID`.
4. **The client drops replays.** An event whose `seq` is at or below its Run's cursor is dropped, so a replayed line never shows twice.
5. **Reconnect with the current set.** A dropped stream reopens after 3 s with the cursors held at that moment. Opening or closing a Run tab closes the stream and reopens it with the new set.

We considered,

- **One stream per Run tab.** Rejected: the connection limit above; a second window exhausts it.
- **`Last-Event-ID` resume.** Rejected: a single id cannot carry several Runs' cursors.
- **Server-held subscriptions.** Rejected: the server would keep state per tab, lose it on restart, and need a way to reconcile it with each tab.

Trade-offs accepted: the URL grows with the number of open Run tabs. Every change to the set reconnects the one stream, so the snapshot is replayed from cursors on each change (cheap, since the cursors skip what is already shown). The server cannot check a cursor, so a client that sends a wrong one gets a wrong replay.
