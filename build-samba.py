#!/usr/bin/python3

import argparse
import pathlib
import shlex
import subprocess


BUILDAH = "buildah"


def host_path(value):
    return str(pathlib.Path(value).resolve())


def _run(cmd, *args, **kwargs):
    print("--->", " ".join([shlex.quote(a) for a in cmd]))
    return subprocess.run(cmd, *args, **kwargs)


class ContainerVolume:
    def __init__(self, host_dir, dest_dir, flags=""):
        self.host_dir = host_dir
        self.dest_dir = dest_dir
        self.flags = flags or "rw,z"


class RunTask:
    def __init__(self, cmd, *, volumes=None):
        self.cmd = cmd
        self.volumes = volumes or []


class CopyTask:
    def __init__(self, sources, dest):
        self.sources = sources
        self.dest = dest


class _DNFTask(RunTask):
    def __init__(self, packages, dnf_cache=None):
        self._packages = packages
        self._dnf_cache = dnf_cache

    @property
    def volumes(self):
        if not self._dnf_cache:
            return []
        cdir = pathlib.Path(self._dnf_cache)
        libdir = cdir / "lib"
        cachedir = cdir / "cache"
        libdir.mkdir(parents=True, exist_ok=True)
        cachedir.mkdir(parents=True, exist_ok=True)
        return [
            ContainerVolume(str(libdir), "/var/lib/dnf"),
            ContainerVolume(str(cachedir), "/var/cache/dnf"),
        ]


class DNFInstallTask(_DNFTask):
    @property
    def cmd(self):
        return ["dnf", "install", "-y"] + list(self._packages)


class DNFBuildDepTask(_DNFTask):
    @property
    def cmd(self):
        return ["dnf", "builddep", "-y"] + list(self._packages)


class BuildahBackend:
    def __init__(self, buildah_path=None, img=None):
        self._buildah = buildah_path or BUILDAH
        self._base = ""
        self._tasks = []
        self._img = img

    def base_image(self, image):
        self._base = image

    def add_build_task(self, task):
        self._tasks.append(task)

    def _commit(self):
        assert self._base
        res = _run(
            [self._buildah, "from", self._base],
            capture_output=True,
            check=True,
        )
        cid = res.stdout.strip().decode("utf8")
        try:
            for task in self._tasks:
                self._do_task(task, cid)
                children = getattr(task, "child_tasks", [])
                for child_task in children:
                    self._do_task(task, cid)
            if self._img:
                _run([self._buildah, "commit", cid, self._img])
        finally:
            _run([self._buildah, "rm", cid], check=True)

    def _do_task(self, task, cid):
        # TODO? consider switching to singledispatch
        if isinstance(task, RunTask):
            cmd = [self._buildah]
            vols = getattr(task, "volumes", [])
            for vol in vols:
                cmd.append(
                    f"--volume={vol.host_dir}:{vol.dest_dir}:{vol.flags}"
                )
            cmd.extend(["run", cid])
            cmd.extend(task.cmd)
            return _run(cmd, check=True)
        if isinstance(task, CopyTask):
            cmd = [self._buildah, "copy", cid]
            cmd.extend(task.sources)
            cmd.append(task.dest)
            return _run(cmd, check=True)
        raise TypeError("unexpected task type")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        if not exc_type:
            self._commit()


def cmd_build_image(cli, is_centos=False):
    phase1 = "samba-builder:dev"
    with BuildahBackend(img=phase1) as builder:
        builder.base_image("registry.fedoraproject.org/fedora:38")
        builder.add_build_task(
            RunTask(
                [
                    "mkdir",
                    "-p",
                    "-m",
                    "0777",
                    "/srv/build",
                    "/usr/local/lib/sources",
                ]
            )
        )
        builder.add_build_task(
            CopyTask(
                [
                    "samba.pamd",
                    "README.downgrade",
                    "samba-master.spec",
                    "smb.conf.example",
                    "pam_winbind.conf",
                    "samba.logrotate",
                    "smb.conf.vendor",
                ],
                "/usr/local/lib/sources",
            )
        )
        pkgs = [
            "git",
            "rsync",
            "gcc",
            "/usr/bin/rpmbuild",
            "dnf-command(builddep)",
        ]
        if is_centos:
            pkgs.append("epel-release")
            pkgs.append("centos-release-gluster")
            pkgs.append("centos-release-ceph")
        builder.add_build_task(DNFInstallTask(pkgs, cli.dnf_cache))
        builder.add_build_task(
            DNFBuildDepTask(
                ["/usr/local/lib/sources/samba-master.spec"], cli.dnf_cache
            )
        )


