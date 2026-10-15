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
  native trading currency. The RUB-budget adapter rejects any non-RUB valuation
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
contracts for the multicurrency implementation. They are not wired into the
working planner or executor. Existing RUB execution, reserve subtraction and
SDK `ExecutionReceipt.cash={}` remain unchanged at this preparatory stage.
Pair discovery, timestamp freshness, reserve allocation, FX submission and
dependent purchase orchestration belong to later implementation steps.

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
scope, lot or budget permission. Later integration must distribute reserve
only within leaves by target RUB values per settlement currency, exclude
COMPOSITE unallocated money, and subtract existing permitted currency before
rounding the necessary FX deficit. Reverse FX remains limited to financing
RUB security purchases, without foreign-currency bridges.

Offline tests in `test/test_fx_broker_contract.py` exercise these contracts
and the real SDK order boundary with autospecced unwrapped services. Existing
broker receipt and execution tests retain isolated/common-pool attribution
and fresh readiness checks. No live, TOM-cycle or production acceptance is
claimed by these tests.

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

StrategyData.begin_snapshot clears UID/catalog caches before each pass.
Full Shares/Etfs catalogs with INSTRUMENT_STATUS_ALL serve searched candidates;
GetInstrumentBy remains the fallback for UIDs absent from a catalog. The
full catalog API flag, not FindInstrument's short flag, remains authoritative.
Current trading status, own caps, money and prices are not cached.
