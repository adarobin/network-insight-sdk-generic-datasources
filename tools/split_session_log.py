#!/usr/bin/env python
# Copyright 2026 The Regents of the University of Michigan
# SPDX-License-Identifier: BSD-2-Clause

"""Split an interactive CLI session log into per-command fixture files.

An alternative to tools/capture_vc_cli.py for when the module is not reachable
from the machine doing the parser work: capture one interactive session by hand
with terminal logging enabled, then split it here into the same fixture layout
the parser tests expect.

    python tools/split_session_log.py session.log

Commands are recognised by the device prompt, so the log needs nothing more than
the prompt lines the device already emits.

Before capturing, two things matter for parseability:

  * Widen the terminal to ~250 columns. The device wraps output to the negotiated
    width, and hard-wrapped rows are painful to parse. PuTTY: Window > Columns.
  * Send 'no pagination' as the first command, so no --More-- markers land in the
    middle of the output.

tools/probe_commands.txt lists the commands to paste, in a useful order.
"""

import argparse
import json
import os
import re
import sys

DEFAULT_PROMPT = 'OneView>'
DEFAULT_OUTPUT_DIR = os.path.join('test', 'fixtures', 'hpe', 'vc-se-100gb-f32')

# PuTTY and friends leave these behind; they are never part of command output.
ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')
PAGER_MARKER = re.compile(r'^\s*-+\s*(more|More|MORE)\s*-+\s*$')


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        description='Split an interactive CLI session log into per-command fixture files.')
    parser.add_argument('logfile', help='Session log captured from the device')
    parser.add_argument('-o', '--output-dir', default=DEFAULT_OUTPUT_DIR,
                        help='Directory for fixture files (default %s)' % DEFAULT_OUTPUT_DIR)
    parser.add_argument('--prompt', default=DEFAULT_PROMPT,
                        help="Device prompt that precedes each command (default %r)"
                             % DEFAULT_PROMPT)
    parser.add_argument('--keep-pager-markers', action='store_true',
                        help='Keep --More-- lines instead of dropping them')
    parser.add_argument('-n', '--dry-run', action='store_true',
                        help='Report what would be written without writing anything')
    return parser.parse_args(argv)


def slugify(command):
    """Match the naming that capture_vc_cli.py uses, so fixtures interchange."""
    if command.strip() == '?':
        return 'question-mark'
    slug = re.sub(r'[^a-z0-9]+', '-', command.strip().lower()).strip('-')
    return slug or 'command'


def clean(line):
    return ANSI_ESCAPE.sub('', line.rstrip('\r\n').replace('\x08', ''))


def split_session(lines, prompt, keep_pager_markers=False):
    """Return [(command, output_lines)] in the order they appear in the log."""
    # A prompt line looks like 'OneView> show vlan'. Anything before the first
    # one is login banner noise.
    prompt_pattern = re.compile(r'^\s*' + re.escape(prompt) + r'\s?(.*)$')

    sections = []
    command = None
    output = []

    for raw in lines:
        line = clean(raw)
        match = prompt_pattern.match(line)
        if match:
            if command is not None:
                sections.append((command, output))
            command = match.group(1).strip()
            output = []
            continue
        if command is None:
            continue  # pre-login banner
        if not keep_pager_markers and PAGER_MARKER.match(line):
            continue
        output.append(line)

    if command is not None:
        sections.append((command, output))

    # A trailing bare prompt means the operator logged out; it carries no command.
    return [(c, o) for c, o in sections if c]


def main(argv=None):
    args = parse_arguments(argv)

    with open(args.logfile, errors='replace') as handle:
        lines = handle.readlines()

    sections = split_session(lines, args.prompt, args.keep_pager_markers)
    if not sections:
        sys.stderr.write(
            "No commands found. Expected lines beginning %r.\n"
            "Pass --prompt if the device prompt differs.\n" % args.prompt)
        return 1

    if not args.dry_run and not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    manifest = {'source_log': os.path.basename(args.logfile), 'prompt': args.prompt,
                'commands': []}

    for command, output in sections:
        text = '\n'.join(output).strip('\n')
        filename = '%s.txt' % slugify(command)
        if not args.dry_run:
            with open(os.path.join(args.output_dir, filename), 'w') as fixture:
                fixture.write(text + '\n' if text else '')
        manifest['commands'].append(
            {'command': command, 'file': filename, 'bytes': len(text),
             'lines': len(output)})
        print('  %-34s -> %-34s %6d bytes' % (command, filename, len(text)))

    if not args.dry_run:
        manifest_path = os.path.join(args.output_dir, '_manifest.json')
        with open(manifest_path, 'w') as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
        print('\n%d commands written to %s' % (len(sections), args.output_dir))
    else:
        print('\n%d commands found (dry run, nothing written)' % len(sections))
    return 0


if __name__ == '__main__':
    sys.exit(main())
