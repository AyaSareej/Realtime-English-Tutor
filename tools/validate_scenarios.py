from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.guided_conversation.catalog import ScenarioCatalogRepository


def main() -> None:
    content_root = ROOT / "services" / "guided_conversation" / "content"
    catalog = ScenarioCatalogRepository(content_root).catalog
    expected_ids = {
        "restaurant.order_drink.a1",
        "restaurant.order_meal.a1",
        "restaurant.wrong_order.b1",
        "airport.check_in.a2",
    }
    actual_ids = {scenario.id for scenario in catalog.scenarios}
    if actual_ids != expected_ids:
        raise SystemExit(
            f"Expected guided scenario IDs {sorted(expected_ids)}, got {sorted(actual_ids)}"
        )
    if {domain.id for domain in catalog.domains} != {"restaurant", "airport"}:
        raise SystemExit("Expected restaurant and airport guided domains")
    restaurant_ids = {
        scenario.id for scenario in catalog.scenarios if scenario.domain_id == "restaurant"
    }
    if restaurant_ids != expected_ids - {"airport.check_in.a2"}:
        raise SystemExit("The three restaurant scenarios must remain in the restaurant domain")
    airport_ids = {
        scenario.id for scenario in catalog.scenarios if scenario.domain_id == "airport"
    }
    if airport_ids != {"airport.check_in.a2"}:
        raise SystemExit("The airport domain must contain the A2 check-in scenario")
    for scenario in catalog.scenarios:
        for turn in scenario.turns:
            if turn.user_display_text != turn.user_expected_text:
                raise SystemExit(
                    f"{scenario.id}/{turn.id}: displayed and expected learner lines must match"
                )
    print(f"Validated {len(catalog.scenarios)} guided scenarios from {catalog.content_version}.")


if __name__ == "__main__":
    main()
