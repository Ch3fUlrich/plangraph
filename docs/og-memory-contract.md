# Durable memory envelope v1

Offline `validate` alone constructs `Validated`, retaining source/actor provenance.
| Kind / target type | Property values | Project hub edge |
| --- | --- | --- |
| Decision | slug ← slug; title ← slug; rationale ← content; status ← accepted; date ← empty string | DecidedIn |
| Rule | slug ← slug; statement ← content; severity ← must | ConstrainsProject |
| Convention | slug ← slug; name ← slug; example ← content | AppliesTo |
| Preference | slug ← slug; statement ← content; scope ← project | none |
| Project hub | slug ← project | — |
`global` is allowed; slugs use lowercase ASCII letters/digits and single internal dashes.
Task/routing-* always fail; `sync-memory-graph --load` alone writes routing records.
Live-schema capabilities gate the sole V1 property table; Preference refuses hub links.
All text uses public worker-core redaction; refusals never retain rejected input.

## GQ mapping (V1)

Authority: `rust/og-memory/src/lib.rs` (`Envelope`, `Source`, `Relation`,
`V1`, `validate`), `rust/og-memory/src/io.rs` (`Transport`, `Saved`,
`Snapshot`), and `rust/og-memory/tests/fixtures/memory.pg`.
This is a partial schema mapping, **not permission to enable writes**.
OPEN items below block OG-MEM-IO-WIRING from claiming durable round trips.
No live Omnigraph calls are needed or permitted for this contract (D-3895).

### Fields, keys, edges, and provenance

All fact types have `@key(slug)`; Project has `@key(slug)` too.
The transport's logical identity is `(project, kind, slug)`, not just slug.
Every row below inherits that unresolved identity mismatch (OPEN O2).
“None” means no fixture property exists, not permission to discard the field.
Provenance is caller-supplied, validated/redaction-checked and retained in
`Validated.envelope`; the fixture cannot persist it (OPEN O1).

| Envelope field | Fixture node.property / value | Node key | Edge, direction (GQ spelling) | Provenance / gap |
| --- | --- | --- | --- | --- |
| `version` | None; validation accepts only `1` | None | None | Protocol discriminator; O1: reconstruction/version storage |
| `project` | `Project.slug = project`; `Preference.scope = project` | Project: project; Preference: slug | Decision → Project `decidedIn`; Rule → Project `constrainsProject`; Convention → Project `appliesTo`; Preference: none | Caller scope; O2/O3 |
| `kind` | Selects `Decision`, `Rule`, `Convention`, or `Preference`; no property | Selected type: slug | Selects the hub edge above | Caller discriminator; recoverable from node type |
| `slug` | `Decision.slug`, `Rule.slug`, `Convention.slug`, `Preference.slug`; also `Decision.title` and `Convention.name` | slug, within selected type | None independently | Caller key; title/name are derived copies |
| `content` | `Decision.rationale`, `Rule.statement`, `Convention.example`, `Preference.statement` | Selected type: slug | None | Caller text; source/actor are not encoded in it |
| `source` | None (container for the following three fields) | None | None | O1: preserve the complete container |
| `source.type` | None | None | None | Caller source category; O1 |
| `source.path` | None | None | None | Caller source locator; O1 |
| `source.hash` | None; nonblank required by validation | None | None | Caller source digest; O1 |
| `actor` | None | None | None | Caller attribution; O1 |
| `relations` | None (ordered vector of the following pair) | None | Zero or more validated fact → Project links | Caller relations versus automatic hub link cannot be recovered; O3 |
| `relations[].edge` | No node property; fixture declaration selected by validator | Fact slug + target Project slug (no fixture edge key) | `DecidedIn` → `decidedIn`, `ConstrainsProject` → `constrainsProject`, `AppliesTo` → `appliesTo`, all fact → Project | Input uses PascalCase declaration names; O3 covers ordering/multiplicity |
| `relations[].target_slug` | Target `Project.slug` | Target Project: target_slug | Same outgoing edge as preceding row | Caller target; not necessarily envelope.project; O3 |

The three allowed hub edge declarations are respectively
`DecidedIn: Decision -> Project`, `ConstrainsProject: Rule -> Project`, and
`AppliesTo: Convention -> Project`. GQ uses their lowerCamelCase names.
Validation rejects other edge endpoints, even though the fixture declares
other relational edges. Preference has no allowed fixture edge to Project.
Do not widen acceptance to `Affects`, `Supersedes`, or other fixture edges.

Derived defaults are `Decision.status = accepted`, `Decision.date = ""`,
and `Rule.severity = must`. There is no provenance in those defaults.
`Decision.embedding` is optional, annotated `@embed("rationale")`, and is
not an Envelope field or a caller parameter. No synthetic source, actor,
receipt, timestamp, superseded flag, or project property may be written.

### Parameterized recall projections (query route)

Bind `$project: String` to the validated caller scope, never interpolate it.
These are exact scalar projection blocks using the repository's GQ
`match`/`where`/`return` form. They return candidates, **not complete
Envelopes**: O1–O5 prevent implementing `Transport::read` with them alone.
Hub-linked candidates must not be assumed to belong exclusively to that hub.

