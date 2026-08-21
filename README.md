# Schema to SPARQL Resolution Effects

This repository contains prompt templates, prompt assets, helper scripts, notebooks, and evaluation outputs for experiments on how schema representations affect SPARQL query generation from competency questions.

## Directory and File Overview

* **cqs/** — Competency question lists used as SPARQL generation tasks. Each file contains 15 CQs ordered by complexity: rows 1-5 are simple, rows 6-10 are moderate, and rows 11-15 are complex.
* **eval/** — Evaluation outputs and cross-model summaries used for analysis.
* **prompt_assets/** — JSON prompt support assets used by the SPARQL generation workflow.
* **prompts/** — Python prompt template modules used to assemble LLM requests for SPARQL generation.
* **results/** — SPARQL generation outputs.
* **schemas/** — Schema representations and synthetic instance data used in the experiments.
* **scripts/** — Helper scripts and notebooks for preprocessing schemas, running experiments, and evaluating generated SPARQL.
* `.gitignore` — Ignore rules for local, generated, cache, environment, and notebook artifacts.
* `LICENSE` — License for the repository.
* `README.md` — Main documentation file for the repository.