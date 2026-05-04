import re
import argparse
from pathlib import Path

def build_prefix_map(ttl_path):
    """
    Scans a TTL file for defined prefixes and builds mappings to convert 
    raw URIs and terms into their clean, prefixed versions.
    """
    prefix_map = {}        # Maps standalone terms (e.g., 'AgentRole' -> 'enslaved:AgentRole')
    namespace_map = {}     # Maps full URI strings to their prefix (e.g., 'http://.../XMLSchema#' -> 'xsd:')
    valid_prefixes = set() # Keeps track of which prefixes are actually declared in the file

    # Regex to find prefix declarations: @prefix prefixName: <URI> .
    # Group 1 captures the prefix name (can be empty for default ':')
    # Group 2 captures the full URI inside the angle brackets
    prefix_decl_pattern = re.compile(r'(?:@prefix|PREFIX)\s+([^:]*):\s+<([^>]+)>', re.IGNORECASE)
    
    # Regex to find terms using a prefix (e.g., owl:Class, :hasAge)
    # Group 1: The prefix itself
    # Group 2: The actual term/entity/predicate
    term_pattern = re.compile(r'([a-zA-Z0-9_-]*):([a-zA-Z_][a-zA-Z0-9_-]+)')
    
    try:
        with open(ttl_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        # Step 1: Collect valid prefixes and their corresponding full URIs
        for match in prefix_decl_pattern.finditer(content):
            pref = match.group(1)
            uri = match.group(2)
            valid_prefixes.add(pref)
            namespace_map[uri] = f"{pref}:"
            
        # Step 2: Find all entities/predicates that use those exact valid prefixes
        for match in term_pattern.finditer(content):
            pref, term = match.groups()
            if pref in valid_prefixes:
                if term not in prefix_map:
                    prefix_map[term] = f"{pref}:{term}"
                    
    except Exception as e:
        print(f"Warning: Could not read TTL file '{ttl_path}': {e}")
        
    return prefix_map, namespace_map

def apply_prefixes_to_axioms(axiom_path, prefix_map, namespace_map):
    """
    Reads the generated text file containing the axioms, fixes malformed datatypes, 
    and prepends prefixes to the mapped terms.
    """
    if not prefix_map and not namespace_map:
        return

    with open(axiom_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    # Sort namespaces by length descending. 
    # This ensures longer, more specific URIs are replaced before shorter, overlapping ones.
    sorted_namespaces = sorted(namespace_map.items(), key=lambda x: len(x[0]), reverse=True)

    updated_lines = []
    for line in lines:
        # Step 1: Fix malformed Datatypes and raw URIs
        for uri, pref in sorted_namespaces:
            # Replaces artifacts like 'Datatypehttp://www.w3.org/2001/XMLSchema#string' -> 'xsd:string'
            line = line.replace(f"Datatype{uri}", pref)
            # Failsafe: also catch the raw URI just in case it appears without 'Datatype'
            line = line.replace(uri, pref)

        # Step 2: Apply regular prefix mapping to standalone terms
        def replace_if_mapped(match):
            word = match.group(0)
            return prefix_map.get(word, word)
            
        # The negative lookbehind '(?<![#/.:])' ensures we only replace standalone words.
        # The '#', '/', and '.' protect raw URIs or paths from being accidentally mangled.
        # The ':' prevents double-prefixing by ignoring terms that already have a prefix attached 
        # (e.g., stopping 'xsd:string' from turning into 'xsd:xsd:string').
        updated_line = re.sub(r'(?<![#/.:])\b[a-zA-Z_][a-zA-Z0-9_-]*\b', replace_if_mapped, line)
        updated_lines.append(updated_line)

    # Overwrite the axiom file with the updated, prefixed lines
    with open(axiom_path, 'w', encoding='utf-8') as f:
        for line in updated_lines:
            f.write(line)

def latex_to_dl_text(input_filepath, output_filepath, ttl_filepath=None):
    """
    Parses a LaTeX file containing description logic axioms, converts the math commands 
    to Unicode symbols, and writes the clean axioms to a text file.
    """
    # Resolve paths to handle '~' (home directory) and relative/absolute paths robustly
    in_path = Path(input_filepath).expanduser().resolve()
    out_path = Path(output_filepath).expanduser().resolve()

    # Dictionary mapping LaTeX commands to standard Unicode DL symbols
    replacements = {
        r'\\ensuremath{\\sqsubseteq}': '⊑',
        r'\\ensuremath{\\equiv}': '≡',
        r'\\ensuremath{\\sqcap}': '⊓',
        r'\\ensuremath{\\sqcup}': '⊔',
        r'\\ensuremath{\\exists}': '∃',
        r'\\ensuremath{\\forall}': '∀',
        r'\\ensuremath{\\lnot}': '¬',
        r'\\ensuremath{\\leq}': '≤',
        r'\\ensuremath{\\geq}': '≥',
        r'\\ensuremath{=}': '=',
        r'\\ensuremath{\\top}': '⊤',
        r'\\ensuremath{\\bot}': '⊥',
        r'\\ensuremath{\\Self}': 'Self',
        r'\\ensuremath{\^-\}': '⁻',      # Inverse property modifier
        r'\\ensuremath{hasValue}': 'hasValue',
        r'\\^\\^': '^^',                 # Datatype casting
        r'\\_': '_',                     # Unescape underscores
        r'~': ' ',                       # Replace LaTeX non-breaking spaces with standard spaces
        r'\\\\': '',                     # Remove forced newlines
    }

    # Regex patterns for removing structural LaTeX artifacts (headers, styling, etc.)
    cleanup_patterns = [
        r'\\begin\{.*?\}',
        r'\\end\{.*?\}',
        r'\\subsection\*\{.*?\}',
        r'\\subsubsection\*\{.*?\}',
        r'\\section\*\{.*?\}',
        r'\\documentclass\{.*?\}',
        r'\\usepackage\{.*?\}',
        r'\\parskip.*',
        r'\\parindent.*',
        r'\\oddsidemargin.*',
        r'\\textwidth.*',
    ]

    # Stop execution if the input file doesn't exist
    if not in_path.is_file():
        print(f"Error: Could not find or read '{in_path}'")
        return

    with open(in_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    parsed_axioms = []
    in_document = False

    # Main processing loop
    for line in lines:
        line = line.strip()

        # Only process content safely contained between the main document tags
        if '\\begin{document}' in line:
            in_document = True
            continue
        if '\\end{document}' in line:
            break
        if not in_document or not line:
            continue

        # Skip headers entirely to keep the text file strictly to DL axioms
        if line.startswith('\\section') or line.startswith('\\sub'):
            continue

        processed_line = line

        # Substitute LaTeX math symbols with their Unicode equivalents
        for latex, unicode_char in replacements.items():
            processed_line = re.sub(latex, unicode_char, processed_line)

        # Catch any remaining \ensuremath{...} wrappers and strip them, keeping inner content
        processed_line = re.sub(r'\\ensuremath\{(.*?)\}', r'\1', processed_line)

        # Remove structural LaTeX commands
        for pattern in cleanup_patterns:
            processed_line = re.sub(pattern, '', processed_line)

        # Handle specific OWL/Protege text artifacts
        processed_line = processed_line.replace('TransitiveProperty', 'TransitiveProperty: ')
        processed_line = processed_line.replace('DisjointObjectProperties', 'DisjointObjectProperties: ')
        
        # Clean up multiple spaces left behind by deletions
        processed_line = re.sub(r'\s+', ' ', processed_line).strip()

        if processed_line:
            parsed_axioms.append(processed_line)

    # Ensure the parent directories for the output file exist before writing
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, 'w', encoding='utf-8') as f:
        for axiom in parsed_axioms:
            f.write(axiom + '\n')

    print(f"Success! Extracted {len(parsed_axioms)} axioms to '{out_path.name}'.")

    # --- PREFIX & NAMESPACE HANDLING ---
    if ttl_filepath:
        ttl_path = Path(ttl_filepath).expanduser().resolve()
        
        if ttl_path.is_file():
            print(f"Scanning '{ttl_path.name}' for prefix and namespace mappings...")
            prefix_map, namespace_map = build_prefix_map(ttl_path)
            
            if prefix_map or namespace_map:
                apply_prefixes_to_axioms(out_path, prefix_map, namespace_map)
                print(f"Cleaned datatypes and attached prefixes to {len(prefix_map)} unique terms.")
            else:
                print("No map-able prefixed terms or namespaces found in the TTL file.")
        else:
            print(f"Warning: '--add-prefixes' flag used, but no TTL file found at '{ttl_path}'.")

if __name__ == "__main__":
    # Setup argparse to handle command line arguments gracefully
    parser = argparse.ArgumentParser(description="Convert LaTeX DL axioms to text format.")
    parser.add_argument("input_filepath", help="Path to the input .tex file")
    parser.add_argument("output_filepath", help="Path to the output .txt file")
    parser.add_argument("--add-prefixes", type=str, metavar="TTL_FILE",
                        help="Path to the corresponding .ttl file to extract and add prefixes.")
    
    args = parser.parse_args()
    
    # Execute the main function with the provided arguments
    latex_to_dl_text(args.input_filepath, args.output_filepath, args.add_prefixes)

# Example usage:
    # To convert without prefixes:
        # python3 latex_to_dl.py <input_tex_path> <output_txt_path>
        # python3 latex_to_dl.py schemas/enslaved/schema.tex schemas/enslaved/axiom.txt
    # To convert with prefixes from the TTL:
        # python3 latex_to_dl.py <input_tex_path> <output_txt_path> --add-prefixes <ttl_path>
        # python3 latex_to_dl.py schemas/enslaved/schema.tex schemas/enslaved/axiom.txt --add-prefixes schemas/enslaved/schema.ttl
