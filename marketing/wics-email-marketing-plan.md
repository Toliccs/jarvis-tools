# WICS: Segmented Email Marketing Plan
**Client:** Western Industrial Cleaning Suppliers (WICS), www.wics.com.au
**Prepared:** November 2026 · **Plan period:** 12 months (Dec 2026 – Nov 2027)

---

## 0. Assumptions (confirm before build)

The site couldn't be reached while this plan was being written, so it rests on a standard Australian B2B cleaning-supply model:

| Assumption | If wrong, adjust |
|---|---|
| B2B distributor of cleaning chemicals, janitorial consumables (paper, liners, washroom), equipment (scrubbers, vacuums, pressure washers), PPE and dispensers | Remove or add product-affinity segments (§3E) |
| Based in Western Australia (or western metro). Serves metro plus regional/remote sites (mining, agriculture) | Swap the geography segments (§3G) and seasonal hooks |
| Sells through trade accounts (30-day terms), quotes and possibly online ordering | If there's no e-commerce, change cart/browse flows to quote/enquiry flows |
| Offers equipment servicing, repairs or hire | Drop the Equipment Owner flow if not |
| Has a CRM/ERP with order history that can sync to an ESP | Without order data, start with the lead-stage segments only |

---

## 1. Objectives & targets

| # | Objective | 12-month target |
|---|---|---|
| 1 | Grow email-attributed revenue | Email ≥ 8–12% of trade revenue |
| 2 | Increase share of wallet (categories per account) | Average +1 category per active account |
| 3 | Lift reorder frequency on consumables | +15% repeat-order rate in the replenishment segments |
| 4 | Convert quotes to orders | +10 pts quote-to-order conversion |
| 5 | Reduce churn | Win back 15% of lapsed accounts; cut at-risk → lapsed by 25% |
| 6 | Build the list | +30% engaged, consented contacts. Every key account has ≥ 2 contacts |

---

## 2. Data foundation (do this first)

Segmentation is only as good as the fields behind it. Capture or sync these for every contact:

**Account-level (from ERP/CRM):** account ID, trade vs cash, industry vertical, number of sites, region/delivery zone, account manager, credit terms, 12-month spend, last order date, order count, categories purchased, equipment owned (model + purchase date), contract/pricing tier.

**Contact-level:** role (buyer/procurement, site/facilities manager, cleaning supervisor, owner, WHS/infection control, accounts payable), consent source and date, preferred content, email engagement score.

**Behavioural (from site + ESP):** categories browsed, quote requests, cart activity, SDS downloads, content clicked (tags per click).

**Progressive profiling:** ask one question per email or landing page, e.g. "What industry are you in?", "How many sites do you clean?", "Which machines do you run?" Offer one-click answer buttons that write tags.

---

## 3. Segmentation framework

Seven dimensions, layered. Each send targets **at least two dimensions** (e.g. *Vertical × Lifecycle*).

### A. Industry vertical (primary content driver)
| Code | Vertical | Core pain points / messaging hooks |
|---|---|---|
| V1 | Commercial cleaning contractors | Margin per job, bulk/concentrate pricing, reliable stock, multi-site delivery, staff training |
| V2 | Healthcare, aged care & medical | Infection control, TGA-listed disinfectants, audit readiness, outbreak kits |
| V3 | Hospitality & food service | HACCP, food-safe sanitisers, kitchen degreasers, guest-facing washrooms, peak-season stock |
| V4 | Food & beverage manufacturing | CIP chemicals, allergen control, colour-coded tools, audit documentation (SQF/BRC) |
| V5 | Mining, resources & heavy industry | Heavy-duty degreasers, hand cleaners, bulk IBCs, remote-site logistics, WHS/PPE, shutdown cleaning |
| V6 | Education & childcare | Child-safe/low-tox products, term-break deep cleans, budget cycles, washroom hygiene |
| V7 | Facilities management, strata & property | Multi-site consolidation, contract compliance, floor-care machines, sustainability reporting |
| V8 | Automotive, transport & logistics | Workshop degreasers, truck/fleet wash, absorbents and spill kits, warehouse floor scrubbers |
| V9 | Government, councils & public venues | Tender/panel compliance, Australian-made, sustainability, public-toilet hygiene |
| V10 | Offices, retail & gyms | Washroom consumables, touchpoint sanitising, dispensers, bin liners |

