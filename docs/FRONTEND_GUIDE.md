# Frontend guide — real uploads, judge demo, and LLM tooling

This doc is for anyone building the UI. The **primary path is not `/demo`**: judges should see **their own CSVs uploaded**, a **dataset built on the server**, engines run **once**, rankings from **that** restaurant, and **tool recommendations fired only when appropriate** (slow LLM — never block upload/analyze).

**Swagger:** `http://<API_HOST>:8080/docs` while uvicorn is running.

---

## What the backend does in five stages

Understand **when work happens** so the UI does not call endpoints too early or twice.

| Stage | HTTP call | Server work | Typical duration | Cached? |
|-------|-----------|-------------|------------------|---------|
| **1. Ingest** | `POST /uploads` | Read files → map columns → **`build_dataset`** → store `Dataset` in memory | ~1–10 s (file count/size) | `dataset_id` until server restart |
| **2. Analyze** | `POST /analyze/{dataset_id}` | Run cost + time **engines once** → store `Analysis` | ~2–15 s | Until server restart |
| **3. Re-rank** | `GET /report/{dataset_id}?alpha=` | **`build_report` only** (blend $ vs time) — **no** re-ingest, **no** re-run detectors | &lt; 1 s | Same `Analysis` |
| **4. Tooling** | `GET /tools` (today) | OpenRouter LLM from `top_problems` — **30–90+ s** | Long | Not cached |
| **5. Grill chat** | `POST /chat/sessions` + `POST /chat/sessions/{id}/messages` (SSE) | OpenRouter chat that interrogates the owner about **one** problem; swaps the problem when the owner changes topic and re-runs the tools pipeline in the background | Reply streams in ~1–3 s; tools **30–90+ s** in background | In memory per `session_id` until server restart |

**Rules for frontend**

1. **Never** call analyze before a successful upload returns `dataset_id`.
2. **Never** call `GET /report` before analyze (you get **409**).
3. **Do not** call `POST /analyze` again when the user moves the **alpha** slider — use **`GET /report/{dataset_id}?alpha=`**.
4. **Do not** call tooling in parallel with upload or analyze — show the report first, then start tooling in a **separate** step (button or lazy panel).
5. **Do not** chain tooling inside the upload/analyze loading spinner (judges will think the app froze).

```mermaid
stateDiagram-v2
  [*] --> Idle
  Idle --> Uploading: user selects files
  Uploading --> Ingested: POST /uploads 200
  Uploading --> Idle: 422/400 error
  Ingested --> Analyzing: user confirms / auto-run
  Analyzing --> ReportReady: POST /analyze 200
  Analyzing --> Ingested: 404 lost id
  ReportReady --> ReportReady: GET /report?alpha (slider)
  ReportReady --> ToolingLoading: user opens tools OR prefetch
  ToolingLoading --> ToolsReady: GET /tools 200
  ToolingLoading --> ReportReady: tools error (keep report)
```

---

## Judge demo script (real files, not `/demo`)

Use this flow on stage so judges see **upload → ingest feedback → analyze → top 3 on their data**.

### Before the demo

1. **Backend running** (do not restart mid-demo — in-memory IDs are wiped):

   ```bash
   cd AISB
   source .venv/bin/activate
   uvicorn backend.api:app --host 0.0.0.0 --port 8080
   ```

2. **`OPENROUTER_API_KEY`** in `AISB/.env` if you will show tooling (stage 4). Restart uvicorn after changing `.env`.

3. **Sample CSVs** (realistic multi-file restaurant pack):

   ```bash
   python -m backend.synthetic.generate --out backend/synthetic/data
   ```

   Upload **all** generated `.csv` files from `backend/synthetic/data/` in **one** `POST /uploads`, or use an equivalent export from a real POS/back-office. More domains → richer `top_problems` and fewer `not_assessed` rows.

4. **Profile JSON** so the header shows a real name (optional but good for judges):

   ```json
   { "name": "Judge Demo — Mario's Trattoria", "sqft": 3200, "seats": 90 }
   ```

### On stage (recommended narrative)

