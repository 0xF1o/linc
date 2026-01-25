#!/usr/bin/env python3

import argparse
import os
import shutil
import subprocess
import sys
import time
import getpass
import shlex
from pathlib import Path
from typing import Iterable

def is_trueish(s: str) -> bool:
    trueish = ['true', 'yes', 'y', '1']
    return s.lower() in trueish

def load_env(filename: str, prefixes: Iterable[str]) -> None:
    for path in reversed([Path.cwd(), *Path.cwd().parents, Path.home()]):
        env_file = path / filename
        if env_file.exists():
            load_env_file(env_file, prefixes)

def load_env_file(path: Path, prefixes: Iterable[str]) -> None:
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if any(line.startswith(pfx) for pfx in prefixes):
                key, value = line.split("=", 1)
                os.environ[key] = " ".join(shlex.split(value))

load_env(".env", ["LINC_"])

def find_runtime() -> str:
    def _find() -> str:
        for cmd in ("container", "podman", "docker"):
            if shutil.which(cmd): return cmd
    return os.environ.get("LINC_RUNTIME", _find())

RUNTIME = find_runtime()
NAME = os.environ.get("LINC_NAME", "linc-shell")
IMAGE = os.environ.get("LINC_IMAGE", "ghcr.io/0xf1o/linc-dind:latest")
PORT = os.environ.get("LINC_PORT", "8000")
PORTMAP = os.environ.get("LINC_PORTMAP", f"{PORT}:8000")
CACHEVOLUME = os.environ.get("LINC_CACHEVOLUME", "linc-cache")
SETUPVERSION = os.environ.get("LINC_SETUPVERSION", "--platform linux/amd64 registry.lakedrops.com/docker/l3d/setup:latest")
PROJECSTDIR = os.environ.get("LINC_PROJECTSDIR", "~/Projects")
FORWARDUSERID = is_trueish(os.environ.get("LINC_FORWARDUSERID", str(sys.platform == "linux" and RUNTIME == "docker")))
FORWARDHOMEDIR = is_trueish(os.environ.get("LINC_FORWARDHOMEDIR", "1"))
DEBUG = is_trueish(os.environ.get("LINC_DEBUG", "0"))
USERNAME = os.environ.get("LINC_USERNAME", getpass.getuser())
RUNARGS = os.environ.get("LINC_RUNARGS","")

class Con:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    UNDERLINE = "\033[4m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"

def debug(*args, **kwargs):
    if DEBUG: print(Con.DIM, end="", flush=True); print(*args, **kwargs); print(Con.RESET, end="", flush=True)

def run(cmd, capture_output:bool=False, check:bool=False, text:bool=True, dbg:bool=True, **kwargs) -> subprocess.CompletedProcess:
    if dbg and DEBUG: print(f"{Con.DIM}run> ", end="", flush=True); debug(str(cmd)[:512])
    result = subprocess.run(cmd, capture_output=capture_output, check=check, text=text, **kwargs)
    if dbg and DEBUG and result.returncode: debug(f"returncode={result.returncode}")
    return result

def run_commands_with_retry(commands, retries=3, delay=0.5, timeout=5):
    for cmd in commands:
        attempt = 0
        while attempt <= retries:
            try:
                print(".", end="", flush=True)
                run(cmd, check=True, timeout=timeout, capture_output=not DEBUG)
                print(".", end="", flush=True)
                break
            except Exception as e:
                attempt += 1
                if attempt > retries:
                    if DEBUG:
                        print(f"Command failed after {retries + 1} attempts, continuing: {cmd}", file=sys.stderr)
                        print(f"{e}", file=sys.stderr)
                    else:
                        print("!", end="", flush=True)
                else:
                    time.sleep(delay)


