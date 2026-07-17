#!/usr/bin/env python3
"""
Build script for DJI automation, agent, and coordinator EXEs.

Usage:
  py build.py terra          — build DJI_AUTOMATE_UI.exe  (automation/PyAutomateDJI.py)
  py build.py ppk            — build DJI_AUTOMATE_PPK.exe (automation/DJIAutomatePPKV2.py)
  py build.py pix4d          — build PIX4D_AUTOMATE.exe   (automation/AutomatePix4D.py)
  py build.py agent          — build DataIntakeAgent.exe  (agent/main.py, Phase 2)
  py build.py coordinator    — build DataIntakeCoordinator.exe (coordinator/main.py)
  py build.py all            — build the payload EXEs (terra, ppk, pix4d)
  py build.py commit pix4d   — commit AutomatePix4D.py    (prompts for a tag, then Y/n before pushing)
  py build.py commit terra   — commit PyAutomateDJI.py    (prompts for a tag, then Y/n before pushing)
  py build.py commit ppk     — commit DJIAutomatePPKV2.py (prompts for a tag, then Y/n before pushing)
  py build.py commit all     — commit all three of the above, one at a time
  py build.py                — interactive menu

Each `commit` target: git add + commit that one script, optionally creates an
annotated tag (blank input skips tagging), then pushes the branch (and tag, if
any) to 'origin' — but only after you confirm at the Y/n prompt. If no
'origin' remote is configured, the push step is skipped with a message
telling you to add one.
"""
import shutil
import subprocess
import sys
from pathlib import Path

# Use the raw __file__ path so we stay on the mapped drive letter (Z:) rather
# than following the Egnyte UNC symlink that resolve() would produce — the
# UNC form doesn't carry per-user ownership info, which trips git's "dubious
# ownership" safety check for every teammate who runs this from their own
# mapped drive letter.
SCRIPT_DIR = Path(__file__).parent.absolute()

BUILDS = {
    "terra": {
        "name":   "DJI_AUTOMATE_UI",
        "script": SCRIPT_DIR / "automation" / "PyAutomateDJI.py",
        "extra":  [],
    },
    "ppk": {
        "name":   "DJI_AUTOMATE_PPK",
        "script": SCRIPT_DIR / "automation" / "DJIAutomatePPKV2.py",
        "extra":  ["--add-data", str(SCRIPT_DIR / "automation" / "embed_ppk_metadata.py") + ";."],
    },
    "pix4d": {
        "name":   "PIX4D_AUTOMATE",
        "script": SCRIPT_DIR / "automation" / "AutomatePix4D.py",
        "extra":  [],
    },
    "agent": {
        "name":   "DataIntakeAgent",
        "script": SCRIPT_DIR / "agent" / "main.py",
        "extra":  [],
    },
    "coordinator": {
        "name":   "DataIntakeCoordinator",
        "script": SCRIPT_DIR / "coordinator" / "main.py",
        "extra":  ["--add-data", str(SCRIPT_DIR / "coordinator" / "dashboard") + ";coordinator/dashboard"],
    },
}

# `build all` covers just the workstation payload EXEs; agent/coordinator are
# built explicitly since they deploy on a different cadence.
PAYLOAD_TARGETS = ["terra", "ppk", "pix4d"]

# Targets that get committed to git instead of (or in addition to) built into
# an exe. Uses the "commit" sub-command so these keys can overlap with BUILDS
# (e.g. "terra" is both an exe target and a commit target) without ambiguity.
COMMITS = {
    "pix4d": {
        "name":   "AutomatePix4D",
        "script": SCRIPT_DIR / "automation" / "AutomatePix4D.py",
    },
    "terra": {
        "name":   "PyAutomateDJI",
        "script": SCRIPT_DIR / "automation" / "PyAutomateDJI.py",
    },
    "ppk": {
        "name":   "DJIAutomatePPKV2",
        "script": SCRIPT_DIR / "automation" / "DJIAutomatePPKV2.py",
    },
}


def ensure_agent_yaml() -> None:
    """Make sure config/agent.yaml exists. agent/config.py's load_config()
    already falls back to this exact path (relative to cwd) with no
    --config flag needed — see load_config()'s candidate list. Only prompts
    the first time; once it's there, later builds leave it untouched so
    per-machine edits survive rebuilds."""
    dest = SCRIPT_DIR / "config" / "agent.yaml"
    if dest.exists():
        print(f"[ok] {dest} already exists — leaving your edits as is")
        return
    default_src = SCRIPT_DIR / "config" / "agent.example.yaml"
    answer = input(f"No config/agent.yaml yet. Path to yaml to use "
                    f"[{default_src}]: ").strip()
    src = Path(answer) if answer else default_src
    if not src.is_file():
        print(f"[warn] {src} not found — copy a yaml to {dest} manually before running the exe")
        return
    shutil.copy(src, dest)
    print(f"[ok] Copied {src} -> {dest}")


