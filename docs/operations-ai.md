# Operations AI (Phase 10)

`app/services/operations/ai.py`. Not a general chatbot: every answer is built from a bounded,
deterministically-fetched context (`build_context`), and the model is only ever asked to narrate
that context in a few sentences.

## Request flow

```
question
  -> build_context(db, organization_id, question)   # keyword-routed reads from repository.py/
                                                      # analytics.py -- real rows only, nothing
                                                      # invented. Order lookups are tenant-scoped.
  -> if context is empty: return "Insufficient operational data." WITHOUT calling the model
                                                      # (mirrors services/ai/agent.py's grounding
                                                      # gate for the website chat agent)
  -> _deterministic_summary(context, evidence)       # the headline fact -- built in code, never
                                                      # by the model (see "Why the summary is
                                                      # computed, not generated" below)
  -> _recommendation_from_context(context)           # recommendation / risk / suggested_action --
                                                      # also computed in code from the same data
  -> llm.generate(SYSTEM_PROMPT + context JSON + question)  -> reasoning_summary only
```

## Safety design

1. **The policy-relevant fields are never parsed out of the model's text.** `evidence`,
   `required_approval`, `suggested_action`, `risk`, and now even `summary` are computed in this
   module from the same data handed to the model. The model's only contribution is
   `reasoning_summary`, a narrative aside. There is no code path from "the model's text said X"
   to `actions.execute_action` -- every action still requires a separate, explicit
   `POST /operations/actions` call through the deterministic action/approval layer
   (`AI output is advisory until accepted/executed by the action layer`, brief section 34).
2. **Untrusted business data is explicitly labelled DATA in the system prompt**, with an
   instruction to never treat it as a command, covering the brief section 35 prompt-injection
   cases (a malicious product title, customer note, or order metadata). See
   `tests/operations/test_ai.py::test_prompt_injection_in_a_recommended_action_is_sent_as_data_not_executed`
   -- it asserts not just that the model receives the guard, but that answering the question
   never creates or executes an `OperationalAction` row as a side effect.
3. **Tenant isolation.** `_find_order` filters by `organization_id` before matching an order
   number; a question naming another tenant's order number resolves to nothing (tested in
   `test_order_lookup_never_resolves_another_tenants_order` and mutation `op-12`).
4. **Grounding refusal is the default**, not a fallback: an unrelated question never reaches the
   model (mutation `op-11` proves the gate is load-bearing, not decorative).

## Why the summary is computed, not generated

Early in building this, the only available local LLM is `FakeLLMProvider`
(`app/services/ai/providers/fake.py`), which was written to answer the website chat agent's own
`SOURCE N\nContent:\n...` prompt shape -- it has no way to produce a useful sentence for a
different prompt format, and would always fall through to its `"I don't have that information."`
sentinel for every Operations AI question. Rather than special-case that one offline provider
(which the application has no business knowing about), the design moved the trustworthy fact into
`_deterministic_summary`, computed directly from the fetched rows, and demoted the model's output
to `reasoning_summary`. The result: the user-facing headline is always accurate and present even
when the configured LLM narrates poorly, and the live end-to-end check below shows exactly this
happening.

**Live-tested** (real Postgres, real demo data, the repo's own `fake` LLM provider from `.env`):

```
POST /api/operations/ai/query {"question": "What orders need attention today?"}
-> {"summary": "12 order(s) currently need attention.",
    "reasoning_summary": "I don't have that information.",   <- the fake provider's own sentinel
    "grounded": true, ...}
```

The headline fact is correct and useful regardless of the narrator; swapping in a real provider
(OpenAI/Anthropic/OpenRouter via the existing `app/services/ai/factory.py`, unmodified) only
improves `reasoning_summary`.

## Example questions handled (brief section 18)

"What orders need attention today?", "Which shipments are delayed?" (routes through the same
issue list filtered to SHIPPING/NDR/RTO/DELIVERY), "Which customers have repeated RTO?" (surfaces
via the `RTO_REPEATED_CUSTOMER` detector's issues), "What inventory is at risk?", "Why is this
order urgent?" / "Why is order #1234 urgent?" (resolves the specific order + its linked issues),
"What should the operations team do next?" / "What are today's biggest operational risks?" (both
route to the KPI + top-issues context). "Which refunds are pending?" reads `ReturnCase` rows in
`RECEIVED` status. A question matching none of these falls through to the insufficient-data
refusal.