def base_run_cmd(workdir:str="/Projects"):
    cmd = [RUNTIME, "run", "-d", "--name", NAME, "-p", PORTMAP]

    if RUNARGS: cmd += shlex.split(RUNARGS)
    if RUNTIME in ("docker", "podman"): cmd.append("--privileged")
    if RUNTIME in ("container"): cmd.append("--rosetta")
    
    ssh_auth_sock = os.environ.get("SSH_AUTH_SOCK")
    if ssh_auth_sock:
        ssh_auth_sock = os.path.realpath(ssh_auth_sock)
    if ssh_auth_sock and os.path.exists(ssh_auth_sock):
        cmd += [
             "-e", "SSH_AUTH_SOCK=/ssh-agent",
             "-v", f"{ssh_auth_sock}:/ssh-agent",
        ]
    elif RUNTIME in ("container"):
        cmd.append("--ssh")

    home_projects = os.path.expanduser(PROJECSTDIR)
    if os.path.isdir(home_projects): cmd += ["-v", f"{home_projects}:/Projects"]
    else: print(f"Warning! Project directory does not exist ({PROJECSTDIR})")

    cmd += [
        "-e", f"USER={USERNAME}",
        "-v", os.path.expanduser("~") + ":/.hostuserhome",
        "-w", workdir,
    ]

    if len(CACHEVOLUME) > 0:
        create_volume(CACHEVOLUME, False)
        create_volume(CACHEVOLUME + "-traefik", FORWARDUSERID)
        create_volume(CACHEVOLUME + "-composer", FORWARDUSERID)
        create_volume(CACHEVOLUME + "-dockerconfig", FORWARDUSERID)
        cmd += ["-v", f"{CACHEVOLUME}:/.hostuserhome/.docker"]
        cmd += ["-v", f"{CACHEVOLUME}:/var/lib/docker"]
        cmd += ["-v", f"{CACHEVOLUME}-traefik:/.hostuserhome/.traefik"]
        cmd += ["-v", f"{CACHEVOLUME}-composer:/.hostuserhome/.composer/cache"]

    return cmd

def create_volume(name:str, chown:bool):
    run([RUNTIME, "volume", "create", name], capture_output=not DEBUG, check=False)
    if chown: run([RUNTIME, "run", "-v", f"{name}:/vol", "--rm", "busybox", "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/vol" ], capture_output=not DEBUG)

def run_setup():
    print("Running linc setup inside container")

    setup_cmd = (
        "cp /usr/share/zoneinfo/UTC /etc/localtime ; "
        "touch /etc/timezone /etc/sudoers ; "
        "until docker info >/dev/null 2>&1; do printf '.'; sleep 1; done; "
        "chmod 666 /var/run/docker.sock ; "
        "docker network create traefik-public 2>/dev/null ; "
        f"docker run -v /usr/local/bin:/setup --rm {SETUPVERSION} ; "
    )

    if FORWARDUSERID:
        setup_cmd += (
            f"echo {USERNAME}:x:{os.getuid()}:{os.getgid()}:l3d user:/home/{USERNAME}:/bin/bash >> /etc/passwd ; "
            f"echo 'root ALL=(ALL:ALL) NOPASSWD: ALL' >> /etc/sudoers ; "
            f"echo '{USERNAME} ALL=(ALL:ALL) NOPASSWD: ALL' >> /etc/sudoers ; "
        )

    setup_cmd += "grep 'registry.lakedrops.com' $(which l3d) ; "
    if DEBUG: setup_cmd = "set -x ; " + setup_cmd
    print(Con.DIM, end="", flush=True)
    subprocess.check_call([RUNTIME, "exec", "-it", NAME, "/bin/sh", "-c", setup_cmd])
    print(Con.RESET, end="", flush=True)

def rm() -> subprocess.CompletedProcess:
    p = run([RUNTIME, "rm", "-f", NAME], check=False, capture_output=not DEBUG)
    if(p.returncode):
        p = run([RUNTIME, "rm", "-f", NAME], check=False, capture_output=not DEBUG)
    run([RUNTIME, "volume", "rm", CACHEVOLUME + "-traefik"], check=False, capture_output=not DEBUG)
    return p

def start():
    container_dir = get_container_dir()
    print(f"Starting {NAME} using {RUNTIME}")
    dind_kill()
    rm()
    cmd = base_run_cmd(container_dir) + [IMAGE]
    try:
        run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Failed to start container '{NAME}' using {RUNTIME}.", file=sys.stderr)
        sys.exit(e.returncode)

    run_setup()

