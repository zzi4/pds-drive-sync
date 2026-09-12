# PDS Read-Only Inventory and Fetch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone command-line tool that authenticates to Alibaba Cloud PDS with a user API Key, inventories every accessible drive, and downloads the files currently present without using an Agent at runtime.

**Architecture:** A Python 3.12 standard-library application orchestrates the official Aliyun CLI and PDS plugin. A narrow CLI adapter owns command construction, User-Agent injection, JSON parsing, retries, and redaction; domain services own drive discovery, inventory, snapshots, and downloads. Runtime data and credentials stay outside version control, and all cloud operations in this phase are read-only.

**Tech Stack:** Python 3.12 (`argparse`, `dataclasses`, `getpass`, `hashlib`, `json`, `pathlib`, `subprocess`, `tomllib`, `unittest`), Bash launcher, Aliyun CLI >= 3.3.16, PDS plugin >= 0.7.7.

**Spec:** `docs/plans/2026-09-12-pds-drive-sync-design.md`

**Execution override (2026-09-12):** At the user's request, implementation was reduced to a minimal core with focused configuration, credential-redaction, path-containment, pagination, and CLI-initialization tests instead of the full per-module test matrix below. The runtime behavior and security constraints remain unchanged.

## Global Constraints

- Runtime must not call Codex, an Agent, or any model API.
- PDS operations must be read-only: `get-user`, drive listing, `search-file`, `resolve-path`, and `download-to-local` only.
- Every `aliyun pds` invocation must include `--user-agent AlibabaCloud-Agent-Skills/alibabacloud-pds-intelligent-workspace/<32-lowercase-hex-session-id>`.
- Never write or print the API Key in project files, snapshots, logs, exceptions, or test fixtures.
- Use the official API Key authentication branch with default domain `bj39311`; do not request a PDS user ID.
- Query all accessible personal, team, and enterprise drives.
- Prefer `list-all-drives`; on a 403, fall back to `list-my-drives` and `list-my-group-drive`.
- Use `search-file` typed flags with `--recursive true`; never construct raw PDS query syntax.
- Download with `download-to-local`, preserve cloud paths under a per-drive directory, and never overwrite an unrelated local file silently.
- Use only Python's standard library for the application and tests.

---

## File Map

- `pds-sync`: executable project entry point; launches `python3 -m pds_sync` from the project root.
- `pds_sync/__main__.py`: argument parsing and `setup`, `status`, `inventory`, and `fetch` command coordination.
- `pds_sync/config.py`: settings model, TOML loading, defaults, validation, and project-relative path resolution.
- `pds_sync/installer.py`: project-private Aliyun CLI discovery, download, safe tar extraction, and version checks.
- `pds_sync/cli.py`: subprocess boundary, stable per-run User-Agent, JSON decoding, retry policy, error classification, and secret redaction.
- `pds_sync/service.py`: PDS user verification, drive discovery with documented fallback, paginated recursive inventory, and path resolution.
- `pds_sync/snapshot.py`: normalized metadata projection, statistics, atomic snapshot writes, and `latest.json` update.
- `pds_sync/download.py`: destination-path containment, existing-file handling, temporary downloads, verification, and atomic publish.
- `config.example.toml`: non-sensitive defaults users can edit.
- `.gitignore`: excludes credentials, runtime config, private CLI binary, snapshots, downloads, caches, and temporary files.
- `README.md`: setup and daily command instructions, security behavior, and output layout.
- `tests/test_config.py`: config defaults, override, and validation tests.
- `tests/test_cli.py`: command construction, session User-Agent, JSON parsing, retry, and redaction tests.
- `tests/test_service.py`: drive listing, 403 fallback, pagination, and item normalization tests.
- `tests/test_snapshot.py`: summary and atomic output tests.
- `tests/test_download.py`: path containment, skip/conflict behavior, temporary file, and size verification tests.
- `tests/test_commands.py`: command-level workflow tests with injected fake services.

---

### Task 1: Project Configuration and Safe Runtime Layout