def cmd_build_srpm(cli):
    phase1 = "samba-builder:dev"
    with BuildahBackend() as builder:
        builder.base_image(phase1)
        volumes = [
            ContainerVolume(str(cli.source_dir), "/build"),
        ]
        if cli.artifacts_dir:
            volumes.append(
                ContainerVolume(str(cli.artifacts_dir), "/srv/dest"),
            )
        builder.add_build_task(
            RunTask(
                [
                    "rsync",
                    "-r",
                    "/usr/local/lib/sources/",
                    "/srv/dest/",
                ],
                volumes=volumes,
            )
        )
        builder.add_build_task(
            RunTask(
                [
                    "cp",
                    "/usr/local/lib/sources/samba-master.spec",
                    "/srv/dest/samba.spec",
                ],
                volumes=volumes,
            )
        )
        rpm_version = "4.999"
        dest = "/srv/dest/samba-4.999.tar.gz"
        builder.add_build_task(
            RunTask(
                [
                    "git",
                    "-C",
                    "/build",
                    "archive",
                    f"--prefix=samba-{rpm_version}/",
                    f"--output={dest}",
                    "HEAD",
                ],
                volumes=volumes,
            )
        )
        builder.add_build_task(
            RunTask(
                [
                    "rpmbuild",
                    "--define",
                    "_topdir /srv/dest/",
                    "--define",
                    "_sourcedir /srv/dest/",
                    "--define",
                    "_srcrpmdir /srv/dest/",
                    "-bs",
                    "/srv/dest/samba.spec",
                ],
                volumes=volumes,
            )
        )


def cmd_build_rpm(cli):
    phase1 = "samba-builder:dev"
    with BuildahBackend() as builder:
        builder.base_image(phase1)
        volumes = [
            ContainerVolume(str(cli.source_dir), "/build"),
        ]
        if cli.artifacts_dir:
            volumes.append(
                ContainerVolume(str(cli.artifacts_dir), "/srv/dest"),
            )
        builder.add_build_task(
            RunTask(
                [
                    "rpmbuild",
                    "--define",
                    "_topdir /srv/dest/",
                    "--rebuild",
                    "/srv/dest/samba-4.999-1.fc38.src.rpm",
                ],
                volumes=volumes,
            )
        )


def parse_cli():
    parser = argparse.ArgumentParser(
        description="""
Automate building samba packages using a container.

It can be invoked using only command line options or using a YAML based
configuration file. Use --help-yaml to see an example.
"""
    )
    parser.add_argument(
        "--samba-source",
        default="/srv/build/samba",
        help="Path to the source checkout of samba",
    )
    parser.add_argument(
        "--package-source",
        default="/usr/local/lib/sources",
        help="Path to packaging specific sources",
    )
    parser.add_argument(
        "--workdir",
        "-w",
        default="/srv/build/work",
        help="Path to working dir",
    )
    parser.add_argument(
        "--job",
        "-j",
        help="Name of path in working dir to write results",
    )
    parser.add_argument(
        "--install-deps-from",
        help="Installs dependencies based on a path to a SPEC file or SRPM",
    )
    parser.add_argument(
        "--git-ref", default="master", help="Samba git ref to check out"
    )
    parser.add_argument(
        "--git-repo",
        default="https://git.samba.org/samba.git",
        help="Samba git repo",
    )
    parser.add_argument(
        "--force-ref",
        action="store_true",
        help="Even if a repo already exists try to checkout the supplied git ref",
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Bootstrap environment & install critical dependency packages",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Do not build packages",
    )
    parser.add_argument(
        "--with-ceph",
        action="store_true",
        help="Enable building Ceph components",
    )
    parser.add_argument(
        "--backend",
        choices=[BUILDAH],
        default=BUILDAH,
        help="Specify continer-build backend",
    )
    parser.add_argument(
        "--source-dir", "-s", type=host_path, help="Path to samba git checkout"
    )
    parser.add_argument(
        "--artifacts-dir",
        "-a",
        type=host_path,
        help="Path to a local directory where output will be saved",
    )
    parser.add_argument(
        "--base-image",
        help="Base image (example: quay.io/centos/centos:stream9)",
    )
    parser.add_argument(
        "--task",
        "-t",
        action="append",
        choices=("image", "packages"),  # TODO: "configure", "make"
        help="What to build",
    )
    parser.add_argument(
        "--dnf-cache",
        help="Path to a directory for caching dnf state",
    )
    parser.add_argument(
        "--shell",
        action="store_true",
        help="Interrupt package build and get a shell within the container instead.",
    )
    parser.add_argument(
        "--example-yaml",
        action="store_true",
        help="Display example configuration yaml",
    )
    parser.add_argument("--config", "-c", help="Provide a configuration file")
    cli = parser.parse_args()
    return cli


def main():
    cli = parse_cli()
    # cmd_build_image(cli)
    cmd_build_srpm(cli)
    cmd_build_rpm(cli)


if __name__ == "__main__":
    main()
