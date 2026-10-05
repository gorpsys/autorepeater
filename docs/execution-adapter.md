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
PostOrder generates its order_id once, outside the retry loop. Neither
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