### B. Buyer role (tone and CTA driver)
| Role | Cares about | Email angle | CTA |
|---|---|---|---|
| Owner / GM | Cost, risk, growth | ROI, total-cost-of-cleaning, case studies | "Book a cost review" |
| Procurement / purchasing | Price, terms, consolidation, compliance | Price lists, bundles, one-invoice supply | "Get a contract quote" |
| Facilities / site manager | Uptime, compliance, simplicity | Checklists, audit tools, service schedules | "Download the checklist" |
| Cleaning supervisor / end user | Ease of use, performance, safety | How-tos, dilution charts, product demos | "Watch the 60-sec demo" |
| WHS / infection control | Safety, SDS, standards | SDS updates, chemical safety, training | "Get SDS pack" |
| Accounts payable | Invoicing | Transactional only. No marketing | n/a |

### C. Lifecycle stage
| Stage | Definition (adjust thresholds to actual purchase cycles) |
|---|---|
| L1 Subscriber | Opted in, no enquiry |
| L2 Lead | Enquiry, quote request or sample request, no order |
| L3 New customer | First order in last 90 days |
| L4 Active | Ordered within normal cycle (e.g. ≤ 60 days for consumables) |
| L5 At-risk | 1.5× normal cycle since last order, or order value down ≥ 30% |
| L6 Lapsed | 3× normal cycle (e.g. 180+ days) |
| L7 Win-back exhausted | No response after win-back. Move to quarterly-only |

### D. Value tier (RFM-based)
| Tier | Criteria (example) | Treatment |
|---|---|---|
| T1 Key accounts | Top 10% by 12-mo spend | Account-manager-signed emails, early access, quarterly business review invites, no discount-led messaging |
| T2 Growth | Mid spend, ≥ 2 categories, rising frequency | Cross-sell, bundles, volume-break offers |
| T3 Standard | Regular, low-mid spend | Replenishment, education, product spotlights |
| T4 Occasional / cash | Infrequent, small | Convert to trade account, promotions, reorder nudges |

### E. Product-category affinity
P1 Chemicals & concentrates · P2 Paper & washroom · P3 Bin liners & waste · P4 Floor care · P5 Machines & equipment · P6 PPE & safety · P7 Dispensers & systems · P8 Spill control & absorbents · P9 Tools (mops, microfibre, colour-coded) · P10 Service / repairs / hire.

