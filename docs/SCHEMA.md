# Forkable API notes (reverse engineered)

Endpoint: `POST https://forkable.com/api/v2/graphql` (graphql-ruby, Rails backend)
Public endpoint (no auth): `POST https://forkable.com/api/v2/public/graphql` (`diets`, `ingredients`, `token`, `requestOneTimeCode`)

**Introspection is disabled** (`__schema` is rejected). Everything here comes from captured
traffic, the web app's JS bundles (`/mc/js/*.js`), and validation-error probing
(`tools/probe_schema.py`). graphql-ruby validates before executing, so invalid documents are safe probes.

## Auth

| What | Detail |
|---|---|
| Session | Cookie `_easyorder_session` (Rails, HttpOnly, Secure). Expiry ~6 weeks, rolling; re-issued on responses. |
| CSRF | `GET /api/v2/csrf_token` → `{"token": "..."}`, sent as `x-csrf-token`. Queries work without it; the app always sends it. |
| Other headers | `forkable-referrer: mc`, `origin`/`referer` = forkable.com. Not obviously required. |
| Login | Mutation `createSession(input: {email, password, mfaCode?, recoveryCode?, skipMfa?, token?, nonce?})` → `errorAttributes errorDetails user {...}`. Sets the cookie. |
| Passwordless | `requestOneTimeCode(input: {email})` on the **public** endpoint emails a 6-digit code; then `createSession(input: {email, otcCode})` on the main endpoint. Fetch `/api/v2/csrf_token` first: it issues an anonymous session cookie that the login upgrades. |
| Login methods | Public `loginMethods(email) { availableMethods }` (`sso`, `password`) and `identities(email) { integration { type provider loginUrl allowSsoPasswordLogin } }`. For SSO accounts, `requestOneTimeCode` returns no errors but **no email is sent**. The code-login UI exists in the bundle but wasn't shown to an SSO account (as of 2026-10). |
| SSO | SAML via the IdP `loginUrl` (RelayState `redirect_to` = `/mc/`). It ends with the cookie set on forkable.com, so it can't be done headlessly. Okta FastPass also refuses to run behind a MITM proxy. |
| Expiry | An invalid or expired session gets **HTTP 401** `{"httpErrorCode":401}` from `/api/v2/graphql`. `csrf_token` still succeeds. |
| AWS cookies | `AWSALBTG*` are load-balancer stickiness cookies, not needed. |

## Domain model

- **Club** (`mealClub`) = a delivery location. A user belongs to several (`me.mealClubs`).
- **Preferred location**: `me.preferredLocations [{wday, clubId}]`, Ruby wday (0=Sun..6=Sat). Changed by
  `updatePreferredLocationDay(input: {wday, clubIds})`. This is the *recurring weekly default*, not a one-off.
- **Delivery** = a club plus a date plus a service window (lunch/dinner).
  `forDeliveryAt` is UTC; convert it with the club's `market.timezone` to get the local date. `myDeliveries(from: Date!, to: Date)` returns one per club per day, even when you have no meal.
  `availableMenuIds`, `isReadOnly`, `pastLateOrderDeadline`, `afternoon` (am/pm shift).
- **Order** = one restaurant inside a delivery. `orders[].menu.sections[].items[].active` = item availability.
- **Piece** = one person's meal inside an order. Mine = `pieces[].userId == me.id`.
  `selections` = `{modifierId: [optionId]}`, `attributes` = human-readable `[{label, value}]`.
- **Menu**: `menus(ids: [Int!]!, clubId: Int!)` → sections → items → `modifiers {id name min max required hidden options {id name price}}`.

## Sold out

Same rule as the web app's `soldOutMenuIds` getter:

```
venueUsage(ids: [venueId], from: Date!, to: Date!)  # JSON scalar: {venueId: {"YYYY-MM-DD": {am: N, pm: N}}}
sold_out = order.isOverVenueCapacity || (venue.capacity && venue.capacity <= usage[date][am|pm])
```

