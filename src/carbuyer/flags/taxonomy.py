"""Flag / desirability / gotcha taxonomies — shared by enricher (Phase 3 prompt)
and valuator (Phase 4 scoring).

Calibration notes from Phase 3 design overlay #23:
- ``needs_work`` is -1 (not -2) — fires on ~80% of auction lots, would dominate
  scoring otherwise. -2 reserved for genuine red events (accident, structural,
  drivetrain).
- ``rust_mentioned`` -1 covers cosmetic; ``frame_rust`` -3 and
  ``frame_rust_perforated`` showstopper cover structural. Western-Canada-specific
  ``rocker_rust`` / ``cab_corner_rust`` -2 are body-at-structural-interface.
- ``wont_start`` is -3 red flag, NOT showstopper — RB / industrial yard "won't
  start" frequently means dead battery. Showstopper reserved for explicit
  ``engine_seized`` / ``for_parts_only`` / ``needs_engine``.
- Bare ``as_is_no_warranty`` REMOVED from showstoppers — fires on every online
  auction. Replaced with ``seller_says_for_parts_only`` requiring explicit
  "sold for parts" / "no questions" phrasing.
- ``classic_or_collector=False`` is the default for any vehicle not in
  ``CLASSIC_EXCEPTIONS`` — pre-2000 is "old", not "classic".
- Gotchas include the diesel powertrain failure set (6.0L/6.4L/6.7L
  PowerStroke, Duramax LML CP4, Cummins 6.7L 68RFE, Hemi MDS lifters,
  GM AFM lifter failure) that dominates Western Canada auction-yard diesels.
"""
from __future__ import annotations

from typing import NotRequired, TypedDict


class FlagDef(TypedDict):
    flag: str
    weight: int
    description: str


class ShowstopperDef(TypedDict):
    flag: str
    description: str


class DesirableEntry(TypedDict):
    make: str
    model: str
    trim: str
    note: str


class ClassicException(TypedDict):
    make: str
    model: str
    year_min: int
    year_max: int
    note: str
    # Optional: when set, matching requires caller's trim to equal this value
    # (case- and punctuation-insensitive). Absent means "any trim qualifies".
    # Used to prevent e.g. a base 1996 Civic from being flagged classic when
    # only the SiR / Type R variants are.
    trim: NotRequired[str]


class GotchaEntry(TypedDict):
    make: str
    model: str
    year_min: int
    year_max: int
    note: str


