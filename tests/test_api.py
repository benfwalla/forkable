"""Offline tests with a fake GraphQL transport. Run: python3 -m unittest discover tests"""

import copy
import datetime as dt
import unittest

from forkable.api import Forkable
from forkable.client import ForkableError

ME = {
    "id": 1, "firstName": "Test", "lastName": "User", "fullName": "Test User", "email": "t@example.com",
    "mealClubAutoOrder": False,
    "mealClubs": [
        {"id": 10, "name": "HQ", "market": {"timezone": "America/New_York"}},
        {"id": 20, "name": "Annex", "market": {"timezone": "America/New_York"}},
        {"id": 30, "name": "West", "market": {"timezone": "America/Los_Angeles"}},
    ],
    "preferredLocations": [{"wday": w, "clubId": 10} for w in range(1, 6)],
}

MODIFIERS = [
    {"id": 114, "name": "Choose Bread", "optionSetId": 1, "min": 1, "max": 1, "free": False, "required": True,
     "hidden": False, "options": [{"id": 89, "name": "Plain Bagel\n", "price": None, "ingredientTags": []},
                                  {"id": 93, "name": "Multigrain Bagel", "price": None, "ingredientTags": []},
                                  {"id": 90, "name": "Plain Bagel (Gluten Free)", "price": 2.0, "ingredientTags": []}]},
    {"id": 179, "name": "Add Side", "optionSetId": 2, "min": 0, "max": 2, "free": False, "required": False,
     "hidden": False, "options": [{"id": 176, "name": "Chips", "price": 2.0, "ingredientTags": []},
                                  {"id": 177, "name": "Cookie", "price": 3.0, "ingredientTags": []}]},
]


def menu(mid, venue_id, capacity=100):
    return {"id": mid, "name": f"Menu {mid}", "displayName": f"Menu {mid}", "tags": [], "isKosher": False,
            "ratingCount": 0, "disableSpecialInstructions": False,
            "venue": {"id": venue_id, "name": f"V{venue_id}", "displayName": f"V{venue_id}", "capacity": capacity},
            "sections": [{"id": 1, "name": "Mains", "description": None, "items": [
                {"id": 162, "menuId": mid, "name": "Sandwich", "description": "", "price": 11.5,
                 "ingredientTags": [], "imageUrl": None, "averageRating": 4.0, "dietLevel": 4,
                 "modifierIds": [114, 179], "modifiers": MODIFIERS},
                {"id": 31, "menuId": mid, "name": "Salad", "description": "", "price": 9.0,
                 "ingredientTags": [], "imageUrl": None, "averageRating": None, "dietLevel": 4,
                 "modifierIds": [], "modifiers": []}]}]}


def order(oid, mid, venue_id, pieces=(), item_active=True):
    return {"id": oid, "state": "initial", "pastLateOrderDeadline": False, "isOverVenueCapacity": False,
            "replacementCutoffTs": None,
            "menu": {"id": mid, "name": f"Menu {mid}",
                     "sections": [{"id": 1, "active": True, "items": [{"id": 162, "active": item_active},
                                                                       {"id": 31, "active": True}]}]},
            "venue": {"id": venue_id, "name": f"V{venue_id}", "displayName": f"V{venue_id}", "capacity": 100,
                      "allowLateMeals": True, "allowLateRemovals": True},
            "pieces": list(pieces)}


def piece(pid, mid, item=31, user=1, confirmed=True, note=""):
    return {"id": pid, "date": "2026-10-22", "itemId": item, "menuId": mid, "name": "Salad", "userId": user,
            "userFullName": "x", "state": "Confirmed" if confirmed else "Not Confirmed", "isConfirmed": confirmed,
            "autoOrder": False, "instructions": note, "selections": {}, "price": 9.0, "description": "",
            "copayAmount": None, "isLateOrder": False, "attributes": [], "menuItem": {"id": item, "sectionId": 1}}