```gq
query memoryRecallDecisions($project: String) {
  match { $n: Decision $n decidedIn $p: Project }
  where { $p.slug == $project }
  return { $n.slug, $n.title, $n.rationale, $n.status, $n.date }
}
```

```gq
query memoryRecallRules($project: String) {
  match { $n: Rule $n constrainsProject $p: Project }
  where { $p.slug == $project }
  return { $n.slug, $n.statement, $n.severity }
}
```

```gq
query memoryRecallConventions($project: String) {
  match { $n: Convention $n appliesTo $p: Project }
  where { $p.slug == $project }
  return { $n.slug, $n.name, $n.example }
}
```

```gq
query memoryRecallPreferences($project: String) {
  match { $n: Preference }
  where { $n.scope == $project }
  return { $n.slug, $n.statement, $n.scope }
}
```

Recall here is exhaustive scalar traversal, not semantic search. No nearest
or bm25 search is specified. Any future nearest/bm25 block MUST declare
typed search parameters and end with an explicit trailing `limit`; a ranked
subset cannot substitute for the snapshot used by remember's equality check.
Do not silently truncate a snapshot; fail closed on incomplete responses.

### Remember and update (mutate route): blocked specification

There is **no fixture-backed, lossless remember or update GQ block**.
Publishing an executable mutate block now would silently drop source/actor
and relations information or invent properties/types. Therefore none is
authorized by V1; this is OPEN O1–O6, not an empty/no-op mutation.
The next lane must resolve these questions before enabling transport writes,
then add exact `query name($p: T) { ... }` mutate blocks with every parameter
declared. Query names do not imply read-only execution: HTTP route selects
query versus mutate. Values must always be bound parameters; schema names
must come from a fixed validated mapping, never caller interpolation.

Required semantics once resolved: atomically upsert the fact, Project hub,
and complete edge set while preserving provenance and logical identity.
The same identity with changed content/provenance/relations is an update,
not an append or a blind overwrite of another project's fact. Equal facts
including relations skip the entire commit. No separate supersede/update
API is established by the fixture; do not infer one from `Supersedes`.

### HTTP 0.13 and read-modify-write preconditions

`origin/lane/og-mem-io-3:rust/og-memory/src/http.rs` establishes a graph-scoped
base route, `POST <base>/query` and
`POST <base>/mutate/if-graph-commit`, with JSON `{query, params}` and
`omnigraph-http-api: 0.13`. Send `Omnigraph-If-Graph-Commit` only for a
read-modify-write mutation, set to the exact nonempty `Snapshot.graph_commit`
from its preceding read. It is not an Envelope field or a GQ parameter.
Recall/query does not send it. All current `Memory::remember` commits are
read-modify-write, including first insertion, so all require it; the boundary
exposes no unconditional mutate fallback.

A **412 means no effects** on nodes or edges: return `Conflict`, re-read the
snapshot, recompute equality/update, and use the new commit on the next
attempt (at most three attempts in `Memory::save`). Never retry with the old
commit or drop the precondition. A lost/malformed mutation response is
`Unknown`, not a retry instruction: read back and reconcile, never replay
automatically. Successful mutation also requires readback and a matching
receipt. `Unverified` may mean committed; it is not rollback.

### OPEN questions (blocking wiring)

1. **O1 — provenance/version:** What approved fixture schema additions or
   durable metadata mechanism store and retrieve `version`, `source.type`,
   `source.path`, `source.hash`, and `actor` losslessly? The fixture has none.
2. **O2 — identity:** How is `(project, kind, slug)` represented when all fact
   keys are only `slug` and the fixture says one Project per graph? Are
   projects isolated graphs, or is an approved composite-key schema needed?
   How are cross-project target slugs resolved without changing caller slugs?
3. **O3 — relations/scope:** How are the owner project, automatic hub link,
   and explicit `relations` distinguished, with vector order and duplicate
   multiplicity preserved? Extra hub targets otherwise leak recall scope;
   the fixture has no relation provenance or ordering properties.
4. **O4 — saved state:** Where do `Saved.receipt` and `Saved.superseded` come
   from for every fact, including non-Decisions? Does Decision's
   `status = superseded` or the `Supersedes` edge participate, and if so how?
   Neither alone defines the transport's per-fact saved state.
5. **O5 — snapshot:** Which HTTP response fields provide `graph_commit` and
   receipt, and how do the four recall queries observe one consistent,
   exhaustive snapshot? The boundary returns untyped JSON; the fixture
   cannot answer response shape, pagination, or snapshot consistency.
6. **O6 — atomic mutation grammar:** What verified Omnigraph 0.13 GQ syntax
   performs keyed node upsert, edge replacement/deduplication, enum binding,
   and receipt return in one transaction? A schema declaration does not
   establish mutation grammar. Supply an offline parser/fixture example and
   the resolved O1–O5 mapping before publishing remember/update blocks.
