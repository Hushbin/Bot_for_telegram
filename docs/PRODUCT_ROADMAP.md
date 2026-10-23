# SMEbot product assessment and marketplace roadmap

**Reviewed: 23 October 2026. Direction confirmed by the product owner: a
marketplace for multiple independent sellers. Status: proposed roadmap.**

## Product assessment

SMEbot is a working marketplace pilot with a substantial seller workbench and
an order-request process. Buyers can discover listings, register, request an item
and receive status updates. Sellers can manage products and stock through
Telegram and the supported web-panel API, accept requests and mark fulfillment.

The next product goal should be **helping buyers find suitable goods and reliably
complete a purchase with an independent seller**. The strongest opportunities
are discovery, the handoff after acceptance, trust/support and measurement.
More catalog-management infrastructure alone would not close those gaps.

Payment and fulfillment are currently arranged outside the product. An order
request is not a paid checkout, and seller-marked completion does not prove
payment or receipt. These distinctions should remain visible in product wording,
analytics and commercial plans.

### Evidence and limits of this review

- Local source matches GitHub `master` at
  [`ba06567`](https://github.com/Hushbin/Bot_for_telegram/commit/ba0656753e94cc6cbc3f4779f85c31f4dd4d4d29).
  The latest [test/deployment workflow](https://github.com/Hushbin/Bot_for_telegram/actions/runs/39506383161)
  succeeded on 20 October: 554 tests ran, 469 passed and 85 optional Oracle tests
  were skipped. This is release evidence, not a live health or usability test on
  23 October.
- This assessment reviews bot code, schemas, maintained guides and the seller
  API. The separate private seller-frontend repository was unavailable through
  the connected GitHub account; its current UI was not independently inspected.
- No production customer records, adoption figures, revenue, interviews or
  conversion baseline were supplied or inspected. Priorities below are
  evidence-informed product hypotheses, not measured customer demand.
- Geography, initial category, launch currency, staffing, budget and revenue
  model remain decisions. Start with one coherent buyer need and service area;
  the existing four categories do not establish which market will succeed.

### Current capability map

| Area | Implemented today | Product gap / opportunity |
| --- | --- | --- |
| Buyer access | Anonymous catalog browsing works; registration can resume the selected order | Welcome starts with Seller/Buyer registration. Make Browse visible; later test whether mandatory email adds unnecessary friction |
| Discovery | Four categories, newest-first pagination, 20 products per page delivered in groups of five, detail/photo view | Public catalog items lack individual View buttons; users type a catalog ID. No buyer keyword search or price/availability filters |
| Seller identity | Listing name, private account/listing contacts and private location | No public storefront, explicitly public service area or buyer-facing fulfillment policy |
| Seller workbench | Guided creation, editing, stock, one photo with replacement/removal, optional authenticated panel API | No actual draft/publish/pause state, multiple images, variants or proactive configurable replenishment alerts |
| Ordering | One product and quantity from one seller; explicit contact-sharing consent; acceptance, decline, cancellation, expiry and history | No basket, payment, delivery/pickup agreement or buyer confirmation of receipt |
| After acceptance | Seller receives buyer contact details; durable status alerts retry delivery | Buyer largely waits for seller contact; accepted-order help and cancellation-request routes are absent |
| Trust and operations | Ownership checks, privacy/erasure, per-account limits and operational recovery | No marketplace report/case queue, moderation role, reputation or seller verification process |
| Retention | Order history and category preference | No favorites, following, fresh-quote reorder or opted-in restock alerts |
| Measurement | Order timestamps, notification outcomes, database-opening measurements and OCI health metrics | No implemented acquisition/discovery funnel or marketplace outcome dashboard; infrastructure health is not product conversion |

Evidence: [commands and routing](../main.py), [buyer catalog and onboarding](../handlers.py)
(`product_page`, `products`, `start`), [categories](../catalog.py),
[order presentation](../orders.py), [order rules](../order_store.py),
[order schema](../order_schema.py), [seller API](../seller_api.py),
[seller-panel scope](SELLER_PANEL.md), [OCI health publishing](../scripts/recovery.py)
and [remaining technical improvements](IMPROVEMENT_SUGGESTIONS.md).

## Product strategy

The initial promise should be simple: **find a suitable item from a reachable
seller, understand the terms, request it and know what happens next**.

A useful positioning hypothesis is an organized marketplace for sellers and
communities already trading through Telegram: a shared catalog, consistent
requests and order history reduce the coordination currently handled in chats.
Validate whether this is valuable compared with their existing groups, direct
messages and other marketplaces. This review does not establish competitive
advantage or willingness to switch.

| Participant | Job the product should help complete | Outcome to learn about |
| --- | --- | --- |
| Buyer | Find a suitable available item and receive it on understood terms | Successful discovery, reliable receipt, confidence to return |
| Independent seller | Turn a small catalog into manageable, worthwhile orders | First useful listing, timely responses, fulfilled demand, repeat buyers |
| Marketplace operator | Keep supply usable and resolve problems with reasonable effort | Healthy supply, manageable cases, sustainable storage/support costs |

Recruit an invitation-only pilot around one category/use case and one service
area. An initial 5–10 sellers and 20–30 buyers would be a practical qualitative
learning cohort, **not a forecast or a statistically representative sample**.
Observe their journeys and seek feedback with explicit permission. Establish a
named support owner and agreed seller response/fulfillment expectations.
In discovery, ask sellers how they currently find buyers and handle stock,
payment and delivery; ask buyers how they find suitable goods, judge sellers and
resolve problems. Use those answers to choose the pilot's strongest use case.

## Prioritized feature opportunities

Now = start the pilot learning loop. Next = strengthen the transaction journey.
Later = depend on successful pilot outcomes. Conditional = build only when a
specific market need or commercial model supports it. Scope sizes are relative
complexity, not delivery estimates: S is a bounded interaction/report; M adds a
workflow or data capability; L spans several interfaces and business rules;
XL changes transaction or payment architecture. The engineer should estimate
individual slices after the BA brief.

| ID / priority | Feature and smallest useful scope | Value and success signal | Dependencies / size |
| --- | --- | --- | --- |
| F01 — Now | **Marketplace scorecard and funnel events.** Report existing request outcomes; add bounded catalog/detail/review events | Reveals where demand is lost; trustworthy stage counts and an initial baseline | Metric dictionary, retention/access rules, distinguish seller completion from receipt; M |
| F02 — Now | **Tappable products and browse-first welcome.** View buttons on public catalog items; visible Browse entry using existing anonymous access | Less effort to reach an item; higher catalog-to-detail progression and successful usability tasks | Existing detail route, preserve active drafts; S |
| F03 — Next | **Fulfillment terms and buyer contact path.** Pickup/delivery availability, public service area, timing and fee policy; an order-scoped contact/help action | Fewer unsuitable requests and stranded accepted buyers | New opt-in public fields, agreed terms in the order, defined contact-sharing policy; L, split into slices |
| F04 — Next | **Buyer receipt feedback and cancellation requests.** Confirm receipt or report a problem; request cancellation after acceptance | Observable outcomes and recoverable problems; receipt-confirmed orders, case resolution | Shared state rules, seller stock-return behavior, support ownership; M–L |
| F05 — Next / before wider onboarding | **Reports and operator case queue.** Report listing/order, reason, case reference, human triage and audited remedies such as hiding a listing or pausing a seller's new activity | Trust and manageable marketplace operations; unresolved cases and resolution time | Explicit operator authorization, recorded reasons, review/restoration rules and retention; preserve existing-order resolution and erasure; M–L |
| F06 — Next | **Seller response queue and reminders.** Surface old pending requests; bounded reminders before the existing 24-hour expiry | More demand receives a timely answer; response time and pending-expiry rate | Existing notification worker, deduplication/preferences; M |
| F07 — Next | **Public seller storefronts and product/shop share links.** Public bio, terms, service area and seller catalog | Sellers can bring initial demand; referred buyers reaching useful listings/orders | Public-field consent and validated deep links; M. Product links can ship separately |
| F08 — Next, as catalog breadth warrants | **Search and practical filters.** Keywords, category, in-stock and price; clear empty results | Buyers find relevant supply; search-to-detail and zero-result rates | Catalog quality and store-query contracts; location filter follows structured public area data; M |
| F09 — Now, refine Next | **Seller activation checklist.** Basic profile/listing/stock/terms checklist first; automate guidance and completeness checks later | More registered sellers become usable supply; first-listing activation and time to publish | Define a usable listing for the pilot category; S–M |
| F10 — Later | **Listing drafts, preview and pause/resume.** Persist seller listings separately from temporary chat drafts | Sellers prepare catalog entries and pause unavailable supply without deletion | Publication state, catalog eligibility, treatment of existing orders; M–L |
| F11 — Later | **Richer listings.** Several photos and category-specific attributes; variants only if pilot needs them | Fewer clarification questions and mismatched items; detail-to-request conversion and issue reasons | Media capacity/retention; variant stock requires its own model; M, variants L |
| F12 — Later | **Saved products, fresh-quote reorder and opted-in alerts.** Start with favorites or reorder; add restock/follow subscriptions separately | Repeat use; repeat buyers and alert-to-order progression | Fresh price/stock/consent review, notification preferences/frequency limits; M |
| F13 — Later | **Seller productivity and insights.** Order filters, configurable low-stock alerts, response and product-interest reports | Demonstrable seller value; handling time, stockout failures and active seller retention | Reliable events; extend the existing panel. Bulk import/export only when catalog sizes justify it; M |
| F14 — Later, after outcome/support foundation | **Order-linked reviews and reputation.** One eligible review per order, seller reply and reporting | Better purchase confidence; review coverage and repeat use alongside dispute rate | Buyer receipt signal, moderation/appeal process, anti-manipulation rules. Label verified order, not verified payment; M–L |
| F15 — Conditional on launch market | **Language, currency and category fit.** Choose the first local language/currency/taxonomy before broad rollout | Product matches the audience; successful tasks and fewer price misunderstandings | Move this earlier if USD/English/four categories do not fit. Define money precision; no implicit currency conversion; M–L |
| F16 — Conditional | **Single-seller basket.** Multiple lines with one seller/fulfillment agreement | Larger useful orders and less repeated checkout; items per fulfilled order | Order header/items migration, atomic stock changes, recovery and erasure updates; L. Cross-seller checkout is a separate later project |
| F17 — Conditional, after commercial validation | **Integrated payments.** One market/provider with receipts, refunds and reconciliation | Verified paid outcomes and payment convenience | Seller onboarding/payouts, fees, failed/duplicate payment rules, disputes and human domain review; XL |
| F18 — Conditional, after demonstrated seller value | **Seller paid plans.** Test willingness to pay for proven productivity or analytics | Sustainable revenue; paid conversion, retention and margin after support costs | Pricing research, billing/entitlements and value evidence; M–L. Transaction commissions require payment/reconciliation control |
| F19 — Conditional | **Buyer Mini App / richer catalog UI.** Reuse product rules while adding visual browsing if chat becomes limiting | Better discovery for larger catalogs; conversion against the Telegram flow | Observed chat usability limits, buyer authentication, UX/accessibility work; L |

Promoted listings, referral rewards and AI recommendations should wait for enough
relevant supply, buyer traffic and trustworthy outcomes to evaluate them. Search,
clear listing information and responsive sellers are more testable first steps.

## Roadmap by outcome

This is a sequenced roadmap, not a calendar commitment. Set release dates after
the pilot market, available team and implementation estimates are agreed. Each
phase can be delivered in small releases; move features between phases when
observed customer problems justify it.

| Phase | Outcome and planned delivery | Evidence needed to advance |
| --- | --- | --- |
| **1. Start a measurable pilot — Now** | Choose category/area and named support owner. F01 baseline plus F02 navigation. F09 basic seller checklist. Review pilot listings and fulfillment terms manually while the structured workflow is designed | A small cohort can find items and submit valid requests; usable supply exists; reports reconcile to source outcomes; every pilot seller has clear terms and a reachable support route |
| **2. Make purchases dependable — Next** | F03 fulfillment/contact, F04 receipt/problem/cancellation feedback, F05 report handling, F06 response reminders. F07 storefront/link slices. Capacity and abuse operations before expanding recruitment | Buyers can reach a seller or support after acceptance; completed and unresolved outcomes are distinguishable; cases have an owner; response/expiry/issue rates are reviewed against pilot expectations |
| **3. Improve matching and repeat use — After the transaction loop works** | F08 search, F10 publication controls, F11 richer listings and F12 favorites/reorder; prioritize F13 seller insights and F14 reviews according to pilot evidence | Buyers return, sellers remain active and new matching/retention features improve the chosen outcome without increasing unresolved cases or operating cost beyond the agreed budget |
| **4. Validate revenue and expand — Conditional** | Test F18 seller willingness to pay. Evaluate F16 basket, F17 payments, F19 Mini App and additional markets only against a demonstrated need. F15 moves to the relevant market-launch gate | A viable fee/subscription model, workable support/refund processes and sustainable acquisition/operation economics; no expansion based solely on sign-ups or requested order value |

Important dependencies: public seller fields → location filtering and storefronts;
fulfillment terms/support → buyer outcome confirmation → credible reputation;
event definitions → conversion and seller insights; stable order-line model →
baskets; payment/settlement policy → payment integration and commission billing.

Before expanding the pilot, continue the existing **6.5** work on total
database/media capacity, retention and broader abuse controls. The documented
schema quota is 20 MB; live remaining headroom was not measured in this review.
Avoid unbounded analytics or image history. Existing health metrics can be
extended with actionable capacity alerts and operator follow-up queues.
Treat **6.2 connection pooling** as conditional on latency measurements, rather
than assuming it is the next customer-value feature.

## Measurement and learning

Proposed north-star measure: **weekly buyer-confirmed fulfilled orders between
independent buyers and sellers**. Until receipt confirmation exists, use
**seller-reported completions** as an explicitly labeled proxy and supplement it
with permitted pilot feedback. Report how many eligible buyers actually respond;
non-response does not prove failure or receipt.

| Measure | Definition / practical use |
| --- | --- |
| Seller activation | New sellers with a first usable published listing within 7 days / sellers registered in that cohort; agree usable-listing criteria before measuring |
| Relevant active supply | Live, in-stock listings from available sellers serving the selected area/category; track alongside demand to avoid growing empty catalog breadth |
| Discovery progression | Unique eligible browsing sessions reaching a product detail / sessions seeing catalog results; define session window and exclude test/owner traffic |
| Request conversion | Unique buyer/product sessions creating a confirmed request / sessions viewing that product; do not count callback retries as new requests |
| Seller response and expiry | Median and upper-percentile time to accept/decline; expired requests / requests in a cohort old enough to pass the 24-hour deadline |
| Fulfillment | Buyer-confirmed received orders / accepted orders whose agreed fulfillment window has elapsed; show pending, problem and no-response outcomes separately |
| Repeat use | Buyers with another fulfilled order within 30 days / first-time fulfilled buyers observed for a full 30 days |
| Marketplace operations | Open/overdue cases, first-response/resolution time, notification failures, storage headroom and operator effort per resolved order |
| Commercial performance | Initially requested/accepted/seller-reported-completed order value, clearly labeled. Verified paid volume, platform revenue and contribution margin require payment/billing evidence |

F01 should first use existing order timestamps for acceptance/expiry/response
reporting with their business meaning checked. `decided_at` is also set by
cancellation and automatic expiry, so `decided_at - created_at` across all orders
would misstate seller response time. Identify acceptance/decline decisions using
status and available transition/notification evidence; label any history that
cannot be reliably reconstructed as unavailable. New catalog, detail, review and abandonment observations require
instrumentation. Store minimal identifiers and event types; do not collect raw
chat text, names, phones, emails or full search strings by default. Define
deduplication, access, retention and erasure treatment before collection. A
pseudonymous user identifier still needs privacy controls.

OCI Monitoring is suitable for aggregate operational counters and alerts. A
marketplace funnel/cohort report requires separate event definitions and analysis;
putting user IDs into monitoring dimensions is not a substitute. Begin with a
bounded weekly report and event retention, then adopt a dedicated analytics tool
only when it solves a demonstrated reporting need.

Establish the baseline during the pilot, then agree a target and evaluation
window for each release. Do not invent percentage uplift forecasts. For small
cohorts, combine counts, individual task observations and problem reasons;
avoid interpreting a handful of conversions as a statistically reliable A/B test.

## Recommended first implementation

**F02, starting with View product buttons in public catalog results**, is the best
small next product change. It removes a concrete step visible in current code,
uses the existing product-detail flow and can be evaluated with a small pilot.
Add the visible Browse welcome entry as a second slice. Define F01's measurement
contract alongside it; richer funnel reporting can ship as its own change.

Initial acceptance criteria:

1. Every displayed public catalog item has a clearly labeled View button tied to
   its catalog ID; opening it displays the existing live detail/photo behavior.
2. An unregistered buyer can open details. Ordering still follows registration,
   fresh review and contact-sharing consent.
3. Browsing details preserves an unrelated active draft and does not extend that
   draft's 30-minute inactivity deadline.
4. A removed or unavailable item gives useful guidance. Invalid/group-chat
   callbacks do not expose private data or mutate products/orders; callbacks are
   acknowledged once.
5. Existing pagination, seller Manage controls and order buttons remain usable.
   Test the real dispatcher and observe the task with pilot users.
6. Define catalog-to-detail progression before release. If event instrumentation
   is not yet available, record that limitation and use observed task completion
   rather than claiming a conversion improvement.

The next larger feature should be F03/F04's accepted-order handoff, informed by
pilot cases. A payment button is not the first dependency for this marketplace.

## Decisions and operating cadence

The product owner chooses the initial category and geography, language/currency,
seller admission policy, fulfillment/contact model, support owner, staffing and
commercial model. A BA can research options and recommend priorities, but these
are business decisions rather than facts available from the repository.

Review the pilot scorecard and customer problems weekly. At each release, select
one outcome, a small feature slice, its acceptance criteria and measurement
window. Keep a decision/learning log that records evidence for continuing,
changing or stopping an initiative. See [agents and skills](PRODUCT_AGENT_WORKFLOW.md)
for a practical BA → engineering → independent review workflow.
