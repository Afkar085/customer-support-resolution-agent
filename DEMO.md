# The 3-minute demo

Double-click **`start.bat`**. The browser opens at <http://localhost:8000>.
Leave the black window open; close it when you are done.

Then do exactly this.

---

## 1 · Say what it is (15 seconds)

> "This is a customer support agent. It reads a complaint, looks up the
> customer's order and the company's refund policy, and answers.
>
> The interesting part is what it refuses to do."

---

## 2 · Send a complaint (30 seconds)

**Console** is already showing *"Owed a refund"*. Click **Send it**.

Point at the amber banner.

> "It worked out that ₹2,499 is owed. It did not pay it. It wrote down exactly
> what it wants to do and stopped. Nothing reached the payment provider."

---

## 3 · Show the work (30 seconds)

Click **Show every step it took**.

> "Checked who's asking. Read the order, the return and the payment. Found the
> policy section — this one, `refund_policy_v2:section_3`. Then a rules check
> that runs in ordinary Python, not in the model."

Then the line that matters:

> "The model proposes. The code decides and pays. The model never computes an
> amount and never calls the payment API."

---

## 4 · Approve it (30 seconds)

Click **Approvals** in the sidebar.

> "This is the reviewer's side. The customer's own words, the agent's reason,
> the order, the policy it quoted, and the reply the customer would get. They
> decide in ten seconds instead of investigating for fifteen minutes."

Click **Approve this refund**, then go back to **Console**.

> "Now it paid, re-checked the approval first, and verified the result with the
> provider. The reply says 'paid' because the provider confirmed it."

---

## 5 · Break it (45 seconds — this is the one that lands)

Click **Reliability**. The failure **accept then timeout** is already selected.

> "This is the hard case for anything that moves money: the provider takes the
> refund, then the connection drops. You don't know whether it went through.
> Retry blindly and you've paid twice."

Click **Arm it**.

Go to **Console**, pick **More examples and settings → Inspection pending**…
actually pick any eligible one, send it, approve it in **Approvals**.

Back on **Console**, click **Show every step it took**:

> "Connection lost, outcome unknown. It did not retry. It asked the provider
> what actually happened, using the same key it sent originally."

Click **Reliability** and scroll to the ledger.

> "Our record on the left, the provider's on the right. Same key, one refund on
> both sides. The guarantee is a unique database constraint, not a prompt."

---

## 6 · The numbers (20 seconds)

> "Sixty test cases, judged on database state rather than on how the reply
> reads. 100% workflow correctness, zero unauthorised actions, zero duplicate
> refunds across retry, concurrency and crash scenarios. The held-out split
> scored 91.7% on its first blind run — that's in the README too."

---

## Questions they will ask

**"Where is the AI?"**
> Four places: classifying the complaint, summarising the records, interpreting
> the retrieved policy, and writing the reply. The policy lookup is RAG over
> versioned documents. Everything that decides whether money moves is ordinary
> code.

**"Is this real data?"**
> No — synthetic customers, synthetic policies, a mock payment provider. The
> mock implements the real idempotency contract, including taking a payment and
> then failing to respond.

**"Does it need an API key?"**
> It ships with a deterministic fallback so the tests and the sixty-case suite
> reproduce on any machine with no key and no cost. Add an Anthropic key and the
> same four steps go to Claude. The console shows which mode produced each run.

**"What would you do next?"**
> A real worker queue instead of running inside the request, pgvector instead of
> in-Python similarity, and partial line-item refunds — which I deliberately
> scoped out, and said so in the README.
