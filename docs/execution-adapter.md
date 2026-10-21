# Read-only execution adapter evidence

Primary contract: RussianInvestments/investAPI, pinned commit
`3eaf23a25f598fe483c913184acdbd9132bc68d2`. Verified from the official
public repository; no account, token or live Invest API request was used.

## Units and availability

- [PortfolioPosition](https://github.com/RussianInvestments/investAPI/blob/3eaf23a25f598fe483c913184acdbd9132bc68d2/src/docs/contracts/operations.proto#L154-L166)
  documents `quantity` in pieces. Marks are per piece. The full destination
  budget is the sum of `(quantity * current_price).quantize(1e-9)` for each
  original position, including currency positions. Duplicate security UIDs
  aggregate pieces only when mark, currency and instrument type agree.
  Aggregation never moves nano rounding after the sum. Free cash is not an
  additional budget term. No lot multiplication is applied to marks.
  `PortfolioEntry.currency` preserves the valuation unit from
  `PortfolioPosition.current_price.currency`, independently of the instrument's
  native trading currency. Without an explicit `Valuation`, the RUB-budget adapter rejects any non-RUB valuation
  before returning a destination snapshot; it never sums incompatible monetary
  units or drops an unsupported holding. RUB-valued holdings retain the original
  per-position nano arithmetic, without an additional instrument-currency filter.
- [GetMaxLotsResponse](https://github.com/RussianInvestments/investAPI/blob/3eaf23a25f598fe483c913184acdbd9132bc68d2/src/docs/contracts/orders.proto#L243-L258)
  documents `currency` as the instrument currency, `buy_limits` as limits on
  own money and `sell_limits` as limits on the own position. Within these,
  `buy_money_amount` is available currency for buying (Quotation units/nano),
  `buy_max_lots` and `sell_max_lots` are counts of lots. Requests include the
  destination account and instrument UID. Margin limits are ignored. The
  separate market-order cap is not evidence of a BESTPRICE cap and is ignored.
  Currency codes from GetMaxLots and metadata are compared case-insensitively;
  no currency conversion or alias substitution is performed. The metadata
  currency is retained in TradeRules. A genuine mismatch fails before trading
  and reports both original currency codes and the instrument UID.
- [GetTradingStatusResponse](https://github.com/RussianInvestments/investAPI/blob/3eaf23a25f598fe483c913184acdbd9132bc68d2/src/docs/contracts/marketdata.proto#L515-L526)
  provides explicit API and BESTPRICE availability flags. API permission also
  requires full instrument metadata permission. Board/status enum values and
  market/limit flags do not substitute for these flags.
- [GetPositionsResponse](https://github.com/RussianInvestments/investAPI/blob/3eaf23a25f598fe483c913184acdbd9132bc68d2/src/docs/contracts/operations.proto#L130-L138)
  describes currency positions, blocked currency positions and loading state;
  it does not establish a general subtraction formula. This adapter never
  subtracts `blocked` from `money` and never uses `balance` as pieces. When all
  explicit blocking is zero, `money` is retained as a currency-position upper
  bound in `available_cash`, to be intersected with the documented own
  `buy_money_amount` and lot caps during future execution. It is not sufficient
  by itself to authorize a purchase. A currency absent from this map has no
  demonstrated cash bound; future execution must defer that purchase.
- [PositionsSecurities](https://github.com/RussianInvestments/investAPI/blob/3eaf23a25f598fe483c913184acdbd9132bc68d2/src/docs/contracts/operations.proto#L193-L203)
  and the [operations guide](https://github.com/RussianInvestments/investAPI/blob/3eaf23a25f598fe483c913184acdbd9132bc68d2/src/docs/head-operations.md#L65-L79)
  document blocking indicators, including depositary `exchange_blocked`.
  Portfolio `blocked` and numeric `blocked_lots` are preserved by the existing
  strategy DTO translator. Any numeric blocking in money, securities, futures
  or options, any portfolio/exchange flag, loading, or active NEW/PARTIALLYFILL
  order makes the whole execution snapshot unready and zeros both cash and
  piece availability. Missing/malformed fields raise ValueError rather than
  being treated as zero. SDK unset source-fixture fields become neutral None
  and are rejected when used for destination execution.

Example: a full currency position of 120 RUB contributes 120 RUB to B.
With explicit currency blocking of 25 RUB, the snapshot is unready and
advertises 0 RUB, not an inferred 95 RUB. With all guards clear, a 120 RUB
currency bound and own `buy_money_amount=75.5 RUB` bound constrain future
purchases to their intersection. An instrument with lot=10, quantity=23.5
pieces and sell_max_lots=2 still has 23.5 portfolio pieces and an own cap of
2 lots. Neither price nor cash is multiplied by 10.

## Scope and settlement

The operations guide explicitly says GetPositions excludes futures collateral.
Therefore currency positions alone are insufficient: own GetMaxLots cash/caps
are an additional mandatory guard, without a margin path. The adapter does not
infer a reserve or read strategy configuration. A fresh read always performs
new requests; there is no execution-state cache.

There is no claimed atomic settlement across these reads. BESTPRICE price
movement and non-atomic reads remain accepted execution risks. Pending sales
in NEW/PARTIALLYFILL are polled through GetOrderState using the broker order_id
every two seconds in a 30-second window, without resubmission or cancellation.
The neutral executor verifies UID, side, order_id and both lot counts before
accepting FILL. Rejection, malformed data, transport failure or execution
timeout stops the pass with ERROR; unconfirmed purchases still stop immediately.
After all sales fill, a separate 30-second window polls fresh destination
readiness and matching physical positions every two seconds. A settlement
timeout defers purchases with WARNING. Readiness is not proof that all proceeds
have arrived: purchases still require fresh own money and caps. Neither
order price nor `total_order_amount` nor target marks establish settled cash.
The same readiness/position polling window runs after each confirmed BUY
when more purchases remain; temporary post-purchase blocking must not abandon
the whole remaining queue immediately. Already submitted UIDs are not resubmitted.
The SDK order adapter therefore leaves neutral `ExecutionReceipt.cash`
empty. A receipt from an executor with documented net monetary facts may
provide those facts per currency: execution attributes them to the sale's
owners and permitted recipients, capped by fresh global availability and own
limits. Unknown sale money cannot fund isolated pools; those purchases defer
with INFO. Fresh global money may fund a common pool only when every seller
can fund every recipient. It is never assigned to an isolated seller from
the account cash delta. This receipt field is used by the neutral funding
protocol and is not an SDK proceeds estimate.

## Broker receipts and request identity

The installed SDK schema (`t-tech-investments==1.51.0`) supplies
`instrument_uid`, `direction`, `order_id`, `execution_report_status`,
`lots_requested` and `lots_executed` in both PostOrderResponse and OrderState.
The common SDK converter copies these response facts into ExecutionReceipt;
it never fills missing identity from the intent or the submitted request ID.
UID and broker order ID must be nonempty strings. Direction must be an
OrderDirection BUY/SELL enum; status must be an OrderExecutionReportStatus
FILL/REJECTED/CANCELLED/NEW/PARTIALLYFILL enum. Plain numbers, bools, strings,
UNSPECIFIED and unsupported enum values are rejected. Lot counts must be int,
excluding bool, with `0 <= lots_executed <= lots_requested`. Missing or malformed
mandatory fields raise OrderExecutionError with a fixed safe reason.
Structurally valid facts remain unchanged even when they describe a different
order: the neutral executor checks them against the intent and stops the pass
on a mismatch. REJECTED/CANCELLED never become a successful FILL.

The client request ID is generated once after validate_intent, before PostOrder,
and sent in the request's `order_id` argument. It is local to that submission;
invalid intents call neither the ID factory nor the SDK. The broker order ID
comes only from a validated response and is not assumed equal to the request ID.
INFO links `request_id` and `broker_order_id` with the response UID, side, status
and lot counts. ExecutionReceipt keeps only the broker ID; SELL polling uses
that ID without generating a request ID or submitting another order.

On a PostOrder RequestError or malformed response, OrderExecutionError preserves
the optional keyword-only `request_id` context; existing message-only calls
remain compatible. The adapter emits ERROR with that same request ID, validated
intent UID/side/lots and a fixed reason. It excludes RequestError text, raw kwargs,
malformed field values and SDK repr. A transport RequestError is preserved as
the exception cause. Malformed GetOrderState responses emit ERROR with the
requested broker ID and a fixed reason, then propagate OrderExecutionError.
Propagation keeps the context and cause: CLI stops the current pass and continues
events, while run_sync/cloud propagate failure without Success. A later event
may initiate a fresh pass. Programming errors are not caught as broker failures.
Preserving cause does not sanitize third-party tracebacks; the broader error and
reporting work in issue #16 is outside this change.

These are local DTO, adapter and lifecycle checks. They do not establish that
every live response populates the schema, or constitute sandbox/production or
cloud deployment acceptance; those checks require separate authorization.

## Multicurrency Monetary Contracts (Preparatory)

`cash_facts.py` and the minimal `FxLotQuote` in `fx_data.py` establish pure
contracts for the multicurrency implementation. SDK `ExecutionReceipt.cash={}`
remains unchanged. The pure funding planner consumes these contracts through
an explicit input; SDK runtime connects them through `ExecutionMarket`.
Direct-pair discovery and timestamp freshness are now provided by the separate
read-only FX layer below. Reserve allocation belongs to the pure funding layer;
FX submission and dependent purchase orchestration are described below.

`MoneyAmount` carries explicit native currency and exact Decimal units. Its
units/nano constructor rejects bools, nonintegers, out-of-range nanos and
opposite component signs; it preserves nanos even for large unit values.
Currency case normalization does not infer identity or introduce aliases.

`SecurityQuote` distinguishes money per piece from percent of nominal. For
percent quotes, native midpoint per piece is `(bid + ask) / 2 / 100 * nominal
+ accrued_interest`. Nominal must be positive, with explicit nonnegative ACI
in the settlement currency, including an explicit zero. A money-per-piece
quote already includes any ACI; supplying nominal or ACI again is rejected.
Neither midpoint is an execution price or BESTPRICE price parameter. The
security midpoint and the direct FX midpoint are independent observations.

`FxLotQuote` stores RUB bid/ask per nominal base currency units and the number
of base units in one lot. Midpoint RUB per base unit divides by nominal;
BUY cost uses ask, SELL proceeds use bid, and base movement uses lots times
lot size. These are estimates before fees. `can_trade` intersects caller-scoped
fresh native cash, readiness, API/BESTPRICE flags and own side-specific lot
caps, plus own monetary limits for BUY. It supplies neither ownership rights
nor a fee allowance. The caller must check total purchase funding separately.
A TOM instrument name imposes no calendar delay when actual readiness and
own limits permit the trade; an earlier FILL alone supplies no such permission.

`OrderAmounts` keeps total and executed commission separate from execution
identity. `order_cash_bound` requires matching nonempty broker ID, UID, side,
positive integer lots and complete FILL. Missing, malformed, negative or
cross-currency monetary fields produce no bound; an explicit zero commission
is valid. No balance delta or operation history supplies missing fields.
For uncertain commission inclusion, BUY uses total plus commission as an
upper debit bound; SELL uses max(0, total minus commission) as a lower credit
bound. Bond total already includes ACI, so ACI is not added again.
`FeeInclusion.INCLUDED` may be selected only with separate evidence for the
specific field/RPC, and leaves the total unchanged; `SEPARATE` adjusts once.
An included fee greater than its total is inconsistent. No inclusion mode
turns `CashBound` into an exact cash receipt or authorizes isolated proceeds.
Late commissions and readiness must still be checked through fresh broker
facts; zeros and estimates do not prove final settlement.

Sandbox evidence from 2026-10-15 (SHAs
`3b377a7b870b740c07ed4da8c6a0766b4abc57af` and
`3ead6898cbf7a35e954e14c8cd210b9cd82ac443`) showed total plus fee for BUY
and total minus fee for SELL. This conflicts with the documented inclusion
description for PostOrder/OrderState and establishes bounds rather than an
exact production rule. GetOrderPrice had different observed total semantics
and requires native money per security, not percent of bond nominal. It is
not required for BESTPRICE. The same history fee can appear in a BUY/SELL
operation and a BROKER_FEE operation; summing both would duplicate it. History
operation IDs did not identify the orders in GetOrderState. The working path
continues to use only broker order IDs, without history or GetOrderPrice RPCs.

`PurchaseCashNeed` is a pure purchase-funding predicate: purchase cost plus
one fee upper bound is checked against available money. The existing leaf
reserve may fund that fee, without an extra percentage or a second reserve
charge. Money sufficient for the purchase and fee needs no exchange even if
the remaining reserve falls below its target. With no security purchase,
reserve shortage alone never calls for FX. This predicate grants no route,
scope, lot or budget permission. The pure funding layer distributes reserve
only within leaves by target RUB values per settlement currency, excludes
COMPOSITE unallocated money, and subtracts existing permitted currency before
rounding the necessary FX deficit. Reverse FX remains limited to financing
RUB security purchases, without foreign-currency bridges.

Offline tests in `test/test_fx_broker_contract.py` exercise these contracts
and the real SDK order boundary with autospecced unwrapped services. Existing
broker receipt and execution tests retain isolated/common-pool attribution
and fresh readiness checks. No live, TOM-cycle or production acceptance is
claimed by these tests.

### Direct FX Read Layer (Preparatory)

`FxData` exposes full pair metadata, two-sided book observations and fresh own
trade rules. `TInvestFxData` uses SDK 1.51.0 Currencies with
INSTRUMENT_STATUS_ALL, GetOrderBook(depth=1), GetTradingStatus and GetMaxLots.
It performs no requests on construction and has no market/limits cache.
Base identity comes from `iso_currency_name`, checked against
`nominal.currency`; `currency` identifies the quote/settlement currency.
Nominal units and lot size remain separate, with strict units/nano conversion.
Ticker strings do not establish currency identity or settlement mode.

Before constructing FxPair, the adapter excludes only the observed noninstrument
shape: exactly zero nominal after strict units/nano conversion, integer lot=0
(not bool), and all three API/buy/sell flags explicitly False. UID, ISO base,
nominal currency and quote currency must still be valid. A bounded WARNING
`fx_catalog_skipped` lists the first20 skipped metadata records and omitted_count
from that same response, without attributing a requested adjacent UID to them.
Positive-nominal/positive-lot unavailable pairs remain usable for valuation.
Other malformed metadata still raises; a required excluded UID has no route
and fails before effects. No defaults, ticker/UID exceptions or extra RPCs.

This addresses run38995748904 on fe88ed2: unused USDFIXME
`c898d684-a9df-4e1a-a30b-ad9dbb5ea1f9`, USD nominal0/0, lot0 and three False
aborted catalog conversion before valid CNYRUB_TOM could be selected. The first
guard was nominal, not lot. These are observed facts, not a hardcoded identity.
Offline reproduction and Runner regression do not establish live acceptance.

The pure selector accepts only foreign-base/RUB-quote routes. It rejects
inverse routes, bridges, duplicate UIDs and ambiguous catalogs. Multiple
settlement instruments require an explicit UID; availability and catalog
order cannot choose between them. Closed trading does not erase an otherwise
valid route or prevent a fresh midpoint valuation.

`FxSnapshot` defensively copies observations into an immutable mapping and
freezes one computed RUB midpoint per native currency unit for the calculation.
FX division uses precision 28 and HALF_EVEN independently of a caller's
Decimal precision/rounding; later context changes cannot alter snapshot rates. RUB
identity requires zero FX requests. Both market and receipt timestamps must
be timezone-aware. Market timestamps may lead the local receipt clock by at most
100 milliseconds inclusive; greater future offsets fail. Receipt ordering remains
strict, and market age is at most 120 seconds inclusive. Empty, crossed,
nonpositive, malformed or stale books fail;
last_price, its timestamp and an implicit rate of 1 are never fallbacks.
The caller can recheck freshness before effects and must read new data on
expiry. A snapshot grants no ownership, spending or trading permission.

`FxQuote.can_trade` separately intersects full metadata side permissions,
API/BESTPRICE flags, own side-specific lot caps, own BUY money and the caller's
fresh `ExecutionSnapshot` readiness/cash. No margin limits, inferred TOM
calendar delay, reserve subtraction, price parameter or GetOrderPrice is added.
Transport errors preserve their cause with a fixed safe public reason.
`test/test_fx.py` and `test/test_tinvest_fx_data.py` verify these pure/read
boundaries offline with autospecced ports and unwrapped SDK services.
The SDK runtime exposes these sources explicitly; neither read adapter submits
orders. Explicit and runtime valuation share their calculation rates below.

### Explicit RUB Valuation Pass

`sync_accounts(dst, valuation=...)` supplies the same `Valuation` and immutable
`FxSnapshot` to all ACCOUNT/INDEX leaves, including nested COMPOSITE children,
and to the destination adapter. The destination must return that exact snapshot.
The pass clears cached security books/currency identities and rejects stale FX
before account reads. The legacy call without a market remains compatible.
Explicit valuation without FX execution still rejects foreign trading before
effects. Runner obtains optional `ValuationSources` from the actual SDK strategy
adapter and constructs `ExecutionMarket`; neutral adapters without that capability
retain their original interface and RUB path. Runtime discovers currencies during
read-only loading, fixing each midpoint once before the financial tree is built.
Debug constructs the same native funding plan without submission.

`TInvestSecurityData` reads matching metadata and depth-one security books,
checks positive two-sided levels and timezone-aware market time up to 120 seconds
old or 100 milliseconds ahead, both inclusive, and returns native `SecurityQuote`
units. Security and direct FX books are
independent reads. Share/ETF midpoints are money per piece; bond clean percent
quotes require matching positive native nominal and explicit native ACI, added
once. Missing units or mismatched currencies fail without guessed conversion.
Neither GetOrderPrice, operation history nor an order-price parameter is used.

On a security book rejection, a bounded warning supplements the original
ValueError: expected/received UID, market timestamp, observation time in UTC,
age in seconds and the shared 120-second limit. Separate categories identify
UID mismatch, missing/invalid/naive timestamps, future data and stale data;
several applicable categories may coexist. `market_time_future` means an offset
greater than the shared 100-millisecond allowance. Raw timestamps and negative
ages remain unchanged, including tolerated skew accompanying another rejection.
The allowance applies only to market quotes, not receipts, source data or orders.
Invalid timestamps are never
replaced with observation time. Missing metadata or an invalid API permission
marker also logs only selected identifiers/category/currency/flag. Missing,
invalid and unloaded fields have distinct markers, without inferred RUB/False.
No raw response, instrument name, transport exception text or diagnostic RPC
is added. Successful fresh books emit no snapshots. Sandbox framework context
adds the current stage, scenario and phase without changing evidence schemas.

Book rejection facts also retain ticker/class/currency from the already loaded
instrument, or an explicit missing label. Observational monotonic and UTC clocks
record request start, response and check, request duration and response-to-check
delay. Receipt age uses observational UTC; check age retains the original
validation clock. Neither clock replaces market time or changes the 120s guard.
E2E aggregates named metadata/catalog/book operations and separates RPC attempts
from quota waits; nested stage totals are inclusive. Successful payloads are not
streamed and no extra lookups, retries or clock calls for validation are added.

The FX catalog boundary supplements the same ValueError with a safe note and
one field/constraint event. Exact internal FxPair guard messages identify their
fields; no stack locals, SDK repr or arbitrary exception text is inspected.
Raw nominal currency/units/nano, source scalar types and normalized Decimal
remain visible, including when an unrelated pair fails before the CNY UID filter.
Selected setup instruments and the explicitly requested pair expose their loaded
lot/permissions/nominal/increment metadata without enumerating successful catalogs.
The E2E fixture forwards only these explicit diagnostic records to stdout and
existing scenario-dump events, including preflight failures before accounts/configs.
Strict attempt reports, collection/SHA, cleanup and financial checks are unchanged.

The strategy decorator and destination adapter preserve broker
`current_price/currency` alongside `native_price/settlement_currency` and
`rub_price`. Shared UID observations use one native book per pass. Capital,
profiles and targets consume RUB `mark`; existing RUB per-position nano rounding
is preserved. Currency positions carry `native_cash`: metadata supplies actual
ISO identity, portfolio quantity supplies native units. For 100 CNY reported
as 1000 RUB but valued at midpoint 12, capital is 1200 RUB exactly once.
GetPositions money is a native availability bound, never another capital term.
Unknown identity/quantity or missing/stale FX cannot use broker RUB price as a
substitute. Cash is excluded from target securities. The valuation layer does not
grant money ownership; native funding and reserve rules are described below.

INDEX stores historical reference units separately in `IndexReferenceUnits`.
The optional root INDEX `preferred_currency` defaults to RUB and is fixed in
`IndexConfig` during preparation. It reaches the pure selector over full SDK
metadata: among API-available instruments matching the exact ticker and accepted
type, prefer those with matching actual currency, ignoring case. Select the first
instrument within that group in original SDK order; if none match, select the
first of all API-available candidates in their original order. Several candidates
in the selected group emit one safe warning with the chosen UID/board and at
most twenty ordered UID/board pairs plus an omitted count. No names, SDK reprs
or malformed identifiers enter that warning. Missing or malformed live
currency fails without an inferred RUB; actual UID/currency metadata is retained.
The selector reuses loaded metadata and adds no RPCs. Currency sandbox scenarios
explicitly configure CNY and use the same preference in their setup helper.
The acceptance fixture additionally requires uniqueness after preference;
its preflight assertions remain independent of runtime first-with-warning choice.
This preference is independent of historical reference units below.
Missing JSON currencies mean RUB even for a security trading in CNY. An explicit
foreign reference price or capitalization currency requires a positive historical
`reference_fx_rate` in capitalization units per price-currency unit; equal
currencies require 1. Only RUB/RUB can omit that rate. Current RUB capitalizations
divide by reference price times historical rate, retaining the original RUB
Decimal operation order and avoiding duplicate free-float/weight coefficients.
Bundled JSON adds explicit RUB units without numerical changes.

### Pure Native Funding Plan

`build_order_plan(..., funding=FundingInputs(...))` integrates a pure funding
calculation into `OrderPlan.funding`. It requires the explicit shared FX snapshot
and native security midpoints; a broker RUB mark cannot supply a missing native
price. Readiness, targets, physical lots, aggregate UID underweight, fixed owners
and leaf security spending ceilings are preserved. The existing absolute monetary
priority queue supplies both RUB and foreign-currency demands. Current zero
GetMaxLots caused by missing currency retains a conditional demand; it supplies
no invented future cap. Only refreshed broker facts may authorize it after FX.
The legacy `OrderPlan.buys` remains available; SDK runtime consumes
`OrderPlan.funding` through the separate executor below.

`CashPools` stores immutable native balances by owner/path and protected funds.
RUB security spending ceilings exclude reserve once; actual/own money bounds
never subtract floor again, including after a commission. The full leaf money
allowance includes its reserve, while internal COMPOSITE unallocated money stays
protected. Matching existing currency is assigned before another currency takes
its allowance. Exact proceeds follow the same `sale_paths` helper as RUB runtime;
unknown scoped proceeds remain deferred (#9), and fresh global money never
attributes them. No persistent ledger or receipt cash fabrication is introduced.

Reserve shares use each leaf's target RUB values by settlement currency;
conversion of an allowance rounds down at Decimal precision28 so recurring
division cannot increase the reserve. Half of every foreign share stays in RUB for the FX fee;
the other half is held in the security's settlement currency. The total reserve
does not grow, and RUB-security shares are unchanged. The foreign share's RUB
half limits FX principal, while the same money remains available for commission.
Its retention follows the owner's existing RUB proportion; existing foreign cash
is not sold merely to relocate reserve. The RUB-security reserve is not deducted
again from FX funding. Only a principal/fee funding shortage
initiates forward FX. Its amount includes the existing share once per owner and
currency, subtracts permitted native money first, then rounds to FX lots. Enough
money for principal plus commission needs no reserve restock. Ask finances BUY;
bid net of estimated fees finances SELL. Combined lots retain exact contributions,
estimated debits/credits, fee estimates, dependent UIDs and shared owner
`debit_limits`; these ceilings are not additive budgets for successive exchanges.
Projected money is labelled separately from observed cash and exact receipts.

Reverse FX requires an actual RUB security principal deficit. Known native
security needs are retained before selling surplus currency; reserve shortages
and fees alone do not justify a reverse exchange. Reverse RUB credits and lot
surplus cannot fund another foreign exchange in the same pass. Fees debit the
original owner; security midpoint already includes ACI once. Runtime readiness,
preflight and fresh GetMaxLots use the executor below, without operation-history/
GetOrderPrice dependencies or live acceptance claims.

### Confirmed FX Runtime

`ExecutionMarket` connects the pure plan, `FxData`, `SecurityData` and
`OrderExecutor`; it imports no SDK. `TInvestStrategyData.valuation_sources` is
an optional read-only capability initialized without RPCs. Actual Runner
CLI/cloud launches obtain readers from their selected client; neutral adapters
need not implement this SDK capability. Shared scalar SDK checks live in
`tinvest_validation.py`, avoiding a cycle through the strategy converter.

Before any order, validate the entire financial plan and union settlement
currencies of sales and conditional purchase demands across all leaves, plus
planned exchanges. This includes zero current native caps, existing native
balances and foreign-paper sales without FX. Closed required currency trading
defers the entire tree, including independent RUB orders. Settlement currency
controls the check, not bond nominal currency. Pure no-op reads valuation books
without currency status/caps. Execution refreshes books independently of frozen
targets and original owner spending ceilings; expired FX is reread and market
age is checked before effects.

FX uses the same BESTPRICE SDK boundary with one request ID and no price.
Its physical intent describes base-currency units, not an owned-security sale.
NEW/PARTIALLYFILL for either FX direction polls the broker ID within30 seconds
at two-second intervals. Full matching FILL is mandatory; malformed identity,
rejection, timeout and transport uncertainty never permit a dependent order or
automatic repeat/cancel/rollback.

`ExecutionReceipt.amounts` optionally retains native total and executed commission
from PostOrder/OrderState separately from identity equality and exact `cash`.
Complete matching same-currency fields supply only the conservative V5 bound.
Forward FX debits the upper RUB bound and credits confirmed base lots; reverse
FX debits base lots and credits the lower RUB bound. Owner contributions and
debit limits constrain both. Fresh available money, readiness, matching physical
holdings and new own GetMaxLots must also permit each subsequent buy. Incomplete
monetary facts still cause a fresh account/cap read after FX, then defer dependencies
without fabricating exact `cash` or attributing a balance delta.

After each buy, conservative owner debits include the observed fee bound;
remaining security ceilings debit the total conservatively without adding ACI.
Floor is never subtracted from actual/own money. Further buys wait for fresh
readiness/positions and reread caps. Bought UIDs and FX routes are not resubmitted
in this pass. Reverse RUB proceeds cannot finance another foreign route, and
currency remainders are never automatically exchanged back. All security-sale
receipts participate together in shared/scoped funding; a late common seller
cannot unlock earlier unknown isolated proceeds (#9). Price/cap changes can
leave confirmed FX as the final partial result.

INFO diagnostics list loading, portfolio/currency/security/exchange blockers,
active order IDs/UIDs/statuses/lot counts, expected versus actual quantities,
submission and verified FILL results, refresh stages and pass result counts.
Only explicitly selected loaded fields are logged; raw responses and credentials
are never dumped. No extra API reads are added solely for logging.
Polling windows do not interrupt in-flight RPCs or the established quota retry
loop; the cloud invocation timeout remains the overall deadline.

## Transport

Runner (both modes) and the explicit calibration script install the same
`grpc.UnaryUnaryClientInterceptor` through the actual SDK
`Client(interceptors=[...])` channel hook before the first RPC. The default
unary timeout is 10 seconds; a smaller existing timeout is preserved.
Streams are not intercepted. The SDK-only call_api wrapper retries unary
RESOURCE_EXHAUSTED errors every 10 seconds without an attempt cap, until
success or a different error. All other errors retain the fail-stop behavior.
PostOrder reuses the same client request ID in every quota retry; the ID factory
runs once outside the retry loop. Deadlines and other errors do not resubmit an
order, and no cross-process exactly-once guarantee is claimed. Neither
request arguments nor tokens are logged. The platform invocation timeout
can still end a retrying cloud call.
RequestError/DataAccessError become ExecutionDataError with their cause.
ValueError and programming errors are not masked. No trading method is
exposed by ExecutionData.

StrategyData.begin_snapshot clears UID/catalog/search caches before each pass.
Only successfully converted nonempty FindInstrument results are reused by exact
query within that snapshot. Each caller receives its own ordered list of frozen
short DTOs, including duplicates; catalog type hints are reapplied on hits.
Empty responses and failed conversions/requests remain uncached. Search reuse
does not supply full metadata, currency, lot size or trading permissions.
Full Shares/Etfs catalogs with INSTRUMENT_STATUS_ALL serve searched candidates;
GetInstrumentBy remains the fallback for UIDs absent from a catalog. The
full catalog API flag, not FindInstrument's short flag, remains authoritative.
Current trading status, own caps, money and prices are not cached.
