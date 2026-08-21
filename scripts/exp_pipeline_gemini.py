import sys
sys.path.append("..")

import os
import json
import importlib
import random
import time
from datetime import datetime
from itertools import product
from google import genai
import google.auth
from google.genai import types
from google.cloud import storage

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================

PROMPT_ASSET_DIR = "../prompt_assets"
LOCAL_JSON_REQUESTS_FILE = "json_requests.jsonl"
LOCAL_RESULTS_DIR = "../results"
BUCKET_NAME = "saini-research"

# Generate a timestamp to match the 'YYYYMMDD_HHMMSS' folder structure from the images
run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
GCS_BASE_PATH = "schema-to-sparql-resolution-effects"
GCS_INPUT_DIR = f"{GCS_BASE_PATH}/inputs_{run_timestamp}"
GCS_OUTPUT_DIR = f"{GCS_BASE_PATH}/batch_results_{run_timestamp}"

schema_lookup = {
    "kwg": {
        "axiom": "../schemas/kwg/axiom.txt",
        "nen": "../schemas/kwg/nen.txt",
        "ttl": "../schemas/kwg/schema.ttl"
    },
    "kwg_lite": {
        "axiom": "../schemas/kwg_lite/axiom.txt",
        "nen": "../schemas/kwg_lite/nen.txt",
        "ttl": "../schemas/kwg_lite/schema.ttl"
    },
    "core_scholar_rich": {
        "axiom": "../schemas/core_scholar_rich/axiom.txt",
        "nen": "../schemas/core_scholar_rich/nen.txt",
        "ttl": "../schemas/core_scholar_rich/schema.ttl"
    },
    "core_scholar_shallow": {
        "axiom": "../schemas/core_scholar_shallow/axiom.txt",
        "nen": "../schemas/core_scholar_shallow/nen.txt",
        "ttl": "../schemas/core_scholar_shallow/schema.ttl"
    }
}

experiment_setups = [
    {"kg_ids": ["kwg"], "representations": ["axiom"], "temperatures": [0.0, 1.0], "prompt_types": ["0_shot", "cot"], "task": "sparql"},
    {"kg_ids": ["kwg"], "representations": ["nen"],  "temperatures": [0.0, 1.0], "prompt_types": ["0_shot", "cot"], "task": "sparql"},
    {"kg_ids": ["kwg"], "representations": ["ttl"], "temperatures": [0.0, 1.0], "prompt_types": ["0_shot", "cot"], "task": "sparql"},

    {"kg_ids": ["kwg_lite"], "representations": ["axiom"], "temperatures": [0.0, 1.0], "prompt_types": ["0_shot", "cot"], "task": "sparql"},
    {"kg_ids": ["kwg_lite"], "representations": ["nen"],  "temperatures": [0.0, 1.0], "prompt_types": ["0_shot", "cot"], "task": "sparql"},
    {"kg_ids": ["kwg_lite"], "representations": ["ttl"], "temperatures": [0.0, 1.0], "prompt_types": ["0_shot", "cot"], "task": "sparql"},

    {"kg_ids": ["core_scholar_rich"], "representations": ["axiom"], "temperatures": [0.0, 1.0], "prompt_types": ["0_shot", "cot"], "task": "sparql"},
    {"kg_ids": ["core_scholar_rich"], "representations": ["nen"],  "temperatures": [0.0, 1.0], "prompt_types": ["0_shot", "cot"], "task": "sparql"},
    {"kg_ids": ["core_scholar_rich"], "representations": ["ttl"], "temperatures": [0.0, 1.0], "prompt_types": ["0_shot", "cot"], "task": "sparql"},

    {"kg_ids": ["core_scholar_shallow"], "representations": ["axiom"], "temperatures": [0.0, 1.0], "prompt_types": ["0_shot", "cot"], "task": "sparql"},
    {"kg_ids": ["core_scholar_shallow"], "representations": ["nen"],  "temperatures": [0.0, 1.0], "prompt_types": ["0_shot", "cot"], "task": "sparql"},
    {"kg_ids": ["core_scholar_shallow"], "representations": ["ttl"], "temperatures": [0.0, 1.0], "prompt_types": ["0_shot", "cot"], "task": "sparql"},

    {"kg_ids": ["kwg"], "representations": ["axiom", "nen"], "temperatures": [0.0, 1.0], "prompt_types": ["0_shot", "cot"], "task": "sparql"},
    {"kg_ids": ["kwg_lite"], "representations": ["axiom", "nen"], "temperatures": [0.0, 1.0], "prompt_types": ["0_shot", "cot"], "task": "sparql"},
    {"kg_ids": ["core_scholar_rich"], "representations": ["axiom", "nen"], "temperatures": [0.0, 1.0], "prompt_types": ["0_shot", "cot"], "task": "sparql"},
    {"kg_ids": ["core_scholar_shallow"], "representations": ["axiom", "nen"], "temperatures": [0.0, 1.0], "prompt_types": ["0_shot", "cot"], "task": "sparql"}
]

