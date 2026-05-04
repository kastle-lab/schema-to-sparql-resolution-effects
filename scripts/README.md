# Scripts

This directory contains helper scripts and notebooks for preprocessing schemas, running experiments, and evaluating generated SPARQL.

## Directory and File Overview

* `automate_latex_to_dl.py` — Batch runner that converts each `schemas/<schema>/schema.tex` file into `axiom.txt` using the matching `schema.ttl` prefixes.
* `exp_pipeline.ipynb` — Notebook for loading schemas, competency questions, prompt templates, and prompt assets, then preparing experiment batches.
* `generate_mock_instances.py` — Generates synthetic RDF/Turtle instance data for each schema in `schemas/*/schema.ttl`.
* `latex_to_dl.py` — Converts LaTeX description logic axioms into plain text, optionally adding prefixes from a Turtle schema.
* `sparql_evaluator.ipynb` — Notebook for checking generated SPARQL syntax, endpoint satisfiability, determinism, result rows, and summary metrics.
* `README.md` — Readme file for this directory.

## Expected Input Structure

Some scripts and notebooks expect a local `schemas/` directory at the repository root. The expected layout is:

```text
schemas/
  <schema_name>/
    schema.ttl
    schema.tex
    axiom.txt
```

`schema.ttl` contains the ontology/schema in Turtle format, `schema.tex` contains exported LaTeX description logic axioms, and `axiom.txt` is produced by the conversion scripts.
