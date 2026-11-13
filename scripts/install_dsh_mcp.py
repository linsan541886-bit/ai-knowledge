"""Install the local knowledge MCP without rewriting existing DSH configuration.

Usage:
  python install_dsh_mcp.py install --install-dir "PATH/TO/DeepSeek Harness"
  python install_dsh_mcp.py install --install-dir "PATH/TO/DeepSeek Harness" --apply
  python install_dsh_mcp.py rollback --report PATH

Only appends one insert patch per profile. Backups are byte-for-byte; rollback
refuses to replace a file changed since installation. No model or MCP server is
started, and no credential store is read. DSH's installed configuration composer
and schema collector are used without mounting plugins or rewriting root files.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
DSH_HOME = Path.home() / ".dsh"
INSTALL = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Programs/DeepSeek Harness"
PYTHON = Path(sys.executable).resolve()
PROFILES = ("desktop", "headless")
ENTRY_ID = "mcp-local-knowledge"
ENTRY = {
    "id": ENTRY_ID,
    "name": "@deepseek-ai/dsh-mcp-client",
    "config": {
        "serverName": "local_knowledge",
        "transport": "stdio",
        "command": PYTHON.as_posix(),
        "args": [(ROOT / "scripts/mcp_server.py").as_posix(), "--root", ROOT.as_posix(), "--allow-write"],
    },
}


def configure(root: Path, home: Path, install_dir: Path, python: Path, profiles: list[str] | None = None) -> None:
    """Resolve explicit local options without reading or changing host configuration."""
    global ROOT, DSH_HOME, INSTALL, PYTHON, PROFILES, ENTRY
    ROOT, DSH_HOME, INSTALL, PYTHON = (p.expanduser().resolve() for p in (root, home, install_dir, python))
    PROFILES = tuple(dict.fromkeys(profiles or (p for p in ("desktop", "headless") if (DSH_HOME / "profiles" / p).is_dir())))
    if not PROFILES or any(not p or p in {".", ".."} or "/" in p or "\\" in p for p in PROFILES):
        raise ValueError("Select existing DSH profiles with --profile; profile names must not contain paths")
    ENTRY = {"id": ENTRY_ID, "name": "@deepseek-ai/dsh-mcp-client", "config": {
        "serverName": "local_knowledge", "transport": "stdio", "command": PYTHON.as_posix(),
        "args": [(ROOT / "scripts/mcp_server.py").as_posix(), "--root", ROOT.as_posix(), "--allow-write"],
    }}

# Use the installed dump implementation's public composer and schema collector.
# Loading the ordinary CLI profile can normalize manifests/root files; this
# read-only loader reproduces its bundle selection without those mutations.
BRIDGE = r"""
const fs = require('node:fs');
const crypto = require('node:crypto');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const digest = x => crypto.createHash('sha256').update(x).digest('hex');
const canonical = v => Array.isArray(v) ? v.map(canonical) :
    v && typeof v === 'object' ? Object.fromEntries(Object.keys(v).sort().map(k=>[k,canonical(v[k])])) : v;