# ==========================================
# 2. HELPER FUNCTIONS
# ==========================================

def load_file_to_string(file_path):
    with open(file_path, "r", encoding="utf-8") as file:
        return file.read()

def fill_prompt_template(template_text, values_dict):
    for key, value in values_dict.items():
        template_text = template_text.replace(f"{{{key}}}", value)
    return template_text

def get_CQ_list(ids):
    cq_list = []
    for id in ids:
        cq_blob = load_file_to_string(f"../cqs/{id}.txt")
        questions = [line.strip() for line in cq_blob.splitlines() if line.strip()]
        cq_list.extend(questions)
    return cq_list

def build_kg_variation_context(ids, representations, schema_lookup=schema_lookup):
    blocks = []
    for kg_id in ids:
        if kg_id not in schema_lookup:
            print(f"KG-{kg_id} not found in schema lookup.")
            continue
        rep_blocks = []
        for rep in representations:
            rep_key = rep.lower()
            if rep_key not in schema_lookup[kg_id]:
                print(f"Representation '{rep}' not found for KG-{kg_id}.")
                continue
            file_name = schema_lookup[kg_id][rep_key]
            rep_content = load_file_to_string(file_name)
            rep_blocks.append(rep_content)
        joined_rep_blocks = "\n\n".join(rep_blocks)
        blocks.append(f"KG-{kg_id} schema context:\n{joined_rep_blocks}")
    return "\n\n".join(blocks)

def load_prompt_module(task, prompt_type):
    module_name = f"prompts.{task}_{prompt_type}"
    prompt_module = importlib.import_module(module_name)
    prompt_module = importlib.reload(prompt_module)
    if not hasattr(prompt_module, "SYSTEM_PROMPT"):
        raise ValueError(f"{module_name} does not define SYSTEM_PROMPT")
    if not hasattr(prompt_module, "USER_PROMPT_TEMPLATE"):
        raise ValueError(f"{module_name} does not define USER_PROMPT_TEMPLATE")
    return prompt_module.SYSTEM_PROMPT, prompt_module.USER_PROMPT_TEMPLATE

def load_prompt_asset(task, kg_id, allow_default=True):
    asset_path = os.path.join(PROMPT_ASSET_DIR, task, f"{kg_id}.json")
    if not os.path.exists(asset_path):
        if not allow_default:
            raise FileNotFoundError(f"Prompt asset not found: {asset_path}")
        asset_path = os.path.join(PROMPT_ASSET_DIR, task, "default.json")
    with open(asset_path, "r", encoding="utf-8") as file:
        return json.load(file)

def format_kg_selection_examples(examples):
    formatted_examples = []
    for index, example in enumerate(examples, start=1):
        formatted_examples.append(
            f"Example {index}:\n"
            f"Competency Question: {example['cq']}\n"
            f"Answer: {example['answer']}"
        )
    return "\n\n".join(formatted_examples)

def format_sparql_examples(examples):
    formatted_examples = []
    for index, example in enumerate(examples, start=1):
        formatted_examples.append(
            f"Example {index}:\n"
            f"Competency Question: {example['cq']}\n"
            f"Answer: {example['query']}"
        )
    return "\n\n".join(formatted_examples)

def build_reasoning_example(example, answer_key, format_reasoning_list=False):
    reasoning = example["reasoning"]
    if format_reasoning_list and isinstance(reasoning, list):
        reasoning = "\n" + "\n".join(f"- {step}" for step in reasoning)
    return (
        "Example 1:\n"
        f"Competency Question: {example['cq']}\n"
        f"Reasoning: {reasoning}\n"
        f"Answer: {example[answer_key]}"
    )

