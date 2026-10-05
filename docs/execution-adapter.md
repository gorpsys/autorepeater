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
movement and non-atomic reads remain accepted execution risks. Step 6 must
check FILL and both lot counts, then obtain fresh destination availability
and own caps before purchases. If a sale is not yet reflected in sufficient
fresh availability, the future executor must defer with INFO. It must not
estimate settled proceeds from `total_order_amount`, order price or a target
mark. The SDK order adapter therefore leaves neutral `ExecutionReceipt.cash`
empty. A receipt from an executor with documented net monetary facts may
provide those facts per currency: execution attributes them to the sale's
owners and permitted recipients, capped by fresh global availability and own
limits. Unknown sale money cannot fund isolated pools; those purchases defer
with INFO. Fresh global money may fund a common pool only when every seller
can fund every recipient. It is never assigned to an isolated seller from
the account cash delta. This receipt field is used by the neutral funding
protocol and is not an SDK proceeds estimate.

## Transport

Runner (both modes) and the explicit calibration script install the same
`grpc.UnaryUnaryClientInterceptor` through the actual SDK
`Client(interceptors=[...])` channel hook before the first RPC. The default
unary timeout is 10 seconds; a smaller existing timeout is preserved.
Streams are not intercepted, and no operation is automatically retried.
RequestError/DataAccessError become ExecutionDataError with their cause.
ValueError and programming errors are not masked. No trading method is
exposed by ExecutionData. The existing engine remains active until step 7.
