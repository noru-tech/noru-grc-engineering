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
framework fields and physical table names require source/schema evidence and are not synthesized.
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