def build(target: str) -> int:
    cfg = BUILDS[target]
    if not cfg["script"].exists():
        print(f"[skip] {cfg['script']} does not exist yet — nothing to build for '{target}'")
        return 1
    extra = list(cfg["extra"])
    if target == "coordinator":
        # Bundle the React UI when it has been built (cd web && npm run build);
        # without it the EXE falls back to the legacy single-file dashboard.
        webui = SCRIPT_DIR / "web" / "dist"
        if (webui / "index.html").exists():
            extra += ["--add-data", str(webui) + ";web/dist"]
        else:
            print("[note] web/dist not found — coordinator EXE will use the legacy dashboard")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--console",
        "--name",      cfg["name"],
        "--distpath",  str(SCRIPT_DIR / "dist"),
        "--workpath",  str(SCRIPT_DIR / "build"),
        "--specpath",  str(SCRIPT_DIR),
        *extra,
        str(cfg["script"]),
    ]
    print(f"\n[build] {cfg['name']}.exe  ←  {cfg['script'].name}")
    print("Command:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(SCRIPT_DIR))
    if result.returncode == 0:
        out = SCRIPT_DIR / "dist" / (cfg["name"] + ".exe")
        print(f"[ok] Output: {out}")
        if target == "agent":
            ensure_agent_yaml()
    else:
        print(f"[error] Build failed (exit {result.returncode})")
    return result.returncode


def commit(target: str) -> int:
    """git add + commit a script's changes, optionally tag it, and push —
    with a confirmation prompt before anything touches the remote (mirrors
    the confirm-then-publish pattern in Sunrise-Intake/build.py)."""
    cfg = COMMITS[target]
    script = cfg["script"]

    if not (SCRIPT_DIR / ".git").is_dir():
        print(f"[git] No repo found in {SCRIPT_DIR} — running 'git init'...")
        result = subprocess.run(["git", "init"], cwd=str(SCRIPT_DIR))
        if result.returncode != 0:
            print("[error] git init failed")
            return result.returncode

    print(f"\n[commit] {script.name}")
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", str(script)],
        cwd=str(SCRIPT_DIR), capture_output=True, text=True,
    ).stdout.strip()

    if not status:
        print(f"[skip] No changes to commit for {script.name}")
        return 0

    print("[git] Changes to be committed:")
    print(status)

    tag = input(f"Tag (e.g. v1.0.0, blank to skip): ").strip()

    what = f"commit + tag {tag} + push" if tag else "commit + push"
    ans = input(f"{what} to GitHub? [Y/n]: ").strip().lower()
    if ans in ("n", "no"):
        print("[git] Skipped — nothing committed.")
        return 0

    add_result = subprocess.run(["git", "add", str(script)], cwd=str(SCRIPT_DIR))
    if add_result.returncode != 0:
        print(f"[error] git add failed (exit {add_result.returncode})")
        return add_result.returncode

    commit_result = subprocess.run(
        ["git", "commit", "-m", f"Update {script.name}"], cwd=str(SCRIPT_DIR)
    )
    if commit_result.returncode != 0:
        print(f"[error] Commit failed (exit {commit_result.returncode})")
        return commit_result.returncode
    print(f"[ok] Committed {script.name}")

    if tag:
        existing = subprocess.run(
            ["git", "tag", "--list", tag], cwd=str(SCRIPT_DIR),
            capture_output=True, text=True,
        ).stdout.strip()
        if existing:
            print(f"[git] Tag {tag} already exists — leaving it as is.")
        else:
            tag_result = subprocess.run(
                ["git", "tag", "-a", tag, "-m", f"{script.name} {tag}"],
                cwd=str(SCRIPT_DIR),
            )
            if tag_result.returncode == 0:
                print(f"[ok] Created tag {tag}")
            else:
                print(f"[error] Tag failed (exit {tag_result.returncode})")

    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=str(SCRIPT_DIR),
        capture_output=True, text=True,
    )
    if remote.returncode != 0:
        print("[git] No 'origin' remote configured — skipping push. "
              "Run: git remote add origin <url>")
        return 0

    push = subprocess.run(["git", "push"], cwd=str(SCRIPT_DIR))
    if push.returncode != 0:
        print(f"[error] Push failed (exit {push.returncode})")
        return push.returncode
    print("[ok] Pushed to origin")

    if tag:
        push_tag = subprocess.run(["git", "push", "origin", tag], cwd=str(SCRIPT_DIR))
        if push_tag.returncode != 0:
            print(f"[error] Push tag failed (exit {push_tag.returncode})")
            return push_tag.returncode
        print(f"[ok] Pushed tag {tag}")

    return 0


def menu():
    print("\nSelect an action:")
    options = [("build", key) for key in BUILDS] + [("build", "all")]
    options += [("commit", key) for key in COMMITS] + [("commit", "all")]
    for i, (action, key) in enumerate(options, 1):
        if action == "build" and key != "all":
            label = f"build {BUILDS[key]['name']}.exe  ({BUILDS[key]['script'].name})"
        elif action == "build":
            label = "build all EXE targets"
        elif key != "all":
            label = f"commit {COMMITS[key]['name']}  ({COMMITS[key]['script'].name})"
        else:
            label = "commit all (pix4d, terra, ppk)"
        print(f"  {i}. {label}")
    choice = input("Choice: ").strip()
    try:
        return options[int(choice) - 1]
    except (ValueError, IndexError):
        print("Invalid choice.")
        sys.exit(1)


def main():
    if len(sys.argv) > 1:
        first = sys.argv[1].lower()
        if first == "commit":
            if len(sys.argv) < 3:
                print("Usage: py build.py commit <pix4d|terra|ppk|all>")
                sys.exit(1)
            action, arg = "commit", sys.argv[2].lower()
        else:
            action, arg = "build", first
    else:
        action, arg = menu()

    if action == "commit":
        targets = list(COMMITS.keys()) if arg == "all" else [arg]
        if any(t not in COMMITS for t in targets):
            valid = ", ".join(COMMITS)
            print(f"Unknown commit target '{arg}'. Use: {valid} or all")
            sys.exit(1)
        for target in targets:
            rc = commit(target)
            if rc != 0:
                sys.exit(rc)
        return

    targets = PAYLOAD_TARGETS if arg == "all" else [arg]
    if any(t not in BUILDS for t in targets):
        valid = ", ".join(BUILDS)
        print(f"Unknown build target '{arg}'. Use: {valid} or all")
        sys.exit(1)

    for target in targets:
        rc = build(target)
        if rc != 0:
            sys.exit(rc)


if __name__ == "__main__":
    main()
