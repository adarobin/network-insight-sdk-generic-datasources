# Device capture fixtures

Raw CLI output captured from real hardware, used by the parser unit tests.

**These captures are deliberately not tracked in git.** They contain internal
management IPs, chassis serials, MAC addresses, VLAN names and LLDP neighbour
hostnames, and this repository is a public fork. `.gitignore` excludes
everything here except this file.

Tests therefore assert against small, hand-sanitised snippets defined inline in
the test modules, not against these files. The captures are for authoring and
debugging parsers, and for diffing output after a firmware upgrade.

## Regenerating

    ./.venv/bin/python tools/capture_vc_cli.py -i <mgmt-ip> -u <read-only-user>

Writes one file per command into `hpe/vc-se-100gb-f32/`, named after the command,
plus `_manifest.json` recording which commands the device actually answered, the
detected prompt, and which paging command it accepted.

The password is prompted for, or read from `VC_PASSWORD` to keep it out of shell
history.

## Layout

    hpe/vc-se-100gb-f32/
        _manifest.json           capture metadata and per-command status
        help.txt                 full command set as reported by the device
        show-interfaces-status.txt
        show-ip-route.txt
        ...
