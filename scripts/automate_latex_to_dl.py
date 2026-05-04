import os

schemas = [
    entry for entry in os.listdir("schemas") 
    if os.path.isdir(os.path.join("schemas", entry))
]

for schema in schemas:
    input_path = os.path.join(f'schemas/{schema}', 'schema.tex')
    output_path = os.path.join(f'schemas/{schema}', 'axiom.txt')
    ttl_path = os.path.join(f'schemas/{schema}', 'schema.ttl')
    os.system(f"python3 latex_to_dl.py {input_path} {output_path} --add-prefixes {ttl_path}")
