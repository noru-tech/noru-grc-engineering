# .NET source support

The collector reads declarations and modeled references offline. It does not build a solution,
restore packages, evaluate MSBuild, run source generators, load assemblies, contact databases or
infer privacy categories from C# property names. This is a bounded structural reader, not Roslyn
or a general C# semantic analyzer. `scripts/test_dotnet.py` exercises its supported cases and gaps.

## Discovery

`.csproj`, `.sln` and `.slnx` identify project boundaries and references. `Directory.Packages.props`
and other `.props`/`.targets` XML supply declared dependency signals. Source signals are labeled
separately from dependency declarations. Neither alone proves runtime use. Missing project
references, shared source directories with multiple projects, conditional/imported MSBuild settings
and explicit Compile configuration require investigation. Project references allow resolution of
models in an explicitly referenced in-repository project; ambiguous model names are not selected.

The normal tracked-file, test-fixture and migration filters apply. `bin` and `obj` are excluded.
Comments and strings are not model declarations. Interpolated/raw strings are opaque and cannot
supply literal storage names. Nullable, warning and region directives are skipped; conditional or
unsupported preprocessor source and malformed literals/declarations produce coverage gaps.
Source-declared enums resolve as scalar fields; missing or ambiguous enum types remain gaps.

## Fields and relationships

| Source pattern | Extracted structure | Identity and limits |
|---|---|---|
| EF `DbContext` with auto-property `DbSet<T>` | Referenced model's public get/set or get/init properties, `Table`/`Column` names, `NotMapped` exclusions | Context-local modeled store; a literal `GetConnectionString` key inside `AddDbContext` with `UseSqlServer`/`UseNpgsql`, or `AddNpgsqlDbContext`, associates contexts within the registration project. Explicit project references resolve cross-project context registrations. Straight-line Aspire `AddPostgres`/`AddDatabase`/literal-path `AddDotnetProject`/`WithReference` wiring can establish a host-scoped logical reference. Connection values are not emitted. |
| MongoDB local `db = client.GetDatabase("name")` followed in the same block by `db.GetCollection<T>("name")` | Typed document fields, `BsonElement`, `BsonId`/conventional ID, `BsonIgnore`, supported nested records/classes | Local receiver scope identifies the modeled reference. Reassigned or unresolved database receivers require investigation. |
| `DynamoDBTable("name")` | Model properties, explicit property/key names and `DynamoDBIgnore` exclusions | Each attributed model retains its own provisional identity; equal table names do not prove a common AWS account, region or client. |
| Dapper `Query<T>`/`QueryAsync<T>` with a literal simple `SELECT columns FROM table` | Typed result when the named projection matches its properties | Query-local modeled reference. SQL connection identity and completeness of the underlying schema still require investigation. Joins, computed projections, dynamic SQL and other queries are not reconstructed. |
| Current SQL DDL | Declared columns; bracketed and schema-qualified SQL Server identifiers | Schema qualification is retained, so two schemas' same-named tables remain distinct. Migrations are not a current schema. |

Nullable type annotations are preserved as source facts, not interpreted as proof of database
constraints. Primitive arrays retain their element type; supported document members enter the
existing recursive classification review. Dictionaries, dynamic objects, recursive/unresolved types
and unknown additional content remain unresolved. Ordinary DTOs without a supported persistence
binding do not become datasets.

Properties with initializers retain their declared types; initializer values are not exported.
Source-defined base members retain their origin evidence. Simple field-backed get/set accessors
are supported. Identity contexts expose custom user fields provisionally under their model name;
framework fields require source or a supported version-specific contract; physical table names
require separate mapping evidence.
Partial/generic models, ambiguous or external inheritance, other custom accessors, unsupported attributes, converters, aliases,
shadowed framework symbols and custom serialization/convention configuration produce gaps.
Explicit `ApplyConfiguration(new T())` registrations support one `IEntityTypeConfiguration<T>`
per entity: literal `ToTable`/`HasDefaultSchema`/`HasColumnName`, `Ignore`, declared backing fields
selected by `Property`/`HasField`, explicitly typed shadow properties, simple `OwnsOne` using
`Navigation_Property` columns, and resolved enum `HasConversion<string>()`. Mapping evidence is
retained on fields and relationships. Unapplied configurations do not supply mappings. Conditional
builder statements, multiple configurations, owned callbacks, custom converters and unsupported
operations remain gaps; relationship/key/index configuration still needs investigation. Declared fields can still appear as provisional observations, but a gap is not closed
by approving those fields. Investigate the source and extend supported extraction where necessary;
do not describe unsupported mappings as complete.

## Review and physical identity

The review shows project/dependency evidence and model-to-collection relationships with citations.
Every automatically discovered relationship is labeled `modeled_reference`, with physical identity
`unverified`. Separate logical references may reach one physical store, and one configuration key
may have different deployment values. Confirm that distinction from evidence before describing a
production inventory. No endpoint credentials or connection-string values are copied into output.

