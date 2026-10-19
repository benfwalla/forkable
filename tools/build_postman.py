"""Generate postman/forkable.postman_collection.json from the queries the client uses.

    uv run python tools/build_postman.py

Re-run after changing forkable/queries.py so the collection stays in sync. The output holds
no credentials: paste your session cookie into the collection's `session` variable in Postman.
"""

from __future__ import annotations

import json
import re
import sys
import textwrap
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forkable import queries as Q  # noqa: E402
from forkable.client import SESSION_COOKIE, USER_AGENT  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "postman" / "forkable.postman_collection.json"

VARIABLES = {
    "baseUrl": ("https://forkable.com", "No trailing slash."),
    "session": ("", f"Your {SESSION_COOKIE} cookie value. See the collection description for how to get it."),
    "csrfToken": ("", "Filled automatically before mutations."),
    "userId": ("", "Set by Account / me."),
    "from": ("2026-10-19", "First day for myDeliveries (YYYY-MM-DD)."),
    "to": ("2026-10-23", "Last day for myDeliveries."),
    "date": ("", "Delivery date. Set by My deliveries."),
    "clubId": ("", "Location id. Set by My deliveries."),
    "deliveryId": ("", "Set by My deliveries (your meal's delivery, else the first one)."),
    "deliveryIds": ("[]", "JSON list of every delivery on `date` (for replaceAllPieces). Set by My deliveries."),
    "targetDeliveryId": ("", "Delivery (location) to move your meal to with replaceAllPieces."),
    "menuIds": ("[]", "JSON list of the delivery's menus. Set by My deliveries."),
    "venueIds": ("[]", "JSON list of the menus' venues. Set by Menus."),
    "menuId": ("", "Set by Menus (first menu) or My deliveries (your meal's menu)."),
    "itemId": ("", "Set by Menus (first item) or My deliveries (your meal)."),
    "orderId": ("", "Order holding your meal. Set by My deliveries."),
    "pieceId": ("", "Your meal (piece). Set by My deliveries."),
    "selections": ("{}", 'JSON {"<modifierId>": [<optionId>, ...]}. Use [-1] for optional modifiers left empty.'),
    "instructions": ("", "Special instructions (the note), e.g. No onions, please."),
    "wday": ("1", "Weekday for the default location: 0=Sun, 1=Mon ... 6=Sat."),
}

# Mutations need an x-csrf-token tied to the session: fetch one before each request that sends it.
PRE_REQUEST = r"""
if (pm.request.headers.has('x-csrf-token')) {
  pm.sendRequest({
    url: pm.variables.get('baseUrl') + '/api/v2/csrf_token',
    header: { Accept: 'application/json', Cookie: '_easyorder_session=' + pm.variables.get('session') },
  }, (err, res) => {
    if (err) return console.error('CSRF fetch failed', err);
    pm.collectionVariables.set('csrfToken', res.json().token);
  });
}
"""

TEST = r"""
pm.test('logged in', () => pm.expect(pm.response.code, 'session missing or expired: copy it again').to.not.equal(401));
pm.test('no GraphQL errors', () => {
  const body = pm.response.json();
  pm.expect(body.errors, JSON.stringify(body.errors)).to.be.undefined;
});
"""

CAPTURE_ME = """
const me = pm.response.json().data.me;
pm.collectionVariables.set('userId', me.id);
"""

CAPTURE_DELIVERIES = """
const data = pm.response.json().data;
if (data.me) pm.collectionVariables.set('userId', data.me.id);
const userId = String(pm.variables.get('userId'));
const deliveries = data.myDeliveries || [];
let pick = null, mine = null;
for (const d of deliveries) {
  for (const o of d.orders || []) {
    for (const p of o.pieces || []) {
      if (!mine && String(p.userId) === userId) { pick = d; mine = { order: o, piece: p }; }
    }
  }
}
pick = pick || deliveries[0];
if (pick) {
  const date = pick.forDeliveryAt.slice(0, 10);
  pm.collectionVariables.set('deliveryId', pick.id);
  pm.collectionVariables.set('date', date);
  pm.collectionVariables.set('clubId', pick.club.id);
  pm.collectionVariables.set('menuIds', JSON.stringify(pick.availableMenuIds));
  pm.collectionVariables.set('deliveryIds', JSON.stringify(
    deliveries.filter(d => d.forDeliveryAt.slice(0, 10) === date).map(d => Number(d.id))));
}
if (mine) {
  pm.collectionVariables.set('orderId', mine.order.id);
  pm.collectionVariables.set('pieceId', mine.piece.id);
  pm.collectionVariables.set('menuId', mine.piece.menuId);
  pm.collectionVariables.set('itemId', mine.piece.itemId);
  pm.collectionVariables.set('instructions', mine.piece.instructions || '');
}
"""

