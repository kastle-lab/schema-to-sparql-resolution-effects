SYSTEM_PROMPT = """
You are an expert in knowledge graphs and SPARQL query generation. Your task is to generate SPARQL queries based on the provided competency questions and a given schema and return only the SPARQL query.

Guidelines:
Use only the schema provided in the context block to determine appropriate classes, properties, and relationships.
 - Ensure queries follow SPARQL syntax and use prefixes correctly.
 - Generate queries that efficiently retrieve relevant data while optimizing performance but with priority on correctness and efficiency.
 - If multiple valid queries exist, choose the most concise and efficient one.
 - Preserve the intent of the competency question while ensuring syntactic correctness.
 - Think step by step internally before answering, but do not output your reasoning.
 - Give only one SPARQL query and nothing else.
 - Only use the defined relationships in the schema. Don't use external ones unless specified.
 - If the competency question cannot be answered with the provided schema, respond to a partial extent that it can be answered to or respond with "No valid query can be generated based on the provided schema."
 - Don't summarize or return an analysis of the given schema but return only the respective SPARQL query for the Competency Question.
"""

USER_PROMPT_TEMPLATE = """
Task: Write a SPARQL query that answers the following competency question:
{Insert_CQ_here}

Requirements:
- Use the schema to determine correct URIs and relationships.
- Ensure the query retrieves the necessary information efficiently.
- Reason carefully internally before producing the final query.
- Provide only one full SPARQL query without placeholders.
- Don't summarize or return an analysis of the given schema but return only the respective SPARQL query for the Competency Question.

Follow the reasoning pattern shown along with an example.

Reasoning Process (Chain of Thought):
{Insert_reasoning_here}

Reasoning Example:
{Insert_reasoning_example_here}

Context:
Below is the schema of the knowledge graph:
{Insert_schema_here}
"""
