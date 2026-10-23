# Product agents and reusable skills

**Proposed on 23 October 2026** for the independent multi-seller marketplace
[roadmap](PRODUCT_ROADMAP.md). These are recommended roles and skill definitions;
this document does not install skills or create persistent agents.

## Recommended team

Start with **three core roles**. The same capable model can perform different
roles with different context and instructions; a BA agent does not require a
separately trained model. Use an independent reviewer for consequential changes
so the implementation is assessed against business expectations.

| Role | Need / ownership | Concrete outputs |
| --- | --- | --- |
| **Business Analyst / Product Analyst** | Essential now. Understand buyer, seller and operator needs; maintain evidence, requirements and priorities | Capability/journey map, opportunity brief, feature specification, business rules, acceptance criteria, metric definitions and learning log |
| **Product Engineer** | Essential for delivery; the existing coding assistant already fills this role | Feasibility and scope estimates, shared Telegram/API/store implementation, migrations where needed, appropriate tests and maintained docs |
| **Independent QA / Acceptance Reviewer** | Essential for changes affecting orders, stock, contacts, permissions or money; useful for other releases | Acceptance traceability, user-journey and failure-path review, regression evidence, defects and explicit remaining manual checks |
| **Conversation and Panel UX specialist** | Bring in for onboarding, discovery, checkout, storefront and panel changes | Example chat journeys, navigation, wording, wireframes and mobile/accessibility checks |
| **Data / Experiment Analyst** | Establish measurement now; initially combine with BA, separate when data volume warrants it | Event dictionary, data-quality checks, funnel/cohort reports, experiment design and post-release evaluation |
| **OCI Reliability / Release specialist** | Use for schema, capacity, retention, latency, recovery and deployment work | Operational impact, migration rehearsal, GitHub CI/deployment evidence, release identity/readiness and recovery runbooks |
| **Payments / Marketplace Operations specialist** | Add when payment, fees, refunds, settlement or disputes enter scope | Commercial flow, reconciliation/refund rules, support procedures and domain questions requiring qualified human decisions |
| **Growth / Content specialist** | Add after activation and repeat use can be measured | Seller acquisition experiments, listing guidance and drafts for opted-in lifecycle communication |

The product owner retains decisions on target market, pricing, policies, budget
and launch commitments. The BA recommends with evidence and tradeoffs. Engineers
resolve routine implementation details within the agreed scope. This division
should reduce repeated clarification, not introduce approval requests for every
reversible edit.

Agent roles concern **how the project is developed**. They do not imply adding
an AI chatbot or autonomous sales agent to the customer-facing product. Such a
feature needs its own customer problem and validation.

## BA agent charter

The BA is accountable for the quality of the problem definition and business
contract, not merely for producing a long feature list.

**Inputs:** current source and maintained docs, product-owner goals, authorized
aggregate product data, support themes, and explicitly supplied research.

**Responsibilities:**

1. Separate implemented capabilities, measured user evidence, assumptions and
   proposals. Link claims to their source and date.
2. Consider buyer, seller and operator journeys together. An easier buyer flow
   that creates unmanageable seller/support work may be a poor release.
3. Identify the smallest useful scope, business rules, dependencies, edge cases,
   acceptance criteria and success measure for each material feature.
4. Maintain distinct meanings for request, seller acceptance, seller-reported
   completion, buyer receipt and verified payment.
5. Explain priority using impact, evidence confidence, effort, risk and
   dependencies. Avoid numeric scoring that pretends unknown demand is measured.
6. Review observed outcomes after release and recommend what to continue,
   change or stop. Keep the roadmap current.

**First assignments:** establish the marketplace capability/evidence register;
specify F02 catalog View buttons; define F01's event/metric contract; map F03/F04's
accepted-order handoff; prepare a pilot interview/task guide. User recruitment
and sending invitations need explicit authorization. Drafting the guide does not.

### Reusable BA agent brief

```text
You are the business analyst for SMEbot, a Telegram marketplace for independent
sellers. Read AGENTS.md and the current product roadmap before starting.

Work from current source, maintained documentation, authorized product data and
explicitly supplied user research. Separate verified capabilities, observed user
evidence, assumptions and proposed features. Cite the evidence and its date.

For each opportunity identify the target persona, problem, current journey,
desired outcome and smallest useful change. Consider buyer, seller and operator
effects together. Define business rules, exceptions, data/consent implications,
dependencies, acceptance criteria and measurable success. Ask only for material
missing decisions; continue independent work using stated assumptions.

Preserve the distinction between order requests, seller acceptance, fulfillment
claims, buyer-confirmed receipt and verified payments. Do not invent usage,
revenue, customer research, implementation status or effort estimates.

Recommend priorities with rationale and confidence. The product owner owns
commercial and policy decisions; the engineer owns routine implementation
choices within scope. Follow the current task's authorization. Do not contact
users, inspect secrets, use production write fixtures, publish or deploy outside
that scope.

Return a concise recommendation and a reviewable feature brief. Store maintained
product documents under docs/ and link them from docs/README.md. Update the
decision/learning log when new evidence changes the recommendation.
```

### Feature brief template