| Step | UI | API | What to say |
|------|-----|-----|-------------|
| 1 | File picker + “Upload” | `POST /uploads` | “We map messy exports to a canonical dataset.” |
| 2 | Ingest summary screen | Response: `domains`, `files`, `data_quality` | “These are the data types we recognized.” |
| 3 | “Run analysis” (spinner) | `POST /analyze/{dataset_id}?alpha=0.7` | “Engines estimate $ and time pain per category.” |
| 4 | Results dashboard | Use analyze response **or** `GET /report` | “Top 3 problems for **this** restaurant.” |
| 5 | Alpha slider | `GET /report/{dataset_id}?alpha=` only | “Owner can weigh money vs time.” |
| 6 | Optional: “Find AI tools” | `GET /tools` (slow) | “Research runs **after** we know the problems.” |

**Do not** open the demo with `GET /demo` if the story is “we ingest your files.” Reserve `/demo` for dev smoke tests or a “skip upload” escape hatch.

### Prove it’s not canned data

Point judges at fields that come from **their** upload:

- `report.restaurant.name` matches `profile.name` you sent.
- `report.files` lists their filenames and domain mappings.
- `report.data_quality` and `domains` match the ingest screen.
- `report.skipped` / `not_assessed` show honest limits when files are missing.

---

## Stage 1 — `POST /uploads` (build dataset **here**)

**This is the only step that runs ingest / `build_dataset`.** Analyze does not re-read raw files from disk; it uses the in-memory `Dataset` stored under `dataset_id`.

**Request:** `multipart/form-data`

| Field | Type | Required |
|-------|------|----------|
| `files` | file[] (repeat field name `files`) | yes |
| `profile` | string, JSON object | no |

Supported extensions: `.csv`, `.xlsx`, `.xls`, `.xlsm`.

**Example (browser):**

```ts
const form = new FormData();
for (const file of selectedFiles) {
  form.append("files", file); // same field name for every file
}
form.append(
  "profile",
  JSON.stringify({ name: restaurantName, sqft: 3000 })
);

const ingest = await fetch(`${API}/uploads`, { method: "POST", body: form });
if (!ingest.ok) throw await ingest.json();
const { dataset_id, domains, files, data_quality } = await ingest.json();
// Persist dataset_id (sessionStorage) until server restart
```

**Success (200):**

```json
{
  "dataset_id": "a1b2c3d4e5f6",
  "domains": ["inventory", "sales", "timeclock"],
  "files": [/* mapping per uploaded file */],
  "data_quality": { "sales": { "...": "..." } }
}
```

**UI after ingest**

- Show `domains.length` and a per-file mapping from `files` (matched vs unmapped columns).
- If `domains` is small, warn that ranking may be thin (`not_assessed` on report).
- Store `dataset_id`; enable “Run analysis” only now.

**Errors**

| Code | Meaning |
|------|---------|
| 400 | Invalid `profile` JSON |
| 422 | No file matched a known schema — show `files` from body |

---

## Stage 2 — `POST /analyze/{dataset_id}` (engines — **once**)

```ts
const report = await fetch(
  `${API}/analyze/${dataset_id}?alpha=0.7`,
  { method: "POST" }
).then(async (r) => {
  if (!r.ok) throw new Error(JSON.stringify(await r.json()));
  return r.json();
});
```

- **First time** for this `dataset_id` you **must** POST analyze.
- Response is the full **report** (same shape as `/report` and `/demo`).
- Keep the report in client state; you do not need to POST analyze again for the same upload.

**Errors**

| Code | Action |
|------|--------|
| 404 | `dataset_id` unknown (server restarted or wrong id) → back to upload |
| 422 | Bad `alpha` query param |

---

## Stage 3 — `GET /report/{dataset_id}?alpha=` (slider — **fast**)

When the user changes money vs time weighting:

```ts
const report = await fetch(
  `${API}/report/${dataset_id}?alpha=${alpha}`
).then((r) => r.json());
```

| `alpha` | Meaning |
|---------|---------|
| `1.0` | Rank by money only |
| `0.0` | Rank by time only |
| `0.7` | Default blend |

**409** means analyze was never run — call `POST /analyze/{dataset_id}` once, then use GET report.

---

## Stage 4 — Tooling (slow — **after** report is visible)

### Timing and UX

| Do | Don’t |
|----|--------|
| Start tooling **after** `top_problems` is on screen | Await `GET /tools` inside upload/analyze |
| Dedicated panel: “Researching tools… 30–60s” | Block the whole app on one spinner |
| `AbortController` + retry on failure | Assume tools finished because analyze did |
| Let user read drivers/summary while tools load | Fire tools on every alpha change |

Optional **prefetch:** when analyze completes, fire `GET /tools` in the background if the user enabled “Include AI recommendations,” but still render the report immediately from analyze.

### Current API behavior (read this before judging)

