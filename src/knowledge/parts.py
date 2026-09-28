"""One row per purchasable part per project: part number, maker, description, quantity
per board, and the designer's alternates note as written.

A part counts when it is in the BOM, has a part number, and is fitted. Board features
(fiducials, holes, logos) have no part number and drop out on their own. Do-not-place is
recognised in the three ways designers mark it — the KiCad attribute, a DNP/DNM
property, or the value itself reading "DNP" — because a part nobody fits is not bought.
Only parts that are certainly electronic components are kept (see is_electronic).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from knowledge.kicad import Placement

#: Property names each project uses for the same thing, compared case-insensitively.
MPN_FIELDS = ("mpn", "part number", "partnumber", "manufacturer part number")
MANUFACTURER_FIELDS = ("manufacturer", "mfr")
DESCRIPTION_FIELDS = ("description",)
ALTERNATE_FIELDS = ("substitution", "alternative", "alternates")
DNP_FIELDS = ("dnp", "dnm")
EMPTY = {"", "~"}

#: Reference prefixes that are certainly electronic components: resistor, thermistor,
#: capacitor, inductor, ferrite bead, diode, LED, transistor, IC, crystal, oscillator,
#: transformer/balun. Connectors (J, P), switches (S, SW), batteries (BT) and mechanical
#: parts (H) are left out, and so is anything not listed: when in doubt, it is not ours.
ELECTRONIC_PREFIXES = {"R", "RT", "C", "L", "FB", "D", "LED", "Q", "U", "X", "Y", "T"}
#: Symbol-library words that mark a part as not purely electronic even under a listed prefix.
#: "switch" is not one: switch ICs live in libraries like "...PowerDistributionSwitches...",
#: and mechanical switches are already out by their S / SW prefix.
NON_ELECTRONIC_LIBRARY_WORDS = ("mechanical", "connector", "battery")


def pick(props: dict[str, str], names: tuple[str, ...]) -> str | None:
    lowered = {key.lower(): value for key, value in props.items()}
    for name in names:
        value = lowered.get(name)
        if value is not None and value.strip() not in EMPTY:
            return value.strip()
    return None


def is_fitted(placement: Placement) -> bool:
    return not (placement.dnp or placement.value.strip().upper() in {"DNP", "DNM"}
                or pick(placement.properties, DNP_FIELDS) is not None)


def is_electronic(placement: Placement) -> bool:
    # Letters before the first digit, e.g. "LED" from "LED3", "RT" from "RT1".
    prefix = re.match(r"[A-Za-z]*", placement.reference).group().upper()
    # The library part of lib_id, e.g. "antmicroMechanicalParts" from "antmicroMechanicalParts:Spacer".
    library = placement.lib_id.split(":", 1)[0].casefold()
    return (prefix in ELECTRONIC_PREFIXES
            and not any(word in library for word in NON_ELECTRONIC_LIBRARY_WORDS))


@dataclass
class Part:
    mpn: str
    manufacturer: str | None
    description: str | None
    quantity: int
    #: The designer's note verbatim, e.g. "any equivalent" or "Murata GRM1555C1H220JA01D".
    alternates: str | None


def build_parts(placements: list[Placement]) -> list[Part]:
    parts: dict[str, Part] = {}
    # A part number with even one placement that is not certainly electronic is dropped whole.
    uncertain: set[str] = set()
    for placement in placements:
        props = placement.properties
        mpn = pick(props, MPN_FIELDS)
        if not (placement.in_bom and mpn and is_fitted(placement)):
            continue
        if not is_electronic(placement):
            uncertain.add(mpn)
            continue
        manufacturer = pick(props, MANUFACTURER_FIELDS)
        part = parts.get(mpn)
        if part is None:
            parts[mpn] = Part(mpn=mpn, manufacturer=manufacturer,
                              description=pick(props, DESCRIPTION_FIELDS), quantity=1,
                              alternates=pick(props, ALTERNATE_FIELDS))
            continue
        if (part.manufacturer or "").casefold() != (manufacturer or "").casefold():
            # One part number, two makers: which one is meant is not ours to decide.
            raise ValueError(f"{mpn}: manufacturer {part.manufacturer!r} vs {manufacturer!r}")
        part.quantity += 1
    return sorted((part for part in parts.values() if part.mpn not in uncertain),
                  key=lambda part: part.mpn)
