import os

EXCLUDE = {".idea", ".venv", ".git"}

def print_tree(start_path, prefix=""):
    try:
        entries = sorted(os.listdir(start_path))
    except PermissionError:
        return

    for i, name in enumerate(entries):
        if name in EXCLUDE:
            continue

        path = os.path.join(start_path, name)
        is_last = (i == len(entries) - 1)
        connector = "└── " if is_last else "├── "
        print(prefix + connector + name)

        if os.path.isdir(path):
            extension = "    " if is_last else "│   "
            print_tree(path, prefix + extension)

if __name__ == "__main__":
    print_tree(".")
