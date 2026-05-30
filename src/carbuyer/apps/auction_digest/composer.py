"""Pure composition of a per-auction digest into a plaintext Discord message.

The runner supplies an already-filtered (dismissed/passed excluded, sections
deduped) header + two lot lists; this module only formats, caps each section at
SECTION_CAP, and returns None when there is nothing to send. Plaintext (not a
Discord embed) matches discord_post.post_simple_message; the result stays under
Discord's 2000-char message limit."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

SECTION_CAP = 10
_MAX_CONTENT = 2000


@dataclass(frozen=True, slots=True)
class DigestHeader:
    auction_id: int
    title: str
    location: str
    starts_at: datetime | None
    lot_count: int
    vehicle_count: int
    url: str


@dataclass(frozen=True, slots=True)
class DigestLot:
    lot_id: int
    summary: str
    search_name: str | None  # set for saved-search matches; None for rare


def _section(label: str, lots: list[DigestLot], bullet: str) -> list[str]:
    shown = lots[:SECTION_CAP]
    lines = [f"{label} ({len(lots)})"]
    for lot in shown:
        tag = f" [{lot.search_name}]" if lot.search_name else ""
        lines.append(f"  {bullet} {lot.summary}{tag} -> /lots/{lot.lot_id}")
    extra = len(lots) - len(shown)
    if extra > 0:
        lines.append(f"  ... and {extra} more -> {{auction_url}}")
    return lines


def compose_digest(
    header: DigestHeader,
    *,
    matches: list[DigestLot],
    rare: list[DigestLot],
) -> str | None:
    if not matches and not rare:
        return None

    when = header.starts_at.strftime("%a, %b %d - %H:%M UTC") if header.starts_at else "TBD"
    lines = [
        f"AUCTION: {header.title} - {header.location}",
        f"{when} | {header.lot_count} lots, {header.vehicle_count} vehicles | {header.url}",
        "",
    ]
    if matches:
        lines += _section("Your saved searches", matches, "*")
        lines.append("")
    if rare:
        lines += _section("Rare / special vehicles", rare, "-")
        lines.append("")
    lines.append("Cheap-deal alerts will arrive closer to close.")

    content = "\n".join(lines).replace("{auction_url}", header.url)
    if len(content) > _MAX_CONTENT:
        content = content[: _MAX_CONTENT - 1].rstrip() + "…"
    return content
