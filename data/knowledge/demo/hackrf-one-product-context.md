# HackRF One product and BOM provenance

## Source boundary

This is an original, short SupplyAgent summary of an open-source hardware design. The
authoritative design files are the HackRF repository at commit
`7f96cc8e3fa625c4263a71ba8dd44d1f6110e4fa`, which is also the commit recorded in
this repository's read-only `data/hardware/raw/hackrf-one/MANIFEST.json`.

It is not a statement that every manufactured HackRF One has this exact BOM. A Mission
must identify the board revision before it uses any part-level conclusion.

## Product context

HackRF One is a USB wideband software-defined radio platform. The local source package
contains the product schematic and its child schematic sheets. SupplyAgent derives the
local `hw_part` rows from those pinned KiCad files; it does not manually type a BOM.

## Relevant component relationship

The derived HackRF One BOM contains Skyworks `SKY13453-385LF` with quantity 7 per board.
The upstream project describes hardware revisions where the RF switch was changed between
SKY13350 and SKY13453. Therefore a part-number match alone is insufficient proof that an
alternative is approved for a customer's requested board revision.

## What this document cannot establish

This document does not establish current stock, price, delivery date, lifecycle status,
or substitute compatibility. Those require dated distributor or manufacturer evidence and,
where applicable, engineering and commercial review.
