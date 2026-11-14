# Premise precision and reconfirmation

Decision indexing distinguishes unchanged orders from scope changes using the
DECISION cell. A leading ORDER/ORDERED, GO, DEFINE/DEFINED, or explicit
`Class: ordered|go|defined` is exempt from newer-decision staleness when no scope
change is present. Item source decisions already cited in the definition remain
part of its premise watermark. Scope changes, replacements and cancellations
take precedence over order classification and logging grace.

For an otherwise unclassified row, `Session: NAME; At: ISO-8601` permits up to
two hours of logging delay after the latest definition by that same actor.
Missing/unparseable metadata, another session, negative delay, or a delay over
two hours receives no grace. The keeper does not invent attribution from a
nearby timestamp. Older rows without metadata still use the conservative gate.

`pg reconfirm PROJECT:KEY D-NNN` records that the current premise was read
against a recorded, non-retired decision. Router and the project's L1 may call
it. The definition and description remain unchanged; the append-only
`premise_reconfirmed` event and ordinary attempt audit record the caller.
The event binds to the latest definition sequence and decision-cell digest.
A definition edit or decision rewrite invalidates it; newer decisions still
refuse. This grants no merged-work, PR, claim, review or closure exemption.

`force_stale` remains available with its audited reason. Keeper alerts JSON
includes the lifetime `force_stale_claims` count for the requested project scope;
text alerts include that count when it is nonzero.
