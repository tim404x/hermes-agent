"""Stdlib-only entrypoint, invoked by absolute path with -IS before any Hermes import."""

import json
import os
import sys


def main():
    path, *command = sys.argv[1:]
    with open(path, encoding="utf-8") as launch_file:
        environment = json.load(launch_file)
    environment["INVOCATION_ID"] = os.environ["INVOCATION_ID"]
    os.umask(0o077)
    os.execve(command[0], command, environment)


if __name__ == "__main__":
    main()
