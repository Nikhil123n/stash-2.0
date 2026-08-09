"""Audit mymind spaces: report cards assigned to 2+ spaces (duplicates) and
cards assigned to 0 spaces (unassigned). Read-only — no changes made.

Usage:
    python scripts/audit_spaces.py
    python scripts/audit_spaces.py --selftest   # run the offline self-check
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _classify(
    all_card_ids: set[str], space_cards: dict[str, list[str]]
) -> tuple[dict[str, list[str]], list[str]]:
    """space_cards: space name -> [card_id, ...] currently in that space.

    Returns (duplicates, unassigned):
      duplicates: card_id -> list of space names it's in (len >= 2)
      unassigned: card_ids in zero spaces
    """
    card_to_spaces: dict[str, list[str]] = {cid: [] for cid in all_card_ids}
    for space_name, card_ids in space_cards.items():
        for cid in card_ids:
            card_to_spaces.setdefault(cid, []).append(space_name)

    duplicates = {cid: names for cid, names in card_to_spaces.items() if len(names) > 1}
    unassigned = [cid for cid, names in card_to_spaces.items() if not names]
    return duplicates, unassigned


def _selftest() -> None:
    dup, unassigned = _classify(
        all_card_ids={"a", "b", "c"},
        space_cards={"Claude": ["a", "b"], "Tech": ["b"]},
    )
    assert dup == {"b": ["Claude", "Tech"]}
    assert unassigned == ["c"]
    print("selftest OK")


async def main() -> None:
    from dotenv import load_dotenv
    load_dotenv(override=True)
    from stash.gateway.mymind import MyMindGateway

    gw = MyMindGateway()

    spaces = await gw.get_spaces()
    all_cards = await gw.search_cards(limit=100_000)
    titles = {c["id"]: c["title"] for c in all_cards}

    space_cards = {}
    for space in spaces:
        cards = await gw.get_space_cards(space["id"])
        space_cards[space["name"]] = [c["id"] for c in cards]

    duplicates, unassigned = _classify(set(titles), space_cards)

    print(f"{len(all_cards)} cards, {len(spaces)} spaces\n")

    print(f"Duplicates (in 2+ spaces): {len(duplicates)}")
    for cid, names in duplicates.items():
        print(f"  {cid}  {titles.get(cid, '(untitled)')!r}  -> {', '.join(names)}")

    print(f"\nUnassigned (in 0 spaces): {len(unassigned)}")
    for cid in unassigned:
        print(f"  {cid}  {titles.get(cid, '(untitled)')!r}")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        asyncio.run(main())