RED_FLAG_TAXONOMY: list[FlagDef] = [
    # ── -4: lot is dispositive for retail buyers but not flippers ──
    {"flag": "salvage_not_rebuilt", "weight": -4,
     "description": (
         "Salvage title that has not been rebuilt. Wholesale-rebuild buyers "
         "and parts buyers may still want this; do not flag as showstopper."
     )},
    {"flag": "outstanding_lien", "weight": -4,
     "description": (
         "Lien holder still on title at sale. Auction houses typically pay "
         "out the lien from sale proceeds, but verify before bidding."
     )},
    {"flag": "lemon_law_buyback", "weight": -4,
     "description": "Manufacturer buyback (lemon-law brand)."},
    {"flag": "flood_damage_partial", "weight": -3,
     "description": (
         "Flood damage that did NOT submerge the interior — waterline below "
         "seats, wet carpet, damp under-rear-floor. Mechanically may be fine. "
         "Trigger phrasing: 'rear floor wet', 'damp carpet', 'water in trunk'. "
         "Total flood (above seats / titled flood) is showstopper, not red."
     )},
    {"flag": "engine_knock", "weight": -3,
     "description": (
         "Engine knock or overheating mentioned "
         "(not seized — see engine_seized showstopper)."
     )},
    {"flag": "wont_start", "weight": -3,
     "description": (
         "Won't start, ran when parked. "
         "May be battery on industrial yards; still risk."
     )},
    {"flag": "transmission_slipping", "weight": -3,
     "description": "Slipping, flaring, harsh shifts, torque-converter or 'shudder' mentioned."},
    {"flag": "head_gasket_suspected", "weight": -3,
     "description": "Coolant in oil, oil in coolant, white smoke, repeated overheating."},
    {"flag": "frame_rust", "weight": -3,
     "description": "Frame rust mentioned, perforation suspected, structural concern."},
    {"flag": "bill_of_sale_only", "weight": -3,
     "description": "No proper title transfer document — registration may be impossible."},
    {"flag": "accident_history", "weight": -2,
     "description": "Accident reported on Carfax or in description (non-trivial damage)."},
    {"flag": "rocker_rust", "weight": -2,
     "description": "Rocker panel or cab corner rust — structural interface."},
    {"flag": "no_keys", "weight": -2,
     "description": "Keys missing or non-functional — modern key program may cost $300+."},
    {"flag": "electrical_issues", "weight": -2,
     "description": "Lights, gauges, modules malfunctioning — diagnosis is expensive."},
    {"flag": "leaks_coolant", "weight": -2,
     "description": "Coolant loss, weeping water pump, radiator leak."},
    {"flag": "diesel_emissions_deleted", "weight": -2,
     "description": "DPF/EGR/DEF delete — regulatory and insurance issue, won't pass inspection."},
    {"flag": "salvage_history_carfax", "weight": -2,
     "description": "Branded but rebuilt; Carfax shows brand event."},
    {"flag": "abandoned_vehicle", "weight": -2,
     "description": "Listed as abandoned — title issues likely."},
    {"flag": "needs_work", "weight": -1,
     "description": (
         "Listing says 'needs work' / 'project' / 'mechanic special' "
         "without specifying components."
     )},
    {"flag": "no_service_records", "weight": -1,
     "description": "No service history mentioned for high-mileage vehicle."},
    {"flag": "rust_mentioned", "weight": -1,
     "description": (
         "Surface rust mentioned (cosmetic, not structural — "
         "see frame_rust for structural)."
     )},
    {"flag": "smoker_owned", "weight": -1,
     "description": "Smoker-owned, interior odor, nicotine staining."},
    {"flag": "high_mileage_no_service", "weight": -1,
     "description": ">200k km with no major service history mentioned."},
    {"flag": "mileage_unknown", "weight": -1,
     "description": "Mileage missing, labeled TMU (true mileage unknown), or exempt."},
    {"flag": "modifications", "weight": -1,
     "description": "Heavy aftermarket modifications without receipts or pre-mod baseline."},
    {"flag": "leaks_oil_minor", "weight": -1,
     "description": "Oil leaks mentioned with no specific component."},
    {"flag": "bald_tires", "weight": -1,
     "description": "Tires below 4/32 tread, dry-rotted, or 'needs tires' mentioned."},
    {"flag": "check_engine_light_on", "weight": -1,
     "description": "Check engine light on with no diagnosis stated."},
    {"flag": "out_of_province", "weight": -1,
     "description": "Different province than buyer — title transfer plus inspection cost."},
    {"flag": "winter_tires_only", "weight": -1,
     "description": "Only winter tires included — resale friction."},
    {"flag": "seller_dealer_only", "weight": -1,
     "description": "Buyer restricted to dealers / exporters only."},
]

GREEN_FLAG_TAXONOMY: list[FlagDef] = [
    {"flag": "no_accidents_carfax", "weight": 2,
     "description": "Carfax shows no accidents."},
    {"flag": "service_records", "weight": 2,
     "description": "Itemized service history attached or referenced with detail."},
    {"flag": "from_southern_climate", "weight": 2,
     "description": "Imported from BC coast / California / Arizona / Texas — no winter exposure."},
    {"flag": "warranty_remaining", "weight": 2,
     "description": "OEM warranty has remaining mileage or months."},
    {"flag": "cpo_certified", "weight": 2,
     "description": "Manufacturer Certified Pre-Owned program."},
    {"flag": "recent_timing_belt", "weight": 1,
     "description": "Recent timing belt or chain replacement (interference engine)."},
    {"flag": "recent_transmission_service", "weight": 1,
     "description": "Recent transmission service / fluid change with paperwork."},
    {"flag": "single_owner", "weight": 1,
     "description": "Single-owner vehicle."},
    {"flag": "recent_major_service", "weight": 1,
     "description": "Recent brakes / tires / suspension service with paperwork."},
    {"flag": "garage_kept", "weight": 1,
     "description": "Stored indoors, plug-in block heater (cold-climate care)."},
    {"flag": "non_smoker", "weight": 1,
     "description": "Explicitly non-smoker."},
    {"flag": "regular_oil_changes", "weight": 1,
     "description": "Synthetic oil or regular oil-change interval mentioned with paperwork."},
    {"flag": "two_sets_of_tires", "weight": 1,
     "description": "Winter + summer tire sets included (serious owner)."},
    {"flag": "block_heater_installed", "weight": 1,
     "description": "Block heater / engine heater installed (cold-climate maintenance)."},
    {"flag": "highway_mileage", "weight": 1,
     "description": "Mileage qualifier — 'highway km' or 'commuter'."},
    {"flag": "recent_inspection_passed", "weight": 1,
     "description": "Recent provincial out-of-province / safety inspection on record."},
    {"flag": "recall_completed", "weight": 1,
     "description": "Major recall (Tacoma frame, Theta II rod bearings) verified completed."},
]