def delivery(did, club, at, orders, window="lunch", confirmed=True):
    return {"id": did, "state": "initial", "simpleState": None, "forDeliveryAt": at, "deliveryWindow": None,
            "isReadOnly": False, "userConfirmed": confirmed, "mealClubId": club, "isPreferred": club == 10,
            "afternoon": False, "availableMenuIds": [o["menu"]["id"] for o in orders],
            "pastLateOrderDeadline": False, "canRequestChanges": False, "allowanceType": None,
            "weeklyAllowance": None, "weeklyAllowanceAvailable": None,
            "serviceWindow": {"name": window, "baseTime": None}, "address": {"formatted": ""},
            "club": {"id": club, "name": {10: "HQ", 20: "Annex", 30: "West"}[club], "copay": 20,
                     "clubCoversAllFees": True},
            "orders": orders, "userReceipt": None}


class FakeGQL:
    def __init__(self, deliveries, usage=None):
        self.deliveries = deliveries
        self.usage = usage or {}
        self.mutations = []

    def execute(self, query, variables=None, public=False, csrf=False):
        if "myDeliveries" in query:
            return {"myDeliveries": copy.deepcopy(self.deliveries), "me": ME}
        if "menus(" in query:
            venues = {o["menu"]["id"]: o["venue"]["id"] for d in self.deliveries for o in d["orders"]}
            return {"menus": [menu(m, venues[m]) for m in variables["ids"]], "venueUsage": self.usage}
        if "mealRestrictions" in query:
            return {"mealRestrictions": {"conflicts": None}}
        if "me {" in query or "me{" in query:
            return {"me": ME}
        raise AssertionError(query)

    def mutate(self, name, input, selection="errors", public=False):
        self.mutations.append((name, input))
        return {"errors": None}


def day_fixture(my_piece_club=10, note=""):
    mine = lambda club: [piece("aaaa1111", 100 + club, note=note)] if club == my_piece_club else []  # noqa: E731
    return [
        delivery(1, 10, "2026-10-22T16:00:00.000Z", [order(11, 110, 1, mine(10)), order(12, 111, 2)]),
        delivery(2, 20, "2026-10-22T16:00:00.000Z", [order(21, 120, 3, mine(20))]),
    ]


class SelectionsTest(unittest.TestCase):
    def setUp(self):
        self.fk = Forkable(FakeGQL(day_fixture()))
        d = self.fk.day("2026-10-22").delivery_for()
        self.item = self.fk.menu(d, 110).item(162)

    def test_by_name_and_optional_defaults(self):
        self.assertEqual(self.fk.build_selections(self.item, {"bread": "multigrain"}), {"114": [93], "179": [-1]})

    def test_by_id_and_multi(self):
        sel = self.fk.build_selections(self.item, {114: 89, "side": ["chips", "cookie"]})
        self.assertEqual(sel, {"114": [89], "179": [176, 177]})

    def test_exact_name_beats_substring(self):
        self.assertEqual(self.fk.build_selections(self.item, {"bread": "plain bagel"})["114"], [89])

    def test_required_missing(self):
        with self.assertRaisesRegex(ForkableError, "required"):
            self.fk.build_selections(self.item, {})

    def test_ambiguous(self):
        with self.assertRaisesRegex(ForkableError, "ambiguous"):
            self.fk.build_selections(self.item, {"bread": "bagel"})

    def test_unknown_modifier(self):
        with self.assertRaisesRegex(ForkableError, "Unknown modifier"):
            self.fk.build_selections(self.item, {"bread": 93, "sauce": "x"})


class DaysTest(unittest.TestCase):
    def test_week_and_selection(self):
        fk = Forkable(FakeGQL(day_fixture(note="No onions")))
        week = fk.week("2026-10-20")
        self.assertEqual([d.date.isoformat() for d in week][0], "2026-10-19")
        thu = week[3]
        self.assertEqual(len(thu.my_selections), 1)
        self.assertEqual(thu.my_selections[0].instructions, "No onions")
        self.assertEqual(thu.preferred_club_id, 10)

    def test_local_date_uses_club_timezone(self):
        # 7pm Pacific dinner on the 22nd is 02:00Z on the 23rd.
        fk = Forkable(FakeGQL([delivery(9, 30, "2026-10-23T02:00:00.000Z", [order(91, 130, 9)], "dinner")]))
        self.assertEqual(fk.deliveries("2026-10-22")[0].date, dt.date(2026, 10, 22))

    def test_sold_out_by_usage(self):
        fk = Forkable(FakeGQL(day_fixture(), usage={"1": {"2026-10-22": {"am": 100, "pm": 0}}}))
        menus = {m.id: m for m in fk.menus(fk.day("2026-10-22").delivery_for(10))}
        self.assertTrue(menus[110].sold_out)
        self.assertFalse(menus[111].sold_out)
        self.assertFalse(menus[110].item(162).available)

    def test_inactive_item(self):
        dl = day_fixture()
        dl[0]["orders"][1] = order(12, 111, 2, item_active=False)
        fk = Forkable(FakeGQL(dl))
        m = fk.menu(fk.day("2026-10-22").delivery_for(10), 111)
        self.assertFalse(m.item(162).available)
        self.assertTrue(m.item(31).available)