def pull():
    cmd = [RUNTIME, "image", "pull", IMAGE]
    p = run(cmd, check=False)
    if(p.returncode):
        print(f"Warning: pull failed")
        debug(cmd)

def container_system_start():
    if RUNTIME not in ("container",):
        return
    cmd = [RUNTIME, "system", "start"]
    p = run(cmd, check=False)
    if(p.returncode):
        print(f"Error: container system error", file=sys.stderr)
        sys.exit(p.returncode)


def stop(clean_cache=False):
    print(f"Stopping {NAME}")
    dind_kill()
    rm()
    if clean_cache and len(CACHEVOLUME) > 0:
        print(f"Cleaning cache")
        run([RUNTIME, "volume", "rm", CACHEVOLUME], check=False, capture_output=not DEBUG)
        run([RUNTIME, "volume", "rm", CACHEVOLUME + "-composer"], check=False, capture_output=not DEBUG)
        run([RUNTIME, "volume", "rm", CACHEVOLUME + "-dockerconfig"], check=False, capture_output=not DEBUG)


def dind_kill() -> subprocess.CompletedProcess: return run([RUNTIME, "exec", NAME, "/bin/sh", "-c", "docker ps -qa | xargs docker rm -f 2>/dev/null"], check=False, capture_output=not DEBUG)

def container_reset():
    if RUNTIME != "container":
        print("container-reset is only supported when runtime=container", file=sys.stderr)
        sys.exit(1)

    print("Resetting container runtime")
    run_commands_with_retry([[RUNTIME, "system", "stop"],[RUNTIME, "system", "start"]])
    stop()
    run_commands_with_retry([[RUNTIME, "stop", "--all"],[RUNTIME, "rm", "-f", NAME]])
    print(".")
    container_system_start()
    print(f"{Con.BRIGHT_GREEN}Looking good!{Con.RESET}")


def shell(cmdparam=["/bin/sh"], execparam=[], check: bool=True, capture_text:bool=False) -> subprocess.CompletedProcess:
    cmd = [RUNTIME, "exec"] + execparam + ["-it", NAME] + cmdparam
    proc = run(cmd, capture_output=capture_text, text=capture_text)
    if check and proc.returncode: sys.exit(proc.returncode)
    return proc

def get_container_dir() -> str:
    projpath = os.path.expanduser(PROJECSTDIR).replace('\\','/')
    homepath = os.path.expanduser("~").replace('\\','/')
    cwd = os.getcwd().replace('\\','/')
    container_dir = ""

    if cwd.startswith(homepath): container_dir = "/.hostuserhome" + cwd[len(homepath):]
    if cwd.startswith(projpath): container_dir = "/Projects" + cwd[len(projpath):]

    if DEBUG: debug(f"PROJECTSDIR: {PROJECSTDIR}"); debug(f"projpath: {projpath}") ;debug(f"homepath: {homepath}"); debug(f"cwd: {cwd}"); debug(f"container_dir: {container_dir}")
    if not container_dir:
        print(f"{Con.BRIGHT_RED}Error{Con.RESET}: Not insiede $HOME or $LINC_PROJECTSDIR.", file=sys.stderr)
        sys.exit(1)
    return container_dir


def l3d(l3darg: str=""):
    def shell_exec(cmdstr):
        cmdparam = ["/bin/sh", "-c", cmdstr]
        execparam = ["-e", "HOME=/.hostuserhome"] if FORWARDHOMEDIR else []

        if FORWARDUSERID:
            groupids = ",".join(str(gid) for gid in os.getgroups())
            cmdparam = ["/bin/setpriv", "--reuid", f"{os.getuid()}", "--regid", f"{os.getgid()}", "--groups", f"{groupids}"] + cmdparam

        shell(cmdparam, execparam)
    container_dir = get_container_dir()        
    shell_exec(f"cd '{container_dir}' && L3DSHELL=/bin/bash l3d {l3darg}")