def load_prompt_support(task, kg_ids, prompt_asset_ids=None):
    asset_ids = prompt_asset_ids or kg_ids
    allow_default_asset = prompt_asset_ids is None
    assets = [load_prompt_asset(task, asset_id, allow_default=allow_default_asset) for asset_id in asset_ids]
    default_asset = load_prompt_asset(task, "default")
    random_asset_index = random.randint(0, len(assets) - 1) if assets else None
    reasoning_steps = assets[random_asset_index].get("reasoning_steps") or default_asset.get("reasoning_steps", [])

    if task in ("kg_sel", "kg_mul_sel"):
        few_shot_examples = []
        for asset in assets:
            few_shot_examples.extend(asset.get("few_shot_examples", []))
        if not few_shot_examples:
            few_shot_examples = default_asset.get("few_shot_examples", [])
        random.shuffle(few_shot_examples)
        few_shot_examples = few_shot_examples[:5]
        reasoning_asset = assets[random_asset_index] if assets else default_asset
        reasoning_example = build_reasoning_example(
            reasoning_asset.get("reasoning_example", default_asset.get("reasoning_example", {})),
            "answer",
            format_reasoning_list=(task == "kg_mul_sel"),
        )
        return {
            "few_shot_examples": format_kg_selection_examples(few_shot_examples),
            "reasoning_steps": "\n".join(reasoning_steps),
            "reasoning_example": reasoning_example,
        }

    if task == "sparql":
        asset = assets[0] if assets else default_asset
        few_shot_examples = asset.get("few_shot_examples") or default_asset.get("few_shot_examples", [])
        reasoning_example = build_reasoning_example(
            asset.get("reasoning_example", default_asset.get("reasoning_example", {})),
            "query",
        )
        return {
            "few_shot_examples": format_sparql_examples(few_shot_examples),
            "reasoning_steps": "\n".join(reasoning_steps),
            "reasoning_example": reasoning_example,
        }
    raise ValueError(f"Unsupported task: {task}")


# ==========================================
# 3. GENERATE LOCAL JSONL BATCH FILE
# ==========================================

print("Generating JSON requests...")
json_requests = []
model = "gemini-2.5-pro"

for setup in experiment_setups:
    cq_ids = setup.get("cq_ids", setup["kg_ids"])
    kg_ids = setup["kg_ids"]
    representations = setup["representations"]
    temperatures = setup["temperatures"]
    prompt_types = setup["prompt_types"]
    task = setup["task"]
    prompt_asset_ids = setup.get("prompt_asset_ids")
    
    if prompt_asset_ids is None and task == "kg_mul_sel":
        prompt_asset_ids = cq_ids

    cqs = get_CQ_list(cq_ids)
    schema_context = build_kg_variation_context(ids=kg_ids, representations=representations, schema_lookup=schema_lookup)
    prompt_support = load_prompt_support(task, kg_ids, prompt_asset_ids=prompt_asset_ids)

    for prompt_type, temperature in product(prompt_types, temperatures):
        try:
            system_prompt, user_prompt_template = load_prompt_module(task, prompt_type)
            for cq in cqs:
                if task in ("kg_sel", "kg_mul_sel"):
                    input_data = {
                        "Insert_CQ_here": cq,
                        "Insert_schemas_here": schema_context,
                    }
                    if prompt_type == "few_shot":
                        input_data["Insert_examples_here"] = prompt_support["few_shot_examples"]
                    elif prompt_type == "cot":
                        input_data["Insert_reasoning_here"] = prompt_support["reasoning_steps"]
                        input_data["Insert_reasoning_example_here"] = prompt_support["reasoning_example"]

                elif task == "sparql":
                    input_data = {
                        "Insert_CQ_here": cq,
                        "Insert_schema_here": schema_context,
                    }
                    if prompt_type == "few_shot":
                        input_data["Insert_examples_here"] = prompt_support["few_shot_examples"]
                    elif prompt_type == "cot":
                        input_data["Insert_reasoning_here"] = prompt_support["reasoning_steps"]
                        input_data["Insert_reasoning_example_here"] = prompt_support["reasoning_example"]
                else:
                    raise ValueError(f"Unsupported task: {task}")

                filled_prompt = fill_prompt_template(user_prompt_template, input_data)

                json_requests.append({
                    "key": f"{task}-{'-'.join(representations)}-{'-'.join(kg_ids)}-{prompt_type}-temp{temperature}-{cq}",
                    "request": {
                        "system_instruction": {"parts": [{"text": system_prompt}]},
                        "contents": [
                            {
                                "role": "user",
                                "parts": [{"text": filled_prompt}]
                            }
                        ]
                    }
                })
        except Exception as e:
            print(f"Error generating requests: task={task} | prompt_type={prompt_type} | temp={temperature} | error={e}")

