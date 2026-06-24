import ast
import os


def parse_config_ast(file_path):
    """
    Parses a python config file using AST to find all assignment nodes,
    returning a list of info dicts for top-level assignments.
    """
    if not os.path.exists(file_path):
        return [], []

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    lines = content.splitlines(keepends=True)
    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        print(f"Syntax error in {file_path}: {e}")
        return [], lines

    assignments = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments.append(
                        {
                            "key": target.id,
                            "start_line": node.lineno - 1,  # 0-indexed line number
                            "end_line": node.end_lineno
                            if hasattr(node, "end_lineno") and node.end_lineno
                            else node.lineno,
                        }
                    )

    return assignments, lines


def extract_sample_blocks(sample_path):
    """
    Extracts all top-level keys from config_sample.py in order, along with their block text (including preceding comments).
    """
    assignments, lines = parse_config_ast(sample_path)
    if not assignments:
        return []

    sample_blocks = []
    current_comments = []
    line_idx = 0

    assign_map = {a["start_line"]: a for a in assignments}

    while line_idx < len(lines):
        if line_idx in assign_map:
            assign_info = assign_map[line_idx]
            end_line = assign_info["end_line"]
            block_lines = lines[line_idx:end_line]

            sample_blocks.append(
                {
                    "key": assign_info["key"],
                    "comments": current_comments,
                    "block": "".join(block_lines),
                    "start_line": line_idx,
                    "end_line": end_line,
                }
            )
            current_comments = []
            line_idx = end_line
        else:
            line = lines[line_idx]
            stripped = line.strip()
            if stripped.startswith("#") or stripped == "":
                current_comments.append(line)
            else:
                current_comments = []
            line_idx += 1

    return sample_blocks


def sync_configs(sample_file, private_file):
    if not os.path.exists(sample_file):
        print(f"Error: Sample file '{sample_file}' not found.")
        return

    if not os.path.exists(private_file):
        print(f"Private config file '{private_file}' does not exist.")
        return

    private_assigns, private_lines = parse_config_ast(private_file)
    sample_blocks = extract_sample_blocks(sample_file)

    private_keys = {a["key"] for a in private_assigns}
    missing_items = [b for b in sample_blocks if b["key"] not in private_keys]

    if not missing_items:
        print(
            f"✅ '{private_file}' is up to date with '{sample_file}'. No missing keys found."
        )
        return

    print(f"Found {len(missing_items)} missing variable(s) in '{private_file}':")
    for item in missing_items:
        print(f"  - {item['key']}")

    # Build relative ordering map from sample_file
    sample_key_order = [b["key"] for b in sample_blocks]

    # Map private key positions (line numbers)
    private_key_end_lines = {}
    for a in private_assigns:
        private_key_end_lines[a["key"]] = a["end_line"]

    # We will insert missing items into private_lines at the exact relative spot
    # Iterate through missing items in sample order
    # To maintain line indices while inserting, we track offset or work backwards/by insertion targets.

    # Create an insertion plan: key -> list of text blocks to insert after line index
    insertions_after_line = {}  # line_index -> string text to insert
    append_blocks = []

    for item in missing_items:
        key = item["key"]
        sample_idx = sample_key_order.index(key)

        # Find nearest preceding key in sample that EXISTS in private config
        prev_existing_key = None
        for prev_key in reversed(sample_key_order[:sample_idx]):
            if prev_key in private_keys:
                prev_existing_key = prev_key
                break

        # Find nearest succeeding key in sample that EXISTS in private config
        next_existing_key = None
        for next_key in sample_key_order[sample_idx + 1 :]:
            if next_key in private_keys:
                next_existing_key = next_key
                break

        # Determine insertion line in private_lines
        block_text = ""
        if item["comments"]:
            block_text += "".join(item["comments"])
        block_text += item["block"]
        if not block_text.endswith("\n"):
            block_text += "\n"

        if prev_existing_key:
            target_line = private_key_end_lines[prev_existing_key]
            insertions_after_line.setdefault(target_line, []).append(block_text)
        elif next_existing_key:
            # Insert before next existing key
            target_assign = next(
                a for a in private_assigns if a["key"] == next_existing_key
            )
            target_line = target_assign["start_line"]
            insertions_after_line.setdefault(
                target_line - 1 if target_line > 0 else 0, []
            ).append(block_text)
        else:
            append_blocks.append(block_text)

    # Reconstruct updated private config lines
    updated_lines = []
    for idx, line in enumerate(private_lines):
        updated_lines.append(line)
        line_num = idx + 1  # 1-indexed line end
        if line_num in insertions_after_line:
            for text in insertions_after_line[line_num]:
                updated_lines.append(text)

    if append_blocks:
        if updated_lines and not updated_lines[-1].endswith("\n"):
            updated_lines.append("\n")
        for text in append_blocks:
            updated_lines.append(text)

    with open(private_file, "w", encoding="utf-8") as pf:
        pf.writelines(updated_lines)

    print(
        f"\n🎉 Successfully updated '{private_file}' with {len(missing_items)} missing configuration option(s) at their matching position!"
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Auto update private config.py from config_sample.py"
    )
    parser.add_argument("--sample", default=None, help="Path to config_sample.py")
    parser.add_argument("--target", default=None, help="Path to private config.py")
    args = parser.parse_args()

    # Determine default paths intelligently:
    # 1. Check current working directory
    # 2. Check parent directories if script is inside gen_scripts/config/
    script_dir = os.path.dirname(os.path.abspath(__file__))
    gen_scripts_dir = os.path.dirname(script_dir)
    root_dir = os.path.dirname(gen_scripts_dir)

    sample_path = args.sample
    if not sample_path:
        if os.path.exists("config_sample.py"):
            sample_path = "config_sample.py"
        elif os.path.exists(os.path.join(root_dir, "config_sample.py")):
            sample_path = os.path.join(root_dir, "config_sample.py")
        elif os.path.exists(os.path.join(gen_scripts_dir, "config_sample.py")):
            sample_path = os.path.join(gen_scripts_dir, "config_sample.py")
        else:
            sample_path = "config_sample.py"

    target_path = args.target
    if not target_path:
        if os.path.exists("config.py"):
            target_path = "config.py"
        elif os.path.exists(os.path.join(root_dir, "config.py")):
            target_path = os.path.join(root_dir, "config.py")
        elif os.path.exists(os.path.join(gen_scripts_dir, "config.py")):
            target_path = os.path.join(gen_scripts_dir, "config.py")
        else:
            target_path = "config.py"

    sync_configs(sample_path, target_path)
