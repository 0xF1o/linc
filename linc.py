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
USERNAME = os.environ.get("LINC_USERNAME", getpass.getuser())

def find_runtime() -> str:
    for cmd in ("container", "docker", "podman"):
        if shutil.which(cmd):
            return cmd
    return None


def run_commands_with_retry(commands, retries=2, delay=1):
    """
    Execute a list of commands.
    Each command is retried `retries` times on failure.
    After retries are exhausted, continue with the next command.
    """
    for cmd in commands:
        attempt = 0
        while attempt <= retries:
            try:
                print(f"Running: {' '.join(cmd)} (attempt {attempt + 1})")
                subprocess.run(cmd, check=True)
                break
            except subprocess.CalledProcessError as e:
                attempt += 1
                if attempt > retries:
                    print(
                        f"Command failed after {retries + 1} attempts, continuing: {' '.join(cmd)}",
                        file=sys.stderr,
                    )
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
    
    if runtime in ("container"):
        cmd.append("--ssh")

    ssh_auth_sock = os.environ.get("SSH_AUTH_SOCK")
    if ssh_auth_sock and os.path.exists(ssh_auth_sock):
        cmd += [
            "-e", "SSH_AUTH_SOCK=/ssh-agent",
            "-v", f"{ssh_auth_sock}:/ssh-agent",
        ]

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
            f"echo '{USERNAME} ALL=(ALL:ALL) NOPASSWD: ALL' > /etc/sudoers ; "
        )

    setup_cmd += "grep 'registry.lakedrops.com' $(which l3d) ; "

    subprocess.check_call([runtime, "exec", "-it", NAME, "/bin/sh", "-c", setup_cmd])


def start(runtime):
    print(f"Starting {NAME} using {runtime}")

    subprocess.run([runtime, "rm", "-f", NAME], check=False, capture_output=True)
    subprocess.run([runtime, "rm", "-f", NAME], check=False, capture_output=True)

    try:
        subprocess.run(base_run_cmd(runtime) + [IMAGE], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Failed to start container '{NAME}' using {runtime}.", file=sys.stderr)
        sys.exit(e.returncode)

    run_setup(runtime)


def stop(runtime):
    print(f"Stopping {NAME}")
    subprocess.run([runtime, "rm", "-f", NAME], check=False)


def container_reset(runtime):
    if runtime != "container":
        print("container-reset is only supported when runtime=container", file=sys.stderr)
        sys.exit(1)

    print("Resetting container runtime")

    commands = [
        [runtime, "system", "stop"],
        [runtime, "system", "start"],
        [runtime, "rm", "-f", NAME],
    ]

    run_commands_with_retry(commands)


def shell(runtime, cmdparam=["/bin/sh"], execparam=[]):
    cmd = [runtime, "exec"] + execparam + ["-it", NAME]
    proc = subprocess.run(cmd + cmdparam)
    sys.exit(proc.returncode)


def l3d(runtime, args):
    home_projects = os.path.expanduser(PROJECSTDIR).replace('\\','/')
    cwd = os.getcwd().replace('\\','/')

    if not cwd.startswith(home_projects):
        print(f"Error: You must run this command inside a project under {PROJECSTDIR}.", file=sys.stderr)
        sys.exit(1)

    container_dir = "/Projects" + cwd[len(home_projects):]

    cmdstr = f"cd '{container_dir}' && l3d"
    if args:
        cmdstr += " " + " ".join(args)
    cmdparam = ["/bin/sh", "-c", cmdstr]
    execparam = []

    if FORWARDUSERID:
        groupids = ",".join(str(gid) for gid in os.getgroups())
        execparam += ["-e", "HOME=/.hostuserhome"]
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
        "  up, start          Start the linc dind container and run initial setup.\n"
        "  down, stop         Stop and remove the linc container.\n"
        "  shell              Open an interactive shell inside the running container.\n"
        "  l3d                Run l3d inside the container (for project commands). Any following args are forwarded to l3d.\n"
        "  container-reset    Restart container system and remove linc container (only when LINC_RUNTIME=container).\n\n"
    )

    parser = argparse.ArgumentParser(
        description=help_description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "command",
        choices=["up", "start", "down", "stop", "shell", "l3d", "container-reset"],
        help="Action to perform",
    )

    parser.add_argument(
        "cmd_args",
        nargs=argparse.REMAINDER,
        help="Extra arguments forwarded to the command (used by l3d)",
    )

    args = parser.parse_args()

    if args.command == "up":
        start(runtime)
    if args.command == "start":
        start(runtime)
    elif args.command == "down":
        stop(runtime)        
    elif args.command == "stop":
        stop(runtime)
    elif args.command == "shell":
        shell(runtime)
    elif args.command == "l3d":
        l3d(runtime, args.cmd_args)
    elif args.command == "container-reset":
        container_reset(runtime)


if __name__ == "__main__":
    main()
