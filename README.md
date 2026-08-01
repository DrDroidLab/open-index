### Droid Brain


The Droid Brain helps you build and maintain accurate organisational information in form of structured data to assist agents to operate in a complex environment effectively.


The primitives of creating a brain are as follows:
- doc_types: the different types of docs that you want to generate and maintain. This can be considered the equivalent of concepts you want to maintain in your knowledge.
- doc_schema: the schema of information stored for a given doc_type
- entity: an instance of a given doc_type, stored as per the schema is an entity
- connectors: the origin from where you extract the information (in case this is extracted from a given source)


# Getting Started

### Creating your first brain
The way the project works is that you run this command and spin up an instance of the droid brain.

### Creating your first doc_type
Then you create the first few types of doc_types. Sample doc types:
- software infrastructure brain: dashboards, metrics, panels, services, products, alert_definitions, runbooks, skills, releases, etc.
- sales brain: leads, deals, accounts, opportunities, meetings, etc.
- lending brain: loans, borrowers, brokers, applications, etc.

### Creating instances of doc_types
You can create instances of doc_types either manually or programmatically:
1. Through manual entry/upload from UI
2. By talking to an agent that's connected to the brain via it's CLI / MCP server
3. Through a webhook/API trigger from a script on your end
4. Through a recurring cron defined in the brain configuration

Read more about this in [Entity Management](./docs/entity_management.md)

# Enabling your LLM/agent to use your brain:

### MCP Server
Run this command to get an MCP server up and running for your brain. This MCP server has multiple tools for the agent to query the brain. Primarily, these three:
1. Brain structure: Gives a textual explanation of the data that's stored within the brain. What doc_types, how many instances of each, example values, description of the doc_type, etc.
2. Search the brain // apply filters
3. Fetch a specific entity

### CLI (experimental)
The same data can be queried using the CLI as well. To do so, generate a key in the platform and authenticate your CLI session in the terminal using that key.

Command to run:
droid-brain 'cli-connect'

# Advanced Capabilities

Once you've setup the brain and it's functionally working, here's where the fun begins:

## Controlling the brain:
There are two parts to how you control the brain:
1. Schema Design
2. Search Design

Schema:
The fields you define within the schema.
- The type of data in that field (string, number, boolean, etc.)
- The processing type of that field (keyword, timestamp, text, etc.)
- The kind of search you want to enable on top of that (semantic, syntactic)

Search:
- You can control the fields within the search tool that the agent has access to.
    - The fields that can be searched on
    - The fields that can be filtered on
    - The fields that can be sorted on
- Entity & Field Boosters - this can help you to give different priorities and weights to different kind of entities within your organisation.
    - Field based boosters
    - Type based boosters
    - Temporal boosters