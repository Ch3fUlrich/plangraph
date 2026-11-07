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
Envelopes**: O1–O4 prevent implementing `Transport::read` with them alone.
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
authorized by V1; this is OPEN O1–O4, not an empty/no-op mutation.
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
5. **O5 — ANSWERED:** SDK response fields and snapshot pinning are documented
   below; implementing them is separate from this docs-only contract.
6. **O6 — ANSWERED (grammar and limits):** Verified examples below establish
   keyed insert/upsert and edge operations, but disprove atomic delete plus
   insert in one mutation. The required lossless remember/update remains
   blocked by O1–O4 and that transaction limitation; no write authorization.

### Primary-source evidence for O5/O6 (offline, round 2)

Source roots (read-only cached packages; no server called, D-3895):
`SDK = the npm-cached Omnigraph 0.13 SDK package (path under the npm cache; cite files relative to the package root)`
and sibling `MCP = ../omnigraph-mcp` relative to SDK.
SDK `package.json:1-3` identifies version 0.13.0; `README.md:3-4`
identifies the HTTP 0.13 contract. Citations below are relative to these roots.
The installed packages contain dist/README files, not standalone tests or
grammar sources; MCP bundles the upstream authoring examples as strings.

#### O5 — exact response fields, completeness, consistent reads

* Query wire response: `graph_commit_id?: string | null`, `query_name`,
  `row_count`, `rows`, `target`, optional `columns`; **no receipt field**
  (`SDK/dist/index.d.ts:842-856`). The ID and rows come from one pinned
  version (`:845-849`). Public SDK result is `graphCommitId`, `queryName`,
  `rowCount`, `rows`, `target`, `columns` via `Camelize` (`:1008-1011`,
  `:1028-1029`; `SDK/README.md:41-43`). Reject missing/null commit IDs for
  the memory read-modify-write boundary rather than fabricating one.
* Mutate wire response: `commit: null | CommitOutput`, `affected_nodes`,
  `affected_edges`, `branch`, `query_name`, optional `actor_id`/`outcome`
  (`SDK/dist/index.d.ts:265-289`). The receipt is **the `commit` object**,
  not a field named `receipt`. It contains `graph_commit_id`, `created_at`
  (Unix microseconds), `graph_manifest_version`, optional `actor_id`,
  `graph_branch`, `parent_commit_id`, `merged_parent_commit_id`
  (`:335-346`). SDK spelling is `commit.graphCommitId`, `createdAt`,
  `graphManifestVersion`, etc. (`:1016-1018`). For data mutations,
  `commit: null` means successful no-op (`:1575-1589`); affected counts
  are not a receipt. Mapping this operation receipt to per-fact saved state
  is still O4, not something returned by recall.
* QueryRequest/ReadOutput have **no pagination/page-token fields**
  (`SDK/dist/index.d.ts:809-856`); do not invent query continuation.
  Change-diff pages instead use wire `next_page_token` (`:323-331`),
  public `nextPageToken`, and request `pageToken` (`:1304-1309`). Feed
  durable `cursor` is different: follow all tokens with unchanged filters
  and branch, omit `start`/`cursor`, and save only the terminal cursor
  (`SDK/README.md:110-123`). These are not recall-query page tokens.
  Exhaustive recall must consume the full unbounded scalar query response
  or fail closed; large full-graph inspection should use export instead
  (MCP bundled authoring guidance, final inspection section).
* Read the first recall query on the intended branch, retain its nonempty
  `graphCommitId`, then send **`snapshot: thatId` and no `branch`** for
  every subsequent recall query. Request `snapshot` and `branch` are
  exclusive (`SDK/dist/index.d.ts:809-840`); snapshot means graph commit ID,
  not the opaque response header (`:1423-1426`; `SDK/README.md:133-136`).
  Require matching response commit IDs throughout. If a pinned read fails,
  abandon the partial set; never fall back to current head. Use the first
  read's ID as `ifGraphCommit` on the eventual conditional mutation
  (`SDK/README.md:65-71`), not a separate branch-head lookup.

#### O6 — verified mutation examples, not approved memory writes

MCP's bundled `QUERIES_MD` export contains sections
“Parameterization”, “Mutations / Insert”, “Insert edge”, “Update”,
“Delete”, and “Multi-statement” are the primary examples below.
All are named `query` blocks, dispatched through mutate, not GraphQL
`mutation` wrappers. Typed values go in `params`, never interpolation.

