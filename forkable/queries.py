"""GraphQL field selections, trimmed from what the Forkable web app requests."""

PIECE_FIELDS = """
  id date itemId menuId name userId userFullName state isConfirmed autoOrder
  instructions selections price description copayAmount isLateOrder
  attributes { label value }
  menuItem { id sectionId }
"""

ORDER_FIELDS = f"""
  id state pastLateOrderDeadline isOverVenueCapacity replacementCutoffTs
  menu {{ id name sections {{ id active items {{ id active }} }} }}
  venue {{ id name displayName capacity allowLateMeals allowLateRemovals }}
  pieces {{ {PIECE_FIELDS} }}
"""

DELIVERY_FIELDS = f"""
  id state simpleState forDeliveryAt deliveryWindow isReadOnly userConfirmed mealClubId
  isPreferred afternoon availableMenuIds pastLateOrderDeadline canRequestChanges
  allowanceType weeklyAllowance weeklyAllowanceAvailable
  serviceWindow {{ name baseTime }}
  address {{ formatted }}
  club {{ id name copay clubCoversAllFees }}
  orders {{ {ORDER_FIELDS} }}
  userReceipt {{ subtotal feesTotal due copayAmount clubCopay }}
"""

MENU_FIELDS = """
  id name displayName tags isKosher ratingCount disableSpecialInstructions
  venue { id name displayName capacity }
  sections {
    id name description
    items {
      id menuId name description price ingredientTags imageUrl averageRating dietLevel
      modifierIds
      modifiers {
        id name optionSetId min max free required hidden
        options { id name price ingredientTags }
      }
    }
  }
"""

ME_FIELDS = """
  id firstName lastName fullName email mealClubAutoOrder
  mealClubs { id name market { timezone } }
  preferredLocations { wday clubId }
"""

ME = f"query {{ me {{ {ME_FIELDS} }} }}"

MY_DELIVERIES = f"""
query ($from: Date!, $to: Date) {{
  myDeliveries(from: $from, to: $to) {{ {DELIVERY_FIELDS} }}
}}
"""

# Same, plus `me` in the same round trip (parsing deliveries needs the user id and timezones).
MY_DELIVERIES_AND_ME = f"""
query ($from: Date!, $to: Date) {{
  me {{ {ME_FIELDS} }}
  myDeliveries(from: $from, to: $to) {{ {DELIVERY_FIELDS} }}
}}
"""

# Menus and their venues' capacity usage in one round trip.
MENUS_WITH_USAGE = f"""
query ($ids: [Int!]!, $clubId: Int!, $venueIds: [Int!]!, $date: Date!) {{
  menus(ids: $ids, clubId: $clubId) {{ {MENU_FIELDS} }}
  venueUsage(ids: $venueIds, from: $date, to: $date)
}}
"""

MEAL_RESTRICTIONS = """
query ($userId: Int!, $menuId: Int!, $itemId: Int!, $customization: Json) {
  mealRestrictions(userId: $userId, menuId: $menuId, itemId: $itemId, customization: $customization) { conflicts }
}
"""

# Selection used for addPiece / replacePiece / removePiece payloads.
PIECE_MUTATION_SELECTION = f"""
  errors errorDetails
  delivery {{ {DELIVERY_FIELDS} }}
"""