```markdown
# Feature <ID>: <name>

Date, status, owner:
Target persona and problem:
Evidence and confidence: <source links; observations vs assumptions>
Current journey:
Desired outcome:
Smallest useful scope and later extensions:
Buyer / seller / operator behavior:
Business rules:
Exceptions: <no stock, stale action, retry, expiry, restart, cancellation>
Data, consent and retention impact:
Dependencies and unresolved decisions:
Acceptance criteria: <observable Given / When / Then examples>
Measurement: <events, denominator, baseline, target hypothesis, evaluation window>
Validation: <automated evidence and required human journey checks>
Rollout and recovery:
Decision / learning log:
```

For F02, one criterion would be: “Given an anonymous buyer viewing catalog
results, when they tap View for a listed product, then its current detail/photo
is shown without requiring them to type the ID or register.” Another must cover
preserving an active draft and its inactivity deadline. These criteria are more
useful than asking an engineer to “improve the catalog UX.”

## Reusable custom skills to build

An agent is a role responsible for an outcome. A skill is a reusable procedure
the agent follows. A connector provides access to a service; it does not supply
the project's business judgment. Keep skills short and refer to maintained
project documents for changing policies instead of copying those policies into
several places.

| Proposed custom skill | Priority / trigger | Required output and project-specific checks |
| --- | --- | --- |
| `marketplace-business-analysis` | Create first. New idea, feature request or roadmap review | Evidence register, current/desired journey, bounded scope, business rules, acceptance criteria and measurable outcome; use the BA charter above |
| `marketplace-acceptance-review` | Create first. A feature is ready for review | Map each criterion to evidence; independently exercise buyer/seller/operator journeys, authorization, stale actions, retries and restart behavior; separate simulated and live checks |
| `marketplace-feature-delivery` | Standardize next. Implementing an agreed feature | Reuse shared store rules across Telegram/panel; preserve stock, idempotency, contact consent, erasure, backup compatibility and media compensation; run appropriate checks and update docs |
| `telegram-marketplace-ux` | Add as onboarding/discovery changes grow | Example chat transcripts and panel flows, short prompts, recovery paths, mobile usability, expiry behavior and clear consent |
| `marketplace-measurement` | Define with F01; initially used by the BA | Event/metric dictionary, deduplication, bounded retention, cohort maturity, baseline and post-release report; no raw contact/chat content in analytics |
| `oci-bot-release` | Standardize before recurring releases by several agents | For an authorized release: migration/recovery compatibility, tests, GitHub deployment monitoring, matching release identity and readiness evidence; reuse repository runbooks |

These proposed skills are not in the current session's installed skill catalog.
Operationalize them later as actual `SKILL.md` packages in the environment's
supported skill location. A Markdown description here is a specification, not
an executable installed skill.

## Tools and connectors

| Tool / capability | Recommendation |
| --- | --- |
| **GitHub — already connected** | Use source, feature issues, decisions, reviews and CI evidence as the initial shared delivery record. Draft issues from BA briefs when requested |
| **Existing tests and local backend** | Use real-dispatcher journeys with simulated Telegram HTTP and isolated SQLite. Oracle write checks require an explicitly disposable database |
| **Cloud Environment skill — when available** | Supports safe environment/network/credential access. It was used for this review; skill availability can change between sessions. Combine it with project runbooks for OCI work |
| **OCI CLI and Monitoring through existing operational tooling** | Health, capacity, costs and release diagnostics. Business funnels still need event definitions and a reporting layer; verify current access before using live services |
| **Browser skill — when available** | Useful for seller-panel usability checks with isolated fictional fixtures and for explicitly authorized research tasks |
| **Figma — optional addition** | Useful when storefront, Mini App or seller-panel design needs shared interactive prototypes; unnecessary for the first catalog-button slice |
| **Linear / Notion — optional additions** | Consider if GitHub issues/docs no longer support the team's planning/research workflow. Keep one authoritative backlog rather than duplicating it |

No plugin installation or external backlog changes are required to use the
roadmap. GitHub and the existing workspace are sufficient for the next slice.

## Delivery workflow

1. **BA:** prepare the opportunity brief from evidence and stated assumptions.
2. **Owner + engineer:** settle material business choices, desired outcome and
   feasible scope. Existing authorization carries through routine implementation.
3. **UX specialist, when useful:** validate the intended conversation or panel
   journey before implementation grows expensive.
4. **Engineer:** implement, run appropriate checks and maintain docs.
5. **Independent reviewer:** assess the result against business criteria and
   record unresolved defects or manual checks; the author should not be the
   sole source of acceptance evidence for consequential flows.
6. **Release role:** execute an authorized release and monitor CI/deployment.
7. **BA / data role:** review the agreed evaluation window and update priorities.

Run independent journey analysis, feasibility analysis and reviews in parallel
when useful. Assign one owner to each edited artifact and reconcile shared
business rules before implementation. Short focused agent tasks with explicit
deliverables are preferable to a large permanent collection of agents.

The existing [repository guidelines](../AGENTS.md),
[ordering rules](ORDERING.md), [product-management safeguards](PRODUCT_MANAGEMENT.md),
[privacy/erasure guidance](../README.md), [conversation lifecycle](CONVERSATION_LIFECYCLE.md)
and [deployment guide](DEPLOYMENT_READINESS.md) remain the implementation sources
of truth. Role instructions should point to them, not replace them.
