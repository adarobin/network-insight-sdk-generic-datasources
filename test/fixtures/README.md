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

There are two routes, producing identical fixture layouts. Use whichever matches
your network access.

### Directly, if the module is reachable

    ./.venv/bin/python tools/capture_vc_cli.py -i <mgmt-ip> -u <read-only-user>

Writes one file per command into `hpe/vc-se-100gb-f32/`, named after the command,
plus `_manifest.json` recording which commands the device actually answered, the
detected prompt, and which paging command it accepted.

The password is prompted for, or read from `VC_PASSWORD` to keep it out of shell
history. Add `-J [user@]jumphost` to hop via a jump host. If that host requires
MFA, authenticate once into an SSH control socket first, otherwise every run
triggers a fresh prompt:

    # ~/.ssh/config
    Host <jump-host>
        ControlMaster auto
        ControlPath ~/.ssh/cm-%r@%h:%p
        ControlPersist 8h

    ssh -fN <jump-host>

### By hand, when the module is only reachable from elsewhere

Useful when the only route in is a jump box you cannot install Python on, such as
a Windows host running PuTTY.

1. **Widen the terminal to about 250 columns** before connecting. The device wraps
   output to the negotiated width and hard-wrapped rows are painful to parse.
   In PuTTY this is Window > Columns.
2. **Enable session logging**: Session > Logging > "All session output", and pick
   a file.
3. Connect to the module and paste the contents of `tools/probe_commands.txt`.
   It begins with `no pagination` so no `--More--` markers interrupt the output.
   Paste in small batches rather than all at once; the CLI can drop input it
   cannot keep up with, and `show running-config` takes a while.
4. Copy the log file back, then split it into fixtures:

       ./.venv/bin/python tools/split_session_log.py <session.log>

   Add `--dry-run` first to check the prompt is being recognised. `--prompt`
   overrides it if the device prompt is not `OneView>`.

`tools/probe_commands.txt` is generated from the same command list the netmiko
tool uses, so the two routes cannot drift apart.

## Layout

    hpe/vc-se-100gb-f32/
        _manifest.json           capture metadata and per-command status
        help.txt                 full command set as reported by the device
        show-interfaces-status.txt
        show-ip-route.txt
        ...