SHOWSTOPPER_TAXONOMY: list[ShowstopperDef] = [
    # Phase 3 review: showstoppers are reserved for cases where literally no
    # buyer profile (flipper, parts, retail) wants the lot. Salvage /
    # outstanding-lien / lemon-buyback are NOT here — flippers and rebuild
    # buyers want salvage; auction houses pay out liens at sale; lemon-buybacks
    # post-fix are common deals. Those live as heavy red flags (-4) below.
    {"flag": "frame_damage_unrepaired",
     "description": (
         "Frame damage or structural compromise, no repair documented. "
         "Trigger phrasing: 'bent frame', 'frame is bent', 'structural damage'."
     )},
    {"flag": "engine_seized",
     "description": (
         "Engine confirmed seized / will not turn over by hand. "
         "Trigger phrasing: 'engine seized', 'engine locked up', 'won't turn over'."
     )},
    {"flag": "for_parts_only",
     "description": (
         "Sold as parts donor / engine in truck bed / catalogued for dismantling. "
         "Trigger phrasing: 'engine in bed', 'parts donor', 'parts truck'."
     )},
    {"flag": "fire_damage",
     "description": (
         "Fire-damaged. Trigger phrasing: 'fire damage', 'burned', "
         "'engine bay fire', 'interior burned'."
     )},
    {"flag": "flood_damage_total",
     "description": (
         "Flood-damaged with waterline above seats / interior submerged / "
         "title brand 'flood'. Trigger phrasing must include 'flood title', "
         "'submerged', 'water above floorboards', or 'interior soaked'. "
         "Mere 'wet floor mat' or 'damp carpet' is NOT a trigger."
     )},
    {"flag": "vin_mismatch",
     "description": (
         "VIN inconsistency, re-VIN, or VIN-etched-from-other-vehicle "
         "(theft recovery indicator)."
     )},
    {"flag": "stolen_recovered",
     "description": (
         "Branded as stolen-recovered on Carfax / title. Trigger phrasing: "
         "'theft recovery', 'stolen recovered title brand'."
     )},
    {"flag": "no_title",
     "description": (
         "No title document exists — cannot legally register. Trigger phrasing: "
         "'no title', 'lost title', 'no paperwork'. NOT to fire on "
         "'bill of sale only' (that's a separate red flag)."
     )},
    {"flag": "non_repairable_brand",
     "description": (
         "Title brand 'non-repairable' or 'junk' — cannot be re-registered. "
         "Trigger phrasing: 'non-repairable title', 'junk title', "
         "'certificate of destruction'."
     )},
    {"flag": "seller_says_for_parts_only",
     "description": (
         "Seller explicitly says 'sold for parts only' / 'no questions answered'. "
         "NOT to fire on RB-style 'as-is, where-is, no further information' "
         "(that phrasing is universal RB consignment language and does not "
         "indicate the seller is excluding running buyers)."
     )},
]


