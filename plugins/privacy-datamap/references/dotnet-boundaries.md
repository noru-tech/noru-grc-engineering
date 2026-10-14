# Authentication, message envelopes and provisioned runtimes

The offline adapters expose `dotnet.authentication_tickets`, `message_envelopes` and
`provisioned_runtimes`. Definitions enter the ordinary datasets/tables and system-flow models;
all extracted fields still require canonical classification. No target application is executed.

Authentication support uses exact OIDC package evidence and the pinned ASP.NET source contracts
in `dotnet-boundary-contracts.json`. Literal cookie schemes with an evidenced OIDC or explicit
sign-in producer can supply a ticket schema. Separate schemes retain separate boundaries.
The adapter follows unique source helper names from runtime entrypoints; ambiguous or unestablished
activation remains a diagnostic. Ticket identities, claim records and property dictionaries remain
open where the serializer permits variable content. Saved-token property keys are optional.
Issuer profile claims are not copied into every client's ticket.

Custom ticket stores/formats, events, transformations, dynamic names, conditional registrations and
unavailable contract versions require further investigation. A server ticket store is reported as
such; its backend is not invented. Current support does not prove effective claim contents or
framework runtime patch identity from an SDK target alone. Review the framework/package evidence,
issuer scopes, claim actions, consumers and customization before resolving content diagnostics.

RabbitMQ propagation extraction separates the header envelope from application bodies. An exact
supported OpenTelemetry package and an explicit built-in propagator with a supported direct header
setter can establish fixed header members. Supported setters include direct assignment and lazy
dictionary initialization. Baggage remains an open string map with encoded wire content.
For injected helpers, the adapter follows source-defined constructor parameters and read-only
default-propagator properties across explicit project references. It requires a supported pinned
hosting contract and an invoked setup method that initializes the SDK before registering both
singleton constructors. Primary constructors are supported. Every referencing runtime must have a
supported binding; unknown versions, conditional activation and reachable overrides remain blocking.
The global API default and SDK-initialized default differ; a property reference alone is insufficient.
The supported consumer decodes header byte arrays as UTF-8; custom decoding retains a diagnostic.
Broker-property observations remain source facts; backend persistence is not inferred
from an envelope alone.

Aspire YARP resources become provisioned runtime systems, independently of the AppHost provisioner.
Supported literal AddDotnetProject destinations, direct WithConfiguration routes and unique invoked
Yarp-resource helper definitions produce forwarding edges. Literal remove-prefix transforms remain
in route evidence. Dynamic destinations and missing helper definitions retain diagnostics. No proxy
datastore is created. More complex helper chains, generated project types and custom transforms
require investigation or an additional adapter.

Supplemental systems can use `meta.noru.origin: provisioned`, with `meta.noru.provisioner` identifying
an evidenced runtime and `meta.noru.refs` citing provisioning code. Register those references as
structural evidence dependencies. They need not be mislabeled as external recipients.

Regression fixtures: `scripts/test_dotnet_boundaries.py`; open-structure/gate tests:
`scripts/test_gap_resolution.py`. Framework contracts contain upstream source hashes and licenses;
scans read the bundled metadata offline and do not fetch framework sources.