Today, **`GET /tools` ignores the user’s `dataset_id`**. It internally builds a **demo** report and passes **demo** `top_problems` into the LLM (`backend/api.py`).

| User journey | Report from upload? | Tools match upload? |
|--------------|---------------------|---------------------|
| Upload → analyze → show report | Yes | **No** (until backend wires `dataset_id`) |
| `GET /demo` only | Synthetic only | Yes (demo problems) |

**For a coherent judge story**, backend should add something like:

- `POST /tools/{dataset_id}` or `GET /tools?dataset_id=` **after** analyze exists, passing **that** report’s `top_problems` to `get_tools`.

Until then, frontend choices:

1. **Honest UX:** After real upload, show report from analyze; label tools as “sample research” or hide the tools button.
2. **Backend fix (preferred):** Wire tools to the same `dataset_id` — frontend calls tools **only after** analyze, with the same id.

**Call pattern today:**

```ts
// Only after report is shown; separate loading state; long timeout (e.g. 120s)
const controller = new AbortController();
const timeout = setTimeout(() => controller.abort(), 120_000);

const raw = await fetch(`${API}/tools`, {
  signal: controller.signal,
}).then(async (r) => {
  clearTimeout(timeout);
  if (!r.ok) {
    const err = await r.json();
    throw new Error(err.detail ?? "tools failed");
  }
  return r.json() as Promise<string>; // body is a JSON-encoded string
});
const tool = JSON.parse(raw) as ToolRec; // the string itself contains JSON text
```

**Response:** `application/json` body is a **string** that itself contains **JSON text** matching the strict schema in `llmConnection.py`: `{ tool_number, tool_name, tool_description, tool_pricing, tool_home_page }`. Parse twice (`r.json()` then `JSON.parse`). Today the schema describes a **single tool object**, not an array per problem. See [Parsing LLM output](#parsing-llm-output-for-the-ui) below.

**500** — show `detail`, keep report on screen, offer retry.

---

## Stage 5 — Grill-me chat (`/chat/sessions`)

A chat where an AI interrogator drills the owner on **one problem at a time** until it is clearly understood. It does **not** need upload/analyze, but you can pass a `dataset_id` (or `"demo"`) so the questions are grounded in that restaurant's top problems.

How it behaves:

- Only **one problem is in focus**. When the owner switches topic, the backend **drops the old problem and its working context**, adopts the new one, and **re-runs the tools pipeline** (`get_tools`) for it **in the background**. Tools run again once the problem becomes "understood".
- The transcript is never wiped: a switch is recorded as a message with `role: "event"` and `type: "problem_changed"` so the UI can draw a divider.
- A problem only becomes `understood: true` after **≥ 3 owner answers** on it and `clarity ≥ 85`.
- Sessions live in server memory (lost on restart, like `dataset_id`).

### 1. Create a session

```ts
const session = await fetch(`${API}/chat/sessions`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ dataset_id }), // body optional; dataset_id may be "demo"
}).then((r) => r.json() as Promise<ChatSession>);
// session.messages[0] is the grilling opener — render it
```

Errors: **404** unknown / un-analyzed `dataset_id`.

### 2. Send a message (SSE stream)

`POST /chat/sessions/{id}/messages` with `{ "message": "…" }` (1–4000 chars). The response is `text/event-stream`. `EventSource` only supports GET, so read the stream with `fetch`:

```ts
async function sendMessage(sessionId: string, message: string, on: (e: ChatEvent) => void) {
  const res = await fetch(`${API}/chat/sessions/${sessionId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!res.ok) throw new Error((await res.json()).detail); // 404 / 409 / 422 / 500 arrive before streaming
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, i);
      buf = buf.slice(i + 2);
      const event = /^event: (.*)$/m.exec(block)![1];
      const data = JSON.parse(/^data: (.*)$/m.exec(block)![1]);
      on({ event, data } as ChatEvent);
    }
  }
}
```

Events, in order:

| Event | When | Payload |
|-------|------|---------|
| `state` | Right after the message is analysed (before any text) | `problem`, `problem_version`, `understood`, `clarity` (0–100), `known_facts[]`, `open_questions[]`, `action` (`keep` \| `refine` \| `new_problem`), `previous_problem` |
| `tools` | Only when tool research was just (re)started | `status: "running"`, `problem` (the exact brief sent to the tools pipeline) |
| `token` | Many times | `delta` — append to the assistant bubble |
| `done` | End of stream | `state`, `tools` (status only, no result) |
| `error` | Analysis or stream failed | `message` — `done` may still follow |

Use `state` to update a "Current problem" header and a clarity meter immediately; when `action === "new_problem"`, show a "Switched problem" divider and clear the tools panel (it is now stale).

### 3. Poll for tool recommendations

Tools run **30–90+ s after** the stream ends, so poll `GET /chat/sessions/{id}` every 2–3 s while `tools.status === "running"`:

```ts
type ToolsState = {
  status: "idle" | "running" | "ready" | "error";
  problem: string | null; // brief these results were produced for
  result: ToolRec | string | null; // parsed JSON from get_tools, or raw text if it wasn't JSON
  error: string | null;
  updated_at: number;
};
```

Only show `result` when `tools.problem` matches the current problem; a run for a replaced problem is discarded by the server.

### Other chat calls and errors

| Call | Notes |
|------|-------|
| `GET /chat/sessions/{id}` | Full session: `messages[]`, problem state, `tools` (with `result`) |
| `DELETE /chat/sessions/{id}` | End the session |

| Code | When |
|------|------|
| 404 | Unknown `session_id` (or `dataset_id` on create) |
| 409 | Previous message is still streaming — disable the input until `done` |
| 422 | Empty or too-long `message` |
| 500 | `OPENROUTER_API_KEY` missing |

The chat model defaults to `openai/gpt-4o-mini`; set `CHAT_STREAM_MODEL` in `.env` to override.

---

## Suggested client state (TypeScript)

```ts
type AppPhase =
  | "idle"
  | "uploading"      // POST /uploads
  | "ingested"       // have dataset_id, show ingest summary
  | "analyzing"      // POST /analyze
  | "report_ready"   // have report JSON
  | "tools_loading"  // GET /tools in flight
  | "tools_ready";