const hash = v => digest(JSON.stringify(canonical(v)));
(async()=>{
  const anchor = path.join(input.install, 'resources/app.asar/dsh/node_modules/@deepseek-ai/dsh/package.json');
  const base = path.join(input.install, 'resources/app.asar/dsh/node_modules/@deepseek-ai');
  const api = await import(pathToFileURL(path.join(base, 'dsh-app-boot/lib/index.js')).href);
  const dir = path.join(input.home, 'profiles', input.profile);
  const manifest = api.readProfileManifest('dsh', dir);
  const bundles = manifest.dsh?.profile?.bundles ?? [];
  const exemptions = bundles.length ? api.readProfileVersionExemptions(dir) : {};
  const layers = [], skippedBundles = [];
  for (const packageName of bundles) {
    try {
      const packageDir = api.resolveBundleDir('dsh', packageName, anchor, dir);
      const bundleManifest = api.readProfileManifest('dsh', packageDir);
      const bundle = bundleManifest.dsh?.bundle;
      if (!bundle) throw Error('not a bundle');
      api.assertBundlePlatform(bundle.platforms, process.platform);
      const issue = api.evaluatePluginCompatibility(bundleManifest, exemptions);
      if (issue && !issue.exempted) throw Error(api.pluginCompatibilityWarning(issue));
      const patchPaths = api.bundlePatchPaths(packageDir, bundle);
      const patches = patchPaths.flatMap(p=>api.loadOverlayPatches('dsh', p));
      layers.push({packageName, packageDir, patchPaths, patches});
    } catch(e) { skippedBundles.push({packageName, reason: String(e)}); }
  }
  const patchPath = path.join(dir, 'cordis.patch.yml');
  const patches = api.loadOverlayPatches('dsh', patchPath);
  const profile = {name: input.profile, dir, layers, patchPath, patches, skippedBundles};
  const namedLayers = layers.map(l=>({label:l.packageName, patches:l.patches}));
  namedLayers.push({label:patchPath, patches});
  const homePatch = path.join(input.home, 'cordis.patch.yml');
  const extra = api.loadOptionalPatches('dsh', homePatch);
  if (extra) namedLayers.push({label:homePatch, patches:extra});
  const warnings = [];
  const entries = api.composeEntries(namedLayers.map(l=>l.patches), m=>warnings.push(m));
  const ids = entries.map(e=>e.id);
  if (new Set(ids).size !== ids.length) throw Error('duplicate effective entry ids');
  const dump = api.renderConfigDump('dsh', path.join(dir,'cordis.yml'), namedLayers, m=>warnings.push(m));
  const matching = entries.filter(e=>e.id===input.entry.id);
  if (matching.length > 1) throw Error('duplicate knowledge entry');
  let mcpConfigValid = null;
  if (matching.length) {
    const mcp = await import(pathToFileURL(path.join(base,'dsh-mcp-client/lib/index.js')).href);
    mcp.Config(matching[0].config);
    mcpConfigValid = true;
  }
  const saveWrite = process.stdout.write;
  let schema;
  process.stdout.write = process.stderr.write.bind(process.stderr);
  try { schema = await api.generateConfigSchema(profile,namedLayers.map(l=>l.patches),anchor); }
  finally { process.stdout.write = saveWrite; }
  const info = schema['x-cordis'] || {};
  const diagnostics = info.diagnostics || [];
  fs.writeSync(1,JSON.stringify({
    entry_count:entries.length,
    entry_hashes:Object.fromEntries(entries.map(e=>[e.id,hash(e)])),
    entry_order:ids,
    knowledge_entry:matching[0] || null,
    mcp_config_valid:mcpConfigValid,
    composition_dump_sha256:digest(dump),
    skipped_bundles:skippedBundles.map(x=>x.packageName),
    warnings_sha256:hash(warnings),
    schema:{complete:info.complete===true,sha256:hash(schema),
      diagnostic_count:diagnostics.length,diagnostics_sha256:hash(diagnostics),
      diagnostics:diagnostics.map(d=>({level:d.level,path:d.path || null}))}
  }));
})().catch(e=>{fs.writeSync(2,'DSH configuration inspection failed: '+e.name+'\n');process.exitCode=1;});
"""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".knowledge-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def inspect_profile(profile: str) -> dict:
    env = os.environ.copy()
    env["ELECTRON_RUN_AS_NODE"] = "1"
    proc = subprocess.run(
        [str(INSTALL / "DeepSeek Harness.exe"), "--expose-internals", "-e", BRIDGE],
        input=json.dumps({"install": INSTALL.as_posix(), "home": DSH_HOME.as_posix(), "profile": profile, "entry": ENTRY}),
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=90,
    )
    if proc.returncode:
        # Do not print raw configuration/import diagnostics, which can contain local values.
        raise RuntimeError(f"{profile}: configuration inspection exit {proc.returncode}; stderr sha256={sha(proc.stderr.encode())}")
    return json.loads(proc.stdout)


def insertion(newline: bytes) -> bytes:
    # JSON-quoted strings are valid YAML scalars and preserve Windows paths.
    q = lambda value: json.dumps(value, ensure_ascii=False)
    lines = ["", "# Local knowledge MCP; managed by AI-Knowledge/scripts/install_dsh_mcp.py",
             "- insert:", f"    - id: {ENTRY_ID}", f"      name: {q(ENTRY['name'])}", "      config:"]
    for key in ("serverName", "transport", "command"):
        lines.append(f"        {key}: {q(ENTRY['config'][key])}")
    lines.append("        args:")
    lines.extend(f"          - {q(arg)}" for arg in ENTRY["config"]["args"])
    return newline.join(line.encode("utf-8") for line in lines) + newline


def compare(before: dict, after: dict) -> dict:
    old, new = before["entry_hashes"], after["entry_hashes"]
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(key for key in old.keys() & new.keys() if old[key] != new[key])
    original_order = [key for key in after["entry_order"] if key != ENTRY_ID]
    expected_order = [key for key in before["entry_order"] if key != ENTRY_ID]
    if removed or changed or added != [ENTRY_ID] or original_order != expected_order:
        raise RuntimeError("Composed configuration changed outside the one new MCP entry")
    if after["knowledge_entry"] != ENTRY or not after["mcp_config_valid"]:
        raise RuntimeError("MCP configuration does not match the requested entry")
    if before["skipped_bundles"] != after["skipped_bundles"]:
        raise RuntimeError("Bundle selection changed during installation")
    if before["schema"]["diagnostics_sha256"] != after["schema"]["diagnostics_sha256"]:
        raise RuntimeError("Configuration schema gained or changed diagnostics")
    return {"added": added, "removed": removed, "changed": changed, "existing_order_preserved": True,
            "existing_schema_diagnostics_unchanged": True}


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def install(report_path: Path, apply: bool = False) -> dict:
    for required in (PYTHON, ROOT / "scripts/mcp_server.py", INSTALL / "DeepSeek Harness.exe"):
        if not required.is_file():
            raise RuntimeError(f"Required executable/server is missing: {required}")
    if apply and report_path.exists():
        existing = json.loads(report_path.read_text(encoding="utf-8"))
        if existing.get("status") == "installed" and existing.get("entry") == ENTRY and all(
            sha(Path(item["path"]).read_bytes()) == item["installed_sha256"] for item in existing["profiles"]
        ):
            return {"status": "already_installed", "report": str(report_path)}
        raise RuntimeError("Report exists but installed files differ; preserve it and choose a new report path")
    previews = []
    for profile in PROFILES:
        path = DSH_HOME / "profiles" / profile / "cordis.patch.yml"
        original = path.read_bytes()
        before = inspect_profile(profile)
        existing = before["knowledge_entry"]
        if existing is not None and existing != ENTRY:
            raise RuntimeError(f"{profile}: a different knowledge MCP entry already exists")
        previews.append({"profile": profile, "path": str(path), "original_sha256": sha(original),
                         "status": "already_equal" if existing == ENTRY else "would_install"})
    if not apply:
        return {"status": "dry_run", "profiles": previews, "entry": ENTRY, "files_changed": False}
    changing_profiles = [item["profile"] for item in previews if item["status"] == "would_install"]
    if not changing_profiles:
        return {"status": "already_equal", "profiles": previews, "files_changed": False}
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
    backup_dir = report_path.parent / ("dsh-mcp-backups-" + stamp)
    backup_dir.mkdir(parents=True, exist_ok=False)
    report = {"status": "preparing", "timestamp_utc": stamp, "entry": ENTRY, "profiles": [],
              "verification_mode": "installed DSH composer + renderConfigDump + generateConfigSchema; plugins not mounted",
              "model_test_performed": False, "credential_store_read": False,
              "rollback": f'"{PYTHON}" "{Path(__file__).resolve()}" rollback --report "{report_path}"'}
    originals: dict[str, bytes] = {}
    protected: dict[str, str] = {}
    try:
        # Prepare and back up both files before writing either one.
        for profile in changing_profiles:
            path = DSH_HOME / "profiles" / profile / "cordis.patch.yml"
            original = path.read_bytes()
            backup = backup_dir / (profile + ".cordis.patch.yml")
            backup.write_bytes(original)
            if backup.read_bytes() != original:
                raise RuntimeError("Backup verification failed")
            originals[profile] = original
            for name in ("package.json", "cordis.yml"):
                guard = path.parent / name
                protected[str(guard)] = sha(guard.read_bytes())
            before = inspect_profile(profile)
            if before["knowledge_entry"] is not None:
                raise RuntimeError(f"{profile}: MCP entry already exists; refusing to overwrite")
            newline = b"\r\n" if b"\r\n" in original else b"\n"
            updated = original + (b"" if original.endswith((b"\n", b"\r")) else newline) + insertion(newline)
            report["profiles"].append({"profile": profile, "path": str(path), "backup": str(backup),
                "original_sha256": sha(original), "installed_sha256": sha(updated), "before": before})
        write_report(report_path, report)
        for item in report["profiles"]:
            profile, path = item["profile"], Path(item["path"])
            original = originals[profile]
            if path.read_bytes() != original:
                raise RuntimeError(f"{profile}: concurrent change detected before install")
            newline = b"\r\n" if b"\r\n" in original else b"\n"
            updated = original + (b"" if original.endswith((b"\n", b"\r")) else newline) + insertion(newline)
            atomic_write(path, updated)
            after = inspect_profile(profile)
            item["after"] = after
            item["semantic_diff"] = compare(item["before"], after)
        if any(sha(Path(path).read_bytes()) != digest for path, digest in protected.items()):
            raise RuntimeError("An unrelated manifest or root config changed during verification")
        report["unrelated_manifests_and_root_configs_unchanged"] = True
        report["status"] = "installed"
        write_report(report_path, report)
        return {"status": "installed", "profiles": [x["profile"] for x in report["profiles"]],
                "report": str(report_path), "schema_complete": {x["profile"]: x["after"]["schema"]["complete"] for x in report["profiles"]}}
    except Exception as error:
        restored, conflicts = [], []
        for item in report["profiles"]:
            path = Path(item["path"])
            current = sha(path.read_bytes())
            if current == item["installed_sha256"]:
                atomic_write(path, originals[item["profile"]])
                restored.append(item["profile"])
            elif current != item["original_sha256"]:
                conflicts.append(item["profile"])
        report.update(status="failed", error=str(error), automatically_restored=restored, concurrent_conflicts=conflicts)
        write_report(report_path, report)
        raise


def rollback(report_path: Path) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("status") != "installed":
        raise RuntimeError("Rollback requires an installed report")
    # Preflight both files; do not partially restore if either changed later.
    for item in report["profiles"]:
        if sha(Path(item["path"]).read_bytes()) != item["installed_sha256"]:
            raise RuntimeError(f"{item['profile']}: later changes detected; refusing to overwrite")
        if sha(Path(item["backup"]).read_bytes()) != item["original_sha256"]:
            raise RuntimeError(f"{item['profile']}: backup hash mismatch")
    for item in report["profiles"]:
        atomic_write(Path(item["path"]), Path(item["backup"]).read_bytes())
    report["status"] = "rolled_back"
    report["rollback_timestamp_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    write_report(report_path, report)
    return {"status": "rolled_back", "report": str(report_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "rollback"))
    parser.add_argument("--report", type=Path, help="Install manifest path, or existing manifest for rollback")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--home", type=Path, default=DSH_HOME, help="DSH configuration directory (usually ~/.dsh)")
    parser.add_argument("--install-dir", type=Path, default=INSTALL, help="Windows directory containing DeepSeek Harness.exe")
    parser.add_argument("--python", type=Path, default=PYTHON)
    parser.add_argument("--profile", action="append", help="Existing profile to change; repeat for multiple profiles")
    parser.add_argument("--apply", action="store_true", help="Apply installation; otherwise only inspect existing configuration")
    args = parser.parse_args()
    try:
        if args.action == "rollback":
            if args.report is None:
                raise ValueError("rollback requires --report INSTALL_MANIFEST")
            result = rollback(args.report.resolve())
        else:
            if os.name != "nt":
                raise RuntimeError("This DSH adapter supports Windows only; use manual MCP configuration on other platforms")
            configure(args.root, args.home, args.install_dir, args.python, args.profile)
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
            report_path = (args.report or ROOT / "integrations/installations" / stamp / "dsh-mcp-install.json").resolve()
            result = install(report_path, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
