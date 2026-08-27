# O-23 — creating the two Stripe prices for the termly plan

Everything behind these two objects is built, tested and applied. This is the
only thing between the termly commitment and being purchasable.

**I cannot do this step.** It needs your Stripe account, and creating live price
objects is an outward-facing action either way.

---

## What to create

Two prices, both against one product (call it *Tauka — Termly plan*). The
amounts below are the ones the pricing page card currently shows; change both
together if you change either.

| | Mode | Stripe shape | Amount |
|---|---|---|---|
| **Prepay** | `payment` | a one-time price | $299 |
| **Instalments** | `subscription` | a recurring monthly price | $109/mo |

### The instalment price needs a fixed iteration count

A term **ends**. A plain monthly subscription does not, and would keep billing
after the term it was sold for had finished.

Stripe has two ways to bound it, and either is fine:

1. **A subscription schedule** with `iterations: 3` on the phase — the cleanest,
   and what `create_term_checkout` is shaped for.
2. **`cancel_at`** set on the subscription at creation, three months out.

If you take neither, the backstop still holds — `record_installment` refuses to
count past `installments_total`, and the daily sweep moves the commitment to
`completed` when `term_end` passes — but the **customer keeps being charged**.
Do not rely on the backstop; it protects the ledger, not the card.

`commitment.term_months` in `app.config` is `3`. The instalment count is derived
from it, so a 3-month term is 3 instalments (O-24). If you change the term
length, change the Stripe iteration count with it.

---

## Then, three edits

1. **`.env`** in `tauka_py_backend/`:

   ```
   STRIPE_PRICE_TERM_PREPAY=price_...
   STRIPE_PRICE_TERM_INSTALLMENT=price_...
   ```

   Both are already declared in `app/config.py` and default to `""`.

2. **`tauka-react-web/src/config/featureStatus.js`**:

   ```js
   termly_commitment: 'ready',   // was 'coming'
   ```

3. **Add `invoice.paid` to the webhook endpoint's event list in the Stripe
   dashboard.** The handler exists (`_handle_invoice_paid`, added in Phase 4) but
   Stripe only sends the events an endpoint is subscribed to. Without it the
   second and third instalments arrive nowhere and `installments_paid` sticks
   at 1.

   The endpoint already needs `checkout.session.completed`,
   `customer.subscription.updated`, `customer.subscription.deleted` and
   `invoice.payment_failed`.

---

## How to check it worked

With the env vars set, `POST /commitments/checkout` stops answering 503. Before
that, it refuses **by name** — `STRIPE_PRICE_TERM_PREPAY is not configured` —
rather than letting Stripe reject an empty price id with something unactionable.

After a test purchase in Stripe test mode:

```sql
select status, billing_mode, installments_paid, installments_total,
       term_start, term_end, tier, stripe_reference
  from app.enrollment_commitment order by created_at desc limit 1;
```

`status` should be `active`, and the student's `app.student.tier` should now be
the commitment's `tier` with `tier_source = 'commitment'`.

---

## What not to do

- **Do not put a `tier` key in the checkout metadata.**
  `_handle_checkout_complete` dispatches on metadata keys, and a `tier` key
  routes the purchase into `_activate_subscription` — which writes
  `student.stripe_subscription_id`, treats a fixed term as a recurring plan, and
  never activates the commitment. The term checkout carries `commitment_id` and
  deliberately omits `tier`.
- **Do not add a fifth tier value for the term.** It names the tier it entitles;
  `app.student.tier` stays the entitlement axis (§4.4).