# Phase 3 overlay #23: Z71 is a package, not a desirable trim. Specific
# performance variants only.
DESIRABLE_TRIMS: list[DesirableEntry] = [
    # Toyota / Lexus
    {"make": "Toyota", "model": "Tacoma", "trim": "TRD Pro",
     "note": "Special-edition Tacoma."},
    {"make": "Toyota", "model": "4Runner", "trim": "TRD Pro",
     "note": "Special-edition 4Runner."},
    {"make": "Toyota", "model": "Tundra", "trim": "TRD Pro",
     "note": "Special-edition Tundra."},
    {"make": "Toyota", "model": "Land Cruiser", "trim": "any",
     "note": "Land Cruiser is desirable in any year/spec on Western Canadian market."},
    {"make": "Toyota", "model": "FJ Cruiser", "trim": "any",
     "note": "FJ Cruiser appreciation, all trims."},
    {"make": "Lexus", "model": "GX 460", "trim": "any",
     "note": "Body-on-frame Land Cruiser cousin."},
    {"make": "Lexus", "model": "GX 470", "trim": "any",
     "note": "First-gen GX, V8."},
    {"make": "Lexus", "model": "LX 470", "trim": "any",
     "note": "100-series Land Cruiser cousin."},
    {"make": "Lexus", "model": "LX 570", "trim": "any",
     "note": "200-series Land Cruiser cousin."},
    # Ford trucks
    {"make": "Ford", "model": "F-150", "trim": "Raptor",
     "note": "Off-road performance F-150."},
    {"make": "Ford", "model": "F-150", "trim": "Tremor",
     "note": "Performance off-road F-150 trim."},
    {"make": "Ford", "model": "F-250", "trim": "King Ranch",
     "note": "High-trim PowerStroke HD."},
    {"make": "Ford", "model": "F-250", "trim": "Platinum",
     "note": "High-trim PowerStroke HD."},
    {"make": "Ford", "model": "F-350", "trim": "Lariat",
     "note": "High-trim PowerStroke HD."},
    {"make": "Ford", "model": "F-350", "trim": "King Ranch",
     "note": "High-trim PowerStroke HD."},
    {"make": "Ford", "model": "Bronco", "trim": "any",
     "note": "Current-gen Bronco (2021+) — all trims desirable."},
    # GM trucks
    {"make": "Chevrolet", "model": "Silverado 1500", "trim": "Trail Boss",
     "note": "Off-road trim with skid plates and lift."},
    {"make": "Chevrolet", "model": "Silverado 2500", "trim": "Z71 LTZ",
     "note": "Z71 + LTZ Duramax — combined trim signal."},
    # Ram trucks
    {"make": "Ram", "model": "2500", "trim": "Power Wagon",
     "note": "Off-road HD with locker, sway disconnect, winch."},
    {"make": "Ram", "model": "2500", "trim": "Cummins Laramie",
     "note": "Cummins HD high-trim."},
    {"make": "Ram", "model": "3500", "trim": "Cummins Laramie",
     "note": "Cummins HD high-trim."},
    # Jeep
    {"make": "Jeep", "model": "Wrangler", "trim": "Rubicon",
     "note": "Off-road-capable trim."},
    {"make": "Jeep", "model": "Wrangler", "trim": "Rubicon 392",
     "note": "V8 Wrangler."},
    {"make": "Jeep", "model": "Gladiator", "trim": "Rubicon",
     "note": "Off-road Gladiator trim."},
    {"make": "Jeep", "model": "Gladiator", "trim": "Mojave",
     "note": "Desert-runner Gladiator trim."},
    # Performance cars
    {"make": "Subaru", "model": "WRX", "trim": "STI",
     "note": "WRX STI."},
    {"make": "Subaru", "model": "BRZ", "trim": "tS",
     "note": "Higher-tier BRZ."},
    {"make": "Volkswagen", "model": "Golf", "trim": "R",
     "note": "AWD performance hatch."},
    {"make": "Volkswagen", "model": "Golf", "trim": "GTI",
     "note": "GTI especially with manual transmission."},
    {"make": "BMW", "model": "M2", "trim": "any",
     "note": "Compact M, increasingly rare."},
    {"make": "BMW", "model": "M3", "trim": "any",
     "note": "Any-era M3."},
    {"make": "BMW", "model": "M4", "trim": "any",
     "note": "Any-era M4."},
    {"make": "BMW", "model": "M5", "trim": "any",
     "note": "Any-era M5."},
    {"make": "Porsche", "model": "911", "trim": "any",
     "note": "Any 911."},
    {"make": "Porsche", "model": "Cayman", "trim": "any",
     "note": "Any Cayman."},
    {"make": "Porsche", "model": "Boxster", "trim": "any",
     "note": "Any Boxster."},
    {"make": "Nissan", "model": "GT-R", "trim": "any",
     "note": "GT-R any model year."},
    {"make": "Nissan", "model": "370Z", "trim": "Nismo",
     "note": "Performance trim."},
    {"make": "Honda", "model": "Civic", "trim": "Type R",
     "note": "Hot Civic."},
    {"make": "Honda", "model": "Civic", "trim": "Si",
     "note": "Si especially with manual transmission."},
    {"make": "Honda", "model": "S2000", "trim": "any",
     "note": "S2000 any year."},
    {"make": "Acura", "model": "NSX", "trim": "any",
     "note": "NSX any era."},
    {"make": "Mazda", "model": "Miata", "trim": "any",
     "note": "Miata / MX-5 any year."},
    {"make": "Mazda", "model": "RX-8", "trim": "any",
     "note": "Rotary RX-8."},
    {"make": "Mazdaspeed", "model": "Mazda3", "trim": "any",
     "note": "Mazdaspeed3 — hot hatch."},
    {"make": "Mazdaspeed", "model": "Mazda6", "trim": "any",
     "note": "Mazdaspeed6 — AWD turbo sedan."},
    {"make": "Mercedes-Benz", "model": "G-Class", "trim": "any",
     "note": "G-Wagen any year."},
    {"make": "Mitsubishi", "model": "Lancer", "trim": "Evolution",
     "note": "Lancer Evo."},
    # Land Rover, Hummer
    {"make": "Land Rover", "model": "Defender", "trim": "any",
     "note": "Defender any year."},
    {"make": "Hummer", "model": "H1", "trim": "any",
     "note": "Mil-spec H1 — rare on Canadian auctions."},
]