```gq
query add_signal($slug: String, $name: String, $brief: String,
                 $stagingTimestamp: DateTime, $createdAt: DateTime, $updatedAt: DateTime) {
    insert Signal {
        slug: $slug, name: $name, brief: $brief,
        stagingTimestamp: $stagingTimestamp,
        createdAt: $createdAt, updatedAt: $updatedAt
    }
}
query link_signal_forms_pattern($signal: String, $pattern: String) {
    insert FormsPattern { from: $signal, to: $pattern }
}
```

The same source explicitly says there is **no `upsert` keyword**:
`insert` on a node/edge with `@key` upserts the derived identity.
Without an edge key, repeated inserts create parallel edges; the memory
fixture's hub edges are unkeyed, so endpoint pairs do not imply deduplication.
Edge removal spelling shown there is
`delete FormsPattern where @src = $signal` (all outgoing matching edges),
inside a named `query` declaring `$signal: String`. This is not a verified
single-pair deletion example; do not silently narrow or expand its scope.
Exact multi-statement example from that same source:

```gq
query add_and_link($slug: String, $pattern: String, $createdAt: DateTime, $updatedAt: DateTime) {
    insert Signal { slug: $slug, name: $slug, brief: $slug,
                    stagingTimestamp: $createdAt, createdAt: $createdAt, updatedAt: $updatedAt }
    insert FormsPattern { from: $slug, to: $pattern }
}
```

This is constructive and atomic (`SDK/dist/index.d.ts:1575-1576`).
The MCP “D₂ rule” explicitly refuses mixing delete with insert/update
before any effects, even if lint passes. Delete/reinsert requires **two
separate mutations**, not an atomic replacement. Edge update is unsupported;
keyed edge reinsert can update non-key properties. Receipt return is the
HTTP envelope described in O5, not a GQ `return` clause. The cached examples
verify String/DateTime typed binding but do not establish a named enum
parameter type; do not invent one for memory status/severity. Inline enum
schema syntax is documented in MCP's bundled schema authoring examples.

### Operator decision packet — O1–O4 remain OPEN

All blocks below are **PROPOSAL — operator approval required**, not deployed
schema, fixture edits, migration approval, or write authorization. One
operator decision can accept/reject this additive companion representation;
existing keyed fact types and hub edges remain unchanged. New types avoid
unsupported in-place key changes (MCP bundled schema authoring examples, “Keep
@key stable”). Names are proposed schema identifiers, never caller values.

**O2 PROPOSAL — identity:** introduce a companion authoritative record;
retain caller slug separately from its composite derived identity. `kind`
must be validated to the four allowed kinds; project targets use Project.slug.
Existing fact nodes become projections, not authoritative cross-project keys.

```pg
node MemoryRecord {
  project: String
  kind: String
  slug: String
  content: String
  @key(project, kind, slug)
}
```

**O1 PROPOSAL — provenance/version:** include these nullable additions on
MemoryRecord; enforce complete presence for new V1 records and reject legacy
records missing metadata rather than synthesizing provenance.

```pg
// Properties inside MemoryRecord
version: I32?
sourceType: String?
sourcePath: String?
sourceHash: String?
actor: String?
```

**O3 PROPOSAL — scope/relations:** owner scope is MemoryRecord.project;
automatic hub links are projections only. Store the exact ordered relation
pair vector as canonical JSON in one scalar, preserving duplicates and empty
vectors; decode/validate before recall. No extra target changes owner scope.

```pg
// Property inside MemoryRecord; canonical array of {edge, target_slug}
relationsJson: String?
```

**O4 PROPOSAL — saved state:** a companion keyed state record can store an
explicit superseded flag and a serialized exact commit receipt per record.
It must not be populated with a guessed future receipt: publication returns
the receipt only afterward. Operator must approve a conditional follow-up
state write/reconciliation protocol; this is not an atomic saved-state claim.
Decision.status and Supersedes remain unrelated unless separately approved.

```pg
node MemorySavedState {
  project: String
  kind: String
  slug: String
  receiptJson: String
  superseded: Bool
  @key(project, kind, slug)
}
```