Item level: `!section.active || !item.active` in the delivery's `orders[].menu` snapshot.

## Meal mutations

All return `errors errorDetails warningDetails userReceipt {...} delivery {...}`. Check `errors` (a list of strings).
`errorDetails.base[].error` codes seen in the JS: `venue_capacity_overage`, `exceeded_allowance`, `below_event_order_minimum_cents`.

| Mutation | Input (required in bold) | Use |
|---|---|---|
| `addPiece` | **deliveryId, itemId, menuId, userId**, instructions, selectionsHash, fromTopRated, topRatedType, myMeals, replacedPieceId, confirm | New meal (no meal that day / extra meal). Auto-confirms. *Captured.* |
| `replacePiece` | **deliveryId, itemId, menuId, oldPieceId**, instructions, selectionsHash, fromTopRated, topRatedType, myMeals | Swap an existing meal (also used for changing options on the same item) |
| `removePiece` | **orderId, pieceId**, myMeals | Remove a meal. *Captured.* |
| `replaceAllPieces` | deliveryIds, newPiece {deliveryId, itemId, menuId, instructions, selectionsHash, fromTopRated, topRatedType}, myMeals | **Switch location for a day** ("Replace meal above"): deliveryIds = every location's delivery that day, newPiece.deliveryId = the chosen one. Returns `deliveries {...}`. *Captured.* The web app also uses it for "same meal on several days". |
| `addGuestPiece` / `replaceGuestPiece` | like add/replace | Guest meals |
| `confirmDelivery` | **deliveryId, confirm (Boolean), changeFrom** (`"dashboard"`) | Week-view **Order / Don't order** toggle. State = `delivery.userConfirmed` (mirrored in `piece.isConfirmed`, `piece.state` "Confirmed"/"Not Confirmed"). *Captured.* |
| `rateMeal` | ? | Ratings |
| `updatePreferredLocationDay` | **wday, clubIds** | Recurring default location |

**Notes** ("special instructions") are the free-form `instructions` field on add/replace. There's no default; "E.g., Please no onions" is only placeholder text. Editing a note on an existing meal = `replacePiece` with the same item and selections.

**Order / Don't order**: a selected meal is delivered only while confirmed. `addPiece` confirms automatically; `confirmDelivery(confirm: false)` keeps the selection but you won't get it.

`selectionsHash`: the web app sends every modifier, with `[-1]` for optional modifiers left empty.
Pre-check: `mealRestrictions(userId, menuId, itemId, customization: Json)` → `{conflicts}`.

## Other queries seen

`me { mealClubAutoOrder mealClubs { market { timezone } } }`: when auto order is off, Forkable's meal-generation suggestions (`mealGenerationScores`, `fromTopRated`/`topRatedType: "meal_generation"`) arrive as unconfirmed pieces.

`me`, `myNotifications`, `myInProgressDeliveryIds`, `mealClubsAs(roles:[...])`,
`mealGenerationScores(deliveryId, userId, menuIds)` (the "top rated / for you" scores),
`menus(...) { sections { items { userRating(userId) userRatingDetails(userId) } } }`, `datesWithMeals`,
`searchMenuItems` (returns `ClassicMenuItemConnection`), `myReceipts`, `lateOrdersRemaining`, `oooSuggestions`.

## All mutation names in the web bundle

addMemberReportedIssue, addMemberToClubs, addOooRange, addPiece, addGuestPiece, addTopRatedMealsImpression,
cancelChangeRequest, confirmDelivery, createSession, createGuestLinkRequest, joinClubs, rateMeal, rateService,
removeOooRange, removePauseRange, removePiece, removePieces, replaceAllPieces, replacePiece, requestOneTimeCode,
updateOooRange, updatePreferredLocationDay, updateSelfUser, updateUser, … (plus admin/menu-editing mutations).
