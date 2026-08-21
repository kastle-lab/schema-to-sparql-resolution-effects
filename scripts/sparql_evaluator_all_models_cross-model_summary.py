import concurrent.futures
import csv
import hashlib
import json
import os
import re
import glob
import pandas as pd
from pathlib import Path
from itertools import combinations
from typing import Any, Dict, Iterable, List, Optional, Tuple

from SPARQLWrapper import JSON, SPARQLWrapper

try:
    from rdflib.plugins.sparql.parser import parseQuery
except Exception:
    parseQuery = None


# ==============================================================================
# CONSTANTS & CONFIGURATION
# ==============================================================================
FUSEKI_BASE_URL = "http://localhost:3030"  # e.g. "http://localhost:3030"
DEFAULT_ENDPOINT = f"{FUSEKI_BASE_URL}/currkg/sparql"
INPUT_DIR = "results"  # Set to the path where your .xlsx or .jsonl files are located
OUTPUT_DIR = "eval"  # Set to the path where your output should go

# Override any graph-specific endpoints here if a graph lives in a different Fuseki dataset.
ENDPOINT_BY_GRAPH = {
    "default": DEFAULT_ENDPOINT,
    "kwg": f"{FUSEKI_BASE_URL}/kwg/sparql",
    "kwg_lite": f"{FUSEKI_BASE_URL}/kwg_lite/sparql",
    "currkg": f"{FUSEKI_BASE_URL}/currkg/sparql",
    "enslaved": f"{FUSEKI_BASE_URL}/enslaved/sparql",
    "enslaved_wiki": f"{FUSEKI_BASE_URL}/enslaved_wiki/sparql",
    "gbo": f"{FUSEKI_BASE_URL}/gbo/sparql",
    "gmo": f"{FUSEKI_BASE_URL}/gmo/sparql",
    "core_scholar_rich": f"{FUSEKI_BASE_URL}/core_scholar_rich/sparql",
    "core_scholar_shallow": f"{FUSEKI_BASE_URL}/core_scholar_shallow/sparql",
}

SCHEMA_PREFIX_FILES = {
    "kwg": "schemas/kwg/schema.ttl",
    "kwg_lite": "schemas/kwg_lite/schema.ttl",
    "currkg": "schemas/currkg/schema.ttl",
    "enslaved": "schemas/enslaved/schema.ttl",
    "enslaved_wiki": "schemas/enslaved_wiki/schema.ttl",
    "gbo": "schemas/gbo/schema.ttl",
    "gmo": "schemas/gmo/schema.ttl",
    "core_scholar_rich": "schemas/core_scholar_rich/schema.ttl",
    "core_scholar_shallow": "schemas/core_scholar_shallow/schema.ttl",
}

CQ_FILES = {
    "kwg": "cqs/kwg.txt",
    "kwg_lite": "cqs/kwg_lite.txt",
    "currkg": "cqs/currkg.txt",
    "enslaved": "cqs/enslaved.txt",
    "enslaved_wiki": "cqs/enslaved_wiki.txt",
    "gbo": "cqs/gbo.txt",
    "gmo": "cqs/gmo.txt",
    "core_scholar_rich": "cqs/core_scholar_rich.txt",
    "core_scholar_shallow": "cqs/core_scholar_shallow.txt",
}


# ==============================================================================
# HELPER FUNCTIONS & SPARQL EXECUTION
# ==============================================================================
def normalize_endpoint(endpoint: str) -> str:
    if not endpoint:
        return endpoint
    if endpoint.startswith(("http://", "https://")):
        return endpoint
    return "http://" + endpoint


def _run(endpoint: str, query: str, headers: Optional[Dict[str, str]] = None, timeout: int = 600):
    """Execute SPARQL and return (bindings, raw_json)."""
    sparql = SPARQLWrapper(normalize_endpoint(endpoint))
    sparql.setTimeout(timeout)
    sparql.setReturnFormat(JSON)
    sparql.setQuery(query)
    if headers:
        for key, value in headers.items():
            sparql.addCustomHttpHeader(key, value)
    raw = sparql.query().convert()
    bindings = raw.get("results", {}).get("bindings", [])
    return bindings, raw