**Files:**
- Create: `.gitignore`
- Create: `config.example.toml`
- Create: `pds-sync`
- Create: `pds_sync/__init__.py`
- Create: `pds_sync/config.py`
- Create: `tests/__init__.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write failing configuration tests**

Cover these cases in `tests/test_config.py`:

```python
class ConfigTests(unittest.TestCase):
    def test_defaults_target_all_spaces_and_detected_domain(self): ...
    def test_relative_runtime_paths_are_resolved_from_project_root(self): ...
    def test_toml_overrides_non_secret_settings(self): ...
    def test_unknown_space_type_is_rejected(self): ...
    def test_api_key_is_not_a_supported_config_field(self): ...
```

- [ ] **Step 2: Run the tests and confirm the expected failure**

Run: `python3 -m unittest tests.test_config -v`

Expected: import failure because `pds_sync.config` does not exist.

- [ ] **Step 3: Implement the minimal settings model**

Implement an immutable `Settings` dataclass with exact defaults:

```python
domain_id = "bj39311"
spaces = ("personal", "team", "enterprise")
page_size = 100
snapshot_dir = PROJECT_ROOT / "snapshots"
download_dir = PROJECT_ROOT / "downloads"
aliyun_cli_path = None
timeout_seconds = 60
read_retries = 2
```

Load `config.toml` with `tomllib` when it exists. Reject unknown top-level keys, especially `api_key`, and validate `page_size` in `1..100`, positive timeout, non-negative retries, and known space values.

- [ ] **Step 4: Add launcher, example config, and ignores**

`pds-sync` must use its own directory as the project root and execute `python3 -m pds_sync "$@"`. The example TOML contains every supported non-secret field. Ignore `config.toml`, `.tools/`, `snapshots/`, `downloads/`, `__pycache__/`, `.coverage`, and `*.part`.

- [ ] **Step 5: Run focused tests and syntax checks**

Run:

```bash
python3 -m unittest tests.test_config -v
python3 -m compileall -q pds_sync tests
bash -n pds-sync
```

Expected: all tests pass and both syntax checks exit 0.

- [ ] **Step 6: Commit the configuration slice**

```bash
git add .gitignore config.example.toml pds-sync pds_sync tests
git commit -m "feat: add standalone PDS sync project configuration"
```

### Task 2: Aliyun CLI Boundary, Installation, and Secret Redaction

**Files:**
- Create: `pds_sync/cli.py`
- Create: `pds_sync/installer.py`
- Create: `tests/test_cli.py`
- Modify: `pds_sync/__main__.py`

- [ ] **Step 1: Write failing CLI adapter tests**

Test an injected subprocess executor so no real network or credentials are needed:

```python
class PdsCliTests(unittest.TestCase):
    def test_every_pds_command_has_one_stable_session_user_agent(self): ...
    def test_api_key_is_redacted_from_errors(self): ...
    def test_json_output_is_required(self): ...
    def test_read_timeout_retries_but_configuration_does_not(self): ...
    def test_nonzero_result_preserves_exit_code_and_sanitized_stderr(self): ...