class OrderTest(unittest.TestCase):
    def run_order(self, fixture, *args, **kw):
        gql = FakeGQL(fixture)
        fk = Forkable(gql)
        fk.order("2026-10-22", *args, **kw)
        return gql.mutations

    def test_replace_same_location_keeps_note(self):
        (name, inp), = self.run_order(day_fixture(note="No onions"), 111, 162, {"bread": "multigrain"})
        self.assertEqual(name, "replacePiece")
        self.assertEqual(inp["oldPieceId"], "aaaa1111")
        self.assertEqual(inp["deliveryId"], 1)
        self.assertEqual(inp["instructions"], "No onions")
        self.assertEqual(inp["selectionsHash"], {"114": [93], "179": [-1]})

    def test_add_when_no_meal(self):
        (name, inp), = self.run_order(day_fixture(my_piece_club=None), 110, 31)
        self.assertEqual(name, "addPiece")
        self.assertEqual(inp["userId"], 1)
        self.assertEqual(inp["deliveryId"], 1)

    def test_add_extra_meal(self):
        (name, _), = self.run_order(day_fixture(), 111, 31, mode="add")
        self.assertEqual(name, "addPiece")

    def test_move_uses_replace_all_pieces(self):
        (name, inp), = self.run_order(day_fixture(my_piece_club=10), 120, 31, club_id=20, instructions="")
        self.assertEqual(name, "replaceAllPieces")
        self.assertEqual(sorted(inp["deliveryIds"]), [1, 2])
        self.assertEqual(inp["newPiece"]["deliveryId"], 2)
        self.assertEqual(inp["newPiece"]["menuId"], 120)

    def test_menu_only_at_other_location_implies_move(self):
        (name, inp), = self.run_order(day_fixture(my_piece_club=10), 120, 31)
        self.assertEqual(name, "replaceAllPieces")

    def test_sold_out_refused(self):
        fk = Forkable(FakeGQL(day_fixture(), usage={"1": {"2026-10-22": {"am": 500}}}))
        with self.assertRaisesRegex(ForkableError, "sold out"):
            fk.order("2026-10-22", 110, 31)

    def test_confirm_and_skip(self):
        gql = FakeGQL(day_fixture())
        fk = Forkable(gql)
        fk.set_confirmed("2026-10-22", False)
        fk.skip("2026-10-22")
        self.assertEqual(gql.mutations[0], ("confirmDelivery", {"deliveryId": 1, "confirm": False,
                                                                 "changeFrom": "dashboard"}))
        self.assertEqual(gql.mutations[1], ("removePiece", {"orderId": 11, "pieceId": "aaaa1111", "myMeals": True}))


if __name__ == "__main__":
    unittest.main()


class HelpersTest(unittest.TestCase):
    def test_meals_prefix_and_required(self):
        day = Forkable(FakeGQL(day_fixture())).day("2026-10-22")
        self.assertEqual([s.piece_id for s in day.meals("aaaa")], ["aaaa1111"])
        self.assertEqual(day.meals("zzz"), [])
        with self.assertRaisesRegex(ForkableError, "No meal"):
            day.meals("zzz", required=True)

    def test_plan_describes_options(self):
        plan = Forkable(FakeGQL(day_fixture())).plan_order("2026-10-22", 111, 162, {"bread": "multigrain"})
        self.assertEqual(plan.action, "replace")
        self.assertEqual(plan.options, [{"label": "Choose Bread", "value": "Multigrain Bagel"}])

    def test_normalize_session(self):
        from forkable.client import normalize_session
        self.assertEqual(normalize_session("abc123"), "abc123")
        self.assertEqual(normalize_session(" _easyorder_session=abc123; path=/ "), "abc123")
