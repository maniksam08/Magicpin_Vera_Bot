# Vera — magicpin AI Proactive Engine

A production-grade FastAPI service implementing Vera: a stateful, proactive AI business partner for local merchants across 5 categories (Dentists, Salons, Restaurants, Gyms, Pharmacies)[cite: 6, 8]. Exposes the exact 5-endpoint API contract expected by the official judge harness: `GET /v1/healthz`, `GET /v1/metadata`, `POST /v1/context`, `POST /v1/tick`, `POST /v1/reply`[cite: 5, 8].

## Architecture

vera-bot/
├── main.py                     # FastAPI app — exposes the 5 mandatory endpoints
├── config.py                   # pydantic-settings: LLM provider, timeouts, team metadata
├── judge_simulator.py          # Local headless smoke-test harness (contract verification)
├── schemas/
│   ├── init.py
│   ├── context_models.py       # Pydantic models for Category, Merchant, Customer, and Trigger contexts
│   └── api_models.py           # Request & response schemas for all 5 endpoints
├── store/
│   ├── init.py
│   └── state_store.py          # Atomic version-controlled context store + suppression ledger + conversation state
├── core/
│   ├── init.py
│   ├── router.py               # B2B ('vera') vs B2C ('merchant_on_behalf') dispatch routing
│   ├── composer.py             # LLM prompt assembly: category voice + Cialdini levers
│   ├── auto_reply_detector.py  # Canned auto-reply & opt-out phrase classifier
│   ├── math_engine.py          # Deterministic metric calculator (zero-hallucination grounding)
│   └── guardrails.py           # Output linter: URL stripping, taboo vocab check, repetition, CTA validation
└── tests/
├── init.py
└── test_endpoints.py       # Pytest contract-compatibility suite



## How a Request Flows

### `POST /v1/context` — Idempotent Ingestion
* Every context write performs an atomic compare-and-set operation against `store/state_store.py`'s in-memory version ledger[cite: 8].
* If `version <= current_version`, the API rejects the payload with an **HTTP 409 Conflict** (`reason: "stale_version"`)[cite: 5, 8].
* If `version > current_version`, it updates the context atomically and returns **HTTP 200 OK** with an acknowledgement ID (`ack_id`)[cite: 5, 8].
* Payloads are parsed into typed Pydantic models (`schemas/context_models.py`) with `extra="allow"` to accommodate flexible upstream attributes without data rejection[cite: 8].

### `POST /v1/tick` — Proactive Trigger Processing
For each trigger ID received in `available_triggers`[cite: 5, 8]:
1. **Context Lookup:** Resolves the `TriggerContext`[cite: 8]. Unregistered trigger IDs are ignored gracefully[cite: 8].
2. **Dual-Scope Routing:** Routes via `core/router.py`[cite: 8]. Triggers scoped to `customer_id` dispatch as B2C (`send_as="merchant_on_behalf"`)[cite: 5, 8], triggers with only `merchant_id` dispatch as B2B (`send_as="vera"`)[cite: 5, 8], and category-only triggers fan out across active merchants within that vertical.
3. **Suppression & Restraint:** Verifies the `suppression_key` ledger[cite: 4, 8]. If a trigger has already been executed or the merchant has opted out, it is silently skipped[cite: 8]. If no triggers qualify, the service returns `{ "actions": [] }`[cite: 5, 8].
4. **Deterministic Fact Extraction:** Extracts exact numbers via `core/math_engine.py` (e.g., segment sizes from `customer_aggregate`, lapsed duration from `last_visit`, slot availability from trigger payloads, pricing from active catalogs) to eliminate hallucinations[cite: 3, 4, 8].
5. **Composition & Linting:** Assembles the message applying category tone guidelines and persuasion levers[cite: 8]. The generated text passes through `core/guardrails.py` before dispatching the response and registering the suppression key[cite: 8].

### `POST /v1/reply` — Multi-Turn Dialogue & State Transition
1. **Intent & Pattern Classification:** `core/auto_reply_detector.py` triages incoming merchant messages into `opt_out`, `canned`, or `normal`[cite: 8].
2. **Opt-Out Handling:** If the merchant requests to stop, the bot returns `action: "end"` and suppresses the merchant profile for 30 days[cite: 5, 8].
3. **Canned Auto-Reply Loops:**
   * **Turn 2:** Sends a follow-up prompt (`action: "send"`)[cite: 5, 8].
   * **Turn 3 (Repeated text):** Enforces backoff (`action: "wait"`, `wait_seconds: 86400`)[cite: 5, 8].
   * **Turn 4+:** Closes the inactive thread (`action: "end"`)[cite: 5, 8].
4. **Commit Phase Transition:** When affirmative commit phrases are detected (e.g., *"ok let's do it"*, *"confirm"*), the bot transitions state from qualification to execution confirmation with `cta: "binary_confirm_cancel"`[cite: 5, 8].

### Guardrails & Output Validation
Every outbound message is validated by `core/guardrails.py` prior to response serialization[cite: 8]:
* **URL Stripping:** Removes all links and web addresses to avoid platform penalties[cite: 5, 8].
* **Taboo Vocabulary Scrubbing:** Filters out prohibited terms based on category voice constraints (e.g., medical guarantees)[cite: 5, 8].
* **Anti-Repetition:** Prevents duplicate text within the same conversation thread[cite: 5, 8].
* **CTA Verification:** Ensures the CTA matches one of the five accepted schema values (`"binary_yes_no"`, `"binary_confirm_cancel"`, `"multi_choice_slot"`, `"open_ended"`, `"none"`)[cite: 5, 8].


## Local Setup & Verification

### 1. Environment Installation
```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt


2. Configure Environment Variables

HOST=0.0.0.0
PORT=8080

# Provider setup (groq | anthropic | openai | mock)
LLM_PROVIDER=groq
LLM_MODEL=llama-3.3-70b-versatile
LLM_API_KEY=gsk_your_groq_api_key_here
LLM_TEMPERATURE=0.5
LLM_TIMEOUT_SECONDS=8.0

# Team Metadata (Returned by GET /v1/metadata)
TEAM_NAME=Your_Team_Name
CONTACT_EMAIL=Your_Email_Address
APP_VERSION=1.0.0


3. Start the FastAPI Service
Bash
uvicorn main:app --host 0.0.0.0 --port 8080


4. Run Contract & Integration Tests

# Run pytest contract suite
pytest tests/test_endpoints.py -v

# Run local judge simulation harness (in a separate terminal)
python judge_simulator.py --base-url http://localhost:8080