# Phase 3 overlay #23: pre-2000 default flips. Only models in this list are
# classic; all other pre-2000 vehicles are "old", not "classic".
CLASSIC_EXCEPTIONS: list[ClassicException] = [
    # JDM / sports
    {"make": "Toyota", "model": "Supra", "year_min": 1993, "year_max": 2002,
     "note": "Mk4 Supra."},
    {"make": "Toyota", "model": "MR2", "year_min": 1985, "year_max": 2005,
     "note": "All MR2 generations."},
    {"make": "Toyota", "model": "Corolla", "year_min": 1983, "year_max": 1987,
     "note": "AE86 / Corolla GT-S."},
    {"make": "Acura", "model": "NSX", "year_min": 1990, "year_max": 2005,
     "note": "First-gen NSX."},
    {"make": "Acura", "model": "Integra", "year_min": 1995, "year_max": 2001,
     "note": "Integra Type R DC2."},
    {"make": "Mazda", "model": "RX-7", "year_min": 1992, "year_max": 2002,
     "note": "FD3S RX-7."},
    {"make": "Mazda", "model": "Miata", "year_min": 1989, "year_max": 2005,
     "note": "NA / NB Miata."},
    {"make": "Honda", "model": "Civic", "trim": "SiR", "year_min": 1992, "year_max": 2000,
     "note": "EG6 SiR."},
    {"make": "Honda", "model": "Civic", "trim": "Type R", "year_min": 1997, "year_max": 2000,
     "note": "EK9 Type R."},
    {"make": "Honda", "model": "S2000", "year_min": 1999, "year_max": 2009,
     "note": "S2000 entire run."},
    {"make": "Nissan", "model": "Skyline", "year_min": 1989, "year_max": 2002,
     "note": "R32 / R33 / R34 Skyline (imported)."},
    {"make": "Nissan", "model": "240SX", "year_min": 1989, "year_max": 1998,
     "note": "240SX S13 / S14."},
    {"make": "Nissan", "model": "300ZX", "year_min": 1989, "year_max": 2000,
     "note": "300ZX TT."},
    {"make": "Subaru", "model": "SVX", "year_min": 1991, "year_max": 1997,
     "note": "Subaru SVX."},
    # Euro
    {"make": "BMW", "model": "M3", "year_min": 1986, "year_max": 1999,
     "note": "E30 M3 / E36 M3."},
    {"make": "BMW", "model": "3 Series", "year_min": 1982, "year_max": 1991,
     "note": "E30 (non-M)."},
    {"make": "BMW", "model": "M5", "year_min": 1998, "year_max": 2003,
     "note": "E39 M5."},
    {"make": "Audi", "model": "Quattro", "year_min": 1980, "year_max": 1991,
     "note": "UR Quattro."},
    {"make": "Porsche", "model": "911", "year_min": 1965, "year_max": 1998,
     "note": "Air-cooled 911."},
    {"make": "Porsche", "model": "944", "year_min": 1982, "year_max": 1991,
     "note": "Porsche 944."},
    {"make": "Porsche", "model": "968", "year_min": 1992, "year_max": 1995,
     "note": "Porsche 968."},
    {"make": "Porsche", "model": "928", "year_min": 1978, "year_max": 1995,
     "note": "Porsche 928."},
    {"make": "Mercedes-Benz", "model": "190E", "year_min": 1984, "year_max": 1993,
     "note": "190E 2.3-16 / 2.5-16 (Cosworth)."},
    {"make": "Volkswagen", "model": "Golf", "year_min": 1983, "year_max": 1999,
     "note": "MK1 / MK2 / MK3 GTI / Corrado VR6."},
    # Trucks / 4x4
    {"make": "Toyota", "model": "Land Cruiser", "year_min": 1990, "year_max": 1997,
     "note": "80 series — solid front axle, 4.5L 1FZ-FE I6, sought-after."},
    {"make": "Toyota", "model": "Land Cruiser", "year_min": 1998, "year_max": 2007,
     "note": "100 series — IFS front (not solid axle), V8 4.7L 2UZ-FE."},
    {"make": "Jeep", "model": "Wrangler", "year_min": 1987, "year_max": 2006,
     "note": "YJ / TJ Wrangler."},
    {"make": "Ford", "model": "Bronco", "year_min": 1966, "year_max": 1996,
     "note": "Vintage Bronco."},
    {"make": "Chevrolet", "model": "K5 Blazer", "year_min": 1973, "year_max": 1991,
     "note": "K5 Blazer / GMT400."},
    {"make": "Dodge", "model": "Power Wagon", "year_min": 1945, "year_max": 1980,
     "note": "Vintage Power Wagon."},
    {"make": "Land Rover", "model": "Defender", "year_min": 1983, "year_max": 2016,
     "note": "Defender 90 / 110."},
]


