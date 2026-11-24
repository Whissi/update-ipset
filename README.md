# update-ipset

Replace an ipset with the addresses collected from a directory of blocklist or
allowlist files. Supported types are `hash:ip` and `hash:net`, for IPv4 (`inet`)
and IPv6 (`inet6`). Run with permission to administer ipsets, normally as root.

## Usage

```sh
update-ipset --quiet --logger \
  --setname ipv4_blacklist --settype hash:net --family inet \
  --data-path /var/lib/ipset/ipv4_blacklist.d
```

For allowlists, add `--disallow-empty-set` to retain the existing set when the
input contains no entries. Empty sets are otherwise allowed, including when no
`.dat` files exist. Use `--help` for all options.

The script requires Bash, ipset, awk, GNU find/coreutils (`find`, `cat`, `mktemp`,
`rm`, and either `realpath` or `readlink`). `logger` is needed for `--logger`.
Set-name validation accepts 3–28 characters
matching `^[[:alnum:]][[:alnum:]_-]{2,27}$`.

## Input format and failures

Only top-level `*.dat` files are read. Each data line contains one numeric IP
address or CIDR prefix, appropriate to the selected family and set type.
IPv4-mapped IPv6 addresses are supported. Hostnames, extra ipset options and
inline comments are not accepted. ipset validates address and prefix syntax.

```text
# Example IPv4 source
192.0.2.1
198.51.100.0/24
```

Blank lines and full-line comments beginning with `#` are ignored. Leading and
trailing whitespace, CRLF line endings and a final line without a newline are
accepted. Directories whose names end in `.dat` are skipped with a warning;
other nonregular sources, including FIFOs and broken symlinks, cause an error.
Symlinks to regular files are allowed.

Lines containing characters outside the selected family's numeric address
alphabet are skipped. IPv6 entries are skipped for `inet`; plain IPv4 entries
are skipped for `inet6`. The success summary reports the skipped count, also
in syslog with `--logger`. `--verbose` additionally reports each skipped entry
with its source and line number. Blank lines and comments do not count as
skipped entries. `--disallow-empty-set` checks the entries left after filtering.

This filter is not full address validation: an entry such as `999.999.999.999`
passes the character filter but fails when ipset validates it during loading.
Errors enumerating or copying sources, writing the restore file, or loading
entries also abort before publication. An existing target retains its previous
contents. Error messages go to stderr even with `--quiet`; `--logger` includes
failure details in syslog.

Reported entry counts are counts of input lines, not unique set elements:
duplicates are harmless, and a CIDR can expand into multiple entries in a
`hash:ip` set.

## Publication and concurrency

Each invocation creates a private temporary set using a name such as
`ipv4_blacklist_tmp_a1B2c3D4e5`. The target prefix is limited to 15 characters,
and the random suffix comes from the invocation's private working directory.
Creation is exclusive. A name collision fails without reusing or destroying the
existing set.

1. Enumerate and copy each source with checked I/O, and generate the restore file.
2. Create and populate the private set. Duplicates are ignored during restore.
3. Publish using `rename` when the target does not exist, or `swap` when it does.
4. Destroy the old set and remove the working directory.

Both first publication and replacement expose a fully populated set atomically.
The target name and its firewall/list:set references continue to refer to the
new data after a swap. Changing `maxelem` is supported; incompatible families or
set types are rejected by ipset. The full sequence is not a transaction: if
cleanup fails after publication, the new data is already active, and the error
message says so.

Overlapping invocations use separate temporary sets. **The last successful
publication wins**, even if that invocation started earlier. The script does
not enforce ordering by invocation start time.

Normal failures and handled signals clean up resources owned by the invocation.
`SIGKILL` or a crash can leave temporary sets behind; other invocations do not
remove them automatically. The recognizable name and `--verbose` output help
identify these resources. `--keep-working-dir` preserves the restore file and
error log for debugging, but does not preserve the temporary kernel set.

Exit statuses include `0` for success, `1` for a general failure or rejected
empty update, `2` for ipset failures, `64` for command-line errors, `74` for I/O
failures, and `78` for configuration errors. Handled signals
use `128 + signal number` (for example, `143` for SIGTERM). A cleanup error after
publication can produce a nonzero status even though the new data is active.

## Updating source files

Prepare each source file completely before making it available to `update-ipset`:
write to a temporary filename that **does not end in `.dat`**, then rename it to
the final `.dat` name on the same filesystem. Reading a file that is still being
overwritten can otherwise yield incomplete data without any I/O error. The
script's checked copies do not provide a transaction across the entire source
directory.

## Capacity and memory

The script calculates `maxelem` from accepted input for every update, with a
reserve of 10,000 elements. There is no fixed application-level element ceiling:

```text
maxelem = min(estimated elements + 10000, 4294967295)
```

The estimate follows the selected set type:

- For `hash:net`, each address or CIDR prefix counts as one element.
- For IPv4 `hash:ip`, a CIDR prefix counts as `2^(32 - prefix length)`
  addresses; a plain address counts as one. The calculation follows ipset's
  numeric notation, including octal prefix lengths with leading zeroes.
- For IPv6 `hash:ip`, each address counts as one. ipset does not support
  expanding IPv6 prefixes shorter than `/128` into this set type.

For example, five addresses produce `maxelem=10005`, while one IPv4 `/18`
in `hash:ip` produces `maxelem=26384`. An estimate of 12,000,000 elements
produces `maxelem=12010000`. Existing sets are replaced with the calculated
capacity, so subsequent updates can grow or shrink without manual recreation.

Duplicates and overlapping IPv4 prefixes may overestimate the required
capacity. This avoids a separate deduplication pass. Estimates and the reserve
are capped at ipset's unsigned 32-bit `maxelem` limit of 4,294,967,295; the
actual load must still fit that limit and the available system resources.

**`maxelem` is a limit, not a reservation of memory for that many entries.**
The initial `hashsize` remains 64, and the hash table grows as userspace adds
entries. Memory depends on the hash table, actual stored elements and their
extensions. During replacement, the old and new populated sets coexist
temporarily.

See the [ipset manual](https://ipset.netfilter.org/ipset.man.html) and
[Linux hash-set implementation](https://github.com/torvalds/linux/blob/master/net/netfilter/ipset/ip_set_hash_gen.h).

## Development and tests

The version remains unchanged during development; update it when preparing a
release and tag, after reviewing the individual commits.

```sh
bash -n update-ipset
shellcheck update-ipset
sudo python3 tests/integration.py -v
```

The Python standard-library integration suite automatically starts a fresh
network namespace using `unshare --net`; it needs Linux, root, ipset and unshare.
It uses real kernel sets, including list:set references, to exercise migrations,
concurrent publication, input failures, signals, cleanup and diagnostics.
Fault-injection wrappers live only in temporary test directories. No production
sets are modified by the test suite.