type Session = {
  datasetId: string | null;
  alpha: number;
  ingest: UploadResponse | null;
  report: Report | null;
  tools: ToolRec | null;
  toolsError: string | null;
  chat: ChatSession | null; // Stage 5 (independent of the phases above)
};

type ToolRec = {
  tool_number: number;
  tool_name: string;
  tool_description: string;
  tool_pricing: string;
  tool_home_page: string;
};

type ChatMessage = {
  role: "user" | "assistant" | "event";
  content: string;
  ts: number;
  type?: "problem_changed"; // role "event" only
  problem?: string;
  previous_problem?: string | null;
};

type ChatSession = {
  session_id: string;
  problem: string | null;
  problem_version: number;
  understood: boolean;
  clarity: number;
  known_facts: string[];
  open_questions: string[];
  messages: ChatMessage[];
  tools: ToolsState;
};

type ChatEvent =
  | { event: "state"; data: Omit<ChatSession, "session_id" | "messages" | "tools"> & { action: string; previous_problem: string | null } }
  | { event: "tools"; data: Omit<ToolsState, "result"> }
  | { event: "token"; data: { delta: string } }
  | { event: "done"; data: { state: unknown; tools: Omit<ToolsState, "result"> } }
  | { event: "error"; data: { message: string } };
```

Persist `datasetId` in `sessionStorage` with a banner: “Server restarted? Upload again.”

---

## Report JSON — what to render after analyze

Same object from `POST /analyze`, `GET /report`, or (dev only) `GET /demo`.

| Key | UI use |
|-----|--------|
| `restaurant` | Name, revenue, date range |
| `alpha` | Slider value |
| `top_problems` | **Top 3 cards** (`rank`, `label`, `summary`, `cost`, `time`, `drivers`) |
| `all_problems` | Full sorted list |
| `needs_more_data` | Low confidence — ask for more files |
| `not_assessed` | Missing domains |
| `totals` | Footer KPIs |
| `sensitivity` | Stability of top 3 across benchmark scenarios |
| `data_quality`, `files`, `skipped` | Trust / transparency for judges |

### `top_problems[]` (hero card)

| Field | Notes |
|-------|--------|
| `category` | Stable id, e.g. `labor_scheduling` |
| `label`, `description`, `summary` | Copy |
| `pain_score`, `confidence` | Ordering and trust |
| `cost` | `annual`, `recoverable`, `range`, `pct_revenue` |
| `time` | `hours_per_week`, `by_role` |
| `drivers` | Evidence bullets; `engine` is `"cost"` or `"time"` |
| `tool_tags` | Catalog filters (not LLM) |
| `missing` | What CSV types would help |

Pass **`top_problems`** (or a trimmed payload: `label`, `summary`, `category`, `cost`, `time`) into tooling once the backend accepts the user’s analysis — not on every alpha tweak.

---

## Parsing LLM output for the UI

`get_tools` forces a strict JSON schema, so the output is **JSON, not Markdown**.

1. `GET /tools`: `await response.json()` → **string** → `JSON.parse` it → `{ tool_number, tool_name, tool_description, tool_pricing, tool_home_page }`.
2. Chat: `tools.result` is already parsed by the backend (an object with the same fields). If it is a **string**, parsing failed — show it as plain text.
3. The schema is currently **one tool object**, not an array per problem. Render it as a single card and don't assume 4 tools per problem; if the backend later returns arrays, reuse the same card component.
4. Disclaimer: AI-generated — verify pricing and integrations on vendor sites.

---

## Reference — all endpoints

| Method | Path | When to call |
|--------|------|----------------|
| `GET` | `/health` | App boot |
| `GET` | `/taxonomy` | Static labels / `tool_tags` glossary |
| `POST` | `/uploads` | User submits files (**build dataset**) |
| `POST` | `/analyze/{dataset_id}` | Once per upload |
| `GET` | `/report/{dataset_id}` | Alpha slider / refresh view |
| `GET` | `/demo` | **Dev / fallback only** — skip ingest |
| `GET` | `/tools` | **After** report; long wait; **demo problems today** |
| `POST` | `/chat/sessions` | Open the grill-me chat (optional `dataset_id`) |
| `POST` | `/chat/sessions/{session_id}/messages` | Each owner message; **SSE** reply |
| `GET` | `/chat/sessions/{session_id}` | Restore transcript / poll tool recommendations |
| `DELETE` | `/chat/sessions/{session_id}` | End the chat |

### Error cheat sheet

| Code | When |
|------|------|
| 404 | Unknown `dataset_id` or `session_id` |
| 409 | `GET /report` before analyze; or a chat message sent while the previous one is still streaming |
| 422 | Bad upload, `alpha`, or empty/too-long chat `message` |
| 500 | Tooling / LLM failure; missing `OPENROUTER_API_KEY` |

---

## curl smoke test (no UI)

Substitute paths and host as needed.

```bash
API=http://127.0.0.1:8080
DIR=backend/synthetic/data