```

Add installer tests for CLI selection priority and safe archive member selection. Mock `urllib.request.urlopen` and `tarfile.open`; never access the network from tests.

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: `python3 -m unittest tests.test_cli -v`

Expected: imports fail because the adapter and installer do not exist.

- [ ] **Step 3: Implement `PdsCli`**

Create one session ID with `secrets.token_hex(16)` in the constructor. Use `subprocess.run` with an argument list and `shell=False`. `run_pds()` appends the exact User-Agent to every command, parses one JSON document, and raises a typed `CliError` containing exit code, sanitized stdout/stderr, and helpers for `is_forbidden` and `is_retryable_read_error`.

Maintain a per-call `secret_values` collection and replace every non-empty secret with `[REDACTED]` before constructing errors. Never include complete argv in user-facing exceptions.

- [ ] **Step 4: Implement project-private CLI discovery and install**

Selection order:

1. Explicit `aliyun_cli_path` from config.
2. `PROJECT_ROOT/.tools/aliyun`.
3. `shutil.which("aliyun")`.

For Linux `x86_64`, download the official `aliyun-cli-linux-latest-amd64.tgz` into a temporary directory, select only the regular archive member whose basename is `aliyun`, copy it to `.tools/aliyun`, and set mode `0755`. Reject symlinks, absolute paths, traversal paths, unexpected architectures, and versions below `3.3.16`.

- [ ] **Step 5: Implement one-time API Key setup orchestration**

`setup` must:

1. Resolve or install the CLI.
2. Run `aliyun version` and enforce `>= 3.3.16`.
3. Run `aliyun configure set --auto-plugin-install true`.
4. Run `aliyun pds version` with the session User-Agent and enforce `>= 0.7.7`.
5. Read the Key with `getpass.getpass("PDS API Key: ")` unless `PDS_API_KEY` is set; never print its value.
6. Invoke `aliyun pds config --domain-id <domain> --authentication-type api_key --api-key <key>` with the session User-Agent and the Key registered for redaction.
7. Delete the local Key reference in a `finally` block and verify with `get-user`.

The environment variable is supported for user-run automation but must never be created, displayed, or persisted by the project.

- [ ] **Step 6: Run focused tests**

Run:

```bash
python3 -m unittest tests.test_cli -v
python3 -m compileall -q pds_sync tests
```

Expected: all tests pass.

- [ ] **Step 7: Commit the CLI boundary**

```bash
git add pds_sync/cli.py pds_sync/installer.py pds_sync/__main__.py tests/test_cli.py
git commit -m "feat: add secure Aliyun PDS CLI adapter"
```

### Task 3: User, Drive, and Recursive Inventory Services

**Files:**
- Create: `pds_sync/service.py`
- Create: `tests/test_service.py`
- Modify: `pds_sync/__main__.py`

- [ ] **Step 1: Write failing service tests**

Use a fake `PdsCli` returning recorded JSON responses:

```python
class PdsServiceTests(unittest.TestCase):
    def test_get_user_returns_only_safe_identity_fields(self): ...
    def test_list_all_drives_filters_configured_space_types(self): ...
    def test_403_falls_back_to_personal_and_group_drive_calls(self): ...
    def test_non_403_drive_error_does_not_fall_back(self): ...
    def test_inventory_repeats_exact_typed_flags_with_next_marker(self): ...
    def test_inventory_normalizes_only_documented_fields(self): ...
    def test_resolve_file_path_requires_an_absolute_contained_cloud_path(self): ...
```

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run: `python3 -m unittest tests.test_service -v`

Expected: import failure because `pds_sync.service` does not exist.

- [ ] **Step 3: Implement user and drive discovery**

`get_user()` returns only `domain_id`, `user_id`, and `nick_name`.

`list_drives()` calls `list-all-drives` once with a `--cli-query` projection for `drive_id`, `drive_name`, `space_type`, `owner_type`, `owner`, `total_size`, and `used_size`. On 403 only, call `list-my-drives` and `list-my-group-drive`, exhaust documented `next_marker` pages, label personal/team/enterprise spaces, and deduplicate by `drive_id`.

- [ ] **Step 4: Implement recursive inventory**

For each selected drive, call the documented typed interface:

```text
search-file --drive-id <id> --limit <1..100>
            --recursive true --return-total-count true
            --cli-query <safe projection>
```

The projection must retain `next_marker` plus these item fields: `drive_id`, `file_id`, `parent_file_id`, `name`, `type`, `size`, `category`, `file_extension`, `created_at`, `updated_at`, `content_hash`, `content_hash_name`, and `revision_id`. For later pages, repeat the identical flags and add only `--marker <returned marker>`. Reject missing IDs rather than issuing a downstream command with an empty ID.

- [ ] **Step 5: Implement path resolution for downloads**

For file items only, call `resolve-path --drive-id <id> --file-id <id>` with a projection containing `path`. Require a leading `/`, reject NUL and `..` components, and return a `PurePosixPath`. Do not reconstruct paths by walking parent IDs.

- [ ] **Step 6: Wire `status` and `inventory` command services**

`status` prints safe user identity and a fixed-width drive table. `inventory` invokes user verification once, drive discovery once, and inventory once per selected drive. Keep filesystem persistence out of this task; return a structured result to the command layer.

- [ ] **Step 7: Run service and regression tests**

Run:

```bash
python3 -m unittest tests.test_service -v
python3 -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit the service slice**

