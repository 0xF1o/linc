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

def load_env(filename: str, prefixes: Iterable[str]) -> None:
    for path in reversed([Path.cwd(), *Path.cwd().parents]):
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
                os.environ[key] = shlex.split(value)[0]

load_env(".env", ["LINC_"])

def is_trueish(s: str) -> bool:
    trueish = ['true', 'yes', 'y', '1'] 
    return s.lower() in trueish

NAME = os.environ.get("LINC_NAME", "linc-shell")
IMAGE = os.environ.get("LINC_IMAGE", "docker:29-dind")
PLATFORM = os.environ.get("LINC_PLATFORM", "linux/amd64")
PORT = os.environ.get("LINC_PORT", "8000:8000")
CACHEVOLUME = os.environ.get("LINC_CACHEVOLUME", "linc-cache")
SETUPVERSION = os.environ.get("LINC_SETUPVERSION", "registry.lakedrops.com/docker/l3d/setup:latest")
PROJECSTDIR = os.environ.get("LINC_PROJECTSDIR", "~/Projects")
FORWARDUSERID = is_trueish(os.environ.get("LINC_FORWARDUSERID", str(sys.platform == "linux")))
DEBUG = is_trueish(os.environ.get("LINC_DEBUG", "0"))
USERNAME = os.environ.get("LINC_USERNAME", getpass.getuser())

def find_runtime() -> str:
    for cmd in ("container", "docker", "podman"):
        if shutil.which(cmd):
            return cmd
    return None


def run_commands_with_retry(commands, retries=2, delay=1, timeout=5):
    for cmd in commands:
        attempt = 0
        while attempt <= retries:
            try:
                if DEBUG:
                    print(f"Running: {' '.join(cmd)} (attempt {attempt + 1})")
                else:
                    print(".")
                subprocess.run(cmd, check=True, timeout=timeout, capture_output=not DEBUG)
                break
            except Exception as e:
                attempt += 1
                if attempt > retries:
                    if DEBUG:
                        print(f"Command failed after {retries + 1} attempts, continuing: {cmd}", file=sys.stderr)
                        print(f"{e}", file=sys.stderr)
                    else:
                        print("!")
                else:
                    time.sleep(delay)


def base_run_cmd(runtime):
    cmd = [
        runtime,
        "run",
        "-d",
        "--name", NAME,
        "--platform", PLATFORM,
        "-p", PORT
    ]

    if runtime in ("docker", "podman"):
        cmd.append("--privileged")
    
    ssh_auth_sock = os.environ.get("SSH_AUTH_SOCK")
    if ssh_auth_sock:
        ssh_auth_sock = os.path.realpath(ssh_auth_sock)
    if ssh_auth_sock and os.path.exists(ssh_auth_sock):
        cmd += [
             "-e", "SSH_AUTH_SOCK=/ssh-agent",
             "-v", f"{ssh_auth_sock}:/ssh-agent",
        ]
    elif runtime in ("container"):
        cmd.append("--ssh")

    home_projects = os.path.expanduser(PROJECSTDIR)
    if os.path.isdir(home_projects):
        cmd += [
            "-v", f"{home_projects}:/Projects",
            "-w", "/Projects"
        ]
    else:
        print(f"Warning! Project directory does not exist ({PROJECSTDIR})")

    cmd += [
        "-e", f"USER={USERNAME}",
        "-v", os.path.expanduser("~") + ":/.hostuserhome"
    ]

    if len(CACHEVOLUME) > 0:
        subprocess.run([runtime, "volume", "create", CACHEVOLUME], check=False)
        cmd += ["-v", f"{CACHEVOLUME}:/var/lib/docker"]

    return cmd


def run_setup(runtime):
    print("Running linc setup inside container")

    setup_cmd = (
        "apk add bash tzdata setpriv ; "
        "cp /usr/share/zoneinfo/UTC /etc/localtime ; "
        "touch /etc/timezone /etc/sudoers ; "
        "until docker info >/dev/null 2>&1; do printf '.'; sleep 1; done; "
        "chmod 666 /var/run/docker.sock ; "
        "docker network create traefik-public 2>/dev/null ; "
        f"docker run -v /usr/local/bin:/setup --rm {SETUPVERSION} ; "
    )

    if FORWARDUSERID:
        setup_cmd += (
            f"echo {USERNAME}:x:{os.getuid()}:{os.getgid()}:l3d user:/home/flo:/bin/bash >> /etc/passwd ; "
            f"echo '{USERNAME} ALL=(ALL:ALL) NOPASSWD: ALL' >> /etc/sudoers ; "
        )

    setup_cmd += "grep 'registry.lakedrops.com' $(which l3d) ; "

    if DEBUG:
        setup_cmd = "set -x ; " + setup_cmd

    subprocess.check_call([runtime, "exec", "-it", NAME, "/bin/sh", "-c", setup_cmd])

def rm(runtime: str) -> subprocess.CompletedProcess:
    p = subprocess.run([runtime, "rm", "-f", NAME], check=False, capture_output=not DEBUG)
    if(p.returncode): # try again - macos bug
        p = subprocess.run([runtime, "rm", "-f", NAME], check=False, capture_output=not DEBUG)
    return p