Use it two ways: **affinity** (what they buy, so they get relevant news and replenishment) and **gap** (what similar accounts buy that they don't, so they get cross-sell).

### F. Behavioural triggers
Browsed a category twice in 7 days · abandoned cart · quote sent, not accepted in 7 days · downloaded an SDS · clicked a machine page · bought a machine (starts the ownership flow) · consumable SKU due for reorder.

### G. Geography / logistics
G1 Metro (next-day delivery) · G2 Regional · G3 Remote/FIFO sites. Use for delivery cut-off reminders, freight-free thresholds, "order before shutdown" timing and local event invites.

### Priority micro-segments (launch first)
| # | Segment | Definition | Why it matters |
|---|---|---|---|
| S1 | Contractor Growth | V1 × T2/T3 × Active | Largest volume buyers. Bundle/consolidation upside |
| S2 | Healthcare Compliance | V2 × any active × Facilities/WHS roles | High-value, compliance-driven, sticky |
| S3 | Hospitality Peak | V3 × Active, Oct–Mar | Seasonal spike. Pre-stock campaigns |
| S4 | Mining & Heavy Industry Remote | V5 × G3 | Bulk orders. Logistics-led messaging |
| S5 | Machine Owners | Owns P5 equipment | Service, parts, consumables and upgrade revenue |
| S6 | Single-Category Buyers | Active, only 1 category | Cross-sell, the biggest share-of-wallet lever |
| S7 | Open Quotes | L2 with quote ≤ 30 days old | Fastest revenue |
| S8 | At-Risk Key Accounts | T1/T2 × L5 | Highest revenue at stake. Triggers AM alert too |
| S9 | Lapsed Trade | L6 × trade account | Win-back |
| S10 | New Trade Accounts | L3 | Onboarding drives the second order |
| S11 | Education Term-Break | V6 | Predictable deep-clean cycle |
| S12 | Cash → Trade | T4 cash buyers with ≥ 2 orders | Convert to account |

---

## 4. Automated flows (always-on)

### Flow 1: Welcome (branch by signup source)
| # | Timing | Content |
|---|---|---|
| 1 | Immediate | Welcome. Who WICS is, delivery areas, how to open a trade account. One question: "What industry are you in?" (sets the vertical tag) |
| 2 | +2 days | Vertical-specific best sellers + one relevant guide (e.g. healthcare disinfectant selection guide) |
| 3 | +5 days | Social proof: case study from the same vertical |
| 4 | +9 days | Offer: free site assessment / product trial / samples |
| 5 | +14 days | Role question ("What best describes your role?") + preference centre link |

### Flow 2: Quote follow-up (S7)
- **Day 2:** "Any questions on your quote?" Plain text, from the sales rep.
- **Day 5:** Proof point relevant to the quoted category (dilution savings, case study).
- **Day 9:** Quote expiry reminder + one-click "Accept quote / Request changes".
- **Day 14:** Task created in CRM for a phone call. Email stops.

### Flow 3: New-customer onboarding (S10)
| # | Timing | Content |
|---|---|---|
| 1 | Order shipped | Thank you, delivery info, SDS links for products ordered |
| 2 | +3 days | How to get the best results: dilution chart / usage guide for purchased SKUs |
| 3 | +10 days | Meet your account manager + how to reorder (portal, standing orders) |
| 4 | +21 days | Complementary products ("Customers who buy X also use Y") |
| 5 | +45 days | Reorder reminder (if no second order) + review request |

### Flow 4: Replenishment reminders
Trigger at about 80% of each account's average reorder interval per consumable category (P1, P2, P3, P9).
- Email 1: "Running low on [product]?" Last order summary + one-click reorder.
- Email 2 (+5 days, no order): bundle or volume-break suggestion.
- Offer standing/scheduled orders after the third replenishment.

### Flow 5: Equipment ownership (S5)
| Timing after purchase | Content |
|---|---|
| Day 1 | Registration, warranty, operator quick-start video |
| Day 14 | Operator training tips + correct chemicals/pads for this model |
| Day 60 | Consumables for this model (pads, brushes, filters, squeegee blades) |
| Month 6 | Service reminder. Book a service |
| Month 11 | Warranty expiry: extended service plan offer |
| Year 3–5 | Upgrade/trade-in offer; hire options during servicing |

### Flow 6: Browse / cart abandonment
- Browse (2+ views of a category, no cart): 1 email at +24h with top products in that category + "Talk to a specialist".
- Cart: +2h reminder → +24h with reviews/specs → +72h freight-free threshold or a small incentive (T3/T4 only. Never discount T1).

### Flow 7: Cross-sell gap (S6)
Monthly check. If the account buys category X but not commonly paired category Y, send one email per gap, every 30 days, max three gaps:
- Chemicals → microfibre/colour-coded tools ("protect your chemical spend")
- Paper → dispensers (free dispenser with contract supply)
- Floor chemicals → floor machines/pads
- Any vertical V5/V8 → spill kits & PPE

### Flow 8: At-risk save (S8 and broader L5)
- Day 0: "Everything OK?" Personal, plain text from the AM. Creates a CRM task for T1/T2.
- Day 7: What's new (new products, price holds, delivery improvements).
- Day 14: Feedback survey (2 questions) + offer to review pricing.

### Flow 9: Win-back (S9)
- Email 1: "We miss you." What's changed since their last order.
- Email 2 (+10 days): Their past top products, at current price, with one-click reorder.
- Email 3 (+20 days): Time-boxed incentive (freight-free or % off the first order back).
- Email 4 (+35 days): "Should we keep emailing you?" Preference centre. No click means L7 (quarterly only).

### Flow 10: Cash → Trade conversion (S12)
After the second cash order: benefits of a trade account (pricing, terms, portal, AM) → testimonial → "Open an account in 3 minutes".

### Flow 11: Post-purchase review & referral
+30 days after order: Google review request. Customers who give a positive NPS get a referral ask (credit for referring another business).

---

## 5. Broadcast campaign calendar (Australian B2B context)

Each broadcast goes out as **versioned sends by vertical** (dynamic content blocks), not one blast.

| Month | Theme | Primary segments | Hooks |
|---|---|---|---|
| **Dec 2026** | Pre-shutdown stock-up | All active, G2/G3 first | Order cut-off dates, holiday trading hours, hospitality peak (V3) |
| **Jan 2027** | Back to school / new-year deep clean | V6, V1, V7 | Term-break deep-clean kits, contractor new-year pricing |
| **Feb** | Summer hygiene & heat | V3, V5, V10 | Odour control, hydration/PPE for outdoor crews, washrooms in peak trade |
| **Mar** | Sustainability & concentrates | V7, V9, V1 | Concentrates/refill systems, packaging reduction, sustainability reporting |
| **Apr** | Floor care & machines | S5, V7, V8, V1 | Machine demo days, autumn floor restoration, Easter/school-holiday clean (V6) |
| **May** | World Hand Hygiene Day (5 May) + flu season prep | V2, V6, V10 | Hand hygiene audits, outbreak kits, dispensers |
| **Jun** | **EOFY**: spend remaining budget, buy equipment before 30 June | T1/T2, owners, procurement, S5 | Equipment purchases before EOFY (instant asset write-off if applicable. Check current rules), bulk pre-buy, price-rise heads-up |
| **Jul** | New FY pricing & contracts | Procurement, T1/T2, V9 | Contract supply reviews, price-lock offers, new catalogue |
| **Aug** | Winter outbreak response | V2, V6, V3 | Gastro/flu outbreak kits, disinfection protocols |
| **Sep** | Spring clean + WHS awareness | V1, V7, V5, V8 | Spring deep clean, chemical safety training, spill-kit audits |
| **Oct** | Global Handwashing Day (15 Oct) + WorkSafe/Safe Work Month | V2, V5, V6, WHS roles | Training sessions, SDS refresh, PPE |
| **Nov** | Hospitality & events peak pre-stock; Black Friday-style trade deal (T3/T4 only) | V3, V10, T3/T4 | Christmas function season, pre-stock bundles, customer appreciation (T1, no discount) |

**Ongoing broadcasts**
- **Monthly "Clean Insights" newsletter.** One per vertical cluster (Healthcare/Aged Care, Hospitality & Food, Industrial & Mining, Contractors & FM, Education & Public). Contents: 1 tip, 1 product spotlight, 1 compliance/industry update, 1 case study.
- **Quarterly Key Account brief (T1).** From the AM: usage summary, savings achieved, new products, invite to a business review.
- **Product launches.** Sent only to segments with category affinity plus their gap segments.
- **SDS / regulatory updates.** Sent to WHS roles + buyers of the affected product (service message).

---

## 6. Personalisation rules

- **Dynamic hero and product blocks by vertical.** One template, ten variants.
- **Sender by tier.** T1/T2 get emails from their named account manager; T3/T4 from "The WICS Team".
- **Last-order block.** "Your recent order" + one-click reorder in all replenishment and win-back emails.
- **Machine-model block.** Consumables and parts for the exact model owned.
- **Geo block.** Delivery cut-off and freight-free threshold by zone (G1/G2/G3).
- **Role-based CTA.** The same campaign carries a different CTA per role (see §3B).
- **Exclusions.** Never send cross-sell for a category bought in the last 30 days. Suppress promos to accounts with open credit holds. Suppress win-back while an account has an open quote.

---

## 7. Cadence & frequency caps

| Segment | Max marketing emails / week | Notes |
|---|---|---|
| T1 key accounts | 1 | Plus AM 1:1 emails. Flows take priority over broadcasts |
| T2/T3 active | 2 | |
| Leads (L1/L2) | 2 | Welcome/quote flows suppress broadcasts while running |
| At-risk / lapsed | 1 | Flow only |
| L7 exhausted | 1 per quarter | Sunset after 12 months of no engagement |

**Send times (test):** Tue–Thu, 6:30–8:00am AWST for site/supervisor roles (pre-shift); 10:00–11:00am for procurement/office roles.
**Flow priority order:** Quote > Onboarding > At-risk > Replenishment > Cross-sell > Broadcast.

---

## 8. Testing roadmap

| Quarter | Test | Segment | Success metric |
|---|---|---|---|
| Q1 | AM-signed plain text vs designed HTML | T1/T2 | Reply rate, CTR |
| Q1 | Send time pre-shift vs mid-morning | Supervisors vs procurement | CTR |
| Q2 | Replenishment trigger at 70% vs 85% of cycle | Replenishment | Reorder rate |
| Q2 | Freight-free vs % discount in cart recovery | T3/T4 | Recovered revenue |
| Q3 | Case study vs checklist lead magnet | Welcome, V2/V4 | Lead → quote rate |
| Q3 | Bundle vs single-product cross-sell | S6 | New category adoption |
| Q4 | Subject line: compliance angle vs cost angle | V2, V9 | CTR, conversions |

Test one variable at a time. Minimum ~1,000 recipients per variant, or run the test across consecutive sends for smaller segments.

---

## 9. KPIs & reporting

Report monthly **by segment**, not just in aggregate:

| Metric | Target range (B2B industrial) |
|---|---|
| Click-through rate (broadcast) | 2–4% |
| Click-through rate (flows) | 5–10% |
| Revenue per recipient (by segment) | Baseline in month 1, then +10% QoQ |
| Quote-to-order (S7) | +10 pts vs pre-email baseline |
| Repeat-order rate (replenishment) | +15% |
| Categories per account | +1 average |
| Win-back rate (S9) | 10–15% |
| Unsubscribe rate | < 0.3% per send |
| Spam complaint rate | < 0.1% |
| List growth (net engaged) | +2–3% / month |

Open rates are unreliable because of Apple Mail Privacy Protection. Use clicks, replies and revenue as the primary signals.

---

## 10. Compliance (Australia)

- **Spam Act 2003:** consent (express, or inferred from an existing business relationship and kept documented), clear sender identification (WICS name + ABN + contact details) and a working unsubscribe in every commercial email, honoured within 5 business days.
- **Privacy Act 1988 / APPs:** privacy policy linked in the footer; record consent source/date; collect only the data you need.
- **Product claims:** disinfectant/hospital-grade claims must match TGA registration/listing and the product label. Keep SDS links current.
- Transactional/service emails (SDS updates, order confirmations) are kept separate from marketing.

---

## 11. Tech stack & implementation roadmap

**Recommended:** an ESP with B2B data handling and flows (e.g. Klaviyo if e-commerce is on Shopify/WooCommerce; HubSpot or ActiveCampaign if CRM-led). Two-way sync with the ERP/CRM for orders, accounts and equipment. Website tracking for browse/cart/quote events.

| Phase | Weeks | Deliverables |
|---|---|---|
| 1. Foundation | 1–3 | Data audit, field mapping, ERP→ESP sync, consent audit, list cleanse, preference centre, vertical/role tagging of existing contacts (AM-assisted for T1/T2) |
| 2. Revenue flows | 4–6 | Quote follow-up, onboarding, replenishment, cart/browse, at-risk |
| 3. Growth flows | 7–9 | Welcome (by vertical), equipment ownership, cross-sell gap, win-back, cash→trade, review/referral |
| 4. Broadcast engine | 8–10 | Modular template with dynamic vertical blocks, five newsletter variants, Dec–Feb campaigns built |
| 5. Optimise | 11+ | Testing roadmap, monthly segment reporting, quarterly re-segmentation (RFM recalculated) |

---

## 12. Open questions for WICS

1. Exact location(s) and delivery footprint?
2. Top 3 industries by revenue today, and which ones they want to grow?
3. E-commerce/online ordering in place? Which platform?
4. CRM/ERP in use, and can order history be exported or synced?
5. Do they service, repair or hire equipment?
6. Current list size, consent status and existing email activity?
7. Key brands carried / own-brand products?
8. Any certifications (ISO, Australian-made, GECA/green certification) to feature?
