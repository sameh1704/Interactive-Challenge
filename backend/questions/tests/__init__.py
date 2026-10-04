"""The Phase 6 question types: ordering, classification and short answer.

Two things are under test throughout:

* **Backward compatibility.** Multiple choice and true/false questions keep
  working exactly as they did, with their original columns and their original
  keys. A phase that adds question types must not quietly change the ones a
  school already has.
* **The answer key stays a server-side secret.** ``as_live_payload`` is the only
  thing a screen receives while a question is open, so it is asserted directly
  for every type, including that it carries no key and no explanation.
"""