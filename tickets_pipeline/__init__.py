"""Single source of truth for building web/tickets.json.

Stages: parse -> classify -> override -> validate -> write.
The local run and the scheduled cloud routine both call this package, so the
classification rules and the row schema exist in exactly one place. See
METHODOLOGY.md.
"""
