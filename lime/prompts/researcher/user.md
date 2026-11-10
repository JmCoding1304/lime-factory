# Research Task

## Variables

### prompt

{{prompt}}

### previous_envelope

{{previous_envelope}}

### context_handoff_dir

{{context_handoff_dir}}

### never

{{never}}

{{rules}}

## Task

Check `.lime/kb/` first if it exists — read and grep it before reaching outside.

Answer the question in `prompt` from outside sources: your `web` tool for general search, and for library/API documentation prefer context7 over a blind web search — `curl -s 'https://context7.com/api/v2/libs/search?libraryName=<lib>&query=<topic>'`, take `results[0].id`, then `curl -s 'https://context7.com/api/v2/context?libraryId=<id>&query=<topic>&type=txt'` (unauthenticated works; if `CONTEXT7_API_KEY` is set in your environment, send it as `Authorization: Bearer $CONTEXT7_API_KEY` for a higher rate limit). Write the brief to `<context_handoff_dir>/research-<slug>.md`, then deliver your Report.

{{report}}
