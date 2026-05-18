"""Lot-first ingester (post-SPA HiBid).

Replaces the discover→scrape two-worker model with a single ingest pass
that walks HiBid's cross-auction `LotSearch` filtered by (province,
vehicle category). Server-side filtering means we only see actual
vehicle lots; auction metadata comes embedded in each lot record so
there's no separate AuctionDetails round-trip.
"""