CAPTURE_MENUS = """
const menus = pm.response.json().data.menus || [];
pm.collectionVariables.set('venueIds', JSON.stringify([...new Set(menus.map(m => Number(m.venue.id)))]));
const first = menus.find(m => m.sections.some(s => s.items.length));
if (first) {
  pm.collectionVariables.set('menuId', first.id);
  pm.collectionVariables.set('itemId', first.sections.find(s => s.items.length).items[0].id);
}
"""


def _doc(text: str) -> str:
    """Tidy a query string from queries.py for display."""
    lines = [line.rstrip() for line in textwrap.dedent(text).strip().splitlines()]
    return "\n".join(line for line in lines if line.strip())


def _mutation(name: str, selection: str) -> str:
    input_type = name[0].upper() + name[1:] + "Input"
    return _doc(f"mutation ($input: {input_type}!) {{\n  {name}(input: $input) {{ {selection} }}\n}}")


def _script(kind: str, code: str) -> dict:
    return {"listen": kind, "script": {"type": "text/javascript", "exec": code.strip().splitlines()}}


def _headers(csrf: bool) -> list:
    h = [
        {"key": "Content-Type", "value": "application/json"},
        {"key": "Accept", "value": "application/json"},
        {"key": "Origin", "value": "{{baseUrl}}"},
        {"key": "Referer", "value": "{{baseUrl}}/mc/"},
        {"key": "forkable-referrer", "value": "mc"},
        {"key": "User-Agent", "value": USER_AGENT},
        {"key": "Cookie", "value": f"{SESSION_COOKIE}={{{{session}}}}"},
    ]
    if csrf:
        h.append({"key": "x-csrf-token", "value": "{{csrfToken}}",
                  "description": "Filled by the collection's pre-request script."})
    return h


def raw(var: str) -> str:
    """A {{var}} placeholder that is inserted unquoted (numbers, booleans, JSON lists/objects)."""
    return f"__RAW__{var}__"


def _variables(values: dict) -> str:
    return re.sub(r'"__RAW__(\w+)__"', r"{{\1}}", json.dumps(values, indent=2))


def gql(name: str, query: str, variables: Optional[dict] = None, description: str = "", csrf: bool = False,
        capture: str = "") -> dict:
    path = ["api", "v2", "graphql"]
    item = {
        "name": name,
        "request": {
            "method": "POST",
            "header": _headers(csrf),
            "url": {"raw": "{{baseUrl}}/" + "/".join(path), "host": ["{{baseUrl}}"], "path": path},
            "body": {"mode": "graphql", "graphql": {"query": query, "variables": _variables(variables or {})}},
            "description": textwrap.dedent(description).strip(),
        },
    }
    if capture:
        item["event"] = [_script("test", capture)]
    return item


def mutation(name: str, title: str, inp: dict, selection: str = Q.PIECE_MUTATION_SELECTION,
             description: str = "") -> dict:
    return gql(title, _mutation(name, selection), {"input": inp}, description, csrf=True)


def folder(name: str, description: str, *items: dict) -> dict:
    return {"name": name, "description": description, "item": list(items)}


PIECE_INPUT = {
    "menuId": raw("menuId"),
    "itemId": raw("itemId"),
    "selectionsHash": raw("selections"),
    "instructions": "{{instructions}}",
    "fromTopRated": False,
    "topRatedType": None,
}