def start(runtime):
    print(f"Starting {NAME} using {runtime}")    
    rm(runtime)
    cmd = base_run_cmd(runtime) + [IMAGE]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Failed to start container '{NAME}' using {runtime}.", file=sys.stderr)
        if DEBUG:
            print("> "+" ".join(shlex.quote(arg) for arg in cmd), file=sys.stderr)
        sys.exit(e.returncode)

    run_setup(runtime)


def stop(runtime, clean_cache=False):
    print(f"Stopping {NAME}")
    subprocess.run([runtime, "exec", NAME, "/bin/sh", "-c", "docker ps -qa | xargs docker rm -f 2>/dev/null"], check=False, capture_output=not DEBUG)
    rm(runtime)
    if clean_cache and len(CACHEVOLUME) > 0:
        print(f"Cleaning cache")
        subprocess.run([runtime, "volume", "rm", CACHEVOLUME], check=False, capture_output=not DEBUG)


def container_reset(runtime):
    if runtime != "container":
        print("container-reset is only supported when runtime=container", file=sys.stderr)
        sys.exit(1)

    print("Resetting container runtime")

    commands = [
        [runtime, "system", "stop"],
        [runtime, "system", "start"],
        [runtime, "stop", "--all"],
        [runtime, "rm", "-f", NAME],
    ]

    run_commands_with_retry(commands)


def shell(runtime, cmdparam=["/bin/sh"], execparam=[]):
    cmd = [runtime, "exec"] + execparam + ["-it", NAME] + cmdparam
    if DEBUG:
        print("shell> "+" ".join(shlex.quote(arg) for arg in cmd), file=sys.stderr)
    proc = subprocess.run(cmd)
    if DEBUG and proc.returncode:
        print(f"Failed run: {cmd}")
    sys.exit(proc.returncode)


def l3d(runtime, args):
    projpath = os.path.expanduser(PROJECSTDIR).replace('\\','/')
    homepath = os.path.expanduser("~").replace('\\','/')
    cwd = os.getcwd().replace('\\','/')
    container_dir = ""

    if cwd.startswith(homepath):
        container_dir = "/.hostuserhome" + cwd[len(homepath):]

    if cwd.startswith(projpath):
        container_dir = "/Projects" + cwd[len(projpath):]

    if DEBUG:
        print(f"PROJECTSDIR: {PROJECSTDIR}")
        print(f"projpath: {projpath}")
        print(f"homepath: {homepath}")
        print(f"cwd: {cwd}")
        print(f"container_dir: {container_dir}")

    if not container_dir:
        print(f"Error: Not insiede $HOME or LINC_PROJECTSDIR.", file=sys.stderr)
        sys.exit(1)

    cmdstr = f"cd '{container_dir}' && l3d"
    if args:
        cmdstr += " " + " ".join(args)
    cmdparam = ["/bin/sh", "-c", cmdstr]
    execparam = ["-e", "HOME=/.hostuserhome"]

    if FORWARDUSERID:
        groupids = ",".join(str(gid) for gid in os.getgroups())
        cmdparam = ["/bin/setpriv", "--reuid", f"{os.getuid()}", "--regid", f"{os.getgid()}", "--groups", f"{groupids}"] + cmdparam

    shell(runtime, cmdparam, execparam)
        
def main():
    runtime = os.environ.get("LINC_RUNTIME", find_runtime())
    if not runtime:
        print("Error: No container runtime found", file=sys.stderr)
        sys.exit(1)

    help_description = (
        "Manage the linc environment.\n\n"
        "Commands:\n"
        "  start-l3d          [Re]Start linc, run setup and start l3d.\n"
        "  up, start          [Re]Start linc and run initial setup.\n"
        "  down, stop [--cc]  Remove linc [and purge cache].\n"
        "  l3d [args...]      Run l3d inside the container (for project commands). Any following args are forwarded to l3d.\n\n"
        "Tools:\n"
        "  shell              Open an interactive root shell inside the running container.\n"        
        "  container-reset    Restart container system and remove linc container (only when LINC_RUNTIME=container).\n\n"
    )

    parser = argparse.ArgumentParser(
        description=help_description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("start-l3d")
    sub.add_parser("up-l3d")
    sub.add_parser("up")
    sub.add_parser("start")

    down = sub.add_parser("down")
    down.add_argument("--cc", action="store_true")

    stop_cmd = sub.add_parser("stop")
    stop_cmd.add_argument("--cc", action="store_true")

    sub.add_parser("shell")

    l3d_cmd = sub.add_parser("l3d")
    l3d_cmd.add_argument("cmd_args", nargs=argparse.REMAINDER)

    sub.add_parser("container-reset")

    args = parser.parse_args()

    if args.command in ("up", "start"):
        start(runtime)
    elif args.command in ("start-l3d", "up-l3d"):
        start(runtime)
        l3d(runtime, [])
    elif args.command in ("down", "stop"):
        stop(runtime, clean_cache=getattr(args, "cc", False))
    elif args.command == "shell":
        shell(runtime)
    elif args.command == "l3d":
        l3d(runtime, args.cmd_args)
    elif args.command == "container-reset":
        container_reset(runtime)



if __name__ == "__main__":
    main()