with open(LOCAL_JSON_REQUESTS_FILE, 'w', encoding='utf-8') as f:
    for request_data in json_requests:
        f.write(json.dumps(request_data) + '\n')
print(f"Successfully generated {LOCAL_JSON_REQUESTS_FILE} with {len(json_requests)} requests.")


# ==========================================
# 4. GCS UPLOAD & BATCH JOB SUBMISSION
# ==========================================

print("\nAuthenticating with Google Cloud...")
credentials, projectId = google.auth.default()
client = genai.Client(
    vertexai=True,
    project=projectId,
    location="us-central1",
    credentials=credentials
)
storage_client = storage.Client()
bucket = storage_client.bucket(BUCKET_NAME)

# Path definitions mapping to folder structures in the screenshots
gcs_destination_blob_name = f"{GCS_INPUT_DIR}/{LOCAL_JSON_REQUESTS_FILE}"
gcs_input_uri = f"gs://{BUCKET_NAME}/{gcs_destination_blob_name}"
gcs_output_prefix = f"gs://{BUCKET_NAME}/{GCS_OUTPUT_DIR}/"

print(f"Uploading {LOCAL_JSON_REQUESTS_FILE} to {gcs_input_uri}...")
blob = bucket.blob(gcs_destination_blob_name)
blob.upload_from_filename(LOCAL_JSON_REQUESTS_FILE)

print("Starting Vertex AI Batch Job...")
file_batch_job = client.batches.create(
    model=model,
    src=gcs_input_uri,
    config=types.CreateBatchJobConfig(
        dest=gcs_output_prefix,
        display_name=f"sparql-{run_timestamp}"
    )
)
print(f"Job created successfully! Name: {file_batch_job.name}")


# ==========================================
# 5. POLL JOB STATUS & DOWNLOAD RESULTS
# ==========================================

print("Polling job status (checks every 30 seconds)...")
job_name = file_batch_job.name

while True:
    batch_job = client.batches.get(name=job_name)
    state = batch_job.state.name
    
    if state == 'JOB_STATE_SUCCEEDED':
        print(f"\nJob completed successfully! Fetching results from GCS...")
        
        # Ensure local results directory exists
        os.makedirs(LOCAL_RESULTS_DIR, exist_ok=True)
        
        # Search the output directory in GCS for the predictions file
        blobs = bucket.list_blobs(prefix=f"{GCS_OUTPUT_DIR}/")
        downloaded = False
        
        for blob in blobs:
            if blob.name.endswith('.jsonl'):
                # Force a uniquely timestamped name so previous runs aren't overwritten
                unique_local_name = f"predictions_{run_timestamp}.jsonl"
                local_file_path = os.path.join(LOCAL_RESULTS_DIR, unique_local_name)
                
                print(f"Downloading {blob.name} to {local_file_path}...")
                blob.download_to_filename(local_file_path)
                print(f"Successfully saved to: {local_file_path}")
                downloaded = True
                break # Usually only one predictions.jsonl per batch job
        
        if not downloaded:
            print("Warning: Job succeeded but no .jsonl output file was found in GCS.")
        break
        
    elif state in ['JOB_STATE_FAILED', 'JOB_STATE_CANCELLED', 'JOB_STATE_PARTIALLY_SUCCEEDED']:
        print(f"\nJob ended with status: {state}")
        if batch_job.error:
            print(f"Error details: {batch_job.error}")
        break
        
    else:
        # Job is still running (e.g., JOB_STATE_PENDING, JOB_STATE_RUNNING)
        print(f"Current State: {state}. Waiting 30 seconds...")
        time.sleep(30)