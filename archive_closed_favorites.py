#!/usr/bin/env python3

import argparse
import csv
import datetime
import html
import json
import logging
import logging.config
import os
import re
from json.decoder import JSONDecodeError
from typing import Dict, Optional
from zoneinfo import ZoneInfo

import shopgoodwill

ARCHIVE_FIELDS = [
    "item_id",
    "end_time",
    "title",
    "final_price",
    "num_bids",
    "num_bidders",
    "won",
    "my_max_bid",
    "buy_now_price",
    "minimum_bid",
    "starting_price",
    "start_time",
    "category",
    "seller",
    "url",
    "image_url",
    "notes",
    "description",
    "archived_at",
]

HTML_TAG_REGEX = re.compile(r"<[^>]+>")


def get_max_bid(notes: Optional[str]) -> str:
    """
    Returns the `max_bid` from a favorite's JSON note (as used by bid_sniper),
    or an empty string if there isn't one
    """

    try:
        max_bid = json.loads(notes).get("max_bid", "")
    except (TypeError, AttributeError, JSONDecodeError):
        return ""

    return "" if max_bid is None else str(max_bid)


def favorite_to_row(favorite: Dict, archived_at: str) -> Dict:
    """
    Given a closed favorite from sgw.get_favorites,
    return a row for the price archive
    """

    item_id = int(favorite["itemId"])
    image_url = favorite.get("imageURL") or ""

    return {
        "item_id": item_id,
        # SGW's timestamps are in Pacific time
        "end_time": favorite["endTime"].split(".")[0],
        "title": favorite["title"].strip(),
        "final_price": favorite["currentPrice"],
        "num_bids": favorite["numBids"],
        "won": (favorite.get("quantityWon") or 0) > 0,
        "my_max_bid": get_max_bid(favorite.get("notes")),
        "buy_now_price": favorite.get("buyNowPrice") or "",
        "minimum_bid": favorite.get("minimumBid") or "",
        "category": favorite.get("catFullName") or "",
        "seller": favorite.get("sellerName") or "",
        "url": f"https://shopgoodwill.com/item/{item_id}",
        "image_url": image_url.replace("\\", "/"),
        "notes": favorite.get("notes") or "",
        "archived_at": archived_at,
    }


def get_item_details(sgw: shopgoodwill.Shopgoodwill, item_id: int) -> Dict:
    """
    Fetch the auction details that aren't included in a favorite,
    for adding to an item's archive row
    """

    item_info = sgw.get_item_info(item_id)
    bids = (item_info.get("bidHistory") or dict()).get("bidSummary") or list()

    # descriptions are HTML - flatten them to a single line of text
    description = html.unescape(
        HTML_TAG_REGEX.sub(" ", item_info.get("description") or "")
    )

    return {
        "num_bidders": len({bid["bidderName"] for bid in bids}),
        "starting_price": item_info.get("startingPrice") or "",
        "start_time": (item_info.get("startTime") or "").split(".")[0],
        "description": " ".join(description.split()),
    }


def load_archive(archive_filename: str) -> Dict[int, Dict]:
    if not os.path.isfile(archive_filename):
        return dict()

    with open(archive_filename, "r", newline="") as f:
        return {int(row["item_id"]): row for row in csv.DictReader(f)}


def write_archive(archive_filename: str, archive: Dict[int, Dict]) -> None:
    # sort by end time so that diffs stay small and readable
    rows = sorted(
        archive.values(), key=lambda row: (row["end_time"], int(row["item_id"]))
    )

    with open(archive_filename, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ARCHIVE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(
        description="Record the final prices of closed favorites to a CSV, "
        "and remove them from favorites once they've been closed for a while"
    )
    parser.add_argument(
        "--remove-after-days",
        type=float,
        help="Remove archived favorites this many days after their auction ended. "
        "Overrides the config value (default 5)",
    )
    parser.add_argument(
        "--archive-file",
        type=str,
        help="Path to the price archive CSV. "
        "Overrides the config value (default ./price_archive.csv)",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="If set, do not write the archive or remove any favorites",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.json",
        help="Path to config file - defaults to ./config.json",
    )
    args = parser.parse_args()

    with open(args.config, "r") as f:
        config = json.load(f)

    archive_config = config.get("price_archive", dict())
    archive_filename = args.archive_file or archive_config.get(
        "archive_filename", "price_archive.csv"
    )
    remove_after_days = args.remove_after_days
    if remove_after_days is None:
        remove_after_days = archive_config.get("remove_after_days", 5)

    # logging setup
    logger = logging.getLogger("shopgoodwill_archive_closed_favorites")
    logging_conf = config.get("logging", dict())

    if logging_conf.get("version", 0) >= 1:
        logging.config.dictConfig(logging_conf)
    else:
        logging.basicConfig()
        logger.setLevel(logging_conf.get("log_level", logging.INFO))

    # init the command account
    if config["auth_info"].get("auth_type", "universal") == "command_bid":
        sgw = shopgoodwill.Shopgoodwill(config["auth_info"]["command_account"])
    else:
        sgw = shopgoodwill.Shopgoodwill(config["auth_info"])

    now = datetime.datetime.now(ZoneInfo("Etc/UTC"))
    archived_at = now.replace(microsecond=0).isoformat()

    archive = load_archive(archive_filename)
    closed_favorites = sgw.get_favorites("close")

    new_count = 0
    for item_id, favorite in closed_favorites.items():
        if item_id not in archive:
            archive[item_id] = favorite_to_row(favorite, archived_at)
            new_count += 1

    # fetch details for new rows, and retry rows whose fetch failed previously
    # (start_time is always set for a successful fetch)
    for item_id, row in archive.items():
        if row.get("start_time"):
            continue

        try:
            row.update(get_item_details(sgw, item_id))
        except Exception as e:
            logger.error(
                f"Failed to get details for item {item_id} - {type(e).__name__} - {e}"
            )

    if args.dry_run:
        logger.info(f"[dry run] would archive {new_count} closed favorites")
    else:
        write_archive(archive_filename, archive)
        logger.info(
            f"Archived {new_count} closed favorites to {archive_filename} "
            f"({len(archive)} total)"
        )

    # only remove favorites whose details are safely in the archive
    removed_count = 0
    for item_id, favorite in closed_favorites.items():
        if not archive[item_id].get("start_time"):
            continue

        end_time = sgw.convert_timestamp_to_datetime(favorite["endTime"])
        if now - end_time < datetime.timedelta(days=remove_after_days):
            continue

        if args.dry_run:
            logger.info(f"[dry run] would remove favorite {item_id}")
            removed_count += 1
            continue

        try:
            sgw.remove_favorite(item_id)
            removed_count += 1
        except Exception as e:
            logger.error(
                f"Failed to remove favorite {item_id} - {type(e).__name__} - {e}"
            )

    logger.info(
        f"{'[dry run] would remove' if args.dry_run else 'Removed'} "
        f"{removed_count} favorites closed for over {remove_after_days} days"
    )


if __name__ == "__main__":
    main()
