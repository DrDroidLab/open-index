### Entity-management

A doc_type is an empirical representation of the type of objects you want to store in your brain. An entity is the actual instance of that doc_type that you're storing within the brain.

The different ways you can create entities are:
1. Through manual entry/upload from UI
Just create a simple JSON object that representing the entity you want to store in the brain.

2. By talking to an agent that's connected to the brain via it's CLI / MCP server.


3. Through a webhook/API trigger from a script on your end
4. Through a recurring cron defined in the brain configuration


Note: If you'd like to extract/distill information from unstructured documents into entities in the brain, there are two recommended approaches:
- Github Repo based knowledge bases: Connect an agent to the brain via an MCP server and prompt it to extract information from the repo
- Commercial tools based knowledge bases (e.g. Notion, Confluence, etc.): Connect an agent to the brain via an MCP server and to the respective tool via MCP server/CLI
- Use a script that connects to the brain via an API and extracts information from documents



Now, you add MCP servers to the brain.
For each of these MCP servers, you define what is the data you extract from a given MCP server and what doc_type you want to store it in.
Then you run it for the first time to generate the entities for that doc_type. You can also define each of these entities to be generated at a given cron.

If you want to manually add entities to a doc_type, you can do so from the UI, API or using the MCP Server. Each entity to be added needs to be a valid JSON.

Distilled knowledge:
- If you want the brain to have knowledge distilled from your existing knowledge base, the recommendation is to connect that MCP server (which has the data) to a claude code instance


Recommendation on entity creation:
Typically, we recommend creating doc_types that have new instances (entities) added at a human pace (e.g. once a day, once a week, etc.) rather than real-time pace.

For example:
1. Kubernetes deployments is a great doc_type to have in the brain but creating an entity for each pod is not recommended. Deployments change at non real-time pace where as pods are ephemeral objects.
2. An entity for each customer for a B2C company would not be recommended but could be done for B2B Enterprise Sales business where the entity count for the doc_type "customer" will be in thousands.

The reason for this is that if you create too many entities:
1. It can lead to information bloat and noise in the brain.
2. It can create overhead of removing the stale entities, which can get challenging over time.

Exceptions:
In some cases, temporal data is valuable and could be relevant for the organisation to keep centrally in the brain:
- Alerts & deployments in a brain that's being used for AI troubleshooting.

In such scenarios, it is recommended to have data that is exponentially decaying w.r.t time. That ways, flushing data will not hurt your brain's quality.