```bash
git add pds_sync/service.py pds_sync/__main__.py tests/test_service.py
git commit -m "feat: inventory all accessible PDS drives"
```

### Task 4: Snapshot Persistence and Existing-Data Statistics

**Files:**
- Create: `pds_sync/snapshot.py`
- Create: `tests/test_snapshot.py`
- Modify: `pds_sync/__main__.py`

- [ ] **Step 1: Write failing snapshot tests**

Cover deterministic output and failure safety:

```python
class SnapshotTests(unittest.TestCase):
    def test_summary_counts_files_folders_bytes_spaces_and_categories(self): ...
    def test_write_creates_expected_json_and_ndjson_files(self): ...
    def test_latest_pointer_updates_only_after_complete_snapshot(self): ...
    def test_partial_failure_report_never_marks_snapshot_complete(self): ...
    def test_atomic_writer_leaves_no_part_file_after_success(self): ...
```

- [ ] **Step 2: Run focused tests and confirm they fail**

Run: `python3 -m unittest tests.test_snapshot -v`

Expected: import failure because `pds_sync.snapshot` does not exist.

- [ ] **Step 3: Implement summary calculation**

Calculate total drives, files, folders, and file bytes plus breakdowns by `space_type`, `drive_id`, and file `category`. Treat missing sizes as zero and keep raw byte counts in JSON; human-readable formatting belongs only to terminal output.

- [ ] **Step 4: Implement atomic snapshot persistence**

Create `snapshots/<UTC-YYYYMMDDTHHMMSSZ>/`, write UTF-8 JSON with `ensure_ascii=False`, write one normalized item per line to `files.ndjson`, `fsync`, and use `os.replace` for each final file. Update `snapshots/latest.json` only after user, drives, files, and summary files all succeed.

- [ ] **Step 5: Wire snapshot creation into `inventory`**

Print the final snapshot directory and compact totals. Return 0 only for a complete inventory; otherwise keep the diagnostic artifact and return nonzero.

- [ ] **Step 6: Run snapshot and regression tests**

Run:

```bash
python3 -m unittest tests.test_snapshot -v
python3 -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit the snapshot slice**

```bash
git add pds_sync/snapshot.py pds_sync/__main__.py tests/test_snapshot.py
git commit -m "feat: persist PDS inventory snapshots and statistics"
```

### Task 5: Safe Download of Current Cloud Files

**Files:**
- Create: `pds_sync/download.py`
- Create: `tests/test_download.py`
- Modify: `pds_sync/__main__.py`

- [ ] **Step 1: Write failing download tests**

Cover security and local-data preservation:

```python
class DownloadTests(unittest.TestCase):
    def test_target_is_namespaced_by_space_and_drive(self): ...
    def test_cloud_path_cannot_escape_download_root(self): ...
    def test_supported_remote_hash_match_skips_existing_file(self): ...
    def test_unverified_existing_file_uses_conflict_name(self): ...
    def test_download_uses_file_id_and_temporary_path(self): ...
    def test_size_mismatch_does_not_publish_partial_file(self): ...
    def test_success_atomically_publishes_and_records_result(self): ...
    def test_one_failure_does_not_prevent_other_downloads(self): ...
```

- [ ] **Step 2: Run focused tests and confirm they fail**

Run: `python3 -m unittest tests.test_download -v`

Expected: import failure because `pds_sync.download` does not exist.

- [ ] **Step 3: Implement contained destination mapping**

Map `/A/B/file.ext` to `downloads/<space_type>-<drive_id>/A/B/file.ext`. Resolve the target and assert it remains under the configured download root. Do not use drive names as directory identifiers. Sanitize only local-invalid NUL and traversal components; preserve Unicode filenames.

- [ ] **Step 4: Implement existing-file policy**

If the cloud item supplies `content_hash_name` of `sha1` or `sha256`, compute that hash locally and skip only on an exact digest match. Otherwise do not assume equal-size files are identical: choose a conflict destination with UTC timestamp and file ID suffix. Never overwrite the pre-existing path.

- [ ] **Step 5: Implement verified temporary download and atomic publish**

Call:

```text
download-to-local --drive-id <drive_id> --file-id <file_id>
                  --save-to <same-directory-temporary-path>
