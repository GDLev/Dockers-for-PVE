#!/usr/bin/env python3
"""Build the PVE panel from the ordered frontend sources."""
import argparse
import json
from pathlib import Path
import sys


SOURCE = Path(__file__).resolve().parents[1] / 'frontend'


def build():
    names = json.loads((SOURCE / 'sources.json').read_text())
    parts = [(SOURCE / 'bootstrap.js').read_text()]
    for name in names:
        path = SOURCE / name
        if path.parent != SOURCE or path.suffix != '.js':
            raise ValueError(f'Invalid frontend source: {name}')
        parts.append(f'// {name}\n' + path.read_text())
    return '(function () {\n' + '\n'.join(parts) + '\n})();\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Write a bundle instead of printing it')
    args = parser.parse_args()
    source = build()
    if args.output:
        args.output.write_text(source)
        args.output.chmod(0o644)
    else:
        sys.stdout.write(source)


if __name__ == '__main__':
    main()