GOTCHAS: list[GotchaEntry] = [
    # Toyota frame rust recall
    {"make": "Toyota", "model": "Tacoma", "year_min": 2005, "year_max": 2015,
     "note": "Frame rust recall: inspect rear leaf-spring perches and crossmembers."},
    {"make": "Toyota", "model": "4Runner", "year_min": 2003, "year_max": 2009,
     "note": "Frame rust recall on early years; verify recall completion."},
    # Honda
    {"make": "Honda", "model": "CR-V", "year_min": 2017, "year_max": 2022,
     "note": (
         "1.5T fuel-in-oil dilution; "
         "check oil level above full and gasoline smell on dipstick."
     )},
    {"make": "Honda", "model": "Pilot", "year_min": 2003, "year_max": 2017,
     "note": (
         "J35 V6 with VCM cylinder deactivation: "
         "oil consumption + transmission failure on 2003-2007."
     )},
    {"make": "Honda", "model": "Odyssey", "year_min": 2003, "year_max": 2017,
     "note": (
         "J35 V6 with VCM cylinder deactivation: "
         "oil consumption + transmission failure on 2003-2007."
     )},
    # Ford trucks
    {"make": "Ford", "model": "F-150", "year_min": 2011, "year_max": 2016,
     "note": "3.5L EcoBoost cam phaser / timing chain rattle on cold start; TSB 16-0027."},
    {"make": "Ford", "model": "F-150", "year_min": 2018, "year_max": 2023,
     "note": "5.0L Coyote oil consumption / rod knock on early 2018s — TSB 19-2244."},
    {"make": "Ford", "model": "F-150", "year_min": 2004, "year_max": 2010,
     "note": "5.4L 3V Triton: cam-phaser rattle, spark plug ejection or break-on-removal."},
    # Ford diesels (the big four)
    {"make": "Ford", "model": "F-250", "year_min": 2003, "year_max": 2007,
     "note": (
         "6.0L PowerStroke: EGR cooler + head-gasket failure "
         "('blue spring' / head studs); $5-8k repair if unaddressed."
     )},
    {"make": "Ford", "model": "F-350", "year_min": 2003, "year_max": 2007,
     "note": (
         "6.0L PowerStroke: EGR cooler + head-gasket failure; "
         "$5-8k repair if unaddressed."
     )},
    {"make": "Ford", "model": "F-250", "year_min": 2008, "year_max": 2010,
     "note": (
         "6.4L PowerStroke: twin-turbo + emissions failures; "
         "rad coolant cross-contamination; expensive."
     )},
    {"make": "Ford", "model": "F-350", "year_min": 2008, "year_max": 2010,
     "note": "6.4L PowerStroke: twin-turbo + emissions failures; expensive."},
    {"make": "Ford", "model": "F-250", "year_min": 2011, "year_max": 2019,
     "note": (
         "6.7L PowerStroke: CP4 fuel pump catastrophic failure "
         "contaminates entire fuel system; $8-12k."
     )},
    {"make": "Ford", "model": "F-350", "year_min": 2011, "year_max": 2019,
     "note": "6.7L PowerStroke: CP4 fuel pump catastrophic failure; $8-12k."},
    # GM trucks / diesel
    {"make": "Chevrolet", "model": "Silverado 1500", "year_min": 2007, "year_max": 2014,
     "note": (
         "5.3L AFM (Active Fuel Management) lifter failure + oil consumption; "
         "AFM-disable kit common."
     )},
    {"make": "GMC", "model": "Sierra 1500", "year_min": 2007, "year_max": 2014,
     "note": "5.3L AFM lifter failure + oil consumption; AFM-disable kit common."},
    {"make": "Chevrolet", "model": "Silverado 2500", "year_min": 2011, "year_max": 2016,
     "note": "6.6L Duramax LML: CP4 fuel pump (same as Ford 6.7); bypass kit recommended."},
    {"make": "GMC", "model": "Sierra 2500", "year_min": 2011, "year_max": 2016,
     "note": "6.6L Duramax LML: CP4 fuel pump (same as Ford 6.7); bypass kit recommended."},
    # Ram / Cummins / Hemi
    {"make": "Ram", "model": "2500", "year_min": 2007, "year_max": 2018,
     "note": "Cummins 6.7L: EGR cooler, NOx sensors; 68RFE transmission torque-converter failure."},
    {"make": "Ram", "model": "3500", "year_min": 2007, "year_max": 2018,
     "note": "Cummins 6.7L: EGR cooler, NOx sensors; 68RFE transmission torque-converter failure."},
    {"make": "Ram", "model": "1500", "year_min": 2009, "year_max": 2020,
     "note": "5.7L Hemi MDS lifter failure / 'Hemi tick'; cam wipe."},
    # Jeep
    {"make": "Jeep", "model": "Wrangler", "year_min": 2007, "year_max": 2018,
     "note": "Death-wobble: inspect track bar, ball joints, steering stabilizer."},
    {"make": "Jeep", "model": "Wrangler", "year_min": 2007, "year_max": 2011,
     "note": "JK 3.8L V6: rocker arm / lifter failure; lower oil pickup tube."},
    {"make": "Jeep", "model": "Grand Cherokee", "year_min": 2014, "year_max": 2017,
     "note": "3.0L EcoDiesel: emissions class action; lifter / bottom-end failure."},
    # Subaru
    {"make": "Subaru", "model": "Forester", "year_min": 1999, "year_max": 2011,
     "note": "EJ25 head gasket failure 100k-150k km; ask for updated MLS gasket."},
    {"make": "Subaru", "model": "WRX", "year_min": 2002, "year_max": 2014,
     "note": "EJ255/EJ257 turbo: ringland failure; rod-bearing failure on tuned cars."},
    # Hyundai/Kia
    {"make": "Hyundai", "model": "Sonata", "year_min": 2011, "year_max": 2019,
     "note": "Theta II 2.0T / 2.4 rod-bearing failure; verify recall / KSDS lifetime warranty."},
    {"make": "Kia", "model": "Optima", "year_min": 2011, "year_max": 2019,
     "note": "Theta II 2.0T / 2.4 rod-bearing failure; same recall as Sonata."},
    # Nissan
    {"make": "Nissan", "model": "Altima", "year_min": 2013, "year_max": 2018,
     "note": "CVT judder / whine class action; check fluid and any rebuild paperwork."},
    {"make": "Nissan", "model": "Titan", "year_min": 2004, "year_max": 2015,
     "note": "5.6L: rear differential whine, exhaust manifold studs."},
    {"make": "Nissan", "model": "Pathfinder", "year_min": 2005, "year_max": 2010,
     "note": "Radiator-to-transmission cooler crossover ('strawberry milkshake of death')."},
    {"make": "Nissan", "model": "Frontier", "year_min": 2005, "year_max": 2010,
     "note": "Radiator-to-transmission cooler crossover ('strawberry milkshake of death')."},
    # Toyota / Lexus V6
    {"make": "Toyota", "model": "Camry", "year_min": 2007, "year_max": 2010,
     "note": "2GR-FE V6: rubber VVT oil hose ruptures; total oil dump."},
    {"make": "Lexus", "model": "ES 350", "year_min": 2007, "year_max": 2010,
     "note": "2GR-FE V6: rubber VVT oil hose ruptures; total oil dump."},
    # BMW
    {"make": "BMW", "model": "335i", "year_min": 2007, "year_max": 2013,
     "note": "N54: HPFP failure, wastegate rattle, oil filter housing leak."},
    {"make": "BMW", "model": "535i", "year_min": 2010, "year_max": 2016,
     "note": "N55: oil filter housing, valve cover leaks."},
    {"make": "BMW", "model": "328i", "year_min": 2012, "year_max": 2017,
     "note": "N20 / N26 4-cyl: timing chain stretch / failure — TSB-only fix."},
    # Mercedes
    {"make": "Mercedes-Benz", "model": "C-Class", "year_min": 2005, "year_max": 2011,
     "note": "M272 V6 / M273 V8: balance-shaft / idler-gear wear; $5k+ job."},
    {"make": "Mercedes-Benz", "model": "E-Class", "year_min": 2005, "year_max": 2014,
     "note": "7G-Tronic 722.9 conductor plate / valve-body failures."},
    # Audi/VW
    {"make": "Volkswagen", "model": "GTI", "year_min": 2005, "year_max": 2014,
     "note": "2.0T FSI / EA888 Gen1+2: timing-chain tensioner; carbon buildup; PCV."},
    {"make": "Audi", "model": "A4", "year_min": 2005, "year_max": 2014,
     "note": "2.0T FSI / EA888: timing-chain tensioner; carbon buildup; PCV."},
    {"make": "Volkswagen", "model": "Jetta", "year_min": 2009, "year_max": 2015,
     "note": "TDI: emissions buyback eligibility, DPF/EGR/HPFP."},
    # Land Rover / Range Rover
    {"make": "Land Rover", "model": "Range Rover", "year_min": 2003, "year_max": 2017,
     "note": "Air suspension, oil leaks, EAS, electrical — chronic and expensive."},
    # Tesla
    {"make": "Tesla", "model": "Model S", "year_min": 2012, "year_max": 2018,
     "note": "MCU1 eMMC failure ($2k); air suspension; battery degradation."},
    # Generic modern diesel reminder
    {"make": "any", "model": "diesel", "year_min": 2008, "year_max": 2099,
     "note": (
         "Modern diesel: DEF/SCR catalyst, DPF, EGR cooler — "
         "emissions equipment is the recurring budget item."
     )},
]