A single reviewed schema binding in the existing connection graph overrides the discovered dataset
identity. Multiple reviewed bindings for one C# file require per-relationship investigation. Fields,
nested members and extraction gaps use the existing classification contract; C# and project files
participate in source freshness. Acceptance and publication retain the existing gates. Retention
handling is outside this feature.

## References

- [EF Core entity properties](https://learn.microsoft.com/en-us/ef/core/modeling/entity-properties)
- [MongoDB POCO mappings](https://www.mongodb.com/docs/drivers/csharp/current/serialization/poco/)
- [DynamoDB .NET mapping attributes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/DeclarativeTagsList.html)

Extraction is only the first validation stage. Fields still need classification, processing-purpose
and data-flow review, resolution of actual uncertainties, and named-owner acceptance. Successful
extraction does not validate preserved draft manifests, and acceptance does not close extraction gaps.

## Registration traversal and adapters

Entity discovery is rooted in a recognized EF context. It combines `DbSet<T>`,
`ModelBuilder.Entity<T>()` (including simple entity callbacks), and explicitly applied
`IEntityTypeConfiguration<T>` implementations. Source-defined context inheritance and explicit
`base.OnModelCreating` calls retain provenance. Unused configuration classes cannot create datasets.
The walker follows unambiguous, explicitly called static extension methods whose only parameter is
`this ModelBuilder`; it follows evidenced project references and namespace imports. It retains the
registration/call chain, detects cycles, and limits traversal to 16 levels and 128 calls per context.
Overloads, conditional statements, unsupported arguments and unresolved symbols remain diagnostics.
This does not evaluate arbitrary helper methods or runtime control flow.

`dotnet_adapters.py` defines registration adapters separately from discovery. EF options registrations
and Aspire Npgsql registrations produce runtime-project → context → logical-connection → datastore
traces. Runtime-conditional registrations retain their conditions and remain unresolved; they are not
assumed to describe the production branch. Preprocessor/build-source uncertainty has a separate
category. Conditional project references do not establish source membership. Unsupported configuration
still needs evidence; logical names never verify physical database identity.

Source-defined generic base parameters are substituted where they can be bound unambiguously.
External model metadata requires available source or an explicit version-specific contract. The
bundled IdentityUser contracts cover exactly versions 8.0.0 and 10.0.0, identified by literal package
references or an unambiguous nearest Directory.Packages.props declaration. Conditional definitions,
other versions, unresolved imports and conflicting versions do not select a contract. The metadata
records the public source URL, source hash and MIT license; it supplies CLR fields, not classifications
or a complete Identity storage schema. Framework table mapping and other unavailable definitions
remain review gaps. The contracts are data, not assemblies or executable target dependencies.

Fields distinguish `clr_type`, modeled `storage_type`, and serialization attribute observations.
Literal supported `HasColumnType` declarations supply an explicit database type; otherwise native
provider types remain unestablished. A custom converter leaves the stored representation unknown.
Validation and JSON serialization attributes do not rename or remove relational columns.
Explicit navigation/foreign-key and owned-object relationships are reported separately from scalar
columns; foreign-key member references identify CLR members. Unsupported relationship semantics,
conflicting inclusions/exclusions, owned callbacks and separate owned tables remain diagnostics.

## Diagnostic and review accounting

Each .NET diagnostic has a stable rule code and diagnostic ID, source evidence, an affected scope,
and a resolution requirement. Categories distinguish missing structure, ambiguous binding,
unsupported semantics, and source-membership uncertainty. Repeated occurrences of the same rule in
the same scope are grouped, with every occurrence and source span retained. Grouping is not resolution.
IDs do not incorporate line numbers or source hashes; evidence and freshness bindings do.

`diagnostic_summary.diagnostics` counts grouped items; `occurrences` counts their underlying source
occurrences, and `by_category` partitions the grouped count. The collector coverage list and canonical
review use the same diagnostic IDs. Review-gate totals also include non-.NET and semantic issues, so
those totals are not interchangeable with .NET extraction counts. Classification and human acceptance
remain separate; new structure and changed evidence cannot be accepted by clearing extraction flags.

## Integration comparison

Use small synthetic tests for feature guarantees (`scripts/test_dotnet_generic.py`) and more than one
repository for integration checks. The comparison tool accepts arbitrary repository paths and compares
their checked-out source against a specified toolkit revision, without running target code:

```bash
python3 scripts/compare_dotnet_extraction.py --baseline-ref <toolkit-commit> \
  --repo /path/to/first-repository --repo /path/to/second-repository \
  --output /tmp/dotnet-comparison.json
```

The report records repository revisions, source digests, extraction counts, before/after diagnostic
and relationship evidence, and repeat-run determinism. It excludes generated/test/migration source
and reports excluded or unavailable paths as a count. Sparse checkouts cover only available files.
No fields, model names or connections from an integration repository are extraction rules.