# 1) Ingest
curl -s -X POST "$API/uploads" \
  -F "profile={\"name\":\"Judge Test\"}" \
  $(for f in "$DIR"/*.csv; do echo -n " -F files=@$f"; done) \
  | tee /tmp/ingest.json

ID=$(python3 -c "import json; print(json.load(open('/tmp/ingest.json'))['dataset_id'])")

# 2) Analyze
curl -s -X POST "$API/analyze/$ID?alpha=0.7" | python3 -c "import json,sys; r=json.load(sys.stdin); print(r['restaurant']['name'], len(r['top_problems']))"

# 3) Re-rank
curl -s "$API/report/$ID?alpha=1.0" | python3 -c "import json,sys; r=json.load(sys.stdin); print([p['label'] for p in r['top_problems']])"

# 4) Tools (slow; optional)
curl -s "$API/tools" | head -c 200

# 5) Grill chat (-N disables buffering so tokens print live)
SID=$(curl -s -X POST "$API/chat/sessions" | python3 -c "import json,sys; print(json.load(sys.stdin)['session_id'])")
curl -N -X POST "$API/chat/sessions/$SID/messages" \
  -H "Content-Type: application/json" \
  -d '{"message":"my servers keep calling out last minute"}'
# poll tools (ready after 30-90s)
curl -s "$API/chat/sessions/$SID" | python3 -c "import json,sys; s=json.load(sys.stdin); print(s['problem'], s['tools']['status'])"
```

---

## Backend coordination checklist

| Need | Owner |
|------|--------|
| Tools use **uploaded** `top_problems` | `api.py` — new route keyed by `dataset_id` |
| Report shape | `backend/ranker.py` |
| Ingest / mapping | `backend/ingest/loader.py` |
| LLM prompt / response | `backend/tool_rec/llmConnection.py` |
| Grill chat (prompts, problem switching, SSE) | `backend/chat_stream/` (routes in `api.py`) |

**Frontend is correct when:** upload builds the dataset once, analyze runs once, slider uses GET report, tooling is optional and slow and runs last — and tools consume the **same** `top_problems` the user already sees on the dashboard.