```

The PDS plugin verifies remote size. Independently compare the returned `size`, cloud metadata size, and local temporary-file size when all are present. On success, `fsync` and `os.replace` to the chosen destination. On failure, remove only the tool-created temporary file and record sanitized error details.

- [ ] **Step 6: Wire `fetch`**

`fetch` performs a fresh complete inventory and snapshot, filters entries with `type == "file"`, resolves each path, downloads sequentially, and writes `downloads.json` atomically into the same snapshot directory. Print counts for downloaded, skipped, conflicted, and failed files. Return nonzero if inventory or any download fails.

- [ ] **Step 7: Run download and regression tests**

Run:

```bash
python3 -m unittest tests.test_download -v
python3 -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit the fetch slice**

```bash
git add pds_sync/download.py pds_sync/__main__.py tests/test_download.py
git commit -m "feat: download current PDS files safely"
```

### Task 6: Command-Level Tests, Documentation, and Real Read-Only Validation

**Files:**
- Create: `tests/test_commands.py`
- Create: `README.md`
- Modify: `config.example.toml`
- Modify: `pds_sync/__main__.py`

- [ ] **Step 1: Write failing command workflow tests**

Test exit codes and output without a live Key:

```python
class CommandTests(unittest.TestCase):
    def test_status_prints_user_and_all_space_types(self): ...
    def test_inventory_prints_snapshot_path_and_totals(self): ...
    def test_fetch_downloads_only_file_items(self): ...
    def test_authentication_failure_never_contains_supplied_key(self): ...
    def test_help_describes_no_mutating_cloud_command(self): ...
```

- [ ] **Step 2: Run the tests and confirm the intended failures**

Run: `python3 -m unittest tests.test_commands -v`

Expected: assertions fail until command dependency injection and final messages are implemented.

- [ ] **Step 3: Complete argument parsing and stable exit codes**

Provide:

```text
./pds-sync setup [--domain-id ID] [--no-install]
./pds-sync status [--config PATH]
./pds-sync inventory [--config PATH]
./pds-sync fetch [--config PATH]
```

Exit codes: `0` success, `2` local configuration/usage error, `3` authentication/permission error, `4` network/CLI error, `5` incomplete inventory or download. Catch expected errors at the outer boundary and print sanitized, actionable messages to stderr.

- [ ] **Step 4: Write README usage and security documentation**

Document that runtime is Agent-free, cloud operations are read-only, `setup` uses hidden input, credentials are owned by Aliyun CLI, and current-file download runs with `./pds-sync fetch`. Include output directory examples, configuration fields, recovery for expired/disabled Keys, and the exact commands for running tests.

- [ ] **Step 5: Run the complete offline verification suite**

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q pds_sync tests
bash -n pds-sync
git diff --check
```

Expected: all tests pass and all other commands exit 0.

- [ ] **Step 6: Install and verify the real project-private Aliyun CLI**

Run `./pds-sync setup` only up to the hidden API Key prompt if interactive input is unavailable. Verify `./.tools/aliyun version` reports at least `3.3.16` and `./.tools/aliyun pds version` reports at least `0.7.7`, with the required User-Agent on the PDS command.

Do not copy the Key from conversation history into a tool call. The user enters it locally with hidden input, or exports `PDS_API_KEY` in their own shell before invoking setup.

- [ ] **Step 7: Perform live user-driven read-only acceptance**

After the user completes hidden setup locally, run:

```bash
./pds-sync status
./pds-sync inventory
./pds-sync fetch
```

Expected: safe user identity, all accessible spaces, a complete snapshot, and the single current cloud file under `downloads/<space_type>-<drive_id>/...`. Inspect only names, sizes, paths, counts, and exit codes; never print credentials or signed URLs.

- [ ] **Step 8: Commit final documentation and integration tests**

```bash
git add README.md config.example.toml pds_sync/__main__.py tests/test_commands.py
git commit -m "docs: explain standalone PDS inventory and fetch workflow"
```

- [ ] **Step 9: Run final repository verification**

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q pds_sync tests
bash -n pds-sync
git status --short
git log --oneline --decorate -8
```

Expected: tests and checks pass; worktree is clean; commit history shows the design, plan, and implementation slices.
