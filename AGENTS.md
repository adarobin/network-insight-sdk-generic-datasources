# AGENTS.md

Guidance for AI agents working in this repository.

## What this repository is

A fork of the **archived** `vmware-archive/network-insight-sdk-generic-datasources`. It
turns CLI output from network devices VCF Operations for Networks (vRNI) does not natively
support into CSVs, zipped for upload as a "generic router/switch" data source.

The SDK runs **on your workstation, not on the collector**. It SSHes to a device, parses
text, writes CSVs, zips them. You upload the zip by hand; the product ingests static CSVs
and never polls the device (the Add Source form takes no credentials). Data is a
point-in-time snapshot, stale until re-uploaded.

Remotes: `origin` is this fork, `upstream` is the archive. Upstream can never accept a PR.
`upstream/aruba`, `upstream/extreme` and `upstream/alcatel-lucent-os6900-mg` are unmerged
vendor branches worth consulting for prior art.

## Commands

Setup (`requirements.txt` pins `netmiko>=4.0`; the editable `network-insight-sdk-python`
line is commented out and only needed for scripted upload):

    python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

Everything below needs the repo root on `PYTHONPATH`:

    export PYTHONPATH=$(pwd)

Tests (plain `unittest`, no pytest config, no CI):

    ./.venv/bin/python -m unittest discover -s test          # all
    ./.venv/bin/python -m unittest test.routers_and_switches.hpe.test_hpe_pre_post_processor
    ./.venv/bin/python -m unittest test.routers_and_switches.hpe.test_hpe_pre_post_processor.RoutesTestCase.test_vrf_is_taken_from_the_routing_table_heading

Several parsers also carry doctests, run by executing the module directly:

    ./.venv/bin/python network_insight_sdk_generic_datasources/parsers/common/horizontal_table_parser.py

Collect from a live device and write the zip:

    ./.venv/bin/python ./network_insight_sdk_generic_datasources/main.py \
        -d hpe -m vc-se-100gb-f32 -s <DEVICE_TYPE> \
        -i <ip> -u <user> -p <pass> -o out.zip

`-d` is the directory under `routers_and_switches/`, `-m` a top-level key in that vendor's
YAML, `-s` a member of the `DeviceType` enum. Leave `-z/--self_zip` at its default; it
copies the whole SDK source into the zip. Output lands in `/tmp/uani/<ip>/`, which is
**not cleared between runs**.

### Working without device access

`tools/parse_fixtures.py` runs the entire real pipeline against captured fixtures with a
stand-in for the SSH layer, and with `-z` writes the uploadable zip. This is both the
regression check after any YAML or parser edit and a complete route to a package when the
device is unreachable from the machine doing the work:

    ./.venv/bin/python tools/parse_fixtures.py -d hpe -m vc-se-100gb-f32 -i <real-ip> -z out.zip

Fixtures come from either `tools/capture_vc_cli.py` (netmiko, supports `-J` jump hosts) or
`tools/split_session_log.py` (splits a hand-captured PuTTY session log on the device
prompt). Both write the same layout, matching commands to files by the same slug.
`tools/probe_commands.txt` is the pasteable command list, generated from the netmiko tool's
own list so the two routes cannot drift.

**Captures under `test/fixtures/` are gitignored** — they contain internal addresses,
serials, MACs and neighbour hostnames, and this fork is public. Tests therefore inline
sanitised snippets. See `test/fixtures/README.md`.

## Architecture

`main.py` → `common/physical_device.py` is the whole engine:

    execute_commands()  →  join_tables()  →  write_results()

`execute_commands` walks `workloads` in YAML order, dispatching on which key a workload
has — `reuse_tables` (transform tables already built) > `reuse_command` (re-parse output
already captured, no second SSH round trip) > `command` (run it). Each workload's result
lands in `result_map[table_id]`. Per workload the chain is:

    block_parser  →  pre_process  →  parser  →  post_process

Then `table_joiners` merge tables, and `result_writer.table_id` names which `table_id`s
become CSVs. Every parser and processor **must return a list of dicts** or the engine
raises `TypeError`.

### Discovery is by filesystem convention, not registration

There is no plugin registry. `main.py` computes `routers_and_switches/<-d>/<-d>.yml`, so a
vendor directory and its YAML must share a name. `import_module_utilities` hardcodes
`<vendor>_pre_post_processor` as the module holding that vendor's classes. A new vendor is
exactly four files and needs no framework change:

    routers_and_switches/<vendor>/
        __init__.py
        <vendor>.yml             anchors, workloads, table_joiners, result_writer
        <model>.yml              manifest selecting/ordering anchors
        <vendor>_pre_post_processor.py

### The YAML anchor/include mechanism

`common/yaml_utilities.py` monkeypatches `SafeLoader.compose_document` so anchors defined
in `<vendor>.yml` stay resolvable inside `<model>.yml`, which is pulled in with
`<model>: !include <model>.yml`. The model file is therefore just a selection and ordering
manifest of `- <<: *ANCHOR` entries. Order matters: a workload using `reuse_tables` must
come after the workloads that build them.

Class references resolve two different ways:

- `parser: name:` and `block_parser` **without** `arguments` → full dotted module path.
- `block_parser` **with** `arguments` → bare class name, looked up in `parsers/common/block_parser`.
- `pre_post_processor:` / `reuse_table_processor:` → bare class name, looked up in the
  vendor's `<vendor>_pre_post_processor` module.

Prefer **fully qualified** paths for `parser: name:`. The truncated
`routers_and_switches.x.y` form the Cisco files use only resolves because `main.py` runs
as a script and puts the package directory on `sys.path`; it breaks under any other import
context, including the tests and `tools/parse_fixtures.py`.