def build() -> dict:
    items = [
        folder(
            "Account", "Who you are, your locations (clubs) and default location per weekday.",
            gql("Me", Q.ME.strip(), capture=CAPTURE_ME,
                description="""
                `mealClubs` are your locations; `preferredLocations` is the default per weekday
                (wday: 0=Sun). `mealClubAutoOrder` false means suggested meals need confirming."""),
        ),
        folder(
            "Deliveries", "Your week: one delivery per location, date and service window, holding your meals.",
            gql("My deliveries", _doc(Q.MY_DELIVERIES_AND_ME), {"from": "{{from}}", "to": "{{to}}"},
                capture=CAPTURE_DELIVERIES,
                description="""
                Deliveries between `from` and `to`. Your meals are the `orders[].pieces[]` whose
                userId is yours. `forDeliveryAt` is UTC. `userConfirmed` is the Order / Don't order
                toggle. Sets deliveryId, date, clubId, menuIds, orderId, pieceId, menuId and itemId."""),
        ),
        folder(
            "Menus", "Everything you can order for a delivery, with customization options and sold-out data.",
            gql("Menus", _doc(f"""
                query ($ids: [Int!]!, $clubId: Int!) {{
                  menus(ids: $ids, clubId: $clubId) {{ {Q.MENU_FIELDS} }}
                }}"""), {"ids": raw("menuIds"), "clubId": raw("clubId")}, capture=CAPTURE_MENUS,
                description="""
                Menus, sections, items, and each item's modifiers (min/max/required) and options.
                Build `selections` as {"<modifierId>": [<optionId>]}. An item is unavailable when
                the delivery's order lists it `active: false`. Sets venueIds, menuId and itemId."""),
            gql("Venue usage (sold out)", _doc("""
                query ($venueIds: [Int!]!, $date: Date!) {
                  venueUsage(ids: $venueIds, from: $date, to: $date)
                }"""), {"venueIds": raw("venueIds"), "date": "{{date}}"},
                description="""
                JSON {venueId: {date: {am, pm}}} of meals already ordered. A menu is sold out when
                usage >= venue.capacity for the delivery's shift (pm if `afternoon`), or when the
                delivery's order has isOverVenueCapacity."""),
            gql("Menus + usage (as the client does)", _doc(Q.MENUS_WITH_USAGE),
                {"ids": raw("menuIds"), "clubId": raw("clubId"), "venueIds": raw("venueIds"), "date": "{{date}}"},
                description="Both of the above in one round trip."),
            gql("Meal restrictions", _doc(Q.MEAL_RESTRICTIONS),
                {"userId": raw("userId"), "menuId": raw("menuId"), "itemId": raw("itemId"),
                 "customization": raw("selections")},
                description="Conflicts between an item (with selections) and your dietary restrictions."),
        ),
        folder(
            "Ordering", "Changes your real order. Each mutation returns errors/errorDetails plus the "
            "updated delivery. Known error codes: venue_capacity_overage, exceeded_allowance, "
            "below_event_order_minimum_cents.",
            mutation("addPiece", "Add meal (addPiece)", {
                "deliveryId": raw("deliveryId"), "userId": raw("userId"), **PIECE_INPUT, "myMeals": True,
            }, description="Adds a new meal to a delivery (use when you have none that day, or for an "
                           "extra meal). Auto-confirms."),
            mutation("replacePiece", "Replace meal (replacePiece)", {
                "deliveryId": raw("deliveryId"), "oldPieceId": "{{pieceId}}", **PIECE_INPUT, "myMeals": True,
            }, description="Swaps your meal `pieceId` for another item or customization, same location."),
            mutation("replaceAllPieces", "Move meal to another location (replaceAllPieces)", {
                "deliveryIds": raw("deliveryIds"),
                "newPiece": {"deliveryId": raw("targetDeliveryId"), **PIECE_INPUT},
                "myMeals": True,
            }, f"errors deliveries {{ {Q.DELIVERY_FIELDS} }}",
                description="""
                The web app's "Replace meal above" after changing location: removes your meals from
                every delivery that day (`deliveryIds`) and adds the new one to `targetDeliveryId`."""),
            mutation("removePiece", "Remove meal (removePiece)",
                     {"orderId": "{{orderId}}", "pieceId": "{{pieceId}}", "myMeals": True}),
            mutation("confirmDelivery", "Order (confirmDelivery true)",
                     {"deliveryId": raw("deliveryId"), "confirm": True, "changeFrom": "dashboard"},
                     description="The week view's Order toggle."),
            mutation("confirmDelivery", "Don't order (confirmDelivery false)",
                     {"deliveryId": raw("deliveryId"), "confirm": False, "changeFrom": "dashboard"},
                     description="Don't order: keeps the meal selected, but you won't get it."),
            mutation("updatePreferredLocationDay", "Set default location for a weekday",
                     {"wday": raw("wday"), "clubIds": [raw("clubId")]}, "errors",
                     description="Recurring default location for `wday` (0=Sun), not a one-off change."),
        ),
    ]
    return {
        "info": {
            "name": "Forkable (unofficial)",
            "description": textwrap.dedent("""
                Unofficial, reverse-engineered Forkable API (GraphQL, introspection disabled).
                Generated by tools/build_postman.py; see docs/SCHEMA.md.

                ## Setup: copy your session cookie

                1. Log in at https://forkable.com/mc/ in your browser (Okta/SSO is fine).
                2. Open DevTools (Cmd+Opt+I) > Application > Cookies > https://forkable.com
                   (Firefox: Storage tab; Safari: enable Settings > Advanced > "Show features for web
                   developers", then Develop > Show Web Inspector > Storage).
                3. Copy the Value of `_easyorder_session`.
                4. Paste it into this collection's Variables > `session` > Current value, and save.

                Using the `forkable` CLI? After `forkable login` the cookie is in
                ~/.config/forkable/session.json.

                The session lasts about 6 weeks, and logging out of Forkable in the browser ends it. A 401
                means it's time to copy it again.

                ## Then

                Run Deliveries > My deliveries, then Menus > Menus. They fill in the ids (deliveryId,
                menuIds, menuId, itemId, pieceId, ...) the other requests use. To order something else, set
                menuId / itemId / selections by hand. Requests in Ordering change your real order.
            """).strip(),
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "event": [_script("prerequest", PRE_REQUEST), _script("test", TEST)],
        "variable": [{"key": k, "value": v, "type": "string", "description": d} for k, (v, d) in VARIABLES.items()],
        "item": items,
    }


def main() -> None:
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=2) + "\n")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