def _normalize_binding(binding: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    out = {}
    for key, value in binding.items():
        normalized = value.get("value", "")
        if "xml:lang" in value:
            normalized += f"@{value['xml:lang']}"
        if "datatype" in value:
            normalized += f"^^<{value['datatype']}>"
        out[key] = normalized
    return out


def _result_checksum(bindings: List[Dict[str, Dict[str, str]]], raw: Optional[Dict[str, Any]] = None) -> str:
    if raw is not None and "boolean" in raw:
        payload = {"boolean": raw.get("boolean")}
    else:
        rows = [tuple(sorted(_normalize_binding(binding).items())) for binding in bindings]
        rows.sort()
        payload = rows
    return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()


def _extract_where(query: str) -> Optional[str]:
    """Extract the WHERE/body group with simple brace matching."""
    if not isinstance(query, str):
        return None

    where_match = re.search(r"\bWHERE\b", query, flags=re.IGNORECASE)
    search_start = where_match.end() if where_match else 0
    start = query.find("{", search_start)
    if start < 0:
        return None

    depth = 0
    for idx in range(start, len(query)):
        char = query[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return query[start + 1:idx]
            if depth < 0:
                return None
    return None


def _extract_prefix_block(query: str) -> str:
    lines = re.findall(r"(?im)^\s*(?:PREFIX|BASE)\s+[^\r\n]+", query or "")
    return "\n".join(lines) + ("\n" if lines else "")


# ==============================================================================
# PREFIX & QUERY EXTRACTION
# ==============================================================================
_prefix_cache: Dict[str, Dict[str, str]] = {}


def load_prefixes_from_ttl(ttl_path: str) -> Dict[str, str]:
    """Read @prefix declarations from a local TTL schema file."""
    if not ttl_path:
        return {}
    if ttl_path in _prefix_cache:
        return _prefix_cache[ttl_path]

    prefixes = {}
    try:
        with open(ttl_path, "r", encoding="utf-8") as handle:
            for line in handle:
                match = re.match(r"\s*@prefix\s+([A-Za-z][A-Za-z0-9_-]*):\s*<([^>]+)>\s*\.", line)
                if match:
                    prefixes[match.group(1)] = match.group(2)
    except FileNotFoundError:
        print(f"  [WARN] Prefix file not found: {ttl_path}; continuing without schema prefixes")

    _prefix_cache[ttl_path] = prefixes
    return prefixes


def extract_sparql(text: str) -> str:
    """Extract a SPARQL query from fenced or plain LLM output."""
    if not isinstance(text, str):
        return ""

    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()
    if "\\n" in text or "\\t" in text or "\\r" in text:
        text = text.replace("\\r", "\r").replace("\\n", "\n").replace("\\t", "\t")

    fenced = re.findall(r"```(?:sparql)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced[0].strip()

    lowered = text.lower()
    starts = [lowered.find(keyword) for keyword in ("prefix", "base", "select", "ask", "construct", "describe")]
    starts = [idx for idx in starts if idx >= 0]
    if starts:
        return text[min(starts):].strip()
    return text.strip()


def align_prefixes_to_schema(query: str, ttl_prefixes: Dict[str, str]) -> str:
    """Make local TTL/schema prefixes authoritative for evaluation."""
    if not query or not ttl_prefixes:
        return query

    declared = set()

    def replace_prefix(match: re.Match) -> str:
        keyword = match.group(1)
        prefix = match.group(2)
        uri = match.group(3)
        declared.add(prefix)
        if prefix not in ttl_prefixes:
            return match.group(0)
        replacement_uri = ttl_prefixes[prefix]
        if keyword.lower() == "@prefix":
            return f"@prefix {prefix}: <{replacement_uri}> ."
        return f"{keyword} {prefix}: <{replacement_uri}>"

    aligned_query = re.sub(
        r"(?im)^[ \t]*(@prefix|PREFIX)[ \t]+([A-Za-z][A-Za-z0-9_-]*):[ \t]*<([^>]+)>[ \t]*\.?",
        replace_prefix,
        query,
    )

    used = set(re.findall(r"\b([A-Za-z][A-Za-z0-9_-]*):[A-Za-z_][\w.-]*", aligned_query))
    missing = sorted((used - declared).intersection(ttl_prefixes))
    if not missing:
        return aligned_query

    prefix_block = "\n".join(f"PREFIX {prefix}: <{ttl_prefixes[prefix]}>" for prefix in missing)
    return prefix_block + "\n" + aligned_query.lstrip()


def add_missing_prefixes(query: str, ttl_prefixes: Dict[str, str]) -> str:
    return align_prefixes_to_schema(query, ttl_prefixes)


# ==============================================================================
# CQ COMPLEXITY MAPPING
# ==============================================================================
_cq_complexity_cache: Dict[str, Dict[str, Tuple[int, str]]] = {}
_global_cq_complexity_cache: Optional[Dict[str, Tuple[int, str]]] = None


def _normalize_cq_text(text: str) -> str:
    """Normalize CQ text for matching JSONL keys/prompts to cqs/*.txt lines."""
    text = (text or "").strip().lower()
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _complexity_for_index(index_1based: int) -> str:
    if index_1based <= 5:
        return "simple"
    if index_1based <= 10:
        return "moderate"
    return "complex"


def load_cq_complexity_map(cq_file: str) -> Dict[str, Tuple[int, str]]:
    if not cq_file:
        return {}
    if cq_file in _cq_complexity_cache:
        return _cq_complexity_cache[cq_file]

    mapping: Dict[str, Tuple[int, str]] = {}
    try:
        with open(cq_file, "r", encoding="utf-8") as handle:
            questions = [line.strip() for line in handle.read().splitlines() if line.strip()]
    except FileNotFoundError:
        questions = []

    for idx, question in enumerate(questions, start=1):
        mapping[_normalize_cq_text(question)] = (idx, _complexity_for_index(idx))
    _cq_complexity_cache[cq_file] = mapping
    return mapping


def load_global_cq_complexity_map() -> Dict[str, Tuple[int, str]]:
    global _global_cq_complexity_cache
    if _global_cq_complexity_cache is not None:
        return _global_cq_complexity_cache

    merged: Dict[str, Tuple[int, str]] = {}
    for cq_file in CQ_FILES.values():
        for question, value in load_cq_complexity_map(cq_file).items():
            merged.setdefault(question, value)
    _global_cq_complexity_cache = merged
    return merged


def infer_cq_complexity(question: str, kg: str) -> Tuple[Optional[int], str]:
    normalized = _normalize_cq_text(question)
    if not normalized:
        return None, "unknown"

    kg_match = load_cq_complexity_map(CQ_FILES.get(kg, "")).get(normalized)
    if kg_match:
        return kg_match

    global_match = load_global_cq_complexity_map().get(normalized)
    if global_match:
        return global_match
    return None, "unknown"


# ==============================================================================
# FILE ITERATION & DATA EXTRACTION UTILITIES
# ==============================================================================
def iter_jsonl(path: str) -> Iterable[Dict[str, Any]]:
    """Yields rows from a JSONL file."""
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line.strip():
                record = json.loads(line)
                record["_line_number"] = line_number
                yield record


def iter_excel_directory(directory: str, model_name: str) -> Iterable[Dict[str, Any]]:
    """Scan a directory for Excel files matching the model name and yield rows."""
    pattern = os.path.join(directory, f"sparql__*__model-{model_name}.xlsx")
    file_paths = glob.glob(pattern)
    
    if not file_paths:
        print(f"  [WARN] No files found matching pattern: {pattern}")
        return

    print(f"  [INFO] Found {len(file_paths)} file(s) for model '{model_name}'.")
    for file_path in file_paths:
        try:
            df = pd.read_excel(file_path)
            # Replace NaNs with None for cleaner processing
            df = df.where(pd.notnull(df), None) 
            
            for index, row in df.iterrows():
                record = row.to_dict()
                record["_source_file"] = os.path.basename(file_path)
                record["_line_number"] = index + 2  # +2 accounts for 0-index and Excel header
                yield record
        except Exception as e:
            print(f"  [ERROR] Error reading {file_path}: {e}")


# ==============================================================================
# JSONL-SPECIFIC PARSERS
# ==============================================================================
def get_candidate_text(record: Dict[str, Any]) -> str:
    """Return the first model text from a Gemini batch JSONL record."""
    candidates = record.get("response", {}).get("candidates", []) or []
    for candidate in candidates:
        parts = candidate.get("content", {}).get("parts", []) or []
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        if text.strip():
            return text
    return ""


def get_competency_question(record: Dict[str, Any]) -> str:
    request = record.get("request", {}) or {}
    contents = request.get("contents", []) or []
    text = "\n".join(
        part.get("text", "")
        for content in contents
        for part in (content.get("parts", []) or [])
        if isinstance(part, dict)
    )
    match = re.search(r"competency question:\s*(.*?)(?:\n\n|\nRequirements:|$)", text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return " ".join(match.group(1).split())
    return ""


def parse_key_metadata(key: str) -> Dict[str, Any]:
    """Parse dynamically to handle multi-hyphen representations (e.g. axiom-nen)"""
    metadata = {
        "task": "",
        "representation": "",
        "kg": "",
        "prompt_type": "",
        "temperature": None,
        "temperature_label": "",
        "question_from_key": "",
    }
    
    if not key:
        return metadata

    # Find the temperature segment (e.g., "-temp1.0-") to use as an anchor
    temp_match = re.search(r'-(temp[+-]?\d+(?:\.\d+)?)(?:-|$)', key)
    
    if temp_match:
        metadata["temperature_label"] = temp_match.group(1)
        temp_val_match = re.search(r'[+-]?\d+(?:\.\d+)?', temp_match.group(1))
        if temp_val_match:
            metadata["temperature"] = float(temp_val_match.group(0))
        
        # Split everything before the temperature anchor
        left_part = key[:temp_match.start()]
        
        # Everything after the temperature anchor is the question
        metadata["question_from_key"] = key[temp_match.end():]
        
        # Parse the left half from the outside in to allow any number of hyphens in representation
        left_parts = left_part.split('-')
        if len(left_parts) >= 4:
            metadata["task"] = left_parts[0]
            metadata["prompt_type"] = left_parts[-1]
            metadata["kg"] = left_parts[-2]
            # Replace hyphens with commas for consistency with Excel model formats
            metadata["representation"] = ", ".join(left_parts[1:-2]) 
            
    else:
        # Fallback naive parsing if temperature format is totally missing
        parts = (key or "").split("-", 5)
        if len(parts) >= 1: metadata["task"] = parts[0]
        if len(parts) >= 2: metadata["representation"] = parts[1]
        if len(parts) >= 3: metadata["kg"] = parts[2]
        if len(parts) >= 4: metadata["prompt_type"] = parts[3]
        if len(parts) >= 5: metadata["temperature_label"] = parts[4]
        if len(parts) >= 6: metadata["question_from_key"] = parts[5]

    return metadata


def infer_graph_id(record: Dict[str, Any]) -> str:
    metadata = parse_key_metadata(record.get("key", "") or "")
    if metadata.get("kg") in SCHEMA_PREFIX_FILES:
        return metadata["kg"]

    key = record.get("key", "") or ""
    candidates = sorted(SCHEMA_PREFIX_FILES, key=len, reverse=True)
    for graph_id in candidates:
        if graph_id in key:
            return graph_id
    return "default"


# ==============================================================================
# EVALUATION METRICS
# ==============================================================================
def syntax_ok(query: str) -> Tuple[bool, str]:
    if not isinstance(query, str) or not query.strip():
        return False, "Empty query"
    if query.count("{") != query.count("}"):
        return False, "Unbalanced braces"
    if not re.search(r"\b(SELECT|ASK|CONSTRUCT|DESCRIBE)\b", query, flags=re.IGNORECASE):
        return False, "No SPARQL query form found"

    if parseQuery is not None:
        try:
            parseQuery(query)
        except Exception as exc:
            return False, str(exc)
    return True, "OK"


def satisfiable(endpoint: str, query: str, timeout: int = 600) -> Tuple[bool, str]:
    ok, message = syntax_ok(query)
    if not ok:
        return False, f"Syntax failed: {message}"

    body = _extract_where(query)
    if not body:
        return False, "Could not extract WHERE/body group"

    ask_query = f"{_extract_prefix_block(query)}ASK WHERE {{ {body} }}"
    try:
        _, raw = _run(endpoint, ask_query, timeout=timeout)
    except Exception as exc:
        return False, str(exc)
    return bool(raw.get("boolean", False)), "OK"


def deterministic(endpoint: str, query: str, runs: int = 3, timeout: int = 600) -> Tuple[bool, str]:
    ok, message = syntax_ok(query)
    if not ok:
        return False, f"Syntax failed: {message}"

    checksums = []
    try:
        for _ in range(runs):
            bindings, raw = _run(endpoint, query, timeout=timeout)
            checksums.append(_result_checksum(bindings, raw))
    except Exception as exc:
        return False, str(exc)
    return len(set(checksums)) == 1, "OK"


def result_shape(endpoint: str, query: str, timeout: int = 600) -> Tuple[int, List[str], str]:
    try:
        bindings, raw = _run(endpoint, query, timeout=timeout)
    except Exception as exc:
        return 0, [], str(exc)

    if "boolean" in raw:
        return int(bool(raw.get("boolean"))), [], "OK"
    variables = raw.get("head", {}).get("vars", []) or []
    return len(bindings), variables, "OK"


# ==============================================================================
# ROW EVALUATORS (XLSX vs JSONL)
# ==============================================================================
def evaluate_excel_record(
    record: Dict[str, Any],
    model_name: str,
    endpoint_by_graph: Optional[Dict[str, str]] = None,
    runs: int = 3,
    timeout: int = 600,
    add_schema_prefixes: bool = True,
) -> Dict[str, Any]:
    """Evaluate one EXCEL row record and return a flat result dict."""
    endpoint_by_graph = endpoint_by_graph or ENDPOINT_BY_GRAPH
    
    task = str(record.get("Task", "sparql") or "sparql")
    graph_id = str(record.get("KG_IDs", "default") or "default")
    representation = str(record.get("Representations", "") or "")
    prompt_type = str(record.get("Prompt_Type", "") or "")
    temperature = record.get("Temperature")
    temperature_label = f"temp{temperature}" if temperature is not None else ""
    competency_question = str(record.get("CQ", "") or "")
    
    synthetic_key = f"{task}-{representation}-{graph_id}-{prompt_type}-{temperature_label}-{competency_question[:20]}"
    endpoint = endpoint_by_graph.get(graph_id) or endpoint_by_graph.get("default") or DEFAULT_ENDPOINT

    raw_text = str(record.get("Analysis_Result") or record.get("Analysis_Raw") or "")
    query = extract_sparql(raw_text)
    
    if add_schema_prefixes:
        query = align_prefixes_to_schema(query, load_prefixes_from_ttl(SCHEMA_PREFIX_FILES.get(graph_id, "")))

    syntax_passed, syntax_message = syntax_ok(query)
    satisfiable_passed, satisfiable_message = (False, "Skipped because syntax failed")
    deterministic_passed, deterministic_message = (False, "Skipped because syntax failed")
    rows, variables, result_message = 0, [], "Skipped because syntax failed"

    if syntax_passed:
        satisfiable_passed, satisfiable_message = satisfiable(endpoint, query, timeout=timeout)
        deterministic_passed, deterministic_message = deterministic(endpoint, query, runs=runs, timeout=timeout)
        rows, variables, result_message = result_shape(endpoint, query, timeout=timeout)

    cq_index, cq_complexity = infer_cq_complexity(competency_question, graph_id)
    all_checks_passed = syntax_passed and satisfiable_passed and deterministic_passed

    return {
        "source_file": record.get("_source_file"),
        "line_number": record.get("_line_number"),
        "model": model_name,
        "key": synthetic_key,
        "task": task,
        "representation": representation,
        "kg": graph_id,
        "graph_id": graph_id,
        "prompt_type": prompt_type,
        "temperature": temperature,
        "temperature_label": temperature_label,
        "cq_index": cq_index,
        "cq_complexity": cq_complexity,
        "endpoint": normalize_endpoint(endpoint),
        "competency_question": competency_question,
        "sparql_query": query,
        "syntax_ok": syntax_passed,
        "syntax_message": syntax_message,
        "satisfiable": satisfiable_passed,
        "satisfiable_message": satisfiable_message,
        "deterministic": deterministic_passed,
        "deterministic_message": deterministic_message,
        "all_checks_passed": all_checks_passed,
        "rows": rows,
        "variables": variables,
        "result_message": result_message,
    }


def evaluate_jsonl_record(
    record: Dict[str, Any],
    model_name: str,
    endpoint_by_graph: Optional[Dict[str, str]] = None,
    runs: int = 3,
    timeout: int = 600,
    add_schema_prefixes: bool = True,
) -> Dict[str, Any]:
    """Evaluate one JSONL result record and return a flat result dict."""
    endpoint_by_graph = endpoint_by_graph or ENDPOINT_BY_GRAPH
    key_metadata = parse_key_metadata(record.get("key", "") or "")
    graph_id = key_metadata.get("kg") or infer_graph_id(record)
    endpoint = endpoint_by_graph.get(graph_id) or endpoint_by_graph.get("default") or DEFAULT_ENDPOINT

    raw_text = get_candidate_text(record)
    query = extract_sparql(raw_text)
    if add_schema_prefixes:
        query = add_missing_prefixes(query, load_prefixes_from_ttl(SCHEMA_PREFIX_FILES.get(graph_id, "")))

    syntax_passed, syntax_message = syntax_ok(query)
    satisfiable_passed, satisfiable_message = (False, "Skipped because syntax failed")
    deterministic_passed, deterministic_message = (False, "Skipped because syntax failed")
    rows, variables, result_message = 0, [], "Skipped because syntax failed"

    if syntax_passed:
        satisfiable_passed, satisfiable_message = satisfiable(endpoint, query, timeout=timeout)
        deterministic_passed, deterministic_message = deterministic(endpoint, query, runs=runs, timeout=timeout)
        rows, variables, result_message = result_shape(endpoint, query, timeout=timeout)

    competency_question = get_competency_question(record) or key_metadata.get("question_from_key", "")
    cq_index, cq_complexity = infer_cq_complexity(competency_question, graph_id)
    all_checks_passed = syntax_passed and satisfiable_passed and deterministic_passed

    return {
        "line_number": record.get("_line_number"),
        "model": model_name,
        "key": record.get("key", ""),
        "task": key_metadata.get("task", ""),
        "representation": key_metadata.get("representation", ""),
        "kg": graph_id,
        "graph_id": graph_id,
        "prompt_type": key_metadata.get("prompt_type", ""),
        "temperature": key_metadata.get("temperature"),
        "temperature_label": key_metadata.get("temperature_label", ""),
        "cq_index": cq_index,
        "cq_complexity": cq_complexity,
        "endpoint": normalize_endpoint(endpoint),
        "competency_question": competency_question,
        "sparql_query": query,
        "syntax_ok": syntax_passed,
        "syntax_message": syntax_message,
        "satisfiable": satisfiable_passed,
        "satisfiable_message": satisfiable_message,
        "deterministic": deterministic_passed,
        "deterministic_message": deterministic_message,
        "all_checks_passed": all_checks_passed,
        "rows": rows,
        "variables": variables,
        "result_message": result_message,
    }


# ==============================================================================
# AGGREGATION & REPORTING
# ==============================================================================
def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def _row_all_checks_passed(row: Dict[str, Any]) -> bool:
    if "all_checks_passed" in row:
        return _as_bool(row.get("all_checks_passed"))
    return (
        _as_bool(row.get("syntax_ok"))
        and _as_bool(row.get("satisfiable"))
        and _as_bool(row.get("deterministic"))
    )


def _mean(values: List[float]) -> Optional[float]:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 4)


def _group_summary(rows: List[Dict[str, Any]], dimensions: List[str], level: str) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
    for row in rows:
        key = tuple(row.get(dimension, "") for dimension in dimensions)
        grouped.setdefault(key, []).append(row)

    summaries = []
    for group_key, group_rows in sorted(grouped.items(), key=lambda item: tuple(str(x) for x in item[0])):
        total = len(group_rows)
        syntax_count = sum(_as_bool(row.get("syntax_ok")) for row in group_rows)
        satisfiable_count = sum(_as_bool(row.get("satisfiable")) for row in group_rows)
        deterministic_count = sum(_as_bool(row.get("deterministic")) for row in group_rows)
        all_checks_count = sum(_row_all_checks_passed(row) for row in group_rows)
        nonzero_rows_count = sum((row.get("rows") or 0) > 0 for row in group_rows)
        summary = {
            "level": level,
            "dimensions": "|".join(dimensions),
            "n": total,
            "syntax_ok_count": syntax_count,
            "syntax_ok_rate": round(syntax_count / total, 4) if total else 0,
            "satisfiable_count": satisfiable_count,
            "satisfiable_rate": round(satisfiable_count / total, 4) if total else 0,
            "deterministic_count": deterministic_count,
            "deterministic_rate": round(deterministic_count / total, 4) if total else 0,
            "all_checks_passed_count": all_checks_count,
            "all_checks_passed_rate": round(all_checks_count / total, 4) if total else 0,
            "nonzero_rows_count": nonzero_rows_count,
            "nonzero_rows_rate": round(nonzero_rows_count / total, 4) if total else 0,
            "avg_rows": _mean([row.get("rows") for row in group_rows]),
        }
        summary.update(dict(zip(dimensions, group_key)))
        summaries.append(summary)
    return summaries


def aggregate_evaluation_results(
    input_rows_or_jsonl: Any,
    output_jsonl: Optional[str] = None,
    output_csv: Optional[str] = None,
) -> List[Dict[str, Any]]:
    
    if isinstance(input_rows_or_jsonl, str):
        print(f"  [INFO] Aggregating results from {input_rows_or_jsonl}...")
        rows = list(iter_jsonl(input_rows_or_jsonl))
    else:
        rows = input_rows_or_jsonl
        
    # 'model' added to base dimensions for full combination coverage
    analysis_dimensions = ["model", "kg", "cq_complexity", "representation", "prompt_type", "temperature_label"]
    levels = [
        ("__".join(dimensions), list(dimensions))
        for size in range(1, len(analysis_dimensions) + 1)
        for dimensions in combinations(analysis_dimensions, size)
    ]

    summaries: List[Dict[str, Any]] = []
    for level, dimensions in levels:
        summaries.extend(_group_summary(rows, dimensions, level))

    if output_jsonl:
        with open(output_jsonl, "w", encoding="utf-8") as handle:
            for summary in summaries:
                handle.write(json.dumps(summary, ensure_ascii=False) + "\n")
        print(f"  [SUCCESS] Saved summary JSONL: {output_jsonl}")

    if output_csv and summaries:
        fieldnames = sorted({field for summary in summaries for field in summary})
        # Note: "model" inserted explicitly between "dimensions" and "kg"
        preferred = [
            "level", "dimensions", "model", "kg", "representation", "prompt_type", "temperature_label", "cq_complexity",
            "n", "syntax_ok_count", "syntax_ok_rate", "satisfiable_count", "satisfiable_rate",
            "deterministic_count", "deterministic_rate", "all_checks_passed_count", "all_checks_passed_rate",
            "nonzero_rows_count", "nonzero_rows_rate",
            "avg_rows",
        ]
        fieldnames = preferred + [field for field in fieldnames if field not in preferred]
        with open(output_csv, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(summaries)
        print(f"  [SUCCESS] Saved summary CSV: {output_csv}")

    return summaries


def generate_cross_model_summary(models: List[str], output_dir: str):
    """
    Reads existing _results_.jsonl files for the specified models and 
    generates a master summary aggregation file bridging them all.
    """
    print(f"\n---> [START] Generating Cross-Model Summary <---")
    
    all_rows = []
    for model_name in models:
        file_path = os.path.join(output_dir, f"sparql_evaluation_results_{model_name}.jsonl")
        if os.path.exists(file_path):
            print(f"  [INFO] Loading {file_path}")
            model_rows = list(iter_jsonl(file_path))
            
            # Inject 'model' and normalize 'temperature_label' for consistency across files
            for row in model_rows:
                if "model" not in row:
                    row["model"] = model_name
                    
                temp_label = row.get("temperature_label", "")
                if temp_label.startswith("temp"):
                    try:
                        # This parses any value after "temp" (e.g., "0" or "0.0") and forces it to 1 decimal place.
                        val = float(temp_label.replace("temp", ""))
                        row["temperature_label"] = f"temp{val:.1f}"
                    except ValueError:
                        pass
                        
            all_rows.extend(model_rows)
        else:
            print(f"  [WARN] Skipping {model_name} - results JSONL not found at {file_path}")
            
    if not all_rows:
        print("  [ERROR] No result files found to aggregate. Aborting cross-model summary.")
        return
        
    print(f"  [INFO] Aggregating {len(all_rows)} total rows across {len(models)} models...")
    
    master_jsonl = os.path.join(output_dir, "sparql_cross_model_summary.jsonl")
    master_csv = os.path.join(output_dir, "sparql_cross_model_summary.csv")
    
    aggregate_evaluation_results(all_rows, output_jsonl=master_jsonl, output_csv=master_csv)
    print("  [SUCCESS] Cross-model summary generated.\n")


def _format_rate(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def _summary_label(row: Dict[str, Any]) -> str:
    dimensions = [d for d in str(row.get("dimensions", "")).split("|") if d]
    parts = []
    for dimension in dimensions:
        value = row.get(dimension, "")
        if value not in (None, ""):
            parts.append(f"{dimension}={value}")
    return ", ".join(parts) if parts else row.get("level", "overall")


def print_summary_table(
    summaries: List[Dict[str, Any]],
    level: str,
    title: str,
    limit: Optional[int] = None,
) -> None:
    rows = [row for row in summaries if row.get("level") == level]
    if not rows:
        return
    if limit is not None:
        rows = rows[:limit]

    print(f"\n{title}")
    print("-" * len(title))
    for row in rows:
        print(
            f"{_summary_label(row)} | "
            f"n={row.get('n', 0)} | "
            f"syntax={_format_rate(row.get('syntax_ok_rate'))} | "
            f"satisfiable={_format_rate(row.get('satisfiable_rate'))} | "
            f"deterministic={_format_rate(row.get('deterministic_rate'))} | "
            f"all_passed={_format_rate(row.get('all_checks_passed_rate'))} | "
            f"nonzero_rows={_format_rate(row.get('nonzero_rows_rate'))} | "
            f"avg_rows={row.get('avg_rows')}"
        )


def print_final_stats(
    row_results_jsonl: str,
    summaries: List[Dict[str, Any]],
    generated_files: List[Optional[str]],
) -> None:
    rows = list(iter_jsonl(row_results_jsonl)) if os.path.exists(row_results_jsonl) else []
    print("\n============================================================")
    print("Final Statistics")
    print("============================================================")
    print("\nGenerated files")
    print("---------------")
    for file_path in generated_files:
        if file_path:
            print(f"  - {file_path}")

    print("\nOverall evaluation")
    print("------------------")
    if not rows:
        print("  No evaluated rows found.")
        return

    total = len(rows)
    syntax_count = sum(_as_bool(row.get("syntax_ok")) for row in rows)
    satisfiable_count = sum(_as_bool(row.get("satisfiable")) for row in rows)
    deterministic_count = sum(_as_bool(row.get("deterministic")) for row in rows)
    all_checks_count = sum(_row_all_checks_passed(row) for row in rows)
    nonzero_rows_count = sum((row.get("rows") or 0) > 0 for row in rows)
    unknown_complexity_count = sum(row.get("cq_complexity") == "unknown" for row in rows)
    print(f"  rows={total}")
    print(f"  syntax_ok={syntax_count}/{total} ({_format_rate(syntax_count / total)})")
    print(f"  satisfiable={satisfiable_count}/{total} ({_format_rate(satisfiable_count / total)})")
    print(f"  deterministic={deterministic_count}/{total} ({_format_rate(deterministic_count / total)})")
    print(f"  all_checks_passed={all_checks_count}/{total} ({_format_rate(all_checks_count / total)})")
    print(f"  nonzero_rows={nonzero_rows_count}/{total} ({_format_rate(nonzero_rows_count / total)})")
    print(f"  unknown_cq_complexity={unknown_complexity_count}/{total} ({_format_rate(unknown_complexity_count / total)})\n")

    print_summary_table(summaries, "model", "By Model")
    print_summary_table(summaries, "kg", "By KG")
    print_summary_table(summaries, "cq_complexity", "By CQ Complexity")
    print_summary_table(summaries, "representation", "By Representation")
    print_summary_table(summaries, "prompt_type", "By Prompt Type")
    print_summary_table(summaries, "model__kg", "By Model And KG")
    print_summary_table(summaries, "model__representation", "By Model And Representation")
    print_summary_table(summaries, "representation__prompt_type", "By Representation And Prompt Type")


# ==============================================================================
# PIPELINES (XLSX vs JSONL)
# ==============================================================================
def evaluate_excel_directory(
    input_directory: str,
    model_name: str,
    output_jsonl: str,
    output_csv: Optional[str] = None,
    summary_jsonl: Optional[str] = None,
    summary_csv: Optional[str] = None,
    endpoint_by_graph: Optional[Dict[str, str]] = None,
    runs: int = 3,
    timeout: int = 600,
    limit: Optional[int] = None,
    resume: bool = True,
) -> List[Dict[str, Any]]:
    """Evaluate generated SPARQLs across multiple Excel files for a specific model."""
    print(f"\n---> [START] Evaluating EXCEL model: {model_name} <---")
    print(f"  [INFO] Scanning directory: {input_directory}")
    
    done_keys = set()
    if resume and os.path.exists(output_jsonl):
        for old in iter_jsonl(output_jsonl):
            file_ref = f"{old.get('source_file')}_{old.get('line_number')}"
            done_keys.add(file_ref)
        if done_keys:
            print(f"  [INFO] Resuming evaluation: {len(done_keys)} rows already processed in {output_jsonl}")

    results = []
    processed = 0
    mode = "a" if resume and os.path.exists(output_jsonl) else "w"
    
    print(f"  [INFO] Beginning row-level evaluation phase...")
    with open(output_jsonl, mode, encoding="utf-8") as out:
        for record in iter_excel_directory(input_directory, model_name):
            if limit is not None and processed >= limit:
                print(f"  [INFO] Reached limit of {limit} rows. Stopping evaluation early.")
                break
                
            file_ref = f"{record.get('_source_file')}_{record.get('_line_number')}"
            if resume and file_ref in done_keys:
                continue

            result = evaluate_excel_record(record, model_name=model_name, endpoint_by_graph=endpoint_by_graph, runs=runs, timeout=timeout)
            out.write(json.dumps(result, ensure_ascii=False) + "\n")
            out.flush()
            results.append(result)
            processed += 1
            print(
                f"    -> [{record.get('_source_file')} - Row {record.get('_line_number')}]: "
                f"kg={result['kg']} | rep={result['representation']} | prompt={result['prompt_type']} | "
                f"syntax={result['syntax_ok']} | satisfiable={result['satisfiable']} | deterministic={result['deterministic']} | all_passed={result['all_checks_passed']}"
            )
            
    print(f"  [INFO] Finished evaluating {processed} new rows for {model_name}.")

    if output_csv:
        print(f"  [INFO] Converting JSONL evaluation results to CSV format...")
        rows = list(iter_jsonl(output_jsonl))
        if rows:
            preferred = [
                "source_file", "line_number", "model", "key", "task", "representation", "kg", "graph_id",
                "prompt_type", "temperature", "temperature_label", "cq_index", "cq_complexity",
                "endpoint", "competency_question", "sparql_query",
                "syntax_ok", "syntax_message", "satisfiable", "satisfiable_message",
                "deterministic", "deterministic_message", "all_checks_passed",
                "rows", "variables", "result_message",
            ]
            all_fields = {field for row in rows for field in row.keys()}
            fieldnames = preferred + [field for field in sorted(all_fields) if field not in preferred]
            with open(output_csv, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                for row in rows:
                    row = dict(row)
                    row["variables"] = json.dumps(row.get("variables", []), ensure_ascii=False)
                    writer.writerow(row)
            print(f"  [SUCCESS] Saved CSV: {output_csv}")

    summaries = []
    if summary_jsonl or summary_csv:
        print(f"  [INFO] Generating aggregated summary reports...")
        summaries = aggregate_evaluation_results(output_jsonl, output_jsonl=summary_jsonl, output_csv=summary_csv)

    print(f"  [SUCCESS] Evaluation phase for {model_name} complete!")
    print_final_stats(output_jsonl, summaries, [output_jsonl, output_csv, summary_jsonl, summary_csv])
    return results


def evaluate_jsonl_file(
    input_jsonl: str,
    output_jsonl: str,
    model_name: str,
    output_csv: Optional[str] = None,
    summary_jsonl: Optional[str] = None,
    summary_csv: Optional[str] = None,
    endpoint_by_graph: Optional[Dict[str, str]] = None,
    runs: int = 3,
    timeout: int = 600,
    limit: Optional[int] = None,
    resume: bool = True,
) -> List[Dict[str, Any]]:
    """Evaluate generated SPARQLs in a single Gemini batch JSONL file."""
    print(f"\n---> [START] Evaluating JSONL model: {model_name} <---")
    print(f"  [INFO] Reading file: {input_jsonl}")
    
    done_keys = set()
    if resume and os.path.exists(output_jsonl):
        for old in iter_jsonl(output_jsonl):
            done_keys.add(old.get("key"))
        if done_keys:
            print(f"  [INFO] Resuming evaluation: {len(done_keys)} rows already processed in {output_jsonl}")

    results = []
    processed = 0
    mode = "a" if resume and os.path.exists(output_jsonl) else "w"
    
    print(f"  [INFO] Beginning row-level evaluation phase...")
    with open(output_jsonl, mode, encoding="utf-8") as out:
        for record in iter_jsonl(input_jsonl):
            if limit is not None and processed >= limit:
                print(f"  [INFO] Reached limit of {limit} rows. Stopping evaluation early.")
                break
            if resume and record.get("key") in done_keys:
                continue

            result = evaluate_jsonl_record(record, model_name=model_name, endpoint_by_graph=endpoint_by_graph, runs=runs, timeout=timeout)
            out.write(json.dumps(result, ensure_ascii=False) + "\n")
            out.flush()
            results.append(result)
            processed += 1
            print(
                f"    -> [Row {record.get('_line_number')}]: "
                f"kg={result['kg']} | rep={result['representation']} | prompt={result['prompt_type']} | "
                f"syntax={result['syntax_ok']} | satisfiable={result['satisfiable']} | deterministic={result['deterministic']} | all_passed={result['all_checks_passed']}"
            )

    print(f"  [INFO] Finished evaluating {processed} new rows.")

    if output_csv:
        print(f"  [INFO] Converting JSONL evaluation results to CSV format...")
        rows = list(iter_jsonl(output_jsonl))
        if rows:
            preferred = [
                "line_number", "model", "key", "task", "representation", "kg", "graph_id",
                "prompt_type", "temperature", "temperature_label", "cq_index", "cq_complexity",
                "endpoint", "competency_question", "sparql_query",
                "syntax_ok", "syntax_message", "satisfiable", "satisfiable_message",
                "deterministic", "deterministic_message", "all_checks_passed",
                "rows", "variables", "result_message",
            ]
            all_fields = {field for row in rows for field in row.keys()}
            fieldnames = preferred + [field for field in sorted(all_fields) if field not in preferred]
            with open(output_csv, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                for row in rows:
                    row = dict(row)
                    row["variables"] = json.dumps(row.get("variables", []), ensure_ascii=False)
                    writer.writerow(row)
            print(f"  [SUCCESS] Saved CSV: {output_csv}")

    summaries = []
    if summary_jsonl or summary_csv:
        print(f"  [INFO] Generating aggregated summary reports...")
        summaries = aggregate_evaluation_results(output_jsonl, output_jsonl=summary_jsonl, output_csv=summary_csv)

    print(f"  [SUCCESS] Evaluation phase for {model_name} complete!")
    print_final_stats(output_jsonl, summaries, [output_jsonl, output_csv, summary_jsonl, summary_csv])
    return resultsFalse, "qwen3.5-122b"
    # LOCAL_MODELS = ["gpt-oss-120b", "qwen3.5-35b", "deepseek-r1-70b", "granite4.1-30b", "qwen3.5-122b"]import concurrent.futures
import csv
import hashlib
import json
import os
import re
import glob
import pandas as pd
from pathlib import Path
from itertools import combinations
from typing import Any, Dict, Iterable, List, Optional, Tuple

from SPARQLWrapper import JSON, SPARQLWrapper

try:
    from rdflib.plugins.sparql.parser import parseQuery
except Exception:
    parseQuery = None


# ==============================================================================
# CONSTANTS & CONFIGURATION
# ==============================================================================
FUSEKI_BASE_URL = "http://localhost:3030"  # e.g. "http://localhost:3030"
DEFAULT_ENDPOINT = f"{FUSEKI_BASE_URL}/currkg/sparql"
INPUT_DIR = "results"  # Set to the path where your .xlsx or .jsonl files are located
OUTPUT_DIR = "eval"  # Set to the path where your output should go

# Override any graph-specific endpoints here if a graph lives in a different Fuseki dataset.
ENDPOINT_BY_GRAPH = {
    "default": DEFAULT_ENDPOINT,
    "kwg": f"{FUSEKI_BASE_URL}/kwg/sparql",
    "kwg_lite": f"{FUSEKI_BASE_URL}/kwg_lite/sparql",
    "currkg": f"{FUSEKI_BASE_URL}/currkg/sparql",
    "enslaved": f"{FUSEKI_BASE_URL}/enslaved/sparql",
    "enslaved_wiki": f"{FUSEKI_BASE_URL}/enslaved_wiki/sparql",
    "gbo": f"{FUSEKI_BASE_URL}/gbo/sparql",
    "gmo": f"{FUSEKI_BASE_URL}/gmo/sparql",
    "core_scholar_rich": f"{FUSEKI_BASE_URL}/core_scholar_rich/sparql",
    "core_scholar_shallow": f"{FUSEKI_BASE_URL}/core_scholar_shallow/sparql",
}

SCHEMA_PREFIX_FILES = {
    "kwg": "schemas/kwg/schema.ttl",
    "kwg_lite": "schemas/kwg_lite/schema.ttl",
    "currkg": "schemas/currkg/schema.ttl",
    "enslaved": "schemas/enslaved/schema.ttl",
    "enslaved_wiki": "schemas/enslaved_wiki/schema.ttl",
    "gbo": "schemas/gbo/schema.ttl",
    "gmo": "schemas/gmo/schema.ttl",
    "core_scholar_rich": "schemas/core_scholar_rich/schema.ttl",
    "core_scholar_shallow": "schemas/core_scholar_shallow/schema.ttl",
}

CQ_FILES = {
    "kwg": "cqs/kwg.txt",
    "kwg_lite": "cqs/kwg_lite.txt",
    "currkg": "cqs/currkg.txt",
    "enslaved": "cqs/enslaved.txt",
    "enslaved_wiki": "cqs/enslaved_wiki.txt",
    "gbo": "cqs/gbo.txt",
    "gmo": "cqs/gmo.txt",
    "core_scholar_rich": "cqs/core_scholar_rich.txt",
    "core_scholar_shallow": "cqs/core_scholar_shallow.txt",
}


# ==============================================================================
# HELPER FUNCTIONS & SPARQL EXECUTION
# ==============================================================================
def normalize_endpoint(endpoint: str) -> str:
    if not endpoint:
        return endpoint
    if endpoint.startswith(("http://", "https://")):
        return endpoint
    return "http://" + endpoint


def _run(endpoint: str, query: str, headers: Optional[Dict[str, str]] = None, timeout: int = 600):
    """Execute SPARQL and return (bindings, raw_json)."""
    sparql = SPARQLWrapper(normalize_endpoint(endpoint))
    sparql.setTimeout(timeout)
    sparql.setReturnFormat(JSON)
    sparql.setQuery(query)
    if headers:
        for key, value in headers.items():
            sparql.addCustomHttpHeader(key, value)
    raw = sparql.query().convert()
    bindings = raw.get("results", {}).get("bindings", [])
    return bindings, raw


def _normalize_binding(binding: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    out = {}
    for key, value in binding.items():
        normalized = value.get("value", "")
        if "xml:lang" in value:
            normalized += f"@{value['xml:lang']}"
        if "datatype" in value:
            normalized += f"^^<{value['datatype']}>"
        out[key] = normalized
    return out


def _result_checksum(bindings: List[Dict[str, Dict[str, str]]], raw: Optional[Dict[str, Any]] = None) -> str:
    if raw is not None and "boolean" in raw:
        payload = {"boolean": raw.get("boolean")}
    else:
        rows = [tuple(sorted(_normalize_binding(binding).items())) for binding in bindings]
        rows.sort()
        payload = rows
    return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()


def _extract_where(query: str) -> Optional[str]:
    """Extract the WHERE/body group with simple brace matching."""
    if not isinstance(query, str):
        return None

    where_match = re.search(r"\bWHERE\b", query, flags=re.IGNORECASE)
    search_start = where_match.end() if where_match else 0
    start = query.find("{", search_start)
    if start < 0:
        return None

    depth = 0
    for idx in range(start, len(query)):
        char = query[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return query[start + 1:idx]
            if depth < 0:
                return None
    return None


def _extract_prefix_block(query: str) -> str:
    lines = re.findall(r"(?im)^\s*(?:PREFIX|BASE)\s+[^\r\n]+", query or "")
    return "\n".join(lines) + ("\n" if lines else "")


# ==============================================================================
# PREFIX & QUERY EXTRACTION
# ==============================================================================
_prefix_cache: Dict[str, Dict[str, str]] = {}


def load_prefixes_from_ttl(ttl_path: str) -> Dict[str, str]:
    """Read @prefix declarations from a local TTL schema file."""
    if not ttl_path:
        return {}
    if ttl_path in _prefix_cache:
        return _prefix_cache[ttl_path]

    prefixes = {}
    try:
        with open(ttl_path, "r", encoding="utf-8") as handle:
            for line in handle:
                match = re.match(r"\s*@prefix\s+([A-Za-z][A-Za-z0-9_-]*):\s*<([^>]+)>\s*\.", line)
                if match:
                    prefixes[match.group(1)] = match.group(2)
    except FileNotFoundError:
        print(f"  [WARN] Prefix file not found: {ttl_path}; continuing without schema prefixes")

    _prefix_cache[ttl_path] = prefixes
    return prefixes


def extract_sparql(text: str) -> str:
    """Extract a SPARQL query from fenced or plain LLM output."""
    if not isinstance(text, str):
        return ""

    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()
    if "\\n" in text or "\\t" in text or "\\r" in text:
        text = text.replace("\\r", "\r").replace("\\n", "\n").replace("\\t", "\t")

    fenced = re.findall(r"```(?:sparql)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced[0].strip()

    lowered = text.lower()
    starts = [lowered.find(keyword) for keyword in ("prefix", "base", "select", "ask", "construct", "describe")]
    starts = [idx for idx in starts if idx >= 0]
    if starts:
        return text[min(starts):].strip()
    return text.strip()


def align_prefixes_to_schema(query: str, ttl_prefixes: Dict[str, str]) -> str:
    """Make local TTL/schema prefixes authoritative for evaluation."""
    if not query or not ttl_prefixes:
        return query

    declared = set()

    def replace_prefix(match: re.Match) -> str:
        keyword = match.group(1)
        prefix = match.group(2)
        uri = match.group(3)
        declared.add(prefix)
        if prefix not in ttl_prefixes:
            return match.group(0)
        replacement_uri = ttl_prefixes[prefix]
        if keyword.lower() == "@prefix":
            return f"@prefix {prefix}: <{replacement_uri}> ."
        return f"{keyword} {prefix}: <{replacement_uri}>"

    aligned_query = re.sub(
        r"(?im)^[ \t]*(@prefix|PREFIX)[ \t]+([A-Za-z][A-Za-z0-9_-]*):[ \t]*<([^>]+)>[ \t]*\.?",
        replace_prefix,
        query,
    )

    used = set(re.findall(r"\b([A-Za-z][A-Za-z0-9_-]*):[A-Za-z_][\w.-]*", aligned_query))
    missing = sorted((used - declared).intersection(ttl_prefixes))
    if not missing:
        return aligned_query

    prefix_block = "\n".join(f"PREFIX {prefix}: <{ttl_prefixes[prefix]}>" for prefix in missing)
    return prefix_block + "\n" + aligned_query.lstrip()


def add_missing_prefixes(query: str, ttl_prefixes: Dict[str, str]) -> str:
    return align_prefixes_to_schema(query, ttl_prefixes)


# ==============================================================================
# CQ COMPLEXITY MAPPING
# ==============================================================================
_cq_complexity_cache: Dict[str, Dict[str, Tuple[int, str]]] = {}
_global_cq_complexity_cache: Optional[Dict[str, Tuple[int, str]]] = None


def _normalize_cq_text(text: str) -> str:
    """Normalize CQ text for matching JSONL keys/prompts to cqs/*.txt lines."""
    text = (text or "").strip().lower()
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _complexity_for_index(index_1based: int) -> str:
    if index_1based <= 5:
        return "simple"
    if index_1based <= 10:
        return "moderate"
    return "complex"


def load_cq_complexity_map(cq_file: str) -> Dict[str, Tuple[int, str]]:
    if not cq_file:
        return {}
    if cq_file in _cq_complexity_cache:
        return _cq_complexity_cache[cq_file]

    mapping: Dict[str, Tuple[int, str]] = {}
    try:
        with open(cq_file, "r", encoding="utf-8") as handle:
            questions = [line.strip() for line in handle.read().splitlines() if line.strip()]
    except FileNotFoundError:
        questions = []

    for idx, question in enumerate(questions, start=1):
        mapping[_normalize_cq_text(question)] = (idx, _complexity_for_index(idx))
    _cq_complexity_cache[cq_file] = mapping
    return mapping


def load_global_cq_complexity_map() -> Dict[str, Tuple[int, str]]:
    global _global_cq_complexity_cache
    if _global_cq_complexity_cache is not None:
        return _global_cq_complexity_cache

    merged: Dict[str, Tuple[int, str]] = {}
    for cq_file in CQ_FILES.values():
        for question, value in load_cq_complexity_map(cq_file).items():
            merged.setdefault(question, value)
    _global_cq_complexity_cache = merged
    return merged


def infer_cq_complexity(question: str, kg: str) -> Tuple[Optional[int], str]:
    normalized = _normalize_cq_text(question)
    if not normalized:
        return None, "unknown"

    kg_match = load_cq_complexity_map(CQ_FILES.get(kg, "")).get(normalized)
    if kg_match:
        return kg_match

    global_match = load_global_cq_complexity_map().get(normalized)
    if global_match:
        return global_match
    return None, "unknown"


# ==============================================================================
# FILE ITERATION & DATA EXTRACTION UTILITIES
# ==============================================================================
def iter_jsonl(path: str) -> Iterable[Dict[str, Any]]:
    """Yields rows from a JSONL file."""
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line.strip():
                record = json.loads(line)
                record["_line_number"] = line_number
                yield record


def iter_excel_directory(directory: str, model_name: str) -> Iterable[Dict[str, Any]]:
    """Scan a directory for Excel files matching the model name and yield rows."""
    pattern = os.path.join(directory, f"sparql__*__model-{model_name}.xlsx")
    file_paths = glob.glob(pattern)
    
    if not file_paths:
        print(f"  [WARN] No files found matching pattern: {pattern}")
        return

    print(f"  [INFO] Found {len(file_paths)} file(s) for model '{model_name}'.")
    for file_path in file_paths:
        try:
            df = pd.read_excel(file_path)
            # Replace NaNs with None for cleaner processing
            df = df.where(pd.notnull(df), None) 
            
            for index, row in df.iterrows():
                record = row.to_dict()
                record["_source_file"] = os.path.basename(file_path)
                record["_line_number"] = index + 2  # +2 accounts for 0-index and Excel header
                yield record
        except Exception as e:
            print(f"  [ERROR] Error reading {file_path}: {e}")


# ==============================================================================
# JSONL-SPECIFIC PARSERS
# ==============================================================================
def get_candidate_text(record: Dict[str, Any]) -> str:
    """Return the first model text from a Gemini batch JSONL record."""
    candidates = record.get("response", {}).get("candidates", []) or []
    for candidate in candidates:
        parts = candidate.get("content", {}).get("parts", []) or []
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        if text.strip():
            return text
    return ""


def get_competency_question(record: Dict[str, Any]) -> str:
    request = record.get("request", {}) or {}
    contents = request.get("contents", []) or []
    text = "\n".join(
        part.get("text", "")
        for content in contents
        for part in (content.get("parts", []) or [])
        if isinstance(part, dict)
    )
    match = re.search(r"competency question:\s*(.*?)(?:\n\n|\nRequirements:|$)", text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return " ".join(match.group(1).split())
    return ""


def parse_key_metadata(key: str) -> Dict[str, Any]:
    """Parse dynamically to handle multi-hyphen representations (e.g. axiom-nen)"""
    metadata = {
        "task": "",
        "representation": "",
        "kg": "",
        "prompt_type": "",
        "temperature": None,
        "temperature_label": "",
        "question_from_key": "",
    }
    
    if not key:
        return metadata

    # Find the temperature segment (e.g., "-temp1.0-") to use as an anchor
    temp_match = re.search(r'-(temp[+-]?\d+(?:\.\d+)?)(?:-|$)', key)
    
    if temp_match:
        metadata["temperature_label"] = temp_match.group(1)
        temp_val_match = re.search(r'[+-]?\d+(?:\.\d+)?', temp_match.group(1))
        if temp_val_match:
            metadata["temperature"] = float(temp_val_match.group(0))
        
        # Split everything before the temperature anchor
        left_part = key[:temp_match.start()]
        
        # Everything after the temperature anchor is the question
        metadata["question_from_key"] = key[temp_match.end():]
        
        # Parse the left half from the outside in to allow any number of hyphens in representation
        left_parts = left_part.split('-')
        if len(left_parts) >= 4:
            metadata["task"] = left_parts[0]
            metadata["prompt_type"] = left_parts[-1]
            metadata["kg"] = left_parts[-2]
            # Replace hyphens with commas for consistency with Excel model formats
            metadata["representation"] = ", ".join(left_parts[1:-2]) 
            
    else:
        # Fallback naive parsing if temperature format is totally missing
        parts = (key or "").split("-", 5)
        if len(parts) >= 1: metadata["task"] = parts[0]
        if len(parts) >= 2: metadata["representation"] = parts[1]
        if len(parts) >= 3: metadata["kg"] = parts[2]
        if len(parts) >= 4: metadata["prompt_type"] = parts[3]
        if len(parts) >= 5: metadata["temperature_label"] = parts[4]
        if len(parts) >= 6: metadata["question_from_key"] = parts[5]

    return metadata


def infer_graph_id(record: Dict[str, Any]) -> str:
    metadata = parse_key_metadata(record.get("key", "") or "")
    if metadata.get("kg") in SCHEMA_PREFIX_FILES:
        return metadata["kg"]

    key = record.get("key", "") or ""
    candidates = sorted(SCHEMA_PREFIX_FILES, key=len, reverse=True)
    for graph_id in candidates:
        if graph_id in key:
            return graph_id
    return "default"


# ==============================================================================
# EVALUATION METRICS
# ==============================================================================
def syntax_ok(query: str) -> Tuple[bool, str]:
    if not isinstance(query, str) or not query.strip():
        return False, "Empty query"
    if query.count("{") != query.count("}"):
        return False, "Unbalanced braces"
    if not re.search(r"\b(SELECT|ASK|CONSTRUCT|DESCRIBE)\b", query, flags=re.IGNORECASE):
        return False, "No SPARQL query form found"

    if parseQuery is not None:
        try:
            parseQuery(query)
        except Exception as exc:
            return False, str(exc)
    return True, "OK"


def satisfiable(endpoint: str, query: str, timeout: int = 600) -> Tuple[bool, str]:
    ok, message = syntax_ok(query)
    if not ok:
        return False, f"Syntax failed: {message}"

    body = _extract_where(query)
    if not body:
        return False, "Could not extract WHERE/body group"

    ask_query = f"{_extract_prefix_block(query)}ASK WHERE {{ {body} }}"
    try:
        _, raw = _run(endpoint, ask_query, timeout=timeout)
    except Exception as exc:
        return False, str(exc)
    return bool(raw.get("boolean", False)), "OK"


def deterministic(endpoint: str, query: str, runs: int = 3, timeout: int = 600) -> Tuple[bool, str]:
    ok, message = syntax_ok(query)
    if not ok:
        return False, f"Syntax failed: {message}"

    checksums = []
    try:
        for _ in range(runs):
            bindings, raw = _run(endpoint, query, timeout=timeout)
            checksums.append(_result_checksum(bindings, raw))
    except Exception as exc:
        return False, str(exc)
    return len(set(checksums)) == 1, "OK"


def result_shape(endpoint: str, query: str, timeout: int = 600) -> Tuple[int, List[str], str]:
    try:
        bindings, raw = _run(endpoint, query, timeout=timeout)
    except Exception as exc:
        return 0, [], str(exc)

    if "boolean" in raw:
        return int(bool(raw.get("boolean"))), [], "OK"
    variables = raw.get("head", {}).get("vars", []) or []
    return len(bindings), variables, "OK"


# ==============================================================================
# ROW EVALUATORS (XLSX vs JSONL)
# ==============================================================================
def evaluate_excel_record(
    record: Dict[str, Any],
    model_name: str,
    endpoint_by_graph: Optional[Dict[str, str]] = None,
    runs: int = 3,
    timeout: int = 600,
    add_schema_prefixes: bool = True,
) -> Dict[str, Any]:
    """Evaluate one EXCEL row record and return a flat result dict."""
    endpoint_by_graph = endpoint_by_graph or ENDPOINT_BY_GRAPH
    
    task = str(record.get("Task", "sparql") or "sparql")
    graph_id = str(record.get("KG_IDs", "default") or "default")
    representation = str(record.get("Representations", "") or "")
    prompt_type = str(record.get("Prompt_Type", "") or "")
    temperature = record.get("Temperature")
    temperature_label = f"temp{temperature}" if temperature is not None else ""
    competency_question = str(record.get("CQ", "") or "")
    
    synthetic_key = f"{task}-{representation}-{graph_id}-{prompt_type}-{temperature_label}-{competency_question[:20]}"
    endpoint = endpoint_by_graph.get(graph_id) or endpoint_by_graph.get("default") or DEFAULT_ENDPOINT

    raw_text = str(record.get("Analysis_Result") or record.get("Analysis_Raw") or "")
    query = extract_sparql(raw_text)
    
    if add_schema_prefixes:
        query = align_prefixes_to_schema(query, load_prefixes_from_ttl(SCHEMA_PREFIX_FILES.get(graph_id, "")))

    syntax_passed, syntax_message = syntax_ok(query)
    satisfiable_passed, satisfiable_message = (False, "Skipped because syntax failed")
    deterministic_passed, deterministic_message = (False, "Skipped because syntax failed")
    rows, variables, result_message = 0, [], "Skipped because syntax failed"

    if syntax_passed:
        satisfiable_passed, satisfiable_message = satisfiable(endpoint, query, timeout=timeout)
        deterministic_passed, deterministic_message = deterministic(endpoint, query, runs=runs, timeout=timeout)
        rows, variables, result_message = result_shape(endpoint, query, timeout=timeout)

    cq_index, cq_complexity = infer_cq_complexity(competency_question, graph_id)
    all_checks_passed = syntax_passed and satisfiable_passed and deterministic_passed

    return {
        "source_file": record.get("_source_file"),
        "line_number": record.get("_line_number"),
        "model": model_name,
        "key": synthetic_key,
        "task": task,
        "representation": representation,
        "kg": graph_id,
        "graph_id": graph_id,
        "prompt_type": prompt_type,
        "temperature": temperature,
        "temperature_label": temperature_label,
        "cq_index": cq_index,
        "cq_complexity": cq_complexity,
        "endpoint": normalize_endpoint(endpoint),
        "competency_question": competency_question,
        "sparql_query": query,
        "syntax_ok": syntax_passed,
        "syntax_message": syntax_message,
        "satisfiable": satisfiable_passed,
        "satisfiable_message": satisfiable_message,
        "deterministic": deterministic_passed,
        "deterministic_message": deterministic_message,
        "all_checks_passed": all_checks_passed,
        "rows": rows,
        "variables": variables,
        "result_message": result_message,
    }


def evaluate_jsonl_record(
    record: Dict[str, Any],
    model_name: str,
    endpoint_by_graph: Optional[Dict[str, str]] = None,
    runs: int = 3,
    timeout: int = 600,
    add_schema_prefixes: bool = True,
) -> Dict[str, Any]:
    """Evaluate one JSONL result record and return a flat result dict."""
    endpoint_by_graph = endpoint_by_graph or ENDPOINT_BY_GRAPH
    key_metadata = parse_key_metadata(record.get("key", "") or "")
    graph_id = key_metadata.get("kg") or infer_graph_id(record)
    endpoint = endpoint_by_graph.get(graph_id) or endpoint_by_graph.get("default") or DEFAULT_ENDPOINT

    raw_text = get_candidate_text(record)
    query = extract_sparql(raw_text)
    if add_schema_prefixes:
        query = add_missing_prefixes(query, load_prefixes_from_ttl(SCHEMA_PREFIX_FILES.get(graph_id, "")))

    syntax_passed, syntax_message = syntax_ok(query)
    satisfiable_passed, satisfiable_message = (False, "Skipped because syntax failed")
    deterministic_passed, deterministic_message = (False, "Skipped because syntax failed")
    rows, variables, result_message = 0, [], "Skipped because syntax failed"

    if syntax_passed:
        satisfiable_passed, satisfiable_message = satisfiable(endpoint, query, timeout=timeout)
        deterministic_passed, deterministic_message = deterministic(endpoint, query, runs=runs, timeout=timeout)
        rows, variables, result_message = result_shape(endpoint, query, timeout=timeout)

    competency_question = get_competency_question(record) or key_metadata.get("question_from_key", "")
    cq_index, cq_complexity = infer_cq_complexity(competency_question, graph_id)
    all_checks_passed = syntax_passed and satisfiable_passed and deterministic_passed

    return {
        "line_number": record.get("_line_number"),
        "model": model_name,
        "key": record.get("key", ""),
        "task": key_metadata.get("task", ""),
        "representation": key_metadata.get("representation", ""),
        "kg": graph_id,
        "graph_id": graph_id,
        "prompt_type": key_metadata.get("prompt_type", ""),
        "temperature": key_metadata.get("temperature"),
        "temperature_label": key_metadata.get("temperature_label", ""),
        "cq_index": cq_index,
        "cq_complexity": cq_complexity,
        "endpoint": normalize_endpoint(endpoint),
        "competency_question": competency_question,
        "sparql_query": query,
        "syntax_ok": syntax_passed,
        "syntax_message": syntax_message,
        "satisfiable": satisfiable_passed,
        "satisfiable_message": satisfiable_message,
        "deterministic": deterministic_passed,
        "deterministic_message": deterministic_message,
        "all_checks_passed": all_checks_passed,
        "rows": rows,
        "variables": variables,
        "result_message": result_message,
    }


# ==============================================================================
# AGGREGATION & REPORTING
# ==============================================================================
def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def _row_all_checks_passed(row: Dict[str, Any]) -> bool:
    if "all_checks_passed" in row:
        return _as_bool(row.get("all_checks_passed"))
    return (
        _as_bool(row.get("syntax_ok"))
        and _as_bool(row.get("satisfiable"))
        and _as_bool(row.get("deterministic"))
    )


def _mean(values: List[float]) -> Optional[float]:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 4)


def _group_summary(rows: List[Dict[str, Any]], dimensions: List[str], level: str) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
    for row in rows:
        key = tuple(row.get(dimension, "") for dimension in dimensions)
        grouped.setdefault(key, []).append(row)

    summaries = []
    for group_key, group_rows in sorted(grouped.items(), key=lambda item: tuple(str(x) for x in item[0])):
        total = len(group_rows)
        syntax_count = sum(_as_bool(row.get("syntax_ok")) for row in group_rows)
        satisfiable_count = sum(_as_bool(row.get("satisfiable")) for row in group_rows)
        deterministic_count = sum(_as_bool(row.get("deterministic")) for row in group_rows)
        all_checks_count = sum(_row_all_checks_passed(row) for row in group_rows)
        nonzero_rows_count = sum((row.get("rows") or 0) > 0 for row in group_rows)
        summary = {
            "level": level,
            "dimensions": "|".join(dimensions),
            "n": total,
            "syntax_ok_count": syntax_count,
            "syntax_ok_rate": round(syntax_count / total, 4) if total else 0,
            "satisfiable_count": satisfiable_count,
            "satisfiable_rate": round(satisfiable_count / total, 4) if total else 0,
            "deterministic_count": deterministic_count,
            "deterministic_rate": round(deterministic_count / total, 4) if total else 0,
            "all_checks_passed_count": all_checks_count,
            "all_checks_passed_rate": round(all_checks_count / total, 4) if total else 0,
            "nonzero_rows_count": nonzero_rows_count,
            "nonzero_rows_rate": round(nonzero_rows_count / total, 4) if total else 0,
            "avg_rows": _mean([row.get("rows") for row in group_rows]),
        }
        summary.update(dict(zip(dimensions, group_key)))
        summaries.append(summary)
    return summaries


def aggregate_evaluation_results(
    input_rows_or_jsonl: Any,
    output_jsonl: Optional[str] = None,
    output_csv: Optional[str] = None,
) -> List[Dict[str, Any]]:
    
    if isinstance(input_rows_or_jsonl, str):
        print(f"  [INFO] Aggregating results from {input_rows_or_jsonl}...")
        rows = list(iter_jsonl(input_rows_or_jsonl))
    else:
        rows = input_rows_or_jsonl
        
    # 'model' added to base dimensions for full combination coverage
    analysis_dimensions = ["model", "kg", "cq_complexity", "representation", "prompt_type", "temperature_label"]
    levels = [
        ("__".join(dimensions), list(dimensions))
        for size in range(1, len(analysis_dimensions) + 1)
        for dimensions in combinations(analysis_dimensions, size)
    ]

    summaries: List[Dict[str, Any]] = []
    for level, dimensions in levels:
        summaries.extend(_group_summary(rows, dimensions, level))

    if output_jsonl:
        with open(output_jsonl, "w", encoding="utf-8") as handle:
            for summary in summaries:
                handle.write(json.dumps(summary, ensure_ascii=False) + "\n")
        print(f"  [SUCCESS] Saved summary JSONL: {output_jsonl}")

    if output_csv and summaries:
        fieldnames = sorted({field for summary in summaries for field in summary})
        # Note: "model" inserted explicitly between "dimensions" and "kg"
        preferred = [
            "level", "dimensions", "model", "kg", "representation", "prompt_type", "temperature_label", "cq_complexity",
            "n", "syntax_ok_count", "syntax_ok_rate", "satisfiable_count", "satisfiable_rate",
            "deterministic_count", "deterministic_rate", "all_checks_passed_count", "all_checks_passed_rate",
            "nonzero_rows_count", "nonzero_rows_rate",
            "avg_rows",
        ]
        fieldnames = preferred + [field for field in fieldnames if field not in preferred]
        with open(output_csv, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(summaries)
        print(f"  [SUCCESS] Saved summary CSV: {output_csv}")

    return summaries


def generate_cross_model_summary(models: List[str], output_dir: str):
    """
    Reads existing _results_.jsonl files for the specified models and 
    generates a master summary aggregation file bridging them all.
    """
    print(f"\n---> [START] Generating Cross-Model Summary <---")
    
    all_rows = []
    for model_name in models:
        file_path = os.path.join(output_dir, f"sparql_evaluation_results_{model_name}.jsonl")
        if os.path.exists(file_path):
            print(f"  [INFO] Loading {file_path}")
            model_rows = list(iter_jsonl(file_path))
            
            # Inject 'model' and normalize 'temperature_label' for consistency across files
            for row in model_rows:
                if "model" not in row:
                    row["model"] = model_name
                    
                temp_label = row.get("temperature_label", "")
                if temp_label.startswith("temp"):
                    try:
                        # This parses any value after "temp" (e.g., "0" or "0.0") and forces it to 1 decimal place.
                        val = float(temp_label.replace("temp", ""))
                        row["temperature_label"] = f"temp{val:.1f}"
                    except ValueError:
                        pass
                        
            all_rows.extend(model_rows)
        else:
            print(f"  [WARN] Skipping {model_name} - results JSONL not found at {file_path}")
            
    if not all_rows:
        print("  [ERROR] No result files found to aggregate. Aborting cross-model summary.")
        return
        
    print(f"  [INFO] Aggregating {len(all_rows)} total rows across {len(models)} models...")
    
    master_jsonl = os.path.join(output_dir, "sparql_cross_model_summary.jsonl")
    master_csv = os.path.join(output_dir, "sparql_cross_model_summary.csv")
    
    aggregate_evaluation_results(all_rows, output_jsonl=master_jsonl, output_csv=master_csv)
    print("  [SUCCESS] Cross-model summary generated.\n")


def _format_rate(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.1f}%"


def _summary_label(row: Dict[str, Any]) -> str:
    dimensions = [d for d in str(row.get("dimensions", "")).split("|") if d]
    parts = []
    for dimension in dimensions:
        value = row.get(dimension, "")
        if value not in (None, ""):
            parts.append(f"{dimension}={value}")
    return ", ".join(parts) if parts else row.get("level", "overall")


def print_summary_table(
    summaries: List[Dict[str, Any]],
    level: str,
    title: str,
    limit: Optional[int] = None,
) -> None:
    rows = [row for row in summaries if row.get("level") == level]
    if not rows:
        return
    if limit is not None:
        rows = rows[:limit]

    print(f"\n{title}")
    print("-" * len(title))
    for row in rows:
        print(
            f"{_summary_label(row)} | "
            f"n={row.get('n', 0)} | "
            f"syntax={_format_rate(row.get('syntax_ok_rate'))} | "
            f"satisfiable={_format_rate(row.get('satisfiable_rate'))} | "
            f"deterministic={_format_rate(row.get('deterministic_rate'))} | "
            f"all_passed={_format_rate(row.get('all_checks_passed_rate'))} | "
            f"nonzero_rows={_format_rate(row.get('nonzero_rows_rate'))} | "
            f"avg_rows={row.get('avg_rows')}"
        )


def print_final_stats(
    row_results_jsonl: str,
    summaries: List[Dict[str, Any]],
    generated_files: List[Optional[str]],
) -> None:
    rows = list(iter_jsonl(row_results_jsonl)) if os.path.exists(row_results_jsonl) else []
    print("\n============================================================")
    print("Final Statistics")
    print("============================================================")
    print("\nGenerated files")
    print("---------------")
    for file_path in generated_files:
        if file_path:
            print(f"  - {file_path}")

    print("\nOverall evaluation")
    print("------------------")
    if not rows:
        print("  No evaluated rows found.")
        return

    total = len(rows)
    syntax_count = sum(_as_bool(row.get("syntax_ok")) for row in rows)
    satisfiable_count = sum(_as_bool(row.get("satisfiable")) for row in rows)
    deterministic_count = sum(_as_bool(row.get("deterministic")) for row in rows)
    all_checks_count = sum(_row_all_checks_passed(row) for row in rows)
    nonzero_rows_count = sum((row.get("rows") or 0) > 0 for row in rows)
    unknown_complexity_count = sum(row.get("cq_complexity") == "unknown" for row in rows)
    print(f"  rows={total}")
    print(f"  syntax_ok={syntax_count}/{total} ({_format_rate(syntax_count / total)})")
    print(f"  satisfiable={satisfiable_count}/{total} ({_format_rate(satisfiable_count / total)})")
    print(f"  deterministic={deterministic_count}/{total} ({_format_rate(deterministic_count / total)})")
    print(f"  all_checks_passed={all_checks_count}/{total} ({_format_rate(all_checks_count / total)})")
    print(f"  nonzero_rows={nonzero_rows_count}/{total} ({_format_rate(nonzero_rows_count / total)})")
    print(f"  unknown_cq_complexity={unknown_complexity_count}/{total} ({_format_rate(unknown_complexity_count / total)})\n")

    print_summary_table(summaries, "model", "By Model")
    print_summary_table(summaries, "kg", "By KG")
    print_summary_table(summaries, "cq_complexity", "By CQ Complexity")
    print_summary_table(summaries, "representation", "By Representation")
    print_summary_table(summaries, "prompt_type", "By Prompt Type")
    print_summary_table(summaries, "model__kg", "By Model And KG")
    print_summary_table(summaries, "model__representation", "By Model And Representation")
    print_summary_table(summaries, "representation__prompt_type", "By Representation And Prompt Type")


# ==============================================================================
# PIPELINES (XLSX vs JSONL)
# ==============================================================================
def evaluate_excel_directory(
    input_directory: str,
    model_name: str,
    output_jsonl: str,
    output_csv: Optional[str] = None,
    summary_jsonl: Optional[str] = None,
    summary_csv: Optional[str] = None,
    endpoint_by_graph: Optional[Dict[str, str]] = None,
    runs: int = 3,
    timeout: int = 600,
    limit: Optional[int] = None,
    resume: bool = True,
) -> List[Dict[str, Any]]:
    """Evaluate generated SPARQLs across multiple Excel files for a specific model."""
    print(f"\n---> [START] Evaluating EXCEL model: {model_name} <---")
    print(f"  [INFO] Scanning directory: {input_directory}")
    
    done_keys = set()
    if resume and os.path.exists(output_jsonl):
        for old in iter_jsonl(output_jsonl):
            file_ref = f"{old.get('source_file')}_{old.get('line_number')}"
            done_keys.add(file_ref)
        if done_keys:
            print(f"  [INFO] Resuming evaluation: {len(done_keys)} rows already processed in {output_jsonl}")

    results = []
    processed = 0
    mode = "a" if resume and os.path.exists(output_jsonl) else "w"
    
    print(f"  [INFO] Beginning row-level evaluation phase...")
    with open(output_jsonl, mode, encoding="utf-8") as out:
        for record in iter_excel_directory(input_directory, model_name):
            if limit is not None and processed >= limit:
                print(f"  [INFO] Reached limit of {limit} rows. Stopping evaluation early.")
                break
                
            file_ref = f"{record.get('_source_file')}_{record.get('_line_number')}"
            if resume and file_ref in done_keys:
                continue

            result = evaluate_excel_record(record, model_name=model_name, endpoint_by_graph=endpoint_by_graph, runs=runs, timeout=timeout)
            out.write(json.dumps(result, ensure_ascii=False) + "\n")
            out.flush()
            results.append(result)
            processed += 1
            print(
                f"    -> [{record.get('_source_file')} - Row {record.get('_line_number')}]: "
                f"kg={result['kg']} | rep={result['representation']} | prompt={result['prompt_type']} | "
                f"syntax={result['syntax_ok']} | satisfiable={result['satisfiable']} | deterministic={result['deterministic']} | all_passed={result['all_checks_passed']}"
            )
            
    print(f"  [INFO] Finished evaluating {processed} new rows for {model_name}.")

    if output_csv:
        print(f"  [INFO] Converting JSONL evaluation results to CSV format...")
        rows = list(iter_jsonl(output_jsonl))
        if rows:
            preferred = [
                "source_file", "line_number", "model", "key", "task", "representation", "kg", "graph_id",
                "prompt_type", "temperature", "temperature_label", "cq_index", "cq_complexity",
                "endpoint", "competency_question", "sparql_query",
                "syntax_ok", "syntax_message", "satisfiable", "satisfiable_message",
                "deterministic", "deterministic_message", "all_checks_passed",
                "rows", "variables", "result_message",
            ]
            all_fields = {field for row in rows for field in row.keys()}
            fieldnames = preferred + [field for field in sorted(all_fields) if field not in preferred]
            with open(output_csv, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                for row in rows:
                    row = dict(row)
                    row["variables"] = json.dumps(row.get("variables", []), ensure_ascii=False)
                    writer.writerow(row)
            print(f"  [SUCCESS] Saved CSV: {output_csv}")

    summaries = []
    if summary_jsonl or summary_csv:
        print(f"  [INFO] Generating aggregated summary reports...")
        summaries = aggregate_evaluation_results(output_jsonl, output_jsonl=summary_jsonl, output_csv=summary_csv)

    print(f"  [SUCCESS] Evaluation phase for {model_name} complete!")
    print_final_stats(output_jsonl, summaries, [output_jsonl, output_csv, summary_jsonl, summary_csv])
    return results


def evaluate_jsonl_file(
    input_jsonl: str,
    output_jsonl: str,
    model_name: str,
    output_csv: Optional[str] = None,
    summary_jsonl: Optional[str] = None,
    summary_csv: Optional[str] = None,
    endpoint_by_graph: Optional[Dict[str, str]] = None,
    runs: int = 3,
    timeout: int = 600,
    limit: Optional[int] = None,
    resume: bool = True,
) -> List[Dict[str, Any]]:
    """Evaluate generated SPARQLs in a single Gemini batch JSONL file."""
    print(f"\n---> [START] Evaluating JSONL model: {model_name} <---")
    print(f"  [INFO] Reading file: {input_jsonl}")
    
    done_keys = set()
    if resume and os.path.exists(output_jsonl):
        for old in iter_jsonl(output_jsonl):
            done_keys.add(old.get("key"))
        if done_keys:
            print(f"  [INFO] Resuming evaluation: {len(done_keys)} rows already processed in {output_jsonl}")

    results = []
    processed = 0
    mode = "a" if resume and os.path.exists(output_jsonl) else "w"
    
    print(f"  [INFO] Beginning row-level evaluation phase...")
    with open(output_jsonl, mode, encoding="utf-8") as out:
        for record in iter_jsonl(input_jsonl):
            if limit is not None and processed >= limit:
                print(f"  [INFO] Reached limit of {limit} rows. Stopping evaluation early.")
                break
            if resume and record.get("key") in done_keys:
                continue

            result = evaluate_jsonl_record(record, model_name=model_name, endpoint_by_graph=endpoint_by_graph, runs=runs, timeout=timeout)
            out.write(json.dumps(result, ensure_ascii=False) + "\n")
            out.flush()
            results.append(result)
            processed += 1
            print(
                f"    -> [Row {record.get('_line_number')}]: "
                f"kg={result['kg']} | rep={result['representation']} | prompt={result['prompt_type']} | "
                f"syntax={result['syntax_ok']} | satisfiable={result['satisfiable']} | deterministic={result['deterministic']} | all_passed={result['all_checks_passed']}"
            )

    print(f"  [INFO] Finished evaluating {processed} new rows.")

    if output_csv:
        print(f"  [INFO] Converting JSONL evaluation results to CSV format...")
        rows = list(iter_jsonl(output_jsonl))
        if rows:
            preferred = [
                "line_number", "model", "key", "task", "representation", "kg", "graph_id",
                "prompt_type", "temperature", "temperature_label", "cq_index", "cq_complexity",
                "endpoint", "competency_question", "sparql_query",
                "syntax_ok", "syntax_message", "satisfiable", "satisfiable_message",
                "deterministic", "deterministic_message", "all_checks_passed",
                "rows", "variables", "result_message",
            ]
            all_fields = {field for row in rows for field in row.keys()}
            fieldnames = preferred + [field for field in sorted(all_fields) if field not in preferred]
            with open(output_csv, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                for row in rows:
                    row = dict(row)
                    row["variables"] = json.dumps(row.get("variables", []), ensure_ascii=False)
                    writer.writerow(row)
            print(f"  [SUCCESS] Saved CSV: {output_csv}")

    summaries = []
    if summary_jsonl or summary_csv:
        print(f"  [INFO] Generating aggregated summary reports...")
        summaries = aggregate_evaluation_results(output_jsonl, output_jsonl=summary_jsonl, output_csv=summary_csv)

    print(f"  [SUCCESS] Evaluation phase for {model_name} complete!")
    print_final_stats(output_jsonl, summaries, [output_jsonl, output_csv, summary_jsonl, summary_csv])
    return results


# ==============================================================================
# ENTRY POINT
# ==============================================================================
if __name__ == "__main__":
    
    # --------------------------------------------------------------------------
    # TOGGLE PIPELINE ACTIONS HERE
    # --------------------------------------------------------------------------
    RUN_MODE = "PROPRIETARY_JSONL"   # Choices: "LOCAL_XLSX" or "PROPRIETARY_JSONL"
    
    # Set this to True to run database validations and single-model summaries
    RUN_EVALUATIONS = False
    
    # Set this to True to run combinations across ALL available results
    RUN_CROSS_MODEL_SUMMARY = True
    
    #  LOCAL_MODELS = ["gpt-oss-120b", "qwen3.5-35b", "deepseek-r1-70b", "granite4.1-30b", "mistral-small3.2-latest", "qwen3.5-122b"]
    LOCAL_MODELS = ["gpt-oss-120b", "deepseek-r1-70b", "granite4.1-30b", "mistral-small3.2-latest", "qwen3.5-122b"]
    LOCAL_MODELS = ["gpt-oss-120b", "deepseek-r1-70b", "granite4.1-30b", "mistral-small3.2-latest", "qwen3.5-122b"]
    # LOCAL_MODELS = ["gpt-oss-120b", "qwen3.5-35b", "deepseek-r1-70b", "granite4.1-30b", "qwen3.5-122b"]
    PROPRIETARY_MODELS = ["gemini-2.5-pro"]
    ALL_MODELS = LOCAL_MODELS + PROPRIETARY_MODELS
    # --------------------------------------------------------------------------
    
    print("\n============================================================")
    print("  INITIALIZING PIPELINE")
    print(f"  Input Directory: {INPUT_DIR}")
    print(f"  Output Directory: {OUTPUT_DIR}")
    print(f"  Mode Selected: {RUN_MODE}")
    print("============================================================")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"  [INFO] Output directory '{OUTPUT_DIR}' is ready.")

    if RUN_EVALUATIONS:
        # ROUTE 1: Local Models (Excel Directory)
        if RUN_MODE == "LOCAL_XLSX":
            for model_name in LOCAL_MODELS:
                evaluate_excel_directory(
                    input_directory=INPUT_DIR,
                    model_name=model_name,
                    output_jsonl=os.path.join(OUTPUT_DIR, f"sparql_evaluation_results_{model_name}.jsonl"),
                    output_csv=os.path.join(OUTPUT_DIR, f"sparql_evaluation_results_{model_name}.csv"),
                    summary_jsonl=os.path.join(OUTPUT_DIR, f"sparql_evaluation_summary_{model_name}.jsonl"),
                    summary_csv=os.path.join(OUTPUT_DIR, f"sparql_evaluation_summary_{model_name}.csv"),
                    runs=3,
                    timeout=600,
                )

        # ROUTE 2: Proprietary Models (Single JSONL File)
        elif RUN_MODE == "PROPRIETARY_JSONL":
            # Assuming gemini is what you are testing, grab the first from the list
            model_name = PROPRIETARY_MODELS[0] 
            INPUT_FILENAME = "batch_results_prediction_model_merged_20260807_150549.jsonl"
            input_file_path = os.path.join(INPUT_DIR, INPUT_FILENAME)
            
            if not os.path.exists(input_file_path):
                print(f"  [ERROR] Input file not found: {input_file_path}")
            else:
                evaluate_jsonl_file(
                    input_jsonl=input_file_path,
                    model_name=model_name,
                    output_jsonl=os.path.join(OUTPUT_DIR, f"sparql_evaluation_results_{model_name}.jsonl"),
                    output_csv=os.path.join(OUTPUT_DIR, f"sparql_evaluation_results_{model_name}.csv"),
                    summary_jsonl=os.path.join(OUTPUT_DIR, f"sparql_evaluation_summary_{model_name}.jsonl"),
                    summary_csv=os.path.join(OUTPUT_DIR, f"sparql_evaluation_summary_{model_name}.csv"),
                    runs=3,
                    timeout=600,
                )
        else:
            print(f"  [ERROR] Unrecognized RUN_MODE: {RUN_MODE}")

    if RUN_CROSS_MODEL_SUMMARY:
        # Generates combinations across all models in ALL_MODELS
        generate_cross_model_summary(ALL_MODELS, OUTPUT_DIR)

    print("\n============================================================")
    print("  PIPELINE COMPLETION")
    print("  All selected operations have finished.")
    print("============================================================\n")