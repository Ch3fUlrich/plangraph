A cancel whose reason names a D-id now repairs the dependents in the same call: every open
dependent loses its edge to the cancelled item (its other dependencies stay), and the owner
role (l1) may do this for an open, unheld item. Edges the cancel cannot repair (an operator
item a lane waits on, a claimed or in-review dependent, an edge that would orphan work) are
kept and named (`kept`; the l1 call is refused naming them). `depends_on` takes a
`{add, remove}` form in `edit_definition`, so a gate over the 200-entry list cap can drop one
edge without passing the whole list. A define under a gate with a standing
`depends_on_cancelled` edge is accepted and reports it. `pg withdraw` usage says in_review only.