# ─── helper functions for prompt assembly ───


def _norm(s: str | None) -> str:
    """Lowercase + strip non-alphanumeric for fuzzy match (F-150 / F150 / F 150)."""
    if s is None:
        return ""
    return "".join(c.lower() for c in s if c.isalnum())


def model_gotchas_for(
    *, make: str | None, model: str | None, year: int | None,
) -> list[str]:
    """Return all gotcha notes matching make/model/year. Case- and
    punctuation-insensitive on make/model so 'F-150' matches 'F150'.
    """
    if not (make and model and year):
        return []
    nm_make = _norm(make)
    nm_model = _norm(model)
    out: list[str] = []
    for g in GOTCHAS:
        if g["make"] == "any":  # global modern-diesel reminder
            continue
        if _norm(g["make"]) != nm_make:
            continue
        if _norm(g["model"]) != nm_model:
            continue
        if g["year_min"] <= year <= g["year_max"]:
            out.append(g["note"])
    return out


def is_classic(
    *,
    make: str | None,
    model: str | None,
    year: int | None,
    trim: str | None = None,
) -> bool:
    """Return True iff make/model/year (and optionally trim) matches a
    CLASSIC_EXCEPTIONS entry. Pre-2000 is NOT automatically classic — only
    listed exceptions are.

    Trim semantics: when an entry has no ``trim`` key, the entry matches any
    trim. When the entry has ``trim`` set, the caller must supply a matching
    ``trim`` argument (case- and punctuation-insensitive). This prevents e.g.
    a base 1996 Civic from being flagged classic when only Type R / SiR
    variants are.
    """
    if not (make and model and year):
        return False
    nm_make = _norm(make)
    nm_model = _norm(model)
    nm_trim = _norm(trim)
    for c in CLASSIC_EXCEPTIONS:
        if (
            _norm(c["make"]) != nm_make
            or _norm(c["model"]) != nm_model
            or not (c["year_min"] <= year <= c["year_max"])
        ):
            continue
        entry_trim = c.get("trim")
        if entry_trim is None:
            return True
        if nm_trim and _norm(entry_trim) == nm_trim:
            return True
    return False


def is_desirable_trim(
    *, make: str | None, model: str | None, trim: str | None,
) -> bool:
    """Return True iff make/model/trim matches a DESIRABLE_TRIMS entry.
    Trim 'any' on the entry matches any input trim for that make/model.
    """
    if not (make and model and trim):
        return False
    nm_make = _norm(make)
    nm_model = _norm(model)
    nm_trim = _norm(trim)
    for d in DESIRABLE_TRIMS:
        if _norm(d["make"]) != nm_make:
            continue
        if _norm(d["model"]) != nm_model:
            continue
        if d["trim"] == "any":
            return True
        if _norm(d["trim"]) == nm_trim:
            return True
    return False