def show_env_vars():
    """Display current LINC_ environment variables and their values."""
    env_vars = {
        "LINC_NAME": (NAME, "Container name"),
        "LINC_IMAGE": (IMAGE, "Container image to run"),
        "LINC_PORT": (PORT, "Port mapping for container"),
        "LINC_CACHEVOLUME": (CACHEVOLUME, "Docker volume for caching (empty to disable)"),
        "LINC_SETUPVERSION": (SETUPVERSION, "Setup image version"),
        "LINC_PROJECTSDIR": (PROJECSTDIR, "Host projects directory"),
        "LINC_FORWARDUSERID": (str(FORWARDUSERID), "Forward host user ID into container"),
        "LINC_FORWARDHOMEDIR": (str(FORWARDHOMEDIR), "Forward user home directory into container"),
        "LINC_DEBUG": (str(DEBUG), "Enable debug output"),
        "LINC_USERNAME": (USERNAME, "Username inside container"),
        "LINC_RUNTIME": (RUNTIME, "Container runtime (docker/podman/container)"),
        "LINC_RUNARGS": (RUNARGS, f"pass arguments to `{RUNTIME} run`"),
    }

    print("\nLINC Environment Variables:")
    print("=" * 70)
    for var, (value, description) in sorted(env_vars.items()):
        print(f"\n{var}")
        print(f"  Current value: {value}")
        print(f"  Description:   {description}")
    print("\n" + "=" * 70)
    print("\nConfiguration via .env:")
    print("  LINC loads environment variables from .env files in the current")
    print("  directory or any parent directory or the home folder.")
    print("  Only variables starting with 'LINC_' are loaded.")
    print("\n  Example .env file:")
    print("    # ~/.env or project/.env")
    print("    LINC_PROJECTSDIR=D:/work")
    print("    LINC_IMAGE=docker:25-dind")
    print("    LINC_DEBUG=1")
    print('    LINC_RUNARGS="--cpu 8 --memory 4G"')
    print()

def main():
    help_description = (
        "Manage the linc environment.\n\n"
        "Commands:\n"
        "  start-l3d [--once] [Re]Start linc, pull, run setup and start l3d.\n"
        "Commands for manual steps:\n"
        "  up, start [--pull] [Re]Start linc and run initial setup.\n"
        "  down, stop [--cc]  Remove linc [and purge cache].\n\n"
        "Tools:\n"
        "  l3d [reset|...]    Run l3d in linc (for project commands). Arg is forwarded to l3d.\n"
        "  shell              Open an interactive root shell on the abstraction layer.\n"
        "  env                Display current LINC_* environment variables and their values.\n"
        "  container-reset    Restart container system and stop/remove existing linc container (only when LINC_RUNTIME=container).\n\n"
        "Configuration:\n"
        "  Environment variables are loaded from .env files. See 'linc env' for details.\n"
    )

    parser = argparse.ArgumentParser(
        description=help_description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sl3d = sub.add_parser("start-l3d")
    sl3d.add_argument("--once")

    ul3d = sub.add_parser("up-l3d")
    ul3d.add_argument("--once")

    up = sub.add_parser("up")
    up.add_argument("--pull", action="store_true")

    st = sub.add_parser("start")
    st.add_argument("--pull", action="store_true")

    down = sub.add_parser("down")
    down.add_argument("--cc", action="store_true")

    stop_cmd = sub.add_parser("stop")
    stop_cmd.add_argument("--cc", action="store_true")

    sub.add_parser("shell")

    l3d_cmd = sub.add_parser("l3d")
    l3d_cmd.add_argument("cmd_args", nargs=argparse.REMAINDER)

    sub.add_parser("env")

    sub.add_parser("container-reset")

    args = parser.parse_args()

    if not RUNTIME:
        print("Error: No container runtime found", file=sys.stderr)
        sys.exit(1)

    if args.command in ("up", "start"):
        if getattr(args, "pull", False): pull()
        start()
    elif args.command in ("start-l3d", "up-l3d"):
        pull()
        container_system_start()
        start()
        l3d()
        if getattr(args, "once", False): stop(clean_cache=False)
    elif args.command in ("down", "stop"): stop(clean_cache=getattr(args, "cc", False))
    elif args.command == "shell": shell()
    elif args.command == "env": show_env_vars()
    elif args.command == "l3d": l3d(l3darg=" ".join(args.cmd_args))
    elif args.command == "container-reset": container_reset()

if __name__ == "__main__": main()
