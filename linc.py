#!/usr/bin/env python3

import argparse
import os
import shutil
import subprocess
import sys
import time

__commit__ = ""
NAME = os.environ.get("LINC_NAME", "linc-shell")
IMAGE = os.environ.get("LINC_IMAGE", "docker:29-dind")
PLATFORM = os.environ.get("LINC_PLATFORM", "linux/amd64")
PORT = os.environ.get("LINC_PORT", "8000:8000")
CACHEVOLUME = os.environ.get("LINC_CACHEVOLUME", "linc-cache")
SETUPVERSION = os.environ.get("LINC_SETUPVERSION", "registry.lakedrops.com/docker/l3d/setup:latest")
PROJECSTDIR = os.environ.get("LINC_PROJECTSDIR", "~/Projects")


def find_runtime():
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

    username = os.environ.get("USERNAME")
    if username:
        cmd += ["-e", f"USERNAME={username}"]

    if len(CACHEVOLUME) > 0:
        subprocess.run([runtime, "volume", "create", CACHEVOLUME], check=False)
        cmd += ["-v", f"{CACHEVOLUME}:/var/lib/docker"]

    return cmd


def run_setup(runtime):
    print("Running linc setup inside container")

    setup_cmd = (
        "apk add bash sudo tzdata ; "
        "cp /usr/share/zoneinfo/UTC /etc/localtime ; "
        "touch /etc/timezone ; "
        "until docker info >/dev/null 2>&1; do echo 'Waiting for Docker...'; sleep 2; done; "
        "docker network create traefik-public ; "
        "docker run -v /usr/local/bin:/setup --rm " + SETUPVERSION
    )

    subprocess.check_call([
        runtime,
        "exec",
        "-it",
        NAME,
        "/bin/sh",
        "-c",
        setup_cmd,
    ])


def start(runtime):
    print(f"Starting {NAME} using {runtime}")

    try:
        subprocess.run(
            base_run_cmd(runtime) + [IMAGE],
            check=True,
        )
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
        [runtime, "rm", "-f", "--all"],
    ]

    run_commands_with_retry(commands)


def shell(runtime, cmdparam):
    cmd = [
        runtime,
        "exec",
        "-it",
        NAME
    ]
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

    shell(runtime, ["/bin/sh", "-c", cmdstr])
        
def main():
    runtime = os.environ.get("LINC_RUNTIME", find_runtime())
    if not runtime:
        print("Error: No container runtime found", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Manage the linc dind container"
    )
    parser.add_argument(
        "command",
        choices=["up", "down", "shell", "l3d", "container-reset"],
        help="Action to perform",
    )
    parser.add_argument(
        "cmd_args",
        nargs=argparse.REMAINDER,
        help="Optional arguments for command",
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
        shell(runtime, ["sh"])
    elif args.command == "l3d":
        l3d(runtime, args.cmd_args)
    elif args.command == "container-reset":
        container_reset(runtime)


if __name__ == "__main__":
    main()