### Reusable parsers

Compose these from YAML rather than writing new ones (`parsers/common/`):
`GenericBlockParser` (facade over `LineBasedBlockParser` via `line_pattern`, or
`PatternBasedBlockParser` via `start_pattern`/`end_pattern`), `HorizontalTableParser`
(columnar; `data_split_size: 2` splits on double spaces and skips dashed rules, which is
what multi-word columns need), `VerticalTableParser` (`key: value`), `GenericTextParser`
(a `rules` dict of regexes), `XmlParser`.

Device hooks: subclass `PrePostProcessor` (`pre_process`/`post_process`, or define `parse`
and be used directly as the `parser`), `TableProcessor` (`process_tables`), or
`SimpleTableJoiner` (override `update`).

`GenericTextParser` behaviour worth knowing: rules are applied to **every line** of a
block, so the last match wins; patterns are matched with `re.match` against the
**stripped** line; and a rule that never matches yields an **empty string** rather than a
missing key, which makes optional fields safe.

## Traps in this codebase

The upstream project is unmaintained and these are real, verified behaviours:

- **`CsvWriter` silently skips an empty table** — zero rows means the CSV is simply absent
  from the zip, with only a log warning. Mandatory vRNI files need at least one row.
- **`PhysicalDevice` indexes `table[0]` unguarded for `table_id: switch`**, so a failed
  identity parse is an `IndexError`, not a warning. It also rewrites that row's `name` to
  `<name>-<ip>` and sets `ipAddress/fqdn` from `-i`.
- **netmiko 4 ignores `delay_factor`/`max_loops`.** `SSHConnectHandler` now sets
  `read_timeout` explicitly (default 120s); reverting that silently reintroduces a 10s
  timeout that truncates long output.
- **`ZipArchiver`** takes the `result_writer` table ids and archives only those, using
  `arcname` so CSVs sit at the archive root. Without both, stale files from an earlier run
  for the same IP get shipped and paths are stored relative to CWD.
- **`package_handler: ZipPackageHandler` in YAML is inert** — no such class exists;
  `main.py` only tests for the key's presence.
- Python 3 only. `dell_pre_post_processor.py` still calls `dict.has_key`, so do not copy
  Dell patterns without checking them.
- `MANIFEST.in` references a stale pre-refactor path; `setup.py`/`setup.cfg` are minimal
  `pbr` with no `install_requires`. The SDK is run from a clone, not pip-installed.

## The CSV contract

`README.md` section 4 is authoritative: which files are mandatory, which columns are
required, and the accepted values (`UP`/`DOWN`, `ACCESS`/`TRUNK`/`OTHER`,
`FULL`/`HALF`/`AUTO`, `connected` as lower-case `true`/`false`, CIDR addresses, `DIRECT`
for a connected route's next hop, comma-separated VLAN ids). `test/sample_csv_files/`
shows real output shapes.

**Known contradiction:** the README specifies interface speeds in bits per second, but the
sample CSVs carry Kbps (Cisco pipes `BW <n> Kbit` through unchanged). The HPE
implementation follows the README. If the product displays speeds 1000x low,
`SPEED_MULTIPLIERS` in `hpe_pre_post_processor.py` is the single place to change.

`EXAMPLE.md` walks through implementing a device end to end (Cisco N5K).

## HPE Virtual Connect SE (`-d hpe -m vc-se-100gb-f32`)

The read-only "netops" CLI is the Aricent/ISS switching stack behind a `OneView>` prompt
with Virtual Connect extensions (`show uplinkset`, `show uplinkport`, `show vfc`). It is
**not** the c-Class Virtual Connect Manager command set — `show enet-connection`,
`show profile`, `show server`, `show domain` and `show mac-cache` do not exist. Paging is
disabled with `no pagination`. SSH lands in a telnet client to 127.0.0.1 in character
mode, which netmiko has to tolerate.

- `show interfaces information` is the only command enumerating **every** port (252,
  including the `S-Channel` server downlinks that carry VM traffic). `show interfaces
  status` sees only the 100 `ethernet` ports. S-Channel blocks omit speed, MTU, duplex and
  hardware address.
- VLAN membership wraps across continuation lines in `show port vlanlist` and
  `show vlan brief`, and abbreviates runs as ranges. Both need unwrapping and expansion.
- There is no bare `show vrf`; the VRF name comes from the `Vrf Name:` heading in
  `show ip route`. That command also names no egress interface for a `via` route, which
  vRNI requires, so it is derived from the connected subnet containing the next hop.
- Use `show lldp neighbors detail`, not the summary: the summary truncates chassis ids and
  leaves the capability column blank on some rows, which shifts a columnar parse. Only the
  detail form carries the peer's real system name.
- VLAN names are OneView-generated GUIDs, not friendly names.

**Not implemented:** the live SSH path. It needs a `DeviceType` member, a custom netmiko
class for the prompt and `no pagination`, and jump-host plumbing through
`SSHConnectHandler` and `physical_device.py`. Until the module is reachable from a machine
that can run this, use the fixture route above. `tools/capture_vc_cli.py` already handles
`-J`, `--ssh-config`, and MFA jump hosts (an SSH `ControlMaster` avoids re-prompting).

## Native alternatives, for context

Both HPE data sources built into the product are pinned to versions from around 2016
(`HP Virtual Connect Manager 4.41, HP OneView 3.0`), and Broadcom states new versions are
not on the roadmap. Synergy has no Virtual Connect Manager at all, and modern OneView
connects but collects nothing. That is why the generic-switch route exists here.